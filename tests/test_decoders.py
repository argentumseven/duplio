"""Pictures without Pillow: the small decoders, movie covers and frames, and what the service does when nothing can show a file."""

from __future__ import annotations

import io
import os
import shutil
import stat
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from PIL import Image

from duplio.events import CancelToken
from duplio.preview import decoders, ffmpeg, png, video
from duplio.preview.service import PreviewItem, PreviewService, Unavailable
from duplio.preview.settings import PreviewSettings
from duplio.util.appdirs import AppDirs
from duplio.vfs.local import LocalBackend

from .previewkit import jpeg_bytes, picture


def sample(size: tuple[int, int] = (41, 27)) -> Image.Image:
    img = Image.new("RGB", size)
    px = img.load()
    assert px is not None
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 6) % 256, (y * 9) % 256, ((x + y) * 5) % 256)
    return img


def saved(img: Image.Image, fmt: str, **kw: object) -> tuple[bytes, Image.Image]:
    out = io.BytesIO()
    img.save(out, fmt, **kw)
    return out.getvalue(), Image.open(io.BytesIO(out.getvalue())).convert("RGB")


# --------------------------------------------------------------------------- the formats
@pytest.mark.parametrize("mode", ["RGB", "L", "P", "1"])
def test_bmp_matches_pillow(mode: str) -> None:
    data, ref = saved(sample().convert(mode), "BMP")
    got = decoders.decode(data, 256, "a.bmp")
    assert (got.width, got.height) == ref.size and got.rgb == ref.tobytes()


