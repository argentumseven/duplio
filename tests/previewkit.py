"""Builders for pictures with the structures real cameras write: EXIF thumbnails, TIFF/RAW-style previews."""

from __future__ import annotations

import io
import random
import struct
from pathlib import Path

from PIL import Image


def picture(width: int, height: int, seed: int = 1) -> Image.Image:
    """A picture with structure (gradients and blocks), so thumbnails of different pictures differ."""
    rng = random.Random(seed)
    img = Image.new("RGB", (width, height))
    px = img.load()
    a, b, c = rng.randrange(256), rng.randrange(256), rng.randrange(256)
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 255 // max(width - 1, 1) + a) % 256, (y * 255 // max(height - 1, 1) + b) % 256,
                        c if (x // 16 + y // 16) % 2 else 255 - c)
    return img


def jpeg_bytes(img: Image.Image, quality: int = 85, **kw) -> bytes:  # type: ignore[no-untyped-def]
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, **kw)
    return out.getvalue()


def _ifd(entries: list[tuple[int, int, int]], next_ifd: int, bo: str) -> bytes:
    """entries are (tag, type, value) with one inline value each (type 3 = SHORT, 4 = LONG)."""
    out = struct.pack(bo + "H", len(entries))
    for tag, typ, value in sorted(entries):
        field = struct.pack(bo + "H", value) + b"\0\0" if typ == 3 else struct.pack(bo + "I", value)
        out += struct.pack(bo + "HHI", tag, typ, 1) + field
    return out + struct.pack(bo + "I", next_ifd)


def exif_block(thumb: bytes, *, orientation: int = 1, pixels: tuple[int, int] = (640, 480), bo: str = "<") -> bytes:
    """The payload of an APP1 EXIF segment: TIFF header, IFD0, Exif IFD, IFD1 with a JPEG thumbnail."""
    order = b"II" if bo == "<" else b"MM"
    ifd0_at = 8
    ifd0_len = 2 + 12 * 2 + 4
    exif_at = ifd0_at + ifd0_len
    exif_len = 2 + 12 * 2 + 4
    ifd1_at = exif_at + exif_len
    ifd1_len = 2 + 12 * 3 + 4
    thumb_at = ifd1_at + ifd1_len
    ifd0 = _ifd([(0x0112, 3, orientation), (0x8769, 4, exif_at)], ifd1_at, bo)
    exif = _ifd([(0xA002, 4, pixels[0]), (0xA003, 4, pixels[1])], 0, bo)
    ifd1 = _ifd([(0x0103, 3, 6), (0x0201, 4, thumb_at), (0x0202, 4, len(thumb))], 0, bo)
    header = order + struct.pack(bo + "HI", 42, ifd0_at)
    return header + ifd0 + exif + ifd1 + thumb


def photo_with_exif_thumbnail(path: Path, *, size: tuple[int, int] = (640, 480), thumb_size: tuple[int, int] = (160, 120),
                              orientation: int = 1, padding: int = 0, seed: int = 1, bo: str = "<") -> bytes:
    """Write a JPEG whose EXIF block holds a thumbnail; ``padding`` bytes of trailing data make the file bigger."""
    main = jpeg_bytes(picture(*size, seed=seed))
    thumb = jpeg_bytes(picture(*thumb_size, seed=seed), quality=70)
    payload = b"Exif\x00\x00" + exif_block(thumb, orientation=orientation, pixels=size, bo=bo)
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    data = main[:2] + segment + main[2:] + (b"\x00" * padding)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return thumb


def raw_like(path: Path, *, thumb_size: tuple[int, int] = (160, 120), preview_size: tuple[int, int] = (1200, 800),
             bo: str = "<", seed: int = 1, padding: int = 200_000) -> tuple[bytes, bytes]:
    """A TIFF-container file (like DNG/CR2/NEF) with a small IFD1 thumbnail and a larger SubIFD preview."""
    order = b"II" if bo == "<" else b"MM"
    thumb = jpeg_bytes(picture(*thumb_size, seed=seed), quality=70)
    preview = jpeg_bytes(picture(*preview_size, seed=seed), quality=80)
    ifd0_at = 8
    sub_at = ifd0_at + (2 + 12 * 4 + 4)
    ifd1_at = sub_at + (2 + 12 * 5 + 4)
    thumb_at = ifd1_at + (2 + 12 * 4 + 4)
    preview_at = thumb_at + len(thumb)
    ifd0 = _ifd([(0x0100, 4, 6000), (0x0101, 4, 4000), (0x0112, 3, 6), (0x014A, 4, sub_at)], ifd1_at, bo)
    sub = _ifd([(0x0100, 4, preview_size[0]), (0x0101, 4, preview_size[1]), (0x0103, 3, 7),
                (0x0111, 4, preview_at), (0x0117, 4, len(preview))], 0, bo)
    ifd1 = _ifd([(0x0100, 4, thumb_size[0]), (0x0101, 4, thumb_size[1]), (0x0201, 4, thumb_at),
                 (0x0202, 4, len(thumb))], 0, bo)
    data = order + struct.pack(bo + "HI", 42, ifd0_at) + ifd0 + sub + ifd1 + thumb + preview + b"\x00" * padding
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return thumb, preview
