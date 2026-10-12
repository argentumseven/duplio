"""Helpers for driving the window in tests and for taking screenshots: demo folders, and waiting while the window works."""

from __future__ import annotations

import shutil
import time
import tkinter as tk
from pathlib import Path
from typing import Any

from .previewkit import photo_with_exif_thumbnail

OLD = 1_600_000_000


def make_demo_folders(root: Path) -> tuple[Path, Path]:
    """Folder A (a "phone") and folder B (a "backup") with pictures that differ in the ways Duplio cares about."""
    import os

    a, b = root / "Phone" / "DCIM", root / "Backup" / "Photos"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    def photo(folder: Path, name: str, seed: int, size=(640, 480), orientation=1):  # type: ignore[no-untyped-def]
        photo_with_exif_thumbnail(folder / name, size=size, thumb_size=(size[0] // 4, size[1] // 4), seed=seed, orientation=orientation,
                                  padding=2000 + seed * 100)
        os.utime(folder / name, (OLD + seed, OLD + seed))

    for n in range(1, 5):  # in both, same name
        photo(a, f"IMG_{n:04d}.jpg", n)
        shutil.copy2(a / f"IMG_{n:04d}.jpg", b / f"IMG_{n:04d}.jpg")
    photo(a, "IMG_0005.jpg", 5)  # renamed in the backup
    (b / "2024").mkdir()
    shutil.copy2(a / "IMG_0005.jpg", b / "2024" / "Beach holiday.jpg")
    photo(a, "IMG_0006.jpg", 6, size=(480, 640), orientation=6)  # only on the phone: not backed up
    photo(a, "IMG_0007.jpg", 7)
    photo(a, "IMG_0008.jpg", 8)
    photo(b, "Scan_old.jpg", 9)  # only in the backup
    shutil.copy2(a / "IMG_0002.jpg", a / "IMG_0002 copy.jpg")  # a copy inside the phone folder
    photo(a, "IMG_0010.jpg", 10)  # same name, different picture
    photo(b, "IMG_0010.jpg", 11)
    (a / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 3000)  # a video: no picture, but a place in the list
    (a / "notes.txt").write_text("Remember to back this up.\n" * 20)
    return a, b


def pump(window: tk.Misc, until: Any = None, timeout: float = 30.0, step: float = 0.02) -> bool:
    """Let the window work (draw, run its timers) until ``until()`` is true or the time is up."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        window.update()
        if until is not None and until():
            return True
        time.sleep(step)
    window.update()
    return until is None or bool(until())