def test_bmp_with_16_and_32_bits_and_top_down_rows() -> None:
    src = sample((41, 27))
    w, h = src.size

    def build(bits: int, top_down: bool) -> bytes:
        stride = ((w * bits + 31) // 32) * 4
        rows = []
        for y in range(h):
            row = bytearray()
            for x in range(w):
                r, g, b = src.getpixel((x, y))  # type: ignore[misc]
                row += struct.pack("<H", ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)) if bits == 16 else bytes((b, g, r, 0))
            rows.append(bytes(row) + bytes(stride - len(row)))
        pixels = b"".join(rows if top_down else rows[::-1])
        header = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, bits, 0, len(pixels), 2835, 2835, 0, 0)
        return b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54) + header + pixels

    for top_down in (False, True):
        assert decoders.decode(build(32, top_down), 256, "x.bmp").rgb == src.tobytes()
        five = decoders.decode(build(16, top_down), 256, "x.bmp").rgb
        assert all(abs(a - ((b >> 3) * 255) // 31) <= 1 for a, b in zip(five, src.tobytes(), strict=True))


def test_a_run_length_compressed_bmp_says_what_it_needs() -> None:
    data = bytearray(saved(sample().convert("P"), "BMP")[0])
    struct.pack_into("<I", data, 30, 1)  # BI_RLE8
    with pytest.raises(decoders.Undecodable) as refused:
        decoders.decode(bytes(data), 256, "x.bmp")
    assert "Pillow or ffmpeg" in refused.value.reason


def test_a_big_bmp_is_sampled_not_decoded_whole() -> None:
    data, _ = saved(sample((3000, 2000)), "BMP")
    got = decoders.decode(data, 256, "big.bmp")
    assert max(got.width, got.height) < 1600 and got.width * 2 == pytest.approx(got.height * 3, abs=6)  # same proportions


@pytest.mark.parametrize(("mode", "kw"), [("RGB", {}), ("P", {}), ("L", {}), ("P", {"interlace": True})])
def test_gif_matches_pillow(mode: str, kw: dict[str, object]) -> None:
    img = sample((300, 220)).quantize(200) if mode == "P" and not kw else sample().convert(mode)
    if kw:
        img = sample().quantize(16)
    data, ref = saved(img, "GIF", **kw)
    got = decoders.decode(data, 256, "a.gif")
    assert (got.width, got.height) == ref.size and got.rgb == ref.tobytes()


def test_a_transparent_gif_pixel_shows_as_white() -> None:
    img = Image.new("P", (4, 4), 0)
    img.putpalette([255, 0, 0, 0, 255, 0] + [0] * 250 * 3)
    img.putpixel((1, 1), 1)
    data, _ = saved(img, "GIF", transparency=1)
    got = decoders.decode(data, 256, "t.gif")
    assert got.rgb[(1 * 4 + 1) * 3 : (1 * 4 + 1) * 3 + 3] == b"\xff\xff\xff" and got.rgb[:3] == b"\xff\x00\x00"


@pytest.mark.parametrize(("mode", "ext"), [("RGB", "ppm"), ("L", "pgm"), ("1", "pbm")])
def test_the_pnm_family_matches_pillow(mode: str, ext: str) -> None:
    data, ref = saved(sample().convert(mode), "PPM")
    got = decoders.decode(data, 256, f"a.{ext}")
    assert got.rgb == ref.tobytes()


def test_plain_text_pnm_and_comments_and_16_bit() -> None:
    assert decoders.decode(b"P3\n# a comment\n2 1\n255\n255 0 0  0 0 255\n", 256).rgb == b"\xff\x00\x00\x00\x00\xff"
    assert decoders.decode(b"P1\n3 1\n101\n", 256).rgb == bytes([0] * 3 + [255] * 3 + [0] * 3)  # 1 is black
    assert decoders.decode(b"P2\n2 1\n4\n0 4\n", 256).rgb == bytes([0, 0, 0, 255, 255, 255])
    assert decoders.decode(b"P5\n2 1\n65535\n\x00\x00\xff\xff", 256).rgb == bytes([0, 0, 0, 255, 255, 255])


@pytest.mark.parametrize(("mode", "kw"), [("RGB", {}), ("RGB", {"compression": "tga_rle"}), ("L", {}), ("RGBA", {})])
def test_tga_matches_pillow(mode: str, kw: dict[str, object]) -> None:
    data, ref = saved(sample().convert(mode), "TGA", **kw)
    got = decoders.decode(data, 256, "a.tga")
    assert got.rgb == ref.tobytes()


def test_unreadable_formats_are_named_in_the_reason_and_damaged_ones_are_damaged() -> None:
    webp, _ = saved(sample(), "WEBP")
    with pytest.raises(decoders.Undecodable) as refused:
        decoders.decode(webp, 256, "a.webp")
    assert refused.value.code == "needs-decoder" and "WebP" in refused.value.reason and "ffmpeg" in refused.value.reason
    bmp = saved(sample(), "BMP")[0]
    with pytest.raises(decoders.Undecodable) as damaged:
        decoders.decode(bmp[:200], 256, "a.bmp")
    assert damaged.value.code == "damaged"


# --------------------------------------------------------------------------- PNG in every layout
def adam7_png(img: Image.Image) -> bytes:
    """An interlaced PNG written by hand (Pillow cannot write one)."""
    w, h = img.size
    raw = bytearray()
    for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
        for y in range(y0, h, dy):
            if x0 < w:
                raw.append(0)
                for x in range(x0, w, dx):
                    raw += bytes(img.getpixel((x, y)))  # type: ignore[arg-type]
    chunk = lambda kind, body: struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))  # noqa: E731
    return (png.SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 1)) + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def test_png_in_every_layout_matches_pillow() -> None:
    base = sample((37, 23))
    for mode in ("RGB", "RGBA", "L", "LA", "P", "1"):
        data, ref = saved(base.convert(mode), "PNG")
        assert png.read_rgb(data)[2] == ref.tobytes(), mode
    data, ref = saved(base.convert("P").quantize(16), "PNG", bits=4)
    assert png.read_rgb(data)[2] == ref.tobytes()
    sixteen, _ = saved(Image.new("I;16", (9, 5), 40000), "PNG")
    assert set(png.read_rgb(sixteen)[2]) == {40000 >> 8}  # the high byte of each sample
    assert png.read_rgb(adam7_png(base))[2] == base.tobytes()  # interlaced
    assert png.read_rgb(adam7_png(sample((5, 3))))[2] == sample((5, 3)).tobytes()  # smaller than one 8x8 block


def test_a_huge_png_is_refused_cleanly_not_unpacked_for_a_minute() -> None:
    header = struct.pack(">IIBBBBB", 9000, 9000, 8, 2, 0, 0, 0)
    chunk = lambda kind, body: struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))  # noqa: E731
    data = png.SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    with pytest.raises(decoders.Undecodable) as refused:
        decoders.decode(data, 256, "huge.png")
    assert refused.value.code == "too-large" and "Pillow" in refused.value.reason


# --------------------------------------------------------------------------- the cover picture inside a movie
def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def mp4_with_cover(cover: bytes | None, *, moov_last: bool, mdat_size: int = 5_000_000) -> bytes:
    ftyp = box(b"ftyp", b"isom\0\0\2\0isomiso2mp41")
    mdat = box(b"mdat", bytes(mdat_size))
    items = box(b"covr", box(b"data", struct.pack(">II", 13, 0) + cover)) if cover else b""
    ilst = box(b"ilst", items)
    meta = box(b"meta", b"\0\0\0\0" + box(b"hdlr", bytes(24)) + ilst)
    moov = box(b"moov", box(b"mvhd", bytes(100)) + box(b"trak", bytes(5000)) + box(b"udta", meta))
    return ftyp + (mdat + moov if moov_last else moov + mdat)


