"""Picture formats read in plain Python, so a thumbnail does not need Pillow.

Pillow reads dozens of formats in C; without it Duplio can still make thumbnails of the ones people actually have lying
about: JPEG (see :mod:`duplio.preview.jpeg`), PNG (:mod:`duplio.preview.png`), and, here, BMP, GIF, the PNM family (PPM, PGM,
PBM) and TGA. Big pictures are *sampled* (every few pixels) instead of decoded in full, so a 20-megapixel BMP does not take
longer than a small one.

Anything else (WebP, HEIC, AVIF, JPEG 2000, PSD, ICO, camera RAW without an embedded preview…) raises :class:`Undecodable`
with a sentence saying what would make it work, and the caller tries ffmpeg or reports it.
"""

from __future__ import annotations

import struct

from duplio.preview import jpeg, png
from duplio.preview.render import Raster

MAX_GIF_PIXELS = 40_000_000  # the LZW stream has to be decoded in full; beyond this it is not worth it


class Undecodable(Exception):
    """A picture this module cannot read; ``reason`` says why, in a sentence for the person."""

    def __init__(self, reason: str, code: str = "needs-decoder") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def sniff(data: bytes, name: str = "") -> str:
    """The format of the data, from its first bytes (a name's extension is only a hint for formats with no signature)."""
    head = data[:16]
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == png.SIGNATURE:
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:1] == b"P" and head[1:2] in b"123456" and head[2:3] in (b"\n", b" ", b"\r", b"\t"):
        return "pnm"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif", b"ftypheim", b"ftypmsf1"):
        return "heif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("tga", "icb", "vda", "vst"):
        return "tga"
    return ""


def decode(data: bytes, want: int, name: str = "") -> Raster:
    """The picture as pixels, no bigger than needed to make a ``want``-pixel thumbnail."""
    kind = sniff(data, name)
    try:
        if kind == "jpeg":
            return _jpeg(data, want)
        if kind == "png":
            w, h, rgb = png.read_rgb(data)
            return Raster(w, h, rgb)
        if kind == "bmp":
            return _bmp(data, want)
        if kind == "gif":
            return _gif(data)
        if kind == "pnm":
            return _pnm(data, want)
        if kind == "tga":
            return _tga(data, want)
    except Undecodable:
        raise
    except png.TooLarge as exc:
        raise Undecodable(f"{exc}; install Pillow (Settings → Picture support) to show it", "too-large") from exc
    except jpeg.Unsupported as exc:
        raise Undecodable(f"{exc}; Pillow or ffmpeg would show it", "needs-decoder") from exc
    except (jpeg.JpegError, ValueError, IndexError, KeyError, struct.error, EOFError, OverflowError, MemoryError) as exc:
        raise Undecodable(f"the picture is damaged or in a variant Duplio cannot read ({exc})", "damaged") from exc
    label = {"webp": "WebP", "heif": "HEIC/AVIF", "tiff": "TIFF"}.get(kind, name.rsplit(".", 1)[-1].upper() if "." in name else "this kind of")
    raise Undecodable(f"a {label} picture needs Pillow or ffmpeg to be shown (Settings → Picture support)", "needs-decoder")


def _jpeg(data: bytes, want: int) -> Raster:
    from duplio.preview.exif import _jpeg_size

    size = _jpeg_size(data)
    decoded = jpeg.decode(data, reduce=size is not None and max(size) > want * 2)
    return Raster(decoded.width, decoded.height, decoded.rgb)


