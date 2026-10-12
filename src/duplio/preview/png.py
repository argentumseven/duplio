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


def read_rgb(data: bytes) -> tuple[int, int, bytes]:
    """``(width, height, rgb)``; raises ValueError for anything but a plain 8-bit, non-interlaced PNG."""
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
    if depth != 8 or interlace != 0 or colour not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported PNG layout")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    if len(raw) < height * (stride + 1):
        raise ValueError("PNG data is short")
    out = bytearray(height * stride)
    previous = bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        kind_byte = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        if kind_byte == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif kind_byte == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind_byte == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind_byte == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind_byte != 0:
            raise ValueError("unknown PNG filter")
        out[y * stride : (y + 1) * stride] = line
        previous = line
    if colour == 2:
        return width, height, bytes(out)
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        if colour == 0:
            rgb[i * 3 : i * 3 + 3] = bytes((out[i],)) * 3
        elif colour == 4:
            rgb[i * 3 : i * 3 + 3] = bytes((out[i * 2],)) * 3
        elif colour == 6:
            rgb[i * 3 : i * 3 + 3] = out[i * 4 : i * 4 + 3]
        else:
            index = out[i]
            rgb[i * 3 : i * 3 + 3] = palette[index * 3 : index * 3 + 3]
    return width, height, bytes(rgb)
