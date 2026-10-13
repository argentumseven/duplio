"""The Duplio mark for the terminal and the window: the icon (``assets/duplio-icon-original.png``) sampled to pixels.

The pixels (``logo_data.ROWS``) keep their own opacity, so the mark is laid on whatever background it is shown on: the
glow of the two figures fades into a dark background instead of leaving a pale fringe. ``composite`` does that blending;
``logo_text`` draws it in the terminal with half-block characters (two pixel rows per text row).
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from duplio.assets.logo_data import ROWS

WORDMARK = "D U P L I O"
TAGLINE = "Duplicates in. One survivor out."
DARK = "#0e0d10"  # the background the terminal and the dark window use

Pixel = tuple[int, int, int, int]  # red, green, blue, opacity (the colour is the light itself, not blended with anything)


def pixels() -> list[list[Pixel]]:
    """The mark as rows of (red, green, blue, opacity)."""
    out: list[list[Pixel]] = []
    for row in ROWS:
        out.append([(int(row[i : i + 2], 16), int(row[i + 2 : i + 4], 16), int(row[i + 4 : i + 6], 16), int(row[i + 6 : i + 8], 16))
                    for i in range(0, len(row), 8)])
    return out


def rgb_of(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def composite(background: str, *, min_opacity: int = 8) -> list[list[tuple[int, int, int] | None]]:
    """The mark laid on ``background`` (``#rrggbb``); ``None`` where nothing is drawn (opacity below ``min_opacity``)."""
    br, bg, bb = rgb_of(background)
    out: list[list[tuple[int, int, int] | None]] = []
    for row in pixels():
        line: list[tuple[int, int, int] | None] = []
        for r, g, b, a in row:
            if a < min_opacity:
                line.append(None)
            else:
                line.append(((r * a + br * (255 - a)) // 255, (g * a + bg * (255 - a)) // 255, (b * a + bb * (255 - a)) // 255))
        out.append(line)
    return out


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def logo_text(*, wordmark: bool = True, background: str = DARK) -> Text:
    """The mark as Rich text. Faint edge pixels (they would show as dim squares on a terminal that is not exactly
    ``background``) are left out; the rest is blended onto ``background``."""
    grid = composite(background, min_opacity=64)
    out = Text(no_wrap=True)
    for top_row, bottom_row in zip(grid[0::2], grid[1::2], strict=True):
        for top, bottom in zip(top_row, bottom_row, strict=True):
            if top is None and bottom is None:
                out.append(" ")
            elif top is not None and bottom is not None:
                out.append("▀", style=Style(color=_hex(top), bgcolor=_hex(bottom)))
            elif top is not None:
                out.append("▀", style=Style(color=_hex(top)))
            else:
                assert bottom is not None
                out.append("▄", style=Style(color=_hex(bottom)))
        out.append("\n")
    if wordmark:
        out.append(f"{WORDMARK}\n", style="bold")
        out.append(TAGLINE, style="italic dim")
    else:
        out.rstrip()
    return out
