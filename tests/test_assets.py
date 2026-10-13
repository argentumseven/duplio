from __future__ import annotations

from pathlib import Path

import pytest

from duplio.assets.logo import composite, logo_text, pixels
from duplio.assets.logo_data import ROWS

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def test_terminal_logo_is_well_formed() -> None:
    assert len(ROWS) % 2 == 0  # two pixel rows per text row
    assert len({len(row) for row in ROWS}) == 1 and len(ROWS[0]) % 8 == 0
    grid = pixels()
    assert all(len(row) == len(grid[0]) for row in grid)
    text = logo_text()
    assert "▀" in text.plain and "D U P L I O" in text.plain
    assert text.plain.count("\n") == len(ROWS) // 2 + 1
    assert logo_text(wordmark=False).plain.count("\n") == len(ROWS) // 2 - 1


def test_the_mark_keeps_its_transparency_so_it_suits_a_dark_background() -> None:
    grid = pixels()
    opacities = [p[3] for row in grid for p in row]
    assert min(opacities) == 0 and max(opacities) >= 240  # empty corners, solid bodies
    assert sum(1 for a in opacities if 0 < a < 255) > 50  # and a soft edge in between, not a hard cut-out
    assert grid[0][0][3] == 0 and grid[-1][-1][3] == 0
    on_dark = composite("#0e0d10")
    assert on_dark[0][0] is None  # nothing is drawn where the icon is empty
    drawn = [p for row in on_dark for p in row if p is not None]
    assert not any(min(p) > 235 for p in drawn)  # no near-white fringe left from a white background
    # an edge pixel takes the background it is laid on
    edge = next((r, c) for r, row in enumerate(grid) for c, p in enumerate(row) if 40 < p[3] < 120)
    dark_px, light_px = on_dark[edge[0]][edge[1]], composite("#f3f4f7")[edge[0]][edge[1]]
    assert dark_px is not None and light_px is not None and sum(dark_px) < sum(light_px)


def test_icon_files_are_the_supplied_artwork_in_every_size() -> None:
    pil = pytest.importorskip("PIL.Image")
    if not ASSETS.exists():
        pytest.skip("icon assets are not part of an installed package")
    original = pil.open(ASSETS / "duplio-icon-original.png")
    assert original.size == (1254, 1254) and original.mode == "RGBA"
    assert original.getpixel((2, 2))[3] == 0  # transparent background
    for size in (512, 256, 128, 64, 32, 16):
        icon = pil.open(ASSETS / f"duplio-icon-{size}.png")
        assert icon.size == (size, size) and icon.mode == "RGBA"
    ico = pil.open(ASSETS / "duplio.ico")
    assert {(16, 16), (32, 32), (256, 256)} <= set(ico.info["sizes"])
