from __future__ import annotations

from pathlib import Path

import pytest

from duplio.assets.logo import GRID, PALETTE, logo_text

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def test_terminal_logo_is_well_formed() -> None:
    assert len(GRID) % 2 == 0  # two pixel rows per text row
    assert len({len(row) for row in GRID}) == 1
    digits = "0123456789abcdefghijklmnopqrstuv"
    used = {ch for row in GRID for ch in row if ch != " "}
    assert used <= set(digits[: len(PALETTE)])
    text = logo_text()
    assert "▀" in text.plain and "D U P L I O" in text.plain
    assert text.plain.count("\n") == len(GRID) // 2 + 1
    assert logo_text(wordmark=False).plain.count("\n") == len(GRID) // 2 - 1


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
