"""Minimal PNG reading and writing with the standard library (zlib), for thumbnails Duplio makes itself.

The writer produces 8-bit RGB images with no filtering. The reader accepts the common non-interlaced 8-bit layouts (grey,
grey+alpha, RGB, RGBA) with every filter type, so it also reads PNGs made by other tools.
"""

from __future__ import annotations

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_rgb(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    return (
        SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


class TooLarge(ValueError):
    """A picture too big to be worth decoding in plain Python (Pillow, or ffmpeg, does it in a fraction of the time)."""


MAX_RAW_BYTES = 24 * 1024 * 1024  # of unpacked pixel data; beyond this pure Python takes the better part of a minute
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))  # x0, y0, dx, dy
_LAYOUTS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _unfilter(raw: bytes, offset: int, rows: int, stride: int, step: int) -> tuple[list[bytearray], int]:
    """The ``rows`` scanlines that start at ``offset``, with their filters undone; and where the next pass starts."""
    out: list[bytearray] = []
    previous = bytearray(stride)
    for _ in range(rows):
        kind = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += stride + 1
        if kind == 1:  # Sub
            for lane in range(step):
                total = 0
                for i in range(lane, stride, step):
                    total = (total + line[i]) & 255
                    line[i] = total
        elif kind == 2:  # Up
            line = bytearray((a + b) & 255 for a, b in zip(line, previous, strict=True))
        elif kind == 3:  # Average
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind == 4:  # Paeth
            for i in range(stride):
                a = line[i - step] if i >= step else 0
                b = previous[i]
                c = previous[i - step] if i >= step else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError("unknown PNG filter")
        out.append(line)
        previous = line
    return out, offset


def _samples(line: bytearray, count: int, depth: int) -> list[int]:
    if depth == 8:
        return list(line[:count])
    if depth == 16:
        return list(line[0 : count * 2 : 2])  # the high byte of each sample is all a thumbnail needs
    mask = (1 << depth) - 1
    out: list[int] = []
    for byte in line:
        for shift in range(8 - depth, -1, -depth):
            out.append((byte >> shift) & mask)
    return out[:count]


def read_rgb(data: bytes) -> tuple[int, int, bytes]:
    """``(width, height, rgb)`` for any PNG layout (every bit depth and colour type, interlaced or not).

    Raises :class:`TooLarge` (a ``ValueError``) for a picture whose pixels would take too long to unpack in Python, and
    ``ValueError`` for data that is not a PNG or is damaged.
    """
    if data[:8] != SIGNATURE:
        raise ValueError("not a PNG")
    pos = 8
    header = None
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, colour, _, _, interlace = header
    if depth not in _LAYOUTS.get(colour, ()) or interlace not in (0, 1) or not width or not height:
        raise ValueError("unsupported PNG layout")
    channels = _CHANNELS[colour]
    bits = channels * depth
    if width * height * max(bits // 8, 1) > MAX_RAW_BYTES:
        raise TooLarge(f"a {width}x{height} PNG is too big to unpack without Pillow")
    step = max(bits // 8, 1)
    raw = zlib.decompress(bytes(idat))
    rgb = bytearray(width * height * 3)
    offset = 0
    for x0, y0, dx, dy in (_ADAM7 if interlace else ((0, 0, 1, 1),)):
        pw, ph = -(-(width - x0) // dx), -(-(height - y0) // dy)
        if pw <= 0 or ph <= 0:
            continue
        stride = (pw * bits + 7) // 8
        if len(raw) < offset + ph * (stride + 1):
            raise ValueError("PNG data is short")
        lines, offset = _unfilter(raw, offset, ph, stride, step)
        for j, line in enumerate(lines):
            values = _samples(line, pw * channels, depth)
            y = y0 + j * dy
            base = y * width
            for i in range(pw):
                if colour == 2:
                    r, g, b = values[i * 3 : i * 3 + 3]
                elif colour == 6:
                    r, g, b = values[i * 4 : i * 4 + 3]
                elif colour == 3:
                    k = values[i] * 3
                    r, g, b = palette[k : k + 3] if k + 3 <= len(palette) else (0, 0, 0)
                else:  # grey, or grey with alpha
                    grey = values[i * channels]
                    r = g = b = grey * 255 // ((1 << depth) - 1) if depth < 8 else grey
                at = (base + x0 + i * dx) * 3
                rgb[at], rgb[at + 1], rgb[at + 2] = r, g, b
    return width, height, bytes(rgb)
