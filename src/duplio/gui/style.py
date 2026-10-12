"""Colours, fonts and the look of the ttk widgets.

Folder A is blue and folder B is red throughout, the colours of the two figures in Duplio's icon, so a glance at the border
of a thumbnail says which folder it is in.
"""

from __future__ import annotations

import sys
import tkinter
from tkinter import font as tkfont
from tkinter import ttk

from duplio.gui.palette import (  # noqa: F401  (re-exported: the colours live in a module that needs no Tk)
    PALETTE,
    SIDE_COLOR,
    SIDE_SOFT,
)


class Fonts:
    """Named fonts made from the system's own, so text looks native on Windows, macOS and Linux."""

    def __init__(self, master: tkinter.Misc) -> None:
        base = tkfont.nametofont("TkDefaultFont")
        family = base.actual("family")
        size = abs(int(base.actual("size"))) or 10
        if sys.platform == "win32":
            size = max(size, 10)
        elif sys.platform == "darwin":
            size = max(size, 12)
        else:
            size = max(size, 10)
        self.base = tkfont.Font(master, family=family, size=size)
        self.small = tkfont.Font(master, family=family, size=max(size - 1, 8))
        self.tiny = tkfont.Font(master, family=family, size=max(size - 2, 7))
        self.bold = tkfont.Font(master, family=family, size=size, weight="bold")
        self.heading = tkfont.Font(master, family=family, size=size + 4, weight="bold")
        self.title = tkfont.Font(master, family=family, size=size + 9, weight="bold")
        self.big = tkfont.Font(master, family=family, size=size + 3, weight="bold")
        self.mono = tkfont.Font(master, family=tkfont.nametofont("TkFixedFont").actual("family"), size=size - 1)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=size)


def enable_hidpi() -> None:
    """Ask Windows not to blur the window on high-resolution screens (must run before the window exists)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass


def apply_style(root: tkinter.Tk) -> Fonts:
    fonts = Fonts(root)
    style = ttk.Style(root)
    if sys.platform != "darwin" and "clam" in style.theme_names():
        style.theme_use("clam")  # the one theme that can be coloured on Windows and Linux
    p = PALETTE
    root.configure(background=p["bg"])
    style.configure(".", background=p["bg"], foreground=p["text"], font=fonts.base)
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Panel.TLabel", background=p["panel"])
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("TEntry", fieldbackground="#ffffff", bordercolor=p["line"], lightcolor=p["line"], darkcolor=p["line"], padding=6)
    style.configure("TCombobox", fieldbackground="#ffffff", bordercolor=p["line"], lightcolor=p["line"], darkcolor=p["line"], padding=6)
    style.configure("TCheckbutton", background=p["bg"])
    style.configure("TRadiobutton", background=p["bg"])
    for kind in ("TCheckbutton", "TRadiobutton"):
        style.map(kind, background=[("active", p["bg"])], indicatorcolor=[("selected", p["a"]), ("!selected", "#ffffff")])
    style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], background=[("active", "#e4e8f1"), ("readonly", "#eef0f6")],
              selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", p["text"])])
    style.configure("Panel.TCheckbutton", background=p["panel"])
    style.configure("Panel.TRadiobutton", background=p["panel"])
    style.configure("Horizontal.TProgressbar", troughcolor="#dfe3ec", background=p["a"], bordercolor=p["line"],
                    lightcolor=p["a"], darkcolor=p["a"])
    style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=int(fonts.base.metrics("linespace") * 1.9),
                    bordercolor=p["line"])
    style.configure("Treeview.Heading", font=fonts.bold, background="#eef0f5", relief="flat")
    style.map("Treeview", background=[("selected", p["a_soft"])], foreground=[("selected", p["text"])])
    style.configure("TScrollbar", troughcolor=p["bg"], bordercolor=p["bg"], arrowcolor=p["muted"])
    style.configure("TNotebook", background=p["bg"], bordercolor=p["line"])
    style.configure("TNotebook.Tab", padding=(14, 6), font=fonts.base)
    return fonts


def shorten(text: str, width_px: int, font: tkfont.Font, *, middle: bool = False) -> str:
    """Cut ``text`` with an ellipsis so it is no wider than ``width_px`` in ``font``."""
    if width_px <= 8:
        return ""
    if font.measure(text) <= width_px:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    if middle:
        # keep the start and the end (the end of a path or a file name is what tells files apart)
        while low < high:
            mid = (low + high + 1) // 2
            head, tail = (mid + 1) // 2, mid // 2
            candidate = text[:head] + ellipsis + (text[-tail:] if tail else "")
            if font.measure(candidate) <= width_px:
                low = mid
            else:
                high = mid - 1
        head, tail = (low + 1) // 2, low // 2
        return text[:head] + ellipsis + (text[-tail:] if tail else "")
    while low < high:
        mid = (low + high + 1) // 2
        if font.measure(text[:mid] + ellipsis) <= width_px:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis
