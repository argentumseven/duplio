"""Pictures of files on drives where reading is slow or costs bandwidth.

The cheapest route is tried first, and every picture that has to be fetched is kept so it is fetched once:

1. **The cache.** Nothing touches the drive.
2. **The photo's own thumbnail.** A JPEG from a phone or camera carries a small preview in its first kilobytes (a RAW file
   carries a bigger one); a movie may carry a cover picture. Reading those few kilobytes with a ranged read costs a fraction
   of the file.
3. **The picture itself,** read whole when it is small, when the user asks (``allow_download``) or when the setting
   ``download_whole`` allows it (it does by default), and always bounded by ``max_download``. It is turned into a thumbnail by
   Pillow if installed, else by the decoders in this package, else by ffmpeg if installed.
4. **A movie** is shown by one frame taken by ffmpeg (needed for any movie that has no cover picture).
5. Otherwise: no preview, with the reason and what would fix it.

One thing is never done without being asked: downloading a file that is only a placeholder for something in the cloud
("online-only" files on OneDrive, Dropbox, Tresorit… where reading it downloads it).

A duplicate group's members have identical contents, so :meth:`PreviewService.pick_source` chooses the member that is
cheapest to read (a copy on the internal disk beats one on the NAS); one picture serves the whole group.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import struct
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rich.text import Text

from duplio.events import CancelToken
from duplio.preview import decoders, exif, ffmpeg, png, render, video
from duplio.preview.settings import PreviewSettings
from duplio.preview.store import StoredThumb, ThumbStore, drive_tag, thumb_key
from duplio.util.appdirs import AppDirs
from duplio.vfs.base import Backend, Capabilities
from duplio.vfs.local import LocalBackend

JPEG_EXT = frozenset({"jpg", "jpeg", "jpe", "jfif"})
TIFF_EXT = frozenset({"tif", "tiff", "dng", "cr2", "nef", "nrw", "arw", "orf", "rw2", "pef", "srw", "3fr", "iiq"})
PILLOW_EXT = frozenset({"png", "gif", "bmp", "webp", "tga", "ico", "ppm", "pgm", "pbm", "pnm", "avif", "heic", "heif", "jp2", "psd"})
PICTURE_EXT = JPEG_EXT | TIFF_EXT | PILLOW_EXT
VIDEO_EXT = frozenset({"mp4", "m4v", "mov", "3gp", "3g2", "mkv", "avi", "webm", "wmv", "mpg", "mpeg", "mts", "m2ts", "flv", "ogv", "vob"})
COVER_EXT = frozenset({"mp4", "m4v", "mov", "3gp", "3g2"})  # the movie formats that can carry a cover picture
MEDIA_EXT = PICTURE_EXT | VIDEO_EXT


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class Unavailable(Exception):
    """No picture can be shown; ``reason`` is a sentence for the user and ``code`` a stable identifier."""

    def __init__(self, reason: str, code: str = "unavailable") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class PreviewItem:
    backend: Backend
    path: str
    size: int
    mtime_ns: int
    name: str  # for the extension
    offline: bool = False

    @property
    def caps(self) -> Capabilities:
        return self.backend.capabilities(self.path)

    @property
    def key(self) -> str:
        return thumb_key(self.backend.id, self.path, self.size, self.mtime_ns)


@dataclass(frozen=True)
class Preview:
    thumb: StoredThumb
    from_cache: bool
    bytes_read: int = 0  # taken from the drive to produce it (0 for a cache hit)

    @property
    def orientation(self) -> int:
        return self.thumb.orientation


@dataclass
class PreviewStats:
    hits: int = 0
    fetched: int = 0
    unavailable: int = 0
    bytes_read: int = 0
    skipped_online_only: int = 0
    not_kept: int = 0  # pictures fetched but not stored (the disk was full or the cache broke); still shown
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **counts: int) -> None:
        with self.lock:
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)


_SERIAL_DECODE = threading.Lock()


def _picture_size(data: bytes) -> tuple[int, int]:
    """Width and height of a JPEG or PNG, read from its headers (0, 0 when it cannot be told)."""
    if data[:8] == png.SIGNATURE and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    size = exif._jpeg_size(data)
    return size if size is not None else (0, 0)


def pillow_available() -> bool:
    return _pillow() is not None


def _pillow() -> object | None:
    try:
        import PIL.Image
    except ImportError:
        return None
    return PIL.Image


class PreviewService:
    def __init__(self, dirs: AppDirs | None, settings: PreviewSettings | None = None) -> None:
        self.settings = settings or PreviewSettings()
        self.settings.validate()
        self._dirs = dirs
        self._stores: dict[str, ThumbStore] = {}
        self._lock = threading.Lock()
        self.stats = PreviewStats()
        self._device_slots: dict[str, threading.BoundedSemaphore] = {}
        self._background_slots: dict[str, threading.BoundedSemaphore] = {}
        self._inflight: dict[str, threading.Event] = {}  # pictures being fetched right now, by key
        self._queue: deque[PreviewItem] = deque()
        self._generation = 0
        self._wake = threading.Condition()
        self._workers: list[threading.Thread] = []
        self._closing = False
        self._listeners: list[Callable[[PreviewItem], None]] = []
        self._ffmpeg_path: str | None = None
        self._ffmpeg_for: str | None = None  # the setting the path was looked up for

    # ------------------------------------------------------------------ stores
    def _stores_for(self, caps: Capabilities) -> list[ThumbStore]:
        """The buffers that may hold a picture from this drive, in the order to look in them.

        A slow, remote or cloud drive has one: the kept buffer (or, if pictures are not kept between runs, a temporary one).
        A fast local disk has a temporary one for what is quick to make again (a photo's own small preview), and, if pictures are
        kept between runs, the kept one for what took real work (a picture decoded from the whole file, a frame of a movie)."""
        mode = self.settings.mode
        if mode == "off":
            return []
        keep = self.settings.keep == "persistent" and self._dirs is not None
        if mode == "auto" and caps.read_cost == 0:
            return [self._buffer("memory"), *([self._buffer("persistent")] if keep else [])]
        return [self._buffer("persistent" if keep else "session")]

    def _buffer(self, kind: str) -> ThumbStore:
        with self._lock:
            store = self._stores.get(kind)
            if store is None:
                if kind == "persistent":
                    assert self._dirs is not None
                    store = ThumbStore(self._dirs.cache / "previews", max_bytes=self.settings.max_bytes, persistent=True)
                    with contextlib.suppress(Exception):
                        store.expire(self.settings.keep_days)  # pictures nobody has looked at for a long while are let go
                elif kind == "memory":
                    store = ThumbStore(None, max_bytes=min(self.settings.max_bytes, 512 * 1024 * 1024), persistent=False)
                else:  # session: as large as the setting allows, so a whole big folder stays at hand while it is browsed
                    store = ThumbStore(None, max_bytes=self.settings.max_bytes, persistent=False)
                self._stores[kind] = store
            return store

    def _store(self, caps: Capabilities, *, expensive: bool = True) -> ThumbStore | None:
        """The buffer a new picture goes into (``expensive``: it took real work to make)."""
        stores = self._stores_for(caps)
        if not stores:
            return None
        return stores[-1] if expensive else stores[0]

    def apply_settings(self) -> None:
        """Settings changed (the size limit of the buffer): existing buffers follow it, trimming if they are now over."""
        with self._lock:
            stores = dict(self._stores)
        for kind, store in stores.items():
            store.max_bytes = min(self.settings.max_bytes, 512 * 1024 * 1024) if kind == "memory" else self.settings.max_bytes
            store.trim()

    def storage_summary(self) -> list[tuple[str, int, int, str]]:
        with self._lock:
            return [(kind, s.stats().count, s.stats().bytes, s.stats().path) for kind, s in self._stores.items()]

    # ------------------------------------------------------------------ lookup
    def peek(self, item: PreviewItem) -> Preview | None:
        """The cached picture, if any. Never touches the drive."""
        for store in self._stores_for(item.caps):
            found = store.get(item.key)
            if found:
                return Preview(found, True)
        return None

    def pick_source(self, items: Sequence[PreviewItem]) -> PreviewItem:
        """The member of a group of identical files that is cheapest to show a picture of."""
        def rank(item: PreviewItem) -> tuple[int, int, int, int]:
            cached = 0 if self.peek(item) else 1
            return (cached, item.offline, item.caps.read_cost, item.size)

        return min(items, key=rank)

    # ------------------------------------------------------------------ the ladder
    def get(
        self,
        item: PreviewItem,
        *,
        cancel: CancelToken | None = None,
        allow_download: bool | None = None,
        allow_online_only: bool = False,
        background: bool = False,
    ) -> Preview:
        """The picture. ``allow_download``: ``True`` reads the whole file if need be (the person asked for this picture),
        ``False`` never does beyond small files (a bulk job such as the similar-image search), ``None`` (the default) follows
        the ``download_whole`` setting, which is what looking at pictures does. ``background`` marks read-ahead: it never takes
        the last free read slot of a drive, so a picture that is wanted on screen right now does not queue behind it."""
        cancel = cancel or CancelToken()
        cached = self.peek(item)
        if cached is not None:
            self.stats.add(hits=1)
            return cached
        ext = extension(item.name)
        if ext not in MEDIA_EXT:
            self.stats.add(unavailable=1)
            raise Unavailable("there is no picture to show for this kind of file", "kind")
        if item.offline and not (allow_online_only or self.settings.online_only):
            self.stats.add(skipped_online_only=1)
            raise Unavailable(
                "this file is only stored in the cloud; showing it would download it", "online-only"
            )
        # One fetch per picture at a time: when the window and the background fetcher want the same picture, the second one
        # waits for the first and uses what it stored, instead of reading the drive twice.
        while True:
            with self._lock:
                pending = self._inflight.get(item.key)
                if pending is None:
                    mine = threading.Event()
                    self._inflight[item.key] = mine
                    break
            while not pending.wait(0.1):
                cancel.check()
            cached = self.peek(item)
            if cached is not None:
                self.stats.add(hits=1)
                return cached
            # the other fetch ended without storing it (it failed, or pictures are not kept): try for ourselves
        try:
            with self._slot(item, background):
                cancel.check()
                try:
                    thumb, read = self._produce(item, ext, cancel, allow_download)
                except Unavailable:
                    self.stats.add(unavailable=1)
                    raise
            store = self._store(item.caps, expensive=thumb.source != "embedded")
            if store is not None:
                try:
                    store.put(item.key, drive_tag(item.backend.id), thumb)
                except (OSError, sqlite3.Error):
                    self.stats.add(not_kept=1)  # a full disk or a broken cache must not lose a picture that was just fetched
            self.stats.add(fetched=1, bytes_read=read)
            return Preview(thumb, False, read)
        finally:
            with self._lock:
                self._inflight.pop(item.key, None)
            mine.set()

    # ------------------------------------------------------------------ the buffer (for a folder that is being browsed)
    def cached(self, item: PreviewItem) -> bool:
        """Whether the picture is already in the buffer (a cheap check: no picture is read)."""
        return any(store.has(item.key) for store in self._stores_for(item.caps))

    def buffer_for(self, item: PreviewItem) -> ThumbStore | None:
        """The buffer that would hold this picture, if pictures from this drive are kept at all."""
        return self._store(item.caps)

    def pin(self, items: Sequence[PreviewItem]) -> None:
        """Keep these pictures when a buffer has to make room: they are the folder that is on screen."""
        wanted: dict[int, tuple[ThumbStore, set[str]]] = {}
        for item in items:
            for store in self._stores_for(item.caps):
                wanted.setdefault(id(store), (store, set()))[1].add(item.key)
        for store in list(self._stores.values()):
            store.pin(wanted.get(id(store), (store, set()))[1])

    @contextlib.contextmanager
    def _slot(self, item: PreviewItem, background: bool = False):  # type: ignore[no-untyped-def]
        """One read of a drive at a time per slot; read-ahead may use all but one, so the screen is never queued behind it."""
        device = item.backend.device_key(item.path)
        with self._lock:
            slot = self._device_slots.get(device)
            if slot is None:
                total = max(1, min(item.caps.max_parallel_reads, 2))
                slot = self._device_slots[device] = threading.BoundedSemaphore(total)
                self._background_slots[device] = threading.BoundedSemaphore(max(1, total - 1))
            reserve = self._background_slots[device]
        if background:
            with reserve, slot:
                yield
        else:
            with slot:
                yield

    def _ffmpeg(self) -> str | None:
        """The ffmpeg program, looked for once per setting."""
        wanted = self.settings.ffmpeg
        if self._ffmpeg_for != wanted:
            self._ffmpeg_path = ffmpeg.find_ffmpeg(wanted)
            self._ffmpeg_for = wanted
        return self._ffmpeg_path

    def _may_download(self, allow_download: bool | None) -> bool:
        return self.settings.download_whole if allow_download is None else allow_download

    def _produce(self, item: PreviewItem, ext: str, cancel: CancelToken, allow_download: bool | None) -> tuple[StoredThumb, int]:
        if ext in VIDEO_EXT:
            return self._produce_video(item, ext, cancel, allow_download)
        read_total = 0
        want = self.settings.size_px
        orientation = 1
        if ext in JPEG_EXT or ext in TIFF_EXT:
            found = exif.find_embedded(
                lambda off, n: item.backend.read_range(item.path, off, n), item.size, want_px=want
            )
            read_total += found.bytes_read
            cancel.check()
            if found.thumbnail is not None:
                return self._from_embedded(found, want, cancel), read_total
            orientation = found.orientation
        # Reading the whole picture: always on a fast disk; on a slow one when it is small, when asked, or when allowed.
        limit = self.settings.max_download
        fast = item.caps.read_cost == 0
        if not (fast or self._may_download(allow_download) or item.size <= self.settings.small_download):
            raise Unavailable("this picture has no small preview inside it; press the fetch key to download it", "needs-download")
        if item.size > limit:
            raise Unavailable(f"the file is larger than the {limit // (1024 * 1024)} MiB limit for previews", "too-large")
        data = self._read_all(item, cancel)
        read_total += len(data)
        return self._from_full(data, item.name, want, cancel, orientation), read_total

    def _read_all(self, item: PreviewItem, cancel: CancelToken) -> bytes:
        parts: list[bytes] = []
        with item.backend.open_read(item.path) as handle:
            while True:
                cancel.check()
                block = handle.read(1024 * 1024)
                if not block:
                    break
                parts.append(block)
        return b"".join(parts)

    # ------------------------------------------------------------------ movies
    def _produce_video(self, item: PreviewItem, ext: str, cancel: CancelToken, allow_download: bool | None) -> tuple[StoredThumb, int]:
        want = self.settings.size_px
        counted = [0]

        def read_range(offset: int, length: int) -> bytes:
            data = item.backend.read_range(item.path, offset, length)
            counted[0] += len(data)
            return data

        if ext in COVER_EXT:
            cover = video.cover_art(read_range, item.size)
            cancel.check()
            if cover is not None:
                out, fmt, w, h, orientation = self._shrink(cover, want, "cover.jpg", cancel)
                return StoredThumb(out, fmt, w, h, orientation, "cover", None), counted[0]
        program = self._ffmpeg()
        if program is None:
            raise Unavailable("a picture of a movie needs ffmpeg, which is not installed (Settings → Picture support)", "needs-ffmpeg")
        fast = item.caps.read_cost == 0
        if not (fast or self._may_download(allow_download)):
            raise Unavailable("a picture of this movie means reading part of it; press the fetch key to allow it", "needs-download")
        fmt = "jpg" if _pillow() is not None else "png"
        if isinstance(item.backend, LocalBackend):  # ffmpeg reads what it needs of the file itself, seeking
            frame = ffmpeg.frame_from_file(program, item.path, want, fmt=fmt, cancel=lambda: cancel.cancelled)
            read_bytes = counted[0]
        else:
            if item.size > self.settings.max_download:
                limit = self.settings.max_download // (1024 * 1024)
                raise Unavailable(f"this movie is on another computer and larger than the {limit} MiB limit for previews", "too-large")
            data = self._read_all(item, cancel)
            read_bytes = counted[0] + len(data)
            with tempfile.TemporaryDirectory(prefix="duplio-movie-") as folder:
                local = Path(folder) / f"movie.{ext}"
                local.write_bytes(data)
                del data
                frame = ffmpeg.frame_from_file(program, str(local), want, fmt=fmt, cancel=lambda: cancel.cancelled)
        width, height = _picture_size(frame)
        return StoredThumb(frame, fmt, width, height, 1, "frame", None), read_bytes

    # ------------------------------------------------------------------ making thumbnails
    def _from_embedded(self, found: exif.Found, want: int, cancel: CancelToken) -> StoredThumb:
        emb = found.thumbnail
        assert emb is not None
        side = emb.long_side
        if side is not None and side <= int(want * 1.6):
            return StoredThumb(emb.jpeg, "jpg", emb.width, emb.height, found.orientation, "embedded", found.pixels)
        # a big embedded preview (RAW): shrink it so the cache holds small files
        data, fmt, w, h, orientation = self._shrink(emb.jpeg, want, "preview.jpg", cancel, orientation=found.orientation)
        return StoredThumb(data, fmt, w, h, orientation, "embedded", found.pixels)

    def _from_full(self, data: bytes, name: str, want: int, cancel: CancelToken, orientation: int) -> StoredThumb:
        out, fmt, w, h, final = self._shrink(data, want, name, cancel, orientation=orientation, upright=True)
        return StoredThumb(out, fmt, w, h, final, "decoded", None)

    def _shrink(self, data: bytes, want: int, name: str, cancel: CancelToken, *, orientation: int = 1,
                upright: bool = False) -> tuple[bytes, str, int, int, int]:
        """``(picture, format, width, height, orientation)`` for a thumbnail of ``want`` pixels made from ``data``.

        Pillow does it if installed (fast, many formats). Otherwise the decoders in this package, and for what they cannot
        read, ffmpeg if there is one. ``upright`` asks for the EXIF orientation to be applied here when Pillow is used (the
        orientation is then 1); otherwise it is passed through for whoever shows the picture to apply.
        Raises :class:`Unavailable` with the reason when nothing can."""
        if _pillow() is not None:
            made = self._shrink_pillow(data, want, upright)
            if made is not None:
                out, w, h = made
                return out, "jpg", w, h, 1 if upright else orientation
        failure: decoders.Undecodable | None = None
        try:
            with _SERIAL_DECODE:  # one picture at a time through the slow pure-Python path, so the window stays responsive
                raster = decoders.decode(data, want, name).fit(want, want)
            return png.write_rgb(raster.width, raster.height, raster.rgb), "png", raster.width, raster.height, orientation
        except decoders.Undecodable as exc:
            failure = exc
        program = self._ffmpeg()
        if program is not None:
            try:
                is_exif = data[:2] == b"\xff\xd8" or data[:4] in (b"II*\x00", b"MM\x00*")
                fmt = "jpg" if _pillow() is not None else "png"
                out = ffmpeg.picture_from_bytes(program, data, want, fmt=fmt, autorotate=not is_exif, cancel=lambda: cancel.cancelled)
                w, h = _picture_size(out)
                return out, fmt, w, h, orientation if is_exif else 1
            except ffmpeg.FfmpegError as exc:
                raise Unavailable(f"ffmpeg could not read this picture ({exc})", "damaged") from exc
        assert failure is not None
        raise Unavailable(failure.reason, failure.code)

    @staticmethod
    def _shrink_pillow(data: bytes, want: int, upright: bool) -> tuple[bytes, int, int] | None:
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(data)) as img:
                img.draft("RGB", (want * 2, want * 2))
                if upright:
                    img = ImageOps.exif_transpose(img) or img
                if img.mode in ("RGBA", "LA", "P"):
                    rgba = img.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    img = Image.alpha_composite(background, rgba)
                img = img.convert("RGB")
                img.thumbnail((want, want))
                out = io.BytesIO()
                img.save(out, "JPEG", quality=82)
                return out.getvalue(), img.width, img.height
        except Exception:
            return None  # an unusual picture Pillow cannot open: the other decoders may be able to

    # ------------------------------------------------------------------ rendering
    def render(self, preview: Preview, *, cols: int = 40, rows: int = 14, color: bool = True) -> Text:
        thumb = preview.thumb
        return _render_cached(thumb.data, thumb.orientation, cols, rows, color)

    # ------------------------------------------------------------------ background work
    def prefetch(self, items: Sequence[PreviewItem]) -> None:
        """Fetch these pictures in the background, replacing whatever was queued before (the user moved on)."""
        if self.settings.prefetch <= 0 or self.settings.mode == "off":
            return
        wanted = [i for i in items[: self.settings.prefetch] if extension(i.name) in PICTURE_EXT]
        with self._wake:
            self._queue = deque(wanted)
            self._generation += 1
            self._start_workers()
            self._wake.notify_all()

    def _start_workers(self) -> None:
        if self._workers or self._closing:
            return
        for n in range(2):
            thread = threading.Thread(target=self._work, name=f"duplio-preview-{n}", daemon=True)
            thread.start()
            self._workers.append(thread)

    def on_ready(self, callback: Callable[[PreviewItem], None]) -> None:
        self._listeners.append(callback)

    def _work(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._closing:
                    self._wake.wait()
                if self._closing:
                    return
                item = self._queue.popleft()
            try:
                if self.peek(item) is None:
                    self.get(item)  # online-only files and big downloads are refused here, as ever
                    for listener in list(self._listeners):
                        with contextlib.suppress(Exception):
                            listener(item)
            except Exception:
                continue  # prefetch is best effort; the foreground request reports the reason

    # ------------------------------------------------------------------ housekeeping
    def clear(self) -> int:
        removed = 0
        with self._lock:
            stores = list(self._stores.values())
        for store in stores:
            removed += store.clear()
        if self._dirs is not None and self.settings.keep == "persistent":
            path = self._dirs.cache / "previews"
            if path.exists() and not any(s.persistent for s in stores):
                with contextlib.suppress(Exception):
                    store = ThumbStore(path, max_bytes=self.settings.max_bytes, persistent=True)
                    removed += store.clear()
                    store.close()
        return removed

    def close(self) -> None:
        with self._wake:
            self._closing = True
            self._wake.notify_all()
        for thread in self._workers:
            thread.join(timeout=2)
        with self._lock:
            stores = list(self._stores.values())
            self._stores.clear()
        for store in stores:
            store.close()


@lru_cache(maxsize=256)
def _render_cached(data: bytes, orientation: int, cols: int, rows: int, color: bool) -> Text:
    return render.render(data, orientation=orientation, cols=cols, rows=rows, color=color)


def persistent_store_path(dirs: AppDirs) -> Path:
    return dirs.cache / "previews"
