"""The desktop window: two folders side by side, with real thumbnails."""

from __future__ import annotations

import os
import sys


def tk_problem() -> str | None:
    """Why the window cannot open on this computer, or ``None`` when it can."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        if sys.platform.startswith("linux"):
            return "The window needs Tk, which this Python does not have. Install it (for example: sudo apt install python3-tk)."
        return "The window needs Tk, which this Python does not have. Reinstall Python from python.org and keep 'tcl/tk' ticked."
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "There is no screen to open a window on (no DISPLAY)."
    return None