@pytest.mark.parametrize("moov_last", [False, True])
def test_a_movie_cover_is_found_with_a_few_small_reads(moov_last: bool) -> None:
    cover = jpeg_bytes(picture(120, 80, seed=2))
    data = mp4_with_cover(cover, moov_last=moov_last)
    reads: list[int] = []

    def read(offset: int, length: int) -> bytes:
        reads.append(length)
        return data[offset : offset + length]

    assert video.cover_art(read, len(data)) == cover
    assert sum(reads) < 20_000 and len(reads) < 30  # the five megabytes of film were not read


def test_a_movie_without_a_cover_or_not_a_movie_gives_none() -> None:
    plain = mp4_with_cover(None, moov_last=True)
    assert video.cover_art(lambda o, n: plain[o : o + n], len(plain)) is None
    junk = os.urandom(5000)
    assert video.cover_art(lambda o, n: junk[o : o + n], len(junk)) is None


# --------------------------------------------------------------------------- ffmpeg
needs_ffmpeg = pytest.mark.skipif(ffmpeg.find_ffmpeg() is None, reason="ffmpeg is not installed")


@needs_ffmpeg
def test_ffmpeg_makes_pictures_of_formats_nothing_else_reads() -> None:
    program = ffmpeg.find_ffmpeg()
    assert program is not None
    webp, _ = saved(sample((300, 200)), "WEBP")
    for fmt, magic in (("jpg", b"\xff\xd8"), ("png", png.SIGNATURE)):
        out = ffmpeg.picture_from_bytes(program, webp, 128, fmt=fmt)
        assert out.startswith(magic) and max(Image.open(io.BytesIO(out)).size) == 128  # shrunk to fit, never enlarged
    small = ffmpeg.picture_from_bytes(program, saved(sample((50, 30)), "WEBP")[0], 128)
    assert Image.open(io.BytesIO(small)).size[0] <= 50 + 1
    with pytest.raises(ffmpeg.FfmpegError):
        ffmpeg.picture_from_bytes(program, b"this is not a picture at all", 128)


