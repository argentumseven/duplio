"""Fonts and the look of the ttk widgets. The colours themselves are in ``palette`` (dark by default, light as an option)."""

from __future__ import annotations

import contextlib
import sys
import tkinter
from tkinter import font as tkfont
from tkinter import ttk

from duplio.gui.palette import PALETTE, SIDE_COLOR, SIDE_SOFT

__all__ = ["PALETTE", "SIDE_COLOR", "SIDE_SOFT", "Fonts", "apply_style", "enable_hidpi", "shorten"]  # the colours are re-exported from palette


MONO_FAMILIES = ("Cascadia Mono", "JetBrains Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Liberation Mono",
                 "Ubuntu Mono", "Noto Sans Mono", "Courier New")


def pick_mono(root: tkinter.Misc) -> str:
    """The first good monospaced font this computer has; the whole window is set in it, like the text screen."""
    have = {name.casefold(): name for name in tkfont.families(root)}
    for name in MONO_FAMILIES:
        if name.casefold() in have:
            return have[name.casefold()]
    return str(tkfont.nametofont("TkFixedFont").actual("family"))


class Fonts:
    """Named fonts. Everything is monospaced, as in the text screen; the names say what a font is for, not its family."""

    def __init__(self, master: tkinter.Misc) -> None:
        family = pick_mono(master)
        size = abs(int(tkfont.nametofont("TkDefaultFont").actual("size"))) or 10
        if sys.platform == "win32":
            size = max(size, 10)
        elif sys.platform == "darwin":
            size = max(size, 12)
        else:
            size = 10
        self.family = family
        self.base = tkfont.Font(master, family=family, size=size)
        self.small = tkfont.Font(master, family=family, size=max(size - 1, 8))
        self.tiny = tkfont.Font(master, family=family, size=max(size - 2, 7))
        self.bold = tkfont.Font(master, family=family, size=size, weight="bold")
        self.heading = tkfont.Font(master, family=family, size=size + 1, weight="bold")
        self.title = tkfont.Font(master, family=family, size=size + 5, weight="bold")
        self.big = tkfont.Font(master, family=family, size=size + 2, weight="bold")
        self.mono = self.small  # (the name older code uses for the small fixed-width text)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkFixedFont", "TkTooltipFont"):
            with contextlib.suppress(tkinter.TclError):
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
    border = {"bordercolor": p["line"], "lightcolor": p["line"], "darkcolor": p["line"]}
    lit = [("focus", p["primary"])]  # a box that has the keyboard focus gets an amber edge
    style.configure(".", background=p["bg"], foreground=p["text"], font=fonts.base, troughcolor=p["trough"], focuscolor=p["bg"],
                    selectbackground=p["primary"], selectforeground=p["on_primary"], insertcolor=p["text"], **border)
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Panel.TLabel", background=p["panel"])
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["text"], padding=6, **border)
    style.map("TEntry", bordercolor=lit, lightcolor=lit, darkcolor=lit)
    style.configure("TCombobox", fieldbackground=p["field"], background=p["raised"], foreground=p["text"], arrowcolor=p["text"],
                    padding=6, **border)
    style.map("TCombobox", fieldbackground=[("readonly", p["field"])], background=[("active", p["hover_raised"]), ("readonly", p["raised"])],
              foreground=[("readonly", p["text"]), ("disabled", p["muted"])], selectbackground=[("readonly", p["field"])],
              selectforeground=[("readonly", p["text"])], arrowcolor=[("disabled", p["muted"])], bordercolor=lit, lightcolor=lit,
              darkcolor=lit)
    for kind in ("TCheckbutton", "TRadiobutton"):
        style.configure(kind, background=p["bg"], foreground=p["text"], indicatorbackground=p["field"], indicatorforeground=p["primary"],
                        upperbordercolor=p["muted"], lowerbordercolor=p["muted"])
        style.map(kind, background=[("active", p["bg"])], indicatorbackground=[("selected", p["field"]), ("!selected", p["field"])],
                  foreground=[("disabled", p["muted"])])
    style.configure("Panel.TCheckbutton", background=p["panel"])
    style.configure("Panel.TRadiobutton", background=p["panel"])
    style.configure("Horizontal.TProgressbar", troughcolor=p["trough"], background=p["primary"], bordercolor=p["line"],
                    lightcolor=p["primary"], darkcolor=p["primary"])
    style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"], foreground=p["text"],
                    rowheight=int(fonts.base.metrics("linespace") * 1.9), bordercolor=p["line"])
    style.configure("Treeview.Heading", font=fonts.bold, background=p["header"], foreground=p["text"], relief="flat")
    style.map("Treeview", background=[("selected", p["sidebar_active"])], foreground=[("selected", p["text"])])
    style.map("Treeview.Heading", background=[("active", p["hover_raised"])])
    style.configure("TScrollbar", troughcolor=p["bg"], background=p["raised"], bordercolor=p["bg"], lightcolor=p["raised"],
                    darkcolor=p["raised"], arrowcolor=p["muted"])
    style.map("TScrollbar", background=[("active", p["hover_raised"]), ("pressed", p["line"])])
    style.configure("TNotebook", background=p["panel"], bordercolor=p["line"])
    style.configure("TNotebook.Tab", padding=(14, 6), font=fonts.base, background=p["raised"], foreground=p["muted"])
    style.map("TNotebook.Tab", background=[("selected", p["panel"])], foreground=[("selected", p["text"])])
    style.configure("TSeparator", background=p["line"])
    # the pop-up list of a drop-down, menus and the classic widgets in dialogs follow the palette too
    # classic widgets that do not name their colours get the palette's (a label with no colour would be black on near-black)
    for pattern, value in (("*Label.foreground", p["text"]), ("*Label.background", p["bg"]), ("*Frame.background", p["bg"]),
                           ("*Toplevel.background", p["panel"]), ("*Canvas.background", p["panel"]),
                           ("*TCombobox*Listbox.background", p["field"]), ("*TCombobox*Listbox.foreground", p["text"]),
                           ("*TCombobox*Listbox.selectBackground", p["primary"]), ("*TCombobox*Listbox.selectForeground", p["on_primary"]),
                           ("*TCombobox*Listbox.borderWidth", 0), ("*Menu.background", p["panel"]), ("*Menu.foreground", p["text"]),
                           ("*Menu.activeBackground", p["sidebar_active"]), ("*Menu.activeForeground", p["text"]),
                           ("*Menu.borderWidth", 1), ("*Menu.relief", "flat"), ("*Menu.activeBorderWidth", 0),
                           ("*Listbox.background", p["field"]), ("*Listbox.foreground", p["text"]),
                           ("*Listbox.selectBackground", p["primary"]), ("*Listbox.selectForeground", p["on_primary"]),
                           ("*Listbox.highlightColor", p["line"]), ("*Listbox.highlightBackground", p["line"])):
        root.option_add(pattern, value)
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
