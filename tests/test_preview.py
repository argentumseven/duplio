"""Pictures from slow and remote drives: embedded thumbnails, decoders, cache, and the cheapest-first ladder."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from duplio.preview import exif, jpeg, png, render  # noqa: E402
from duplio.preview.service import PreviewItem, PreviewService, Unavailable, extension  # noqa: E402
from duplio.preview.settings import PreviewSettings  # noqa: E402
from duplio.preview.store import StoredThumb, ThumbStore, drive_tag, thumb_key  # noqa: E402
from duplio.util.appdirs import AppDirs  # noqa: E402
from duplio.vfs.base import Capabilities, MediaKind  # noqa: E402
from duplio.vfs.local import LocalBackend  # noqa: E402

from .previewkit import jpeg_bytes, photo_with_exif_thumbnail, picture, raw_like  # noqa: E402

KIB = 1024


def read_from(data: bytes):  # type: ignore[no-untyped-def]
    calls: list[tuple[int, int]] = []

    def read(offset: int, length: int) -> bytes:
        calls.append((offset, length))
        return data[offset : offset + length]

    return read, calls


# --------------------------------------------------------------------------- the photo's own thumbnail
@pytest.mark.parametrize("bo", ["<", ">"], ids=["little-endian", "big-endian"])
def test_exif_thumbnail_is_found_in_the_first_kilobytes(tmp_path: Path, bo: str) -> None:
    thumb = photo_with_exif_thumbnail(tmp_path / "p.jpg", orientation=6, padding=3_000_000, bo=bo)
    data = (tmp_path / "p.jpg").read_bytes()
    read, calls = read_from(data)
    found = exif.find_embedded(read, len(data))
    assert found.thumbnail is not None and found.thumbnail.jpeg == thumb
    assert (found.thumbnail.width, found.thumbnail.height) == (160, 120)
    assert found.orientation == 6 and found.pixels == (640, 480)
    assert found.bytes_read <= 70 * KIB and len(data) > 3_000_000  # a few dozen KiB of a multi-megabyte file
    assert all(offset == 0 or offset >= 0 for offset, _ in calls) and sum(n for _, n in calls) <= 70 * KIB


def test_a_jpeg_without_exif_has_no_embedded_thumbnail() -> None:
    data = jpeg_bytes(picture(200, 100))
    found = exif.find_embedded(read_from(data)[0], len(data))
    assert found.thumbnail is None and found.pixels == (200, 100)


def test_raw_style_files_offer_their_previews_and_the_right_one_is_chosen(tmp_path: Path) -> None:
    thumb, preview = raw_like(tmp_path / "shot.dng")
    data = (tmp_path / "shot.dng").read_bytes()
    small = exif.find_embedded(read_from(data)[0], len(data), want_px=100)
    assert small.thumbnail is not None and small.thumbnail.jpeg == thumb  # smallest that is big enough
    large = exif.find_embedded(read_from(data)[0], len(data), want_px=800)
    assert large.thumbnail is not None and large.thumbnail.jpeg == preview and large.thumbnail.source == "embedded-preview"
    assert large.orientation == 6


@pytest.mark.parametrize("bo", ["<", ">"])
def test_tiff_byte_order_is_honoured(tmp_path: Path, bo: str) -> None:
    thumb, _ = raw_like(tmp_path / "x.nef", bo=bo)
    data = (tmp_path / "x.nef").read_bytes()
    assert exif.find_embedded(read_from(data)[0], len(data), want_px=100).thumbnail.jpeg == thumb  # type: ignore[union-attr]


def test_garbage_and_truncated_files_never_raise() -> None:
    for blob in (b"", b"\xff\xd8", b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00II*\x00", b"II*\x00\xff\xff\xff\xff", b"hello world",
                 b"MM\x00*\x00\x00\x00\x08\x00\x05" + b"\x00" * 10):
        found = exif.find_embedded(read_from(blob)[0], len(blob))
        assert found.thumbnail is None


def test_offsets_past_the_end_of_the_file_are_ignored(tmp_path: Path) -> None:
    raw_like(tmp_path / "x.dng")
    data = (tmp_path / "x.dng").read_bytes()
    cut = data[:150]  # the directory is there, the pictures it points at are not
    assert exif.find_embedded(read_from(cut)[0], len(cut)).thumbnail is None


def test_the_byte_budget_stops_a_file_that_asks_for_too_much(tmp_path: Path) -> None:
    raw_like(tmp_path / "big.dng", preview_size=(3000, 2000))
    data = (tmp_path / "big.dng").read_bytes()
    found = exif.find_embedded(read_from(data)[0], len(data), want_px=2500, budget=20 * KIB)
    assert found.thumbnail is None or found.bytes_read <= 20 * KIB


# --------------------------------------------------------------------------- the decoders
@pytest.mark.parametrize("subsampling", [0, 1, 2], ids=["444", "422", "420"])
@pytest.mark.parametrize("size", [(160, 120), (97, 61), (8, 8), (33, 17)])
def test_pure_python_jpeg_decoder_matches_pillow(subsampling: int, size: tuple[int, int]) -> None:
    data = jpeg_bytes(picture(*size, seed=3), quality=88, subsampling=subsampling)
    mine = jpeg.decode(data)
    ref = Image.open(io.BytesIO(data)).convert("RGB").tobytes()
    assert (mine.width, mine.height) == size and len(mine.rgb) == len(ref)
    diff = [abs(a - b) for a, b in zip(mine.rgb, ref, strict=True)]
    assert max(diff) <= 3 and sum(diff) / len(diff) < 0.5  # the same picture, give or take rounding


def test_grayscale_and_restart_markers_decode() -> None:
    gray = jpeg_bytes(picture(120, 80).convert("L"))
    ref = Image.open(io.BytesIO(gray)).convert("RGB").tobytes()
    assert max(abs(a - b) for a, b in zip(jpeg.decode(gray).rgb, ref, strict=True)) <= 2
    restarts = jpeg_bytes(picture(120, 80), restart_marker_blocks=3, subsampling=0)
    assert b"\xff\xdd" in restarts  # a DRI segment is present
    ref2 = Image.open(io.BytesIO(restarts)).convert("RGB").tobytes()
    assert max(abs(a - b) for a, b in zip(jpeg.decode(restarts).rgb, ref2, strict=True)) <= 3


def test_reduced_decoding_gives_an_eighth_scale_picture() -> None:
    data = jpeg_bytes(picture(800, 600, seed=2), quality=85)
    small = jpeg.decode(data, reduce=True)
    assert (small.width, small.height) == (100, 75) and small.reduced
    ref = Image.open(io.BytesIO(data)).convert("RGB").resize((100, 75), Image.Resampling.BOX).tobytes()
    assert sum(abs(a - b) for a, b in zip(small.rgb, ref, strict=True)) / len(ref) < 8


def test_a_progressive_jpeg_is_read_at_an_eighth_scale_from_its_first_scan() -> None:
    data = jpeg_bytes(picture(800, 600, seed=4), quality=85, progressive=True)
    small = jpeg.decode(data)  # no reduce asked for: a progressive file can only be read this way
    assert (small.width, small.height) == (100, 75) and small.reduced
    ref = Image.open(io.BytesIO(data)).convert("RGB").resize((100, 75), Image.Resampling.BOX).tobytes()
    assert sum(abs(a - b) for a, b in zip(small.rgb, ref, strict=True)) / len(ref) < 8
    grey = jpeg.decode(jpeg_bytes(picture(160, 120, seed=5).convert("L"), progressive=True))
    assert (grey.width, grey.height) == (20, 15)


def test_unsupported_and_non_jpeg_data_are_refused_cleanly() -> None:
    arithmetic = bytearray(jpeg_bytes(picture(64, 64)))
    arithmetic[arithmetic.index(b"\xff\xc0") + 1] = 0xC9  # the frame header of an arithmetic-coded file
    with pytest.raises(jpeg.Unsupported):
        jpeg.decode(bytes(arithmetic))
    with pytest.raises(jpeg.JpegError):
        jpeg.decode(b"not a jpeg at all")
    with pytest.raises(jpeg.JpegError):
        jpeg.decode(jpeg_bytes(picture(64, 64))[:200])


def test_png_round_trip_and_foreign_pngs() -> None:
    rgb = bytes((i * 11) % 256 for i in range(31 * 17 * 3))
    assert png.read_rgb(png.write_rgb(31, 17, rgb)) == (31, 17, rgb)
    img = Image.frombytes("RGB", (31, 17), rgb)
    for mode in ("RGB", "RGBA", "L", "P"):
        out = io.BytesIO()
        img.convert(mode).save(out, "PNG", optimize=True)
        ref = Image.open(io.BytesIO(out.getvalue())).convert("RGB").tobytes()
        assert png.read_rgb(out.getvalue())[2] == ref
    with pytest.raises(ValueError):
        png.read_rgb(b"nope")


# --------------------------------------------------------------------------- showing it
def test_orientation_matches_pillow() -> None:
    img = Image.new("RGB", (5, 3))
    for i, (x, y) in enumerate((x, y) for y in range(3) for x in range(5)):
        img.putpixel((x, y), (i * 15 % 256, i * 31 % 256, i * 7 % 256))
    base = render.Raster(5, 3, img.tobytes())
    methods = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
               5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
               8: Image.Transpose.ROTATE_90}
    assert base.oriented(1) is base
    for orientation, method in methods.items():
        want = img.transpose(method)
        got = base.oriented(orientation)
        assert (got.width, got.height) == want.size and got.rgb == want.tobytes(), orientation


def test_fit_shrinks_but_never_enlarges_and_text_has_two_pixels_per_row() -> None:
    raster = render.Raster(160, 120, picture(160, 120).tobytes())
    fitted = raster.fit(40, 28)
    assert fitted.width <= 40 and fitted.height <= 28 and fitted.width * 3 == len(fitted.rgb) // fitted.height
    assert raster.fit(1000, 1000) is raster
    text = render.to_text(fitted)
    assert len(text.plain.split("\n")) == (fitted.height + 1) // 2 and "▀" in text.plain
    assert set(render.to_text(fitted, color=False).plain) <= set(" .:-=+*#%@\n")


def test_render_works_without_pillow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    thumb = photo_with_exif_thumbnail(tmp_path / "p.jpg")
    import builtins

    real_import = builtins.__import__

    def no_pillow(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("simulated: Pillow is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    text = render.render(thumb, cols=30, rows=10)
    assert "▀" in text.plain and len(text.plain.split("\n")) <= 10


# --------------------------------------------------------------------------- the store
def test_store_round_trip_trim_and_clear(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=3000, persistent=True)
    thumbs = {thumb_key("drive", f"/p{n}.jpg", 10, n): StoredThumb(bytes([n]) * 1000, "jpg", 16, 12, 1, "embedded", (64, 48))
              for n in range(1, 6)}
    for key, thumb in thumbs.items():
        store.put(key, drive_tag("drive"), thumb)
        time.sleep(0.01)
    stats = store.stats()
    assert stats.bytes <= 3000 and stats.count < 5  # least recently used went
    keys = list(thumbs)
    assert store.get(keys[-1]) == thumbs[keys[-1]] and store.get(keys[0]) is None
    remaining = store.stats().count
    assert store.clear() == remaining and store.stats().count == 0
    store.close()


def test_store_index_holds_no_file_names(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=10**6, persistent=True)
    store.put(thumb_key("smb://nas", "/secret/holiday.jpg", 1, 2), drive_tag("smb://nas"),
              StoredThumb(b"x" * 10, "jpg", 1, 1, 1, "embedded", None))
    store.close()
    blob = b"".join(p.read_bytes() for p in (tmp_path / "s").rglob("*") if p.is_file())
    assert b"holiday" not in blob and b"secret" not in blob and b"nas" not in blob


def test_session_store_disappears_when_closed() -> None:
    store = ThumbStore(None, max_bytes=10**6, persistent=False)
    root = store.root
    store.put("ab" * 16, "d", StoredThumb(b"1", "jpg", 1, 1, 1, "embedded", None))
    assert root.exists()
    store.close()
    assert not root.exists()


def test_a_removed_thumbnail_file_is_forgotten(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=10**6, persistent=True)
    key = "cd" * 16
    store.put(key, "d", StoredThumb(b"123", "jpg", 1, 1, 1, "embedded", None))
    next((tmp_path / "s").rglob("*.jpg")).unlink()
    assert store.get(key) is None and store.stats().count == 0


def test_a_changed_file_gets_a_new_key() -> None:
    assert thumb_key("d", "/a.jpg", 10, 1) != thumb_key("d", "/a.jpg", 10, 2)
    assert thumb_key("d", "/a.jpg", 10, 1) != thumb_key("d", "/a.jpg", 11, 1)
    assert thumb_key("d", "/a.jpg", 10, 1) == thumb_key("d", "/a.jpg", 10, 1)


# --------------------------------------------------------------------------- the ladder
class CountingBackend(LocalBackend):
    """A local backend that behaves like a slow network drive and counts the bytes asked of it."""

    def __init__(self, kind: MediaKind = MediaKind.NETWORK) -> None:
        super().__init__()
        self.kind = kind
        self.bytes_read = 0
        self.reads = 0
        self._guard = threading.Lock()

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(kind=self.kind, label="Test drive", max_parallel_reads=2)

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        data = super().read_range(path, offset, length)
        with self._guard:
            self.bytes_read += len(data)
            self.reads += 1
        return data

    def open_read(self, path: str):  # type: ignore[no-untyped-def]
        handle = super().open_read(path)
        outer = self

        class Counting:
            def read(self, n: int = -1) -> bytes:
                data = handle.read(n)
                with outer._guard:
                    outer.bytes_read += len(data)
                return data

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *exc: object) -> None:
                handle.close()

        return Counting()


def item_for(backend: CountingBackend, path: Path, *, offline: bool = False) -> PreviewItem:
    st = path.stat()
    return PreviewItem(backend, str(path), st.st_size, st.st_mtime_ns, path.name, offline)


@pytest.fixture
def dirs(tmp_path: Path) -> AppDirs:
    return AppDirs(tmp_path / "cfg", tmp_path / "state", tmp_path / "cache").ensure()


def test_a_photo_on_a_slow_drive_costs_kilobytes_the_first_time_and_nothing_after(tmp_path: Path, dirs: AppDirs) -> None:
    photo_with_exif_thumbnail(tmp_path / "d" / "IMG_1.jpg", padding=4_000_000)
    backend = CountingBackend()
    service = PreviewService(dirs)
    item = item_for(backend, tmp_path / "d" / "IMG_1.jpg")
    first = service.get(item)
    assert not first.from_cache and first.thumb.source == "embedded" and first.thumb.original == (640, 480)
    assert backend.bytes_read <= 70 * KIB < item.size // 20
    again = service.get(item)
    assert again.from_cache and backend.bytes_read <= 70 * KIB  # no second trip
    assert service.stats.hits == 1 and service.stats.fetched == 1
    assert "▀" in service.render(first, cols=30, rows=10).plain
    service.close()


def test_a_picture_without_a_preview_is_not_downloaded_unless_small_asked_or_allowed(tmp_path: Path, dirs: AppDirs) -> None:
    big = tmp_path / "d" / "big.png"
    big.parent.mkdir(parents=True)
    Image.effect_noise((900, 900), 80).convert("RGB").save(big, "PNG")  # incompressible: several MiB
    assert big.stat().st_size > 1024 * 1024
    backend = CountingBackend()
    service = PreviewService(dirs, PreviewSettings(download_whole=False))
    with pytest.raises(Unavailable) as refused:
        service.get(item_for(backend, big))
    assert refused.value.code == "needs-download" and backend.bytes_read == 0
    with pytest.raises(Unavailable):  # a bulk job says "never" outright
        service.get(item_for(backend, big), allow_download=False)
    preview = service.get(item_for(backend, big), allow_download=True)
    assert preview.thumb.source == "decoded" and backend.bytes_read >= big.stat().st_size
    small = tmp_path / "d" / "small.png"
    picture(64, 48).save(small, "PNG")
    before = backend.bytes_read
    assert service.get(item_for(backend, small)).thumb.width == 64 and backend.bytes_read > before
    service.close()


def test_by_default_a_picture_with_no_small_preview_is_read_whole_to_make_one(tmp_path: Path, dirs: AppDirs) -> None:
    """A phone photo with no thumbnail inside gets a thumbnail on a slow drive too (the setting is on unless turned off)."""
    path = tmp_path / "d" / "plain.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(jpeg_bytes(picture(400, 300, seed=7)) + b"\0" * 2_500_000)  # over the small-file limit, no EXIF thumbnail
    backend = CountingBackend()
    service = PreviewService(dirs)
    preview = service.get(item_for(backend, path))
    assert preview.thumb.source == "decoded" and backend.bytes_read >= 2_500_000
    assert service.get(item_for(backend, path)).from_cache  # and it is kept: the drive is asked once
    service.close()


def test_a_cloud_only_file_is_never_fetched_unasked(tmp_path: Path, dirs: AppDirs) -> None:
    photo_with_exif_thumbnail(tmp_path / "d" / "cloud.jpg")
    backend = CountingBackend()
    service = PreviewService(dirs)
    item = item_for(backend, tmp_path / "d" / "cloud.jpg", offline=True)
    with pytest.raises(Unavailable) as refused:
        service.get(item)
    assert refused.value.code == "online-only" and backend.bytes_read == 0 and service.stats.skipped_online_only == 1
    assert service.get(item, allow_online_only=True).thumb.source == "embedded"
    service.close()


def test_unknown_kinds_have_no_preview(tmp_path: Path, dirs: AppDirs) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "notes.txt").write_text("hello")
    service = PreviewService(dirs)
    with pytest.raises(Unavailable) as refused:
        service.get(item_for(CountingBackend(), tmp_path / "d" / "notes.txt"))
    assert refused.value.code == "kind"
    assert extension("a.JPG") == "jpg" and extension("noext") == ""
    service.close()


def test_pick_source_prefers_what_is_cached_then_what_is_cheap(tmp_path: Path, dirs: AppDirs) -> None:
    photo_with_exif_thumbnail(tmp_path / "net" / "a.jpg")
    photo_with_exif_thumbnail(tmp_path / "usb" / "a.jpg")
    photo_with_exif_thumbnail(tmp_path / "ssd" / "a.jpg")
    net, usb, ssd = CountingBackend(MediaKind.NETWORK), CountingBackend(MediaKind.REMOVABLE), CountingBackend(MediaKind.LOCAL)
    items = [item_for(net, tmp_path / "net" / "a.jpg"), item_for(usb, tmp_path / "usb" / "a.jpg"),
             item_for(ssd, tmp_path / "ssd" / "a.jpg")]
    service = PreviewService(dirs)
    assert service.pick_source(items).backend is ssd
    service.get(items[0])  # the network copy is already cached: use it, touch nothing
    assert service.pick_source(items).backend is net
    # a cloud-only placeholder loses to a real file, even a slower one
    placeholder = PreviewItem(net, str(tmp_path / "net" / "other.jpg"), 10, 1, "other.jpg", offline=True)
    assert service.pick_source([placeholder, items[1]]).backend is usb
    service.close()


def test_modes_and_where_things_are_kept(tmp_path: Path, dirs: AppDirs) -> None:
    photo_with_exif_thumbnail(tmp_path / "d" / "a.jpg")
    backend = CountingBackend()
    item = item_for(backend, tmp_path / "d" / "a.jpg")
    off = PreviewService(dirs, PreviewSettings(mode="off"))
    off.get(item)
    off.get(item)
    assert off.stats.fetched == 2 and off.stats.hits == 0  # nothing remembered
    off.close()
    # session: gone when the service closes
    session = PreviewService(dirs, PreviewSettings(keep="session"))
    session.get(item)
    root = Path(session.storage_summary()[0][3])
    assert root.exists()
    session.close()
    assert not root.exists() and not (dirs.cache / "previews").exists()
    # persistent: still there for the next run
    kept = PreviewService(dirs, PreviewSettings(keep="persistent"))
    kept.get(item)
    kept.close()
    reads = backend.bytes_read
    again = PreviewService(dirs, PreviewSettings(keep="persistent"))
    assert again.get(item).from_cache and backend.bytes_read == reads
    assert again.clear() >= 1
    again.close()


def test_a_changed_file_is_fetched_again(tmp_path: Path, dirs: AppDirs) -> None:
    path = tmp_path / "d" / "a.jpg"
    photo_with_exif_thumbnail(path, seed=1)
    backend = CountingBackend()
    service = PreviewService(dirs)
    first = service.get(item_for(backend, path))
    photo_with_exif_thumbnail(path, seed=9)
    import os

    os.utime(path, ns=(1, path.stat().st_mtime_ns + 5_000_000_000))
    second = service.get(item_for(backend, path))
    assert not second.from_cache and second.thumb.data != first.thumb.data
    service.close()


def test_prefetch_warms_the_cache_in_the_background(tmp_path: Path, dirs: AppDirs) -> None:
    backend = CountingBackend()
    items = []
    for n in range(6):
        photo_with_exif_thumbnail(tmp_path / "d" / f"{n}.jpg", seed=n)
        items.append(item_for(backend, tmp_path / "d" / f"{n}.jpg"))
    service = PreviewService(dirs, PreviewSettings(prefetch=4))
    done = threading.Event()
    seen: list[str] = []

    def ready(item: PreviewItem) -> None:
        seen.append(item.name)
        if len(seen) == 4:
            done.set()

    service.on_ready(ready)
    service.prefetch(items)
    assert done.wait(10)
    assert sorted(seen) == ["0.jpg", "1.jpg", "2.jpg", "3.jpg"]  # only as many as the setting allows
    assert service.peek(items[0]) is not None and service.peek(items[5]) is None
    service.close()


def test_without_pillow_the_embedded_thumbnail_still_works_and_big_pictures_say_why(
        tmp_path: Path, dirs: AppDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.preview import service as service_module

    monkeypatch.setattr(service_module, "_pillow", lambda: None)
    photo_with_exif_thumbnail(tmp_path / "d" / "a.jpg")
    picture(300, 200).save(tmp_path / "d" / "b.png", "PNG")
    backend = CountingBackend()
    service = PreviewService(dirs)
    assert service.get(item_for(backend, tmp_path / "d" / "a.jpg")).thumb.fmt == "jpg"
    monkeypatch.setattr(service_module.ffmpeg, "find_ffmpeg", lambda configured="": None)
    assert service.get(item_for(backend, tmp_path / "d" / "b.png")).thumb.fmt == "png"  # the built-in PNG reader
    Image.new("RGB", (80, 60), (200, 10, 10)).save(tmp_path / "d" / "c.webp", "WEBP")
    with pytest.raises(Unavailable) as refused:  # WebP needs Pillow or ffmpeg, and says so
        service.get(item_for(backend, tmp_path / "d" / "c.webp"))
    assert refused.value.code == "needs-decoder" and "Pillow or ffmpeg" in refused.value.reason
    # a plain JPEG with no thumbnail inside is decoded by the built-in decoder
    (tmp_path / "d" / "plain.jpg").write_bytes(jpeg_bytes(picture(900, 600)))
    plain = service.get(item_for(backend, tmp_path / "d" / "plain.jpg"))
    assert plain.thumb.fmt == "png" and max(plain.thumb.width or 0, plain.thumb.height or 0) <= 256
    assert "▀" in service.render(plain, cols=30, rows=10).plain
    service.close()


def test_raw_style_preview_is_shrunk_before_it_is_stored(tmp_path: Path, dirs: AppDirs) -> None:
    raw_like(tmp_path / "d" / "shot.dng", preview_size=(1200, 800))
    backend = CountingBackend()
    service = PreviewService(dirs, PreviewSettings(size_px=256))
    preview = service.get(item_for(backend, tmp_path / "d" / "shot.dng"))
    assert max(preview.thumb.width or 0, preview.thumb.height or 0) <= 256  # the 1200 px preview was shrunk
    assert backend.bytes_read < (tmp_path / "d" / "shot.dng").stat().st_size
    assert preview.orientation == 6  # and it keeps its orientation, applied only when shown
    service.close()


# --------------------------------------------------------------------------- similar images without reading whole photos
def _similar_run(root: Path, backend: LocalBackend, previews: PreviewService | None, *, full_read: bool = False, progress=None):  # type: ignore[no-untyped-def]
    from duplio.compare.engine import compare
    from duplio.compare.model import ScanOptions
    from duplio.scan.filters import ScanFilter
    from duplio.scan.inventory import RootRole, make_roots, scan
    from duplio.vfs.location import Location

    inv = scan(make_roots([(Location(backend, str(root)), RootRole.NORMAL)]), ScanFilter())
    opts = ScanOptions(similar_images=True, similar_full_read=full_read)
    return compare(inv, opts, previews=previews, progress=progress)


def _photos_for_similarity(root: Path) -> None:
    photo_with_exif_thumbnail(root / "IMG_1.jpg", padding=3_000_000, seed=3)
    photo_with_exif_thumbnail(root / "copy of IMG_1.jpg", padding=1_500_000, seed=3)  # same picture, different file
    photo_with_exif_thumbnail(root / "IMG_2.jpg", padding=2_000_000, seed=9, size=(480, 640), thumb_size=(120, 160))


def test_similar_images_on_a_slow_drive_read_the_embedded_thumbnails_not_the_photos(tmp_path: Path, dirs: AppDirs) -> None:
    pytest.importorskip("PIL")
    _photos_for_similarity(tmp_path / "d")
    backend = CountingBackend()
    result = _similar_run(tmp_path / "d", backend, PreviewService(dirs))
    assert [sorted(m.rel for m in g.members) for g in result.similar] == [["IMG_1.jpg", "copy of IMG_1.jpg"]]
    total = sum(p.stat().st_size for p in (tmp_path / "d").iterdir())
    assert total > 6_000_000 and backend.bytes_read < total // 20  # a few dozen KiB per photo, not megabytes


def test_similar_full_read_goes_back_to_reading_whole_photos(tmp_path: Path, dirs: AppDirs) -> None:
    pytest.importorskip("PIL")
    _photos_for_similarity(tmp_path / "d")
    backend = CountingBackend()
    result = _similar_run(tmp_path / "d", backend, PreviewService(dirs), full_read=True)
    assert [sorted(m.rel for m in g.members) for g in result.similar] == [["IMG_1.jpg", "copy of IMG_1.jpg"]]
    assert backend.bytes_read >= 6_000_000


def test_fast_drives_are_still_fingerprinted_from_the_picture_itself(tmp_path: Path, dirs: AppDirs) -> None:
    pytest.importorskip("PIL")
    _photos_for_similarity(tmp_path / "d")
    backend = CountingBackend(MediaKind.LOCAL)  # read_cost 0: nothing to save
    service = PreviewService(dirs)
    result = _similar_run(tmp_path / "d", backend, service)
    assert len(result.similar) == 1 and service.stats.fetched == 0


def test_a_photo_with_no_small_preview_is_not_compared_on_a_slow_drive_and_the_user_is_told(tmp_path: Path, dirs: AppDirs) -> None:
    from duplio.events import RecordingProgress

    pytest.importorskip("PIL")
    (tmp_path / "d").mkdir()
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / "d" / name).write_bytes(jpeg_bytes(picture(400, 300, seed=5)) + b"\0" * 3_000_000)  # no EXIF thumbnail
    backend = CountingBackend()
    progress = RecordingProgress()
    result = _similar_run(tmp_path / "d", backend, PreviewService(dirs), progress=progress)
    assert result.similar == []
    assert backend.bytes_read < 400_000  # the 3 MB files were not pulled down
    assert any("--similar-full-read" in text and "2" in text for text in progress.warnings())


def test_on_a_fast_disk_only_pictures_that_took_work_are_kept_between_runs(tmp_path: Path, dirs: AppDirs) -> None:
    """A photo's own thumbnail costs a glance, so it is kept for this session only; a picture decoded from the whole file, or a frame
    of a movie, took real work and is kept like the ones from slow drives."""
    plain = tmp_path / "d" / "plain.jpg"
    plain.parent.mkdir(parents=True)
    plain.write_bytes(jpeg_bytes(picture(400, 300, seed=3)))  # no thumbnail inside: it has to be decoded
    photo_with_exif_thumbnail(tmp_path / "d" / "camera.jpg")
    backend = CountingBackend(MediaKind.LOCAL)
    service = PreviewService(dirs)
    assert service.get(item_for(backend, plain)).thumb.source == "decoded"
    assert service.get(item_for(backend, tmp_path / "d" / "camera.jpg")).thumb.source == "embedded"
    kinds = {kind: count for kind, count, _bytes, _path in service.storage_summary()}
    assert kinds == {"memory": 1, "persistent": 1}
    service.close()
    tomorrow = PreviewService(dirs)
    assert tomorrow.peek(item_for(backend, plain)) is not None  # the picture that took work is still at hand
    assert tomorrow.peek(item_for(backend, tmp_path / "d" / "camera.jpg")) is None  # the cheap one is simply made again
    tomorrow.close()