# ------------------------------------------------------------------------------------------------------------------ BMP
def _bmp(data: bytes, want: int) -> Raster:
    if len(data) < 26:
        raise ValueError("a BMP file is cut short")
    pixels_at = struct.unpack_from("<I", data, 10)[0]
    dib = struct.unpack_from("<I", data, 14)[0]
    compression = 0
    masks: tuple[int, int, int, int] | None = None
    colors_used = 0
    if dib == 12:  # the old OS/2 header
        width, height, _planes, bpp = struct.unpack_from("<HHHH", data, 18)
        entry = 3
    else:
        width, height, _planes, bpp, compression, _size, _x, _y, colors_used = struct.unpack_from("<iiHHIIiiI", data, 18)
        entry = 4
        if compression in (3, 6):
            off = 54  # the masks follow a 40-byte header, or are the first fields of a longer one
            count = 4 if compression == 6 or dib >= 56 else 3
            found = struct.unpack_from(f"<{count}I", data, off)
            masks = (found[0], found[1], found[2], found[3] if count == 4 else 0)
    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0 or width * height > 400_000_000:
        raise ValueError("a BMP with an impossible size")
    if compression not in (0, 3, 6):
        raise Undecodable("a run-length compressed BMP needs Pillow or ffmpeg to be shown", "needs-decoder")
    if bpp not in (1, 4, 8, 16, 24, 32):
        raise ValueError(f"a {bpp}-bit BMP")
    palette: list[tuple[int, int, int]] = []
    if bpp <= 8:
        count = colors_used or (1 << bpp)
        base = 14 + dib
        for i in range(min(count, 1 << bpp)):
            b, g, r = data[base + i * entry : base + i * entry + 3]
            palette.append((r, g, b))
        palette += [(0, 0, 0)] * ((1 << bpp) - len(palette))
    stride = ((width * bpp + 31) // 32) * 4
    if pixels_at + stride * height > len(data) + stride:
        raise ValueError("a BMP file is cut short")
    step = max(1, min(width, height) // (want * 2))  # sample every step-th pixel in each direction
    xs = list(range(0, width, step))
    out_w, out_h = len(xs), len(range(0, height, step))
    out = bytearray(out_w * out_h * 3)
    if bpp in (16, 32):
        fields = _channel_masks(bpp, masks)
    for oy, y in enumerate(range(0, height, step)):
        source_row = y if top_down else height - 1 - y
        row = data[pixels_at + source_row * stride : pixels_at + (source_row + 1) * stride]
        if len(row) < stride:
            row = row + bytes(stride - len(row))
        at = oy * out_w * 3
        if bpp == 24:
            for x in xs:
                i = x * 3
                out[at], out[at + 1], out[at + 2] = row[i + 2], row[i + 1], row[i]
                at += 3
        elif bpp == 32 or bpp == 16:
            size = bpp // 8
            for x in xs:
                value = int.from_bytes(row[x * size : x * size + size], "little")
                for k, (mask, shift, bits) in enumerate(fields):
                    sample = (value & mask) >> shift
                    out[at + k] = sample if bits == 8 else sample * 255 // ((1 << bits) - 1)
                at += 3
        else:
            per = 8 // bpp
            for x in xs:
                byte = row[x // per]
                shift = 8 - bpp - (x % per) * bpp
                r, g, b = palette[(byte >> shift) & ((1 << bpp) - 1)]
                out[at], out[at + 1], out[at + 2] = r, g, b
                at += 3
    return Raster(out_w, out_h, bytes(out))


def _channel_masks(bpp: int, masks: tuple[int, int, int, int] | None) -> list[tuple[int, int, int]]:
    """``(mask, shift, bits)`` for red, green and blue."""
    if masks is None:
        masks = (0x7C00, 0x03E0, 0x001F, 0) if bpp == 16 else (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
    out = []
    for mask in masks[:3]:
        if not mask:
            out.append((0, 0, 8))
            continue
        shift = (mask & -mask).bit_length() - 1
        out.append((mask, shift, (mask >> shift).bit_length()))
    return out


# ------------------------------------------------------------------------------------------------------------------ GIF
def _gif(data: bytes) -> Raster:
    width, height, flags, _background = struct.unpack_from("<HHBB", data, 6)
    if width * height > MAX_GIF_PIXELS:
        raise Undecodable("this GIF is too big to unpack without Pillow", "too-large")
    pos = 13
    table: list[tuple[int, int, int]] = []
    if flags & 0x80:
        count = 2 << (flags & 7)
        table = [tuple(data[pos + 3 * i : pos + 3 * i + 3]) for i in range(count)]  # type: ignore[misc]
        pos += 3 * count
    transparent = -1
    while pos < len(data):
        block = data[pos]
        pos += 1
        if block == 0x21:  # extension
            label = data[pos]
            pos += 1
            if label == 0xF9 and data[pos] >= 4 and data[pos + 1] & 1:
                transparent = data[pos + 4]
            while pos < len(data):  # skip the sub-blocks
                size = data[pos]
                pos += 1 + size
                if size == 0:
                    break
        elif block == 0x2C:  # the first picture is all a thumbnail needs
            left, top, w, h, local = struct.unpack_from("<HHHHB", data, pos)
            pos += 9
            if local & 0x80:
                count = 2 << (local & 7)
                table = [tuple(data[pos + 3 * i : pos + 3 * i + 3]) for i in range(count)]  # type: ignore[misc]
                pos += 3 * count
            min_code = data[pos]
            pos += 1
            chunks = bytearray()
            while pos < len(data):
                size = data[pos]
                pos += 1
                if size == 0:
                    break
                chunks += data[pos : pos + size]
                pos += size
            indices = _lzw(bytes(chunks), min_code, w * h)
            if local & 0x40:  # interlaced: rows come in four passes
                order = [*range(0, h, 8), *range(4, h, 8), *range(2, h, 4), *range(1, h, 2)]
                rows = [b""] * h
                for n, y in enumerate(order):
                    rows[y] = bytes(indices[n * w : (n + 1) * w])
                indices = bytearray(b"".join(rows))
            if not table:
                table = [(i, i, i) for i in range(256)]
            white = (255, 255, 255)
            canvas = bytearray(white * (width * height))
            for y in range(h):
                if top + y >= height:
                    break
                for x in range(w):
                    if left + x >= width:
                        break
                    index = indices[y * w + x] if y * w + x < len(indices) else 0
                    if index == transparent:
                        continue
                    r, g, b = table[index] if index < len(table) else white
                    at = ((top + y) * width + left + x) * 3
                    canvas[at], canvas[at + 1], canvas[at + 2] = r, g, b
            return Raster(width, height, bytes(canvas))
        elif block == 0x3B:
            break
        else:
            raise ValueError("damaged GIF")
    raise ValueError("a GIF with no picture in it")


def _lzw(data: bytes, min_code: int, expected: int) -> bytearray:
    clear, end = 1 << min_code, (1 << min_code) + 1
    out = bytearray()
    code_size = min_code + 1
    table: list[bytes] = [bytes((i,)) for i in range(clear)] + [b"", b""]
    previous: bytes | None = None
    acc = bits = 0
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= code_size:
            code = acc & ((1 << code_size) - 1)
            acc >>= code_size
            bits -= code_size
            if code == clear:
                code_size = min_code + 1
                table = table[: clear + 2]
                previous = None
                continue
            if code == end:
                return out
            if previous is None:
                entry = table[code] if code < len(table) else b""
            elif code < len(table):
                entry = table[code]
                table.append(previous + entry[:1])
            else:
                entry = previous + previous[:1]
                table.append(entry)
            out += entry
            previous = entry
            if len(table) >= (1 << code_size) and code_size < 12:
                code_size += 1
            if len(out) >= expected:
                return out
    return out


# ------------------------------------------------------------------------------------------------------------------ PNM
def _pnm(data: bytes, want: int) -> Raster:
    kind = data[1] - 48  # P1..P6
    pos = 2

    def token() -> bytes:
        nonlocal pos
        while pos < len(data):
            if data[pos : pos + 1] == b"#":
                while pos < len(data) and data[pos] not in b"\r\n":
                    pos += 1
            elif data[pos] in b" \t\r\n":
                pos += 1
            else:
                break
        start = pos
        while pos < len(data) and data[pos] not in b" \t\r\n":
            pos += 1
        return data[start:pos]

    width, height = int(token()), int(token())
    maxval = 1 if kind in (1, 4) else int(token())
    if width <= 0 or height <= 0 or width * height > 400_000_000 or not 0 < maxval < 65536:
        raise ValueError("a PNM with an impossible size")
    channels = 3 if kind in (3, 6) else 1
    wide = maxval > 255
    if kind in (1, 2, 3):  # plain text
        if kind == 1:
            samples = [c - 48 for c in data[pos:] if c in b"01"]  # the digits may run together
        else:
            samples = [int(t) for t in data[pos:].split() if t.isdigit()]
        body = b""
    else:
        pos += 1  # the single white space after the header
        samples = []
        body = data[pos:]
    stride = (width + 7) // 8 if kind == 4 else width * channels * (2 if wide else 1)

    def sample(x: int, y: int, c: int) -> int:
        """One sample, scaled to 0-255."""
        if kind in (1, 2, 3):
            i = (y * width + x) * channels + c
            value = samples[i] if i < len(samples) else 0
        elif kind == 4:
            i = y * stride + x // 8
            value = (body[i] >> (7 - x % 8)) & 1 if i < len(body) else 0
        elif wide:
            i = y * stride + (x * channels + c) * 2
            return body[i] if i < len(body) else 0  # the high byte
        else:
            i = y * stride + x * channels + c
            value = body[i] if i < len(body) else 0
        if kind in (1, 4):
            return 0 if value else 255  # in PBM a 1 is black
        return min(255, value * 255 // maxval)

    step = max(1, min(width, height) // (want * 2))
    xs, ys = range(0, width, step), range(0, height, step)
    out = bytearray(len(xs) * len(ys) * 3)
    at = 0
    for y in ys:
        for x in xs:
            if channels == 3:
                out[at], out[at + 1], out[at + 2] = sample(x, y, 0), sample(x, y, 1), sample(x, y, 2)
            else:
                out[at] = out[at + 1] = out[at + 2] = sample(x, y, 0)
            at += 3
    return Raster(len(xs), len(ys), bytes(out))


# ------------------------------------------------------------------------------------------------------------------ TGA
def _tga(data: bytes, want: int) -> Raster:
    id_len, map_type, image_type = data[0], data[1], data[2]
    map_start, map_len, map_bits = struct.unpack_from("<HHB", data, 3)
    width, height, bpp, descriptor = struct.unpack_from("<HHBB", data, 12)
    if image_type not in (1, 2, 3, 9, 10, 11) or width <= 0 or height <= 0 or width * height > 400_000_000:
        raise Undecodable("this kind of TGA picture needs Pillow or ffmpeg to be shown", "needs-decoder")
    pos = 18 + id_len
    palette: list[tuple[int, int, int]] = []
    if map_type:
        size = (map_bits + 7) // 8
        for i in range(map_len):
            entry = data[pos + i * size : pos + (i + 1) * size]
            if size >= 3:
                palette.append((entry[2], entry[1], entry[0]))
            else:
                value = int.from_bytes(entry, "little")
                palette.append((((value >> 10) & 31) * 255 // 31, ((value >> 5) & 31) * 255 // 31, (value & 31) * 255 // 31))
        pos += map_len * size
    per = (bpp + 7) // 8
    total = width * height
    if image_type >= 9:  # run-length encoded
        pixels = bytearray()
        while len(pixels) < total * per and pos < len(data):
            header = data[pos]
            pos += 1
            count = (header & 127) + 1
            if header & 128:
                pixels += data[pos : pos + per] * count
                pos += per
            else:
                pixels += data[pos : pos + per * count]
                pos += per * count
        raw = bytes(pixels)
    else:
        raw = data[pos : pos + total * per]
    if len(raw) < total * per:
        raise ValueError("a TGA file is cut short")
    step = max(1, min(width, height) // (want * 2))
    xs = list(range(0, width, step))
    ys = list(range(0, height, step))
    out = bytearray(len(xs) * len(ys) * 3)
    at = 0
    top_origin = bool(descriptor & 0x20)
    right_origin = bool(descriptor & 0x10)
    for y in ys:
        row = y if top_origin else height - 1 - y
        for x in xs:
            col = width - 1 - x if right_origin else x
            i = (row * width + col) * per
            px = raw[i : i + per]
            if image_type in (1, 9):
                r, g, b = palette[px[0] - map_start] if 0 <= px[0] - map_start < len(palette) else (0, 0, 0)
            elif image_type in (3, 11):
                r = g = b = px[0]
            elif per >= 3:
                r, g, b = px[2], px[1], px[0]
            else:
                value = int.from_bytes(px, "little")
                r, g, b = ((value >> 10) & 31) * 255 // 31, ((value >> 5) & 31) * 255 // 31, (value & 31) * 255 // 31
            out[at], out[at + 1], out[at + 2] = r, g, b
            at += 3
    return Raster(len(xs), len(ys), bytes(out))
