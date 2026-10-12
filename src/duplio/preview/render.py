"""Turn a stored thumbnail into something a terminal can show.

A terminal cell is about twice as tall as it is wide, so each cell shows two pixels, one above the other: the upper half
block character ``▀`` coloured with the top pixel and with the bottom pixel as its background. Without colour (a plain
terminal, ``--no-color``) a brightness ramp of ASCII characters is used instead.

Decoding uses Pillow when it is installed and otherwise the small decoders in this package (baseline JPEG, plain PNG).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from rich.color import Color
from rich.style import Style
from rich.text import Text

from duplio.preview import jpeg, png


class CannotDecode(Exception):
    """The stored picture is in a format that cannot be decoded here."""


@dataclass
class Raster:
    width: int
    height: int
    rgb: bytes

    def oriented(self, orientation: int) -> Raster:
        """Apply an EXIF orientation (1-8) so the picture is shown upright."""
        if orientation not in range(2, 9):
            return self
        w, h, px = self.width, self.height, self.rgb
        rows = [[px[(y * w + x) * 3 : (y * w + x) * 3 + 3] for x in range(w)] for y in range(h)]

        def transpose(r: list[list[bytes]]) -> list[list[bytes]]:
            return [list(c) for c in zip(*r, strict=True)]

        if orientation == 2:  # mirrored left-right
            rows = [r[::-1] for r in rows]
        elif orientation == 3:  # upside down
            rows = [r[::-1] for r in rows[::-1]]
        elif orientation == 4:  # mirrored top-bottom
            rows = rows[::-1]
        elif orientation == 5:  # transposed
            rows = transpose(rows)
        elif orientation == 6:  # needs a quarter turn clockwise
            rows = [r[::-1] for r in transpose(rows)]
        elif orientation == 7:  # transposed and turned half way round
            rows = [r[::-1] for r in transpose(rows)[::-1]]
        else:  # 8: needs a quarter turn counter-clockwise
            rows = transpose(rows)[::-1]
        height = len(rows)
        width = len(rows[0]) if rows else 0
        return Raster(width, height, b"".join(b"".join(r) for r in rows))

    def fit(self, max_width: int, max_height: int) -> Raster:
        """Shrink (never enlarge) to fit the box, averaging the pixels that fall into each new pixel."""
        scale = min(max_width / self.width, max_height / self.height, 1.0)
        out_w, out_h = max(1, round(self.width * scale)), max(1, round(self.height * scale))
        if (out_w, out_h) == (self.width, self.height):
            return self
        px, w = self.rgb, self.width
        out = bytearray(out_w * out_h * 3)
        for oy in range(out_h):
            y0, y1 = oy * self.height // out_h, max(oy * self.height // out_h + 1, (oy + 1) * self.height // out_h)
            for ox in range(out_w):
                x0, x1 = ox * w // out_w, max(ox * w // out_w + 1, (ox + 1) * w // out_w)
                r = g = b = n = 0
                for y in range(y0, y1):
                    base = y * w * 3
                    for x in range(x0, x1):
                        i = base + x * 3
                        r += px[i]
                        g += px[i + 1]
                        b += px[i + 2]
                        n += 1
                j = (oy * out_w + ox) * 3
                out[j], out[j + 1], out[j + 2] = r // n, g // n, b // n
        return Raster(out_w, out_h, bytes(out))


def decode_raster(data: bytes, *, max_side: int = 512) -> Raster:
    """Decode a stored thumbnail (JPEG or PNG; anything Pillow reads when it is installed)."""
    try:
        from PIL import Image
    except ImportError:
        pass
    else:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.draft("RGB", (max_side, max_side))
                rgb = img.convert("RGB")
                return Raster(rgb.width, rgb.height, rgb.tobytes())
        except Exception as exc:
            raise CannotDecode(str(exc)) from exc
    try:
        if data[:8] == png.SIGNATURE:
            w, h, px = png.read_rgb(data)
            return Raster(w, h, px)
        decoded = jpeg.decode(data, reduce=_too_big(data, max_side))
        return Raster(decoded.width, decoded.height, decoded.rgb)
    except (jpeg.JpegError, ValueError, IndexError, KeyError, OSError) as exc:
        raise CannotDecode(str(exc)) from exc


def _too_big(data: bytes, max_side: int) -> bool:
    from duplio.preview.exif import _jpeg_size

    size = _jpeg_size(data)
    return size is not None and max(size) > 3 * max_side


def to_text(raster: Raster, *, color: bool = True) -> Text:
    """The raster as a block of terminal text (two pixel rows per text row)."""
    if not color:
        return _ascii(raster)
    text = Text(no_wrap=True, overflow="crop")
    px, w, h = raster.rgb, raster.width, raster.height
    for y in range(0, h, 2):
        for x in range(w):
            top = y * w * 3 + x * 3
            bottom = (y + 1) * w * 3 + x * 3 if y + 1 < h else top
            style = Style(
                color=Color.from_rgb(px[top], px[top + 1], px[top + 2]),
                bgcolor=Color.from_rgb(px[bottom], px[bottom + 1], px[bottom + 2]),
            )
            text.append("▀", style)
        if y + 2 < h:
            text.append("\n")
    return text


_RAMP = " .:-=+*#%@"


def _ascii(raster: Raster) -> Text:
    px, w, h = raster.rgb, raster.width, raster.height
    lines = []
    for y in range(0, h, 2):
        line = []
        for x in range(w):
            i = y * w * 3 + x * 3
            level = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) // 1000
            line.append(_RAMP[(255 - level) * (len(_RAMP) - 1) // 255])
        lines.append("".join(line))
    return Text("\n".join(lines), no_wrap=True, overflow="crop")


def render(data: bytes, *, orientation: int = 1, cols: int = 40, rows: int = 14, color: bool = True) -> Text:
    """Decode ``data`` and fit it into ``cols`` x ``rows`` text cells."""
    raster = decode_raster(data).oriented(orientation).fit(cols, rows * 2)
    return to_text(raster, color=color)
