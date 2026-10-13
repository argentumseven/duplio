"""Pictures of files on drives where reading is slow or costs bandwidth.

The cheapest route is tried first, and every picture that has to be fetched is kept so it is fetched once:

1. **The cache.** Nothing touches the drive.
2. **The photo's own thumbnail.** A JPEG from a phone or camera carries a small preview in its first kilobytes (a RAW file
   carries a bigger one). Reading those few kilobytes with a ranged read costs a fraction of the photo.
3. **The picture itself,** only when it is small, or when the user asks (``allow_download``), and always bounded.
4. Otherwise: no preview, with the reason.

Two things are never done without being asked: downloading a file that is only a placeholder for something in the cloud
("online-only" files on OneDrive, Dropbox, Tresorit… where reading it downloads it), and reading a large file in full.

A duplicate group's members have identical contents, so :meth:`PreviewService.pick_source` chooses the member that is
cheapest to read (a copy on the internal disk beats one on the NAS); one picture serves the whole group.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rich.text import Text

from duplio.events import CancelToken
from duplio.preview import exif, jpeg, png, render
from duplio.preview.settings import PreviewSettings
from duplio.preview.store import StoredThumb, ThumbStore, drive_tag, thumb_key
from duplio.util.appdirs import AppDirs
from duplio.vfs.base import Backend, Capabilities

JPEG_EXT = frozenset({"jpg", "jpeg", "jpe", "jfif"})
TIFF_EXT = frozenset({"tif", "tiff", "dng", "cr2", "nef", "nrw", "arw", "orf", "rw2", "pef", "srw", "3fr", "iiq"})
PILLOW_EXT = frozenset({"png", "gif", "bmp", "webp", "tga", "ico", "ppm", "pgm", "avif", "heic", "heif", "jp2", "psd"})
PICTURE_EXT = JPEG_EXT | TIFF_EXT | PILLOW_EXT


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
        self._inflight: dict[str, threading.Event] = {}  # pictures being fetched right now, by key
        self._queue: deque[PreviewItem] = deque()
        self._generation = 0
        self._wake = threading.Condition()
        self._workers: list[threading.Thread] = []
        self._closing = False
        self._listeners: list[Callable[[PreviewItem], None]] = []

    # ------------------------------------------------------------------ stores
    def _store(self, caps: Capabilities) -> ThumbStore | None:
        mode = self.settings.mode
        if mode == "off":
            return None
        slow = caps.read_cost > 0
        if mode == "auto" and not slow:
            kind = "memory"  # fast local disk: keep only for this session
        elif self.settings.keep == "persistent" and self._dirs is not None:
            kind = "persistent"
        else:
            kind = "session"
        with self._lock:
            store = self._stores.get(kind)
            if store is None:
                if kind == "persistent":
                    assert self._dirs is not None
                    store = ThumbStore(self._dirs.cache / "previews", max_bytes=self.settings.max_bytes, persistent=True)
                elif kind == "memory":
                    store = ThumbStore(None, max_bytes=min(self.settings.max_bytes, 512 * 1024 * 1024), persistent=False)
                else:  # session: as large as the setting allows, so a whole big folder stays at hand while it is browsed
                    store = ThumbStore(None, max_bytes=self.settings.max_bytes, persistent=False)
                self._stores[kind] = store
            return store

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
        store = self._store(item.caps)
        if store is None:
            return None
        found = store.get(item.key)
        return Preview(found, True) if found else None

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
        allow_download: bool = False,
        allow_online_only: bool = False,
    ) -> Preview:
        cancel = cancel or CancelToken()
        cached = self.peek(item)
        if cached is not None:
            self.stats.add(hits=1)
            return cached
        ext = extension(item.name)
        if ext not in PICTURE_EXT:
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
            with self._slot(item):
                cancel.check()
                try:
                    thumb, read = self._produce(item, ext, cancel, allow_download)
                except Unavailable:
                    self.stats.add(unavailable=1)
                    raise
            store = self._store(item.caps)
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
        store = self._store(item.caps)
        return store is not None and store.has(item.key)

    def buffer_for(self, item: PreviewItem) -> ThumbStore | None:
        """The buffer that would hold this picture, if pictures from this drive are kept at all."""
        return self._store(item.caps)

    def pin(self, items: Sequence[PreviewItem]) -> None:
        """Keep these pictures when the buffer has to make room: they are the folder that is on screen."""
        by_store: dict[int, tuple[ThumbStore, set[str]]] = {}
        for item in items:
            store = self._store(item.caps)
            if store is not None:
                by_store.setdefault(id(store), (store, set()))[1].add(item.key)
        for store in list(self._stores.values()):
            store.pin(by_store.get(id(store), (store, set()))[1])

    def _slot(self, item: PreviewItem):  # type: ignore[no-untyped-def]
        device = item.backend.device_key(item.path)
        with self._lock:
            slot = self._device_slots.get(device)
            if slot is None:
                slot = threading.BoundedSemaphore(max(1, min(item.caps.max_parallel_reads, 2)))
                self._device_slots[device] = slot
        return slot

    def _produce(self, item: PreviewItem, ext: str, cancel: CancelToken, allow_download: bool) -> tuple[StoredThumb, int]:
        read_total = 0
        want = self.settings.size_px
        if ext in JPEG_EXT or ext in TIFF_EXT:
            found = exif.find_embedded(
                lambda off, n: item.backend.read_range(item.path, off, n), item.size, want_px=want
            )
            read_total += found.bytes_read
            cancel.check()
            if found.thumbnail is not None:
                return self._from_embedded(found, want), read_total
        # Reading the whole picture: always on a fast disk; on a slow one only when small or asked for.
        limit = self.settings.max_download
        fast = item.caps.read_cost == 0
        if not (fast or allow_download or item.size <= self.settings.small_download):
            raise Unavailable("this picture has no small preview inside it; press the fetch key to download it", "needs-download")
        if item.size > limit:
            raise Unavailable(f"the file is larger than the {limit // (1024 * 1024)} MiB limit for previews", "too-large")
        data = self._read_all(item, cancel)
        read_total += len(data)
        return self._from_full(data, ext, want), read_total

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

    # ------------------------------------------------------------------ making thumbnails
    def _from_embedded(self, found: exif.Found, want: int) -> StoredThumb:
        emb = found.thumbnail
        assert emb is not None
        side = emb.long_side
        if side is not None and side <= int(want * 1.6):
            return StoredThumb(emb.jpeg, "jpg", emb.width, emb.height, found.orientation, "embedded", found.pixels)
        # a big embedded preview (RAW): shrink it so the cache holds small files
        shrunk = self._shrink(emb.jpeg, want, "embedded")
        if shrunk is None:
            raise Unavailable("the embedded preview is too large to decode without Pillow", "needs-pillow")
        data, fmt, w, h = shrunk
        return StoredThumb(data, fmt, w, h, found.orientation, "embedded", found.pixels)

    def _from_full(self, data: bytes, ext: str, want: int) -> StoredThumb:
        shrunk = self._shrink(data, want, "decoded", transpose=True)
        if shrunk is None:
            raise Unavailable("a picture of this kind needs Pillow to be shown (pip install 'duplio[media]')", "needs-pillow")
        out, fmt, w, h = shrunk
        return StoredThumb(out, fmt, w, h, 1, "decoded", None)

    def _shrink(self, data: bytes, want: int, source: str, *, transpose: bool = False) -> tuple[bytes, str, int, int] | None:
        image_module = _pillow()
        if image_module is not None:
            from PIL import Image, ImageOps

            try:
                with Image.open(io.BytesIO(data)) as img:
                    original = img.size
                    img.draft("RGB", (want * 2, want * 2))
                    if transpose:
                        img = ImageOps.exif_transpose(img) or img
                    if img.mode in ("RGBA", "LA", "P"):
                        rgba = img.convert("RGBA")
                        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                        img = Image.alpha_composite(background, rgba)
                    img = img.convert("RGB")
                    img.thumbnail((want, want))
                    out = io.BytesIO()
                    img.save(out, "JPEG", quality=82)
                    del original
                    return out.getvalue(), "jpg", img.width, img.height
            except Exception:
                return None
        if data[:2] != b"\xff\xd8":
            return None
        try:
            side = exif._jpeg_size(data)
            reduce = side is not None and max(side) > want * 2
            decoded = jpeg.decode(data, reduce=reduce)
        except (jpeg.JpegError, ValueError, IndexError, KeyError):
            return None
        raster = render.Raster(decoded.width, decoded.height, decoded.rgb).fit(want, want)
        return png.write_rgb(raster.width, raster.height, raster.rgb), "png", raster.width, raster.height

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