@needs_ffmpeg
def test_ffmpeg_takes_a_frame_from_a_movie(tmp_path: Path) -> None:
    program = ffmpeg.find_ffmpeg()
    assert program is not None
    movie = tmp_path / "clip.mp4"
    made = subprocess.run([program, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=10", "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", str(movie)], capture_output=True, check=False)
    if made.returncode != 0:
        pytest.skip("this ffmpeg cannot make a test movie")
    frame = ffmpeg.frame_from_file(program, str(movie), 128)
    assert Image.open(io.BytesIO(frame)).size == (128, 72)
    half_second = tmp_path / "short.mp4"  # shorter than the one-second seek: the first frame is taken instead
    subprocess.run([program, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=160x90:rate=10", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(half_second)], capture_output=True, check=True)
    assert Image.open(io.BytesIO(ffmpeg.frame_from_file(program, str(half_second), 128))).size == (128, 72)


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a stand-in for ffmpeg")
def test_a_stuck_ffmpeg_is_stopped_by_the_time_limit_and_by_cancel(tmp_path: Path) -> None:
    slow = tmp_path / "ffmpeg"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(ffmpeg.FfmpegError, match="too long"):
        ffmpeg.picture_from_bytes(str(slow), b"x", 64, timeout=0.6)
    token = CancelToken()
    token.cancel("stop")
    from duplio.errors import CancelledError

    with pytest.raises(CancelledError):
        ffmpeg.picture_from_bytes(str(slow), b"x", 64, cancel=lambda: token.cancelled, timeout=20)


def test_ffmpeg_is_looked_for_in_the_setting_then_the_environment_then_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "my-ffmpeg"
    fake.write_text("")
    other = tmp_path / "env-ffmpeg"
    other.write_text("")
    assert ffmpeg.find_ffmpeg(str(fake)) == str(fake)
    monkeypatch.setenv("DUPLIO_FFMPEG", str(other))
    assert ffmpeg.find_ffmpeg("") == str(other)
    assert ffmpeg.find_ffmpeg(str(tmp_path / "missing")) == str(other)  # a setting that points nowhere is ignored
    monkeypatch.delenv("DUPLIO_FFMPEG")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(ffmpeg.os.path, "isfile", lambda p: False)
    assert ffmpeg.find_ffmpeg("") is None


# --------------------------------------------------------------------------- the service, for movies and unusual pictures
class SlowDrive(LocalBackend):
    """A local folder that claims to be a slow network drive."""

    def __init__(self) -> None:
        super().__init__()
        self.bytes_read = 0

    def capabilities(self, path: str):  # type: ignore[no-untyped-def]
        from duplio.vfs.base import Capabilities, MediaKind

        return Capabilities(kind=MediaKind.NETWORK, label="Test drive", max_parallel_reads=2)

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        data = super().read_range(path, offset, length)
        self.bytes_read += len(data)
        return data


def item_for(backend: LocalBackend, path: Path) -> PreviewItem:
    info = path.stat()
    return PreviewItem(backend, str(path), info.st_size, info.st_mtime_ns, path.name)


def fake_ffmpeg(directory: Path) -> Path:
    """A stand-in that answers every request with a small JPEG, so the plumbing can be tested without a real ffmpeg."""
    jpeg = directory / "frame.jpg"
    jpeg.write_bytes(jpeg_bytes(picture(160, 90, seed=9)))
    script = directory / "ffmpeg"
    script.write_text(f"#!/bin/sh\ncat '{jpeg}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a stand-in for ffmpeg")
def test_a_movie_gets_a_thumbnail_from_ffmpeg_and_it_is_kept(tmp_path: Path, dirs: AppDirs) -> None:
    program = fake_ffmpeg(tmp_path)
    movie = tmp_path / "m" / "holiday.mov"
    movie.parent.mkdir()
    movie.write_bytes(b"\0" * 5000)
    service = PreviewService(dirs, PreviewSettings(ffmpeg=str(program)))
    backend = SlowDrive()
    first = service.get(item_for(backend, movie))
    assert first.thumb.source == "frame" and (first.thumb.width, first.thumb.height) == (160, 90)
    assert service.get(item_for(backend, movie)).from_cache
    service.close()


def test_a_movie_with_a_cover_needs_no_ffmpeg_and_reads_little(tmp_path: Path, dirs: AppDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.preview import service as service_module

    monkeypatch.setattr(service_module.ffmpeg, "find_ffmpeg", lambda configured="": None)
    cover = jpeg_bytes(picture(120, 80, seed=4))
    movie = tmp_path / "m" / "film.m4v"
    movie.parent.mkdir()
    movie.write_bytes(mp4_with_cover(cover, moov_last=True))
    backend = SlowDrive()
    service = PreviewService(dirs)
    got = service.get(item_for(backend, movie))
    assert got.thumb.source == "cover" and backend.bytes_read < 40_000
    service.close()


def test_a_movie_without_ffmpeg_says_what_is_needed(tmp_path: Path, dirs: AppDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.preview import service as service_module

    monkeypatch.setattr(service_module.ffmpeg, "find_ffmpeg", lambda configured="": None)
    movie = tmp_path / "m" / "clip.mp4"
    movie.parent.mkdir()
    movie.write_bytes(mp4_with_cover(None, moov_last=True, mdat_size=1000))
    service = PreviewService(dirs)
    with pytest.raises(Unavailable) as refused:
        service.get(item_for(SlowDrive(), movie))
    assert refused.value.code == "needs-ffmpeg" and "ffmpeg" in refused.value.reason
    service.close()


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a stand-in for ffmpeg")
def test_a_picture_nothing_else_reads_goes_to_ffmpeg(tmp_path: Path, dirs: AppDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.preview import service as service_module

    monkeypatch.setattr(service_module, "_pillow", lambda: None)
    program = fake_ffmpeg(tmp_path)
    pic = tmp_path / "m" / "photo.webp"
    pic.parent.mkdir()
    sample().save(pic, "WEBP")
    service = PreviewService(dirs, PreviewSettings(ffmpeg=str(program)))
    got = service.get(item_for(SlowDrive(), pic))
    assert got.thumb.source == "decoded" and got.thumb.width == 160
    service.close()


def test_movies_are_not_read_on_a_slow_drive_when_downloads_are_off(tmp_path: Path, dirs: AppDirs) -> None:
    movie = tmp_path / "m" / "clip.mp4"
    movie.parent.mkdir()
    movie.write_bytes(mp4_with_cover(None, moov_last=True, mdat_size=1000))
    service = PreviewService(dirs, PreviewSettings(download_whole=False, ffmpeg=__file__))
    with pytest.raises(Unavailable) as refused:
        service.get(item_for(SlowDrive(), movie))
    assert refused.value.code == "needs-download"
    service.close()
