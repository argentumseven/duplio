"""A small baseline-JPEG decoder in plain Python, for when Pillow is not installed.

It exists so that the thumbnails cameras embed in photos can be shown with nothing but the standard library. It reads
baseline and extended-sequential JPEGs (8-bit, Huffman coded: what cameras and phones write for thumbnails), grayscale or
YCbCr in any sampling, with restart markers. Progressive, arithmetic-coded, 12-bit and CMYK files raise
:class:`Unsupported`; the caller then falls back to "no preview" (or to Pillow when it is available).

For big pictures a full inverse DCT would take seconds, so ``reduce=True`` decodes only each block's DC term, giving an
image at 1/8 scale at a fraction of the cost: enough for a terminal-sized preview.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21,
    28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54,
    47, 55, 62, 63,
)  # fmt: skip


class JpegError(Exception):
    """The data is not a JPEG this decoder can read."""


class Unsupported(JpegError):
    """A valid JPEG of a kind this decoder does not handle (progressive, arithmetic, 12-bit, CMYK)."""


@dataclass
class Decoded:
    width: int
    height: int
    rgb: bytes  # width * height * 3
    reduced: bool  # True when decoded at 1/8 scale


@dataclass
class _Component:
    ident: int
    h: int
    v: int
    tq: int
    td: int = 0
    ta: int = 0
    blocks_w: int = 0
    blocks_h: int = 0
    plane: list[list[int]] | None = None  # rows of samples


def _build_huffman(counts: list[int], symbols: list[int]) -> dict[int, int]:
    """Map ``(length << 16) | code`` to symbol, using the canonical code assignment."""
    table: dict[int, int] = {}
    code = 0
    index = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if index >= len(symbols):
                raise JpegError("damaged Huffman table")
            table[(length << 16) | code] = symbols[index]
            index += 1
            code += 1
        code <<= 1
    return table


_COS = [[(math.sqrt(0.125) if u == 0 else 0.5) * math.cos((2 * x + 1) * u * math.pi / 16) for x in range(8)]
        for u in range(8)]  # _COS[u][x]


def _idct(block: list[int]) -> list[int]:
    """Inverse DCT of 64 dequantised coefficients (natural order) to 64 samples, level shifted and clamped."""
    if not any(block[1:]):
        level = block[0] * 0.125 + 128
        return [0 if level < 0 else 255 if level > 255 else int(level + 0.5)] * 64
    # rows then columns; skip all-zero input rows
    tmp = [[0.0] * 8 for _ in range(8)]
    for v in range(8):
        row = block[v * 8 : v * 8 + 8]
        if not any(row):
            continue
        for x in range(8):
            total = 0.0
            for u in range(8):
                c = row[u]
                if c:
                    total += c * _COS[u][x]
            tmp[v][x] = total
    out = [0] * 64
    for x in range(8):
        column = [tmp[v][x] for v in range(8)]
        for y in range(8):
            total = 0.0
            for v in range(8):
                cv = column[v]
                if cv:
                    total += cv * _COS[v][y]
            value = int(total + 128.5) if total > -128.5 else 0
            out[y * 8 + x] = 255 if value > 255 else value
    return out


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.acc = 0
        self.nbits = 0

    def bit(self) -> int:
        if self.nbits == 0:
            if self.pos >= len(self.data):
                self.acc, self.nbits = 0, 8  # past the end: zeros (the stream may end a few bits early)
            else:
                self.acc, self.nbits = self.data[self.pos], 8
                self.pos += 1
        self.nbits -= 1
        return (self.acc >> self.nbits) & 1

    def receive(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def symbol(self, table: dict[int, int]) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bit()
            found = table.get((length << 16) | code)
            if found is not None:
                return found
        raise JpegError("bad Huffman code")


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _extend(value: int, size: int) -> int:
    return value - (1 << size) + 1 if size and value < (1 << (size - 1)) else value


def _split_scan(data: bytes, pos: int) -> tuple[list[bytes], int]:
    """The entropy-coded bytes after a scan header, unstuffed and split at restart markers; and where they end."""
    intervals: list[bytes] = []
    current = bytearray()
    i = pos
    while True:
        j = data.find(b"\xff", i)
        if j == -1:
            current += data[i:]
            i = len(data)
            break
        current += data[i:j]
        nxt = data[j + 1] if j + 1 < len(data) else 0xD9
        if nxt == 0:
            current.append(0xFF)
            i = j + 2
        elif 0xD0 <= nxt <= 0xD7:
            intervals.append(bytes(current))
            current = bytearray()
            i = j + 2
        elif nxt == 0xFF:
            i = j + 1
        else:
            i = j
            break
    intervals.append(bytes(current))
    return intervals, i


def decode(data: bytes, *, reduce: bool = False) -> Decoded:
    """Decode a JPEG. ``reduce`` decodes at 1/8 scale (DC terms only)."""
    if data[:2] != b"\xff\xd8":
        raise JpegError("not a JPEG")
    quant: dict[int, list[int]] = {}
    huff: dict[tuple[int, int], dict[int, int]] = {}
    components: list[_Component] = []
    width = height = 0
    restart = 0
    adobe_transform: int | None = None
    pos = 2
    scans = 0
    hmax = vmax = 1
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise JpegError("damaged JPEG")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        seg = data[pos + 4 : pos + 2 + length]
        if marker == 0xDB:
            i = 0
            while i < len(seg):
                precision, ident = seg[i] >> 4, seg[i] & 15
                i += 1
                if precision == 0:
                    table = list(seg[i : i + 64])
                    i += 64
                else:
                    table = list(struct.unpack(">64H", seg[i : i + 128]))
                    i += 128
                quant[ident] = table
        elif marker == 0xC4:
            i = 0
            while i + 17 <= len(seg):
                kind, ident = seg[i] >> 4, seg[i] & 15
                counts = list(seg[i + 1 : i + 17])
                total = sum(counts)
                symbols = list(seg[i + 17 : i + 17 + total])
                huff[(kind, ident)] = _build_huffman(counts, symbols)
                i += 17 + total
        elif marker in (0xC0, 0xC1):
            precision, height, width, count = struct.unpack(">BHHB", seg[:6])
            if precision != 8:
                raise Unsupported("only 8-bit JPEGs are supported")
            if count not in (1, 3):
                raise Unsupported("only grayscale and YCbCr JPEGs are supported")
            components = []
            for k in range(count):
                ident, sampling, tq = struct.unpack(">BBB", seg[6 + 3 * k : 9 + 3 * k])
                components.append(_Component(ident, sampling >> 4, sampling & 15, tq))
        elif marker in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise Unsupported("progressive, lossless and arithmetic-coded JPEGs are not supported")
        elif marker == 0xDD:
            restart = struct.unpack(">H", seg[:2])[0]
        elif marker == 0xEE and seg[:5] == b"Adobe" and len(seg) >= 12:
            adobe_transform = seg[11]
        elif marker == 0xDA:
            if not components or not width or not height:
                raise JpegError("scan before frame header")
            if scans == 0:
                hmax = max(c.h for c in components)
                vmax = max(c.v for c in components)
                mcu_w = -(-width // (8 * hmax))
                mcu_h = -(-height // (8 * vmax))
                for c in components:
                    cols, rows = mcu_w * c.h, mcu_h * c.v
                    c.blocks_w, c.blocks_h = cols, rows
                    size = 1 if reduce else 8
                    c.plane = [[128] * (cols * size) for _ in range(rows * size)]
            count = seg[0]
            in_scan: list[_Component] = []
            for k in range(count):
                ident, tables = seg[1 + 2 * k], seg[2 + 2 * k]
                comp = next((c for c in components if c.ident == ident), None)
                if comp is None:
                    raise JpegError("scan names an unknown component")
                comp.td, comp.ta = tables >> 4, tables & 15
                in_scan.append(comp)
            intervals, end = _split_scan(data, pos + 2 + length)
            _decode_scan(intervals, in_scan, components, quant, huff, restart, reduce, hmax, vmax, width, height)
            scans += 1
            pos = end
            continue
        pos += 2 + length
    if not scans:
        raise JpegError("no image data")
    return _assemble(components, width, height, hmax, vmax, reduce, adobe_transform)


def _decode_scan(intervals, in_scan, components, quant, huff, restart, reduce, hmax, vmax, width, height):  # type: ignore[no-untyped-def]
    interleaved = len(in_scan) > 1
    mcu_w = -(-width // (8 * hmax))
    mcu_h = -(-height // (8 * vmax))
    if interleaved:
        total_mcus = mcu_w * mcu_h
    else:
        comp = in_scan[0]
        # a non-interleaved scan covers only the blocks that hold picture, not the padding to a whole MCU
        bw = _ceil_div(_ceil_div(width * comp.h, hmax), 8)
        bh = _ceil_div(_ceil_div(height * comp.v, vmax), 8)
        total_mcus = bw * bh
    per_interval = restart or total_mcus
    mcu = 0
    for data in intervals:
        if mcu >= total_mcus:
            break
        bits = _Bits(data)
        preds = {id(c): 0 for c in in_scan}
        for _ in range(per_interval):
            if mcu >= total_mcus:
                break
            if interleaved:
                mx, my = mcu % mcu_w, mcu // mcu_w
                for comp in in_scan:
                    for by in range(comp.v):
                        for bx in range(comp.h):
                            _decode_block(bits, comp, preds, quant, huff, reduce, mx * comp.h + bx, my * comp.v + by)
            else:
                comp = in_scan[0]
                bw = _ceil_div(_ceil_div(width * comp.h, hmax), 8)
                _decode_block(bits, comp, preds, quant, huff, reduce, mcu % bw, mcu // bw)
            mcu += 1


def _decode_block(bits, comp, preds, quant, huff, reduce, bx, by):  # type: ignore[no-untyped-def]
    dc_table, ac_table = huff.get((0, comp.td)), huff.get((1, comp.ta))
    q = quant.get(comp.tq)
    if dc_table is None or ac_table is None or q is None:
        raise JpegError("missing table")
    size = bits.symbol(dc_table)
    diff = _extend(bits.receive(size), size) if size else 0
    key = id(comp)
    preds[key] += diff
    coefs = [0] * 64
    coefs[0] = preds[key] * q[0]
    k = 1
    while k < 64:
        rs = bits.symbol(ac_table)
        run, size = rs >> 4, rs & 15
        if size == 0:
            if run == 15:
                k += 16
                continue
            break
        k += run
        if k > 63:
            break
        value = _extend(bits.receive(size), size)
        if not reduce:
            coefs[ZIGZAG[k]] = value * q[k]
        k += 1
    plane = comp.plane
    if reduce:
        level = coefs[0] * 0.125 + 128
        plane[by][bx] = 0 if level < 0 else 255 if level > 255 else int(level + 0.5)
        return
    samples = _idct(coefs)
    top, left = by * 8, bx * 8
    for row in range(8):
        plane[top + row][left : left + 8] = samples[row * 8 : row * 8 + 8]


def _full_plane(comp, hmax, vmax, out_w, out_h, fancy=True):  # type: ignore[no-untyped-def]
    """The component's samples at the picture's full resolution, upsampled the way libjpeg does it ("fancy": a triangle
    filter for the common 2:1 cases, which is what keeps subsampled colour from looking blocky)."""
    plane = comp.plane
    fx, fy = hmax // comp.h, vmax // comp.v
    cw, ch = -(-out_w * comp.h // hmax), -(-out_h * comp.v // vmax)
    if fx == 1 and fy == 1:
        return [row[:out_w] for row in plane[:out_h]]
    if not fancy or fx * comp.h != hmax or fy * comp.v != vmax or fx > 2 or fy > 2:
        return [[plane[y * comp.v // vmax][x * comp.h // hmax] for x in range(out_w)] for y in range(out_h)]
    rows_in = [plane[y][:cw] for y in range(ch)]

    def horizontal_h2v1(row):  # type: ignore[no-untyped-def]
        n = len(row)
        out = [0] * (2 * n)
        for i in range(n):
            cur = row[i]
            prev = row[i - 1] if i > 0 else cur
            nxt = row[i + 1] if i < n - 1 else cur
            out[2 * i] = (3 * cur + prev + 1) >> 2
            out[2 * i + 1] = (3 * cur + nxt + 2) >> 2
        return out

    if fy == 1:  # h2v1
        full = [horizontal_h2v1(r)[:out_w] for r in rows_in[:out_h]]
        return full
    out_rows: list[list[int]] = []
    for y in range(out_h):
        j = y >> 1
        near = rows_in[min(j, ch - 1)]
        far_index = j - 1 if y % 2 == 0 else j + 1
        far = rows_in[min(max(far_index, 0), ch - 1)]
        if fx == 1:  # h1v2: vertical only
            bias = 1 if y % 2 == 0 else 2
            out_rows.append([(3 * a + b + bias) >> 2 for a, b in zip(near, far, strict=True)][:out_w])
            continue
        col = [3 * a + b for a, b in zip(near, far, strict=True)]  # h2v2: vertical, then horizontal
        n = len(col)
        out = [0] * (2 * n)
        for i in range(n):
            cur = col[i]
            prev = col[i - 1] if i > 0 else cur
            nxt = col[i + 1] if i < n - 1 else cur
            out[2 * i] = (3 * cur + prev + 8) >> 4
            out[2 * i + 1] = (3 * cur + nxt + 7) >> 4
        out_rows.append(out[:out_w])
    return out_rows


def _assemble(components, width, height, hmax, vmax, reduce, adobe):  # type: ignore[no-untyped-def]
    scale = 8 if reduce else 1
    out_w, out_h = -(-width // scale), -(-height // scale)
    rgb = bytearray(out_w * out_h * 3)
    if len(components) == 1:
        plane = components[0].plane
        for y in range(out_h):
            row = plane[y]
            for x in range(out_w):
                v = row[x]
                i = (y * out_w + x) * 3
                rgb[i] = rgb[i + 1] = rgb[i + 2] = v
        return Decoded(out_w, out_h, bytes(rgb), reduce)
    ycc = adobe != 0
    # at 1/8 scale each colour sample stands for a whole block: repeat it (smoothing would blur block edges)
    y_p, cb_p, cr_p = (_full_plane(c, hmax, vmax, out_w, out_h, fancy=not reduce) for c in components)
    for y in range(out_h):
        yr, br, rr = y_p[y], cb_p[y], cr_p[y]
        for x in range(out_w):
            luma, cb, cr = yr[x], br[x], rr[x]
            i = (y * out_w + x) * 3
            if ycc:
                r = luma + 1.402 * (cr - 128)
                g = luma - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
                b = luma + 1.772 * (cb - 128)
            else:
                r, g, b = luma, cb, cr
            rgb[i] = 0 if r < 0 else 255 if r > 255 else int(r + 0.5)
            rgb[i + 1] = 0 if g < 0 else 255 if g > 255 else int(g + 0.5)
            rgb[i + 2] = 0 if b < 0 else 255 if b > 255 else int(b + 0.5)
    return Decoded(out_w, out_h, bytes(rgb), reduce)
