"""The window's colours, in two themes, chosen once when Duplio starts.

``dark`` is the look of the text screen (near-black with warm red and amber accents). ``light`` is the paler one. Folder A is
blue and folder B is red in both, the colours of the two figures in Duplio's icon, so a glance at the border of a picture
says which folder it is in. (Kept apart from the Tk code so plain logic can use it.)

The theme is read when this module is first imported, from ``DUPLIO_THEME`` or the window's saved state, so every colour
derived from it elsewhere is consistent; changing it in Settings takes effect the next time Duplio starts.
"""

from __future__ import annotations

import contextlib
import json
import os

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0e0d10", "panel": "#17151a", "raised": "#211e25", "field": "#121014",
        "sidebar": "#09080b", "sidebar_hover": "#17151a", "sidebar_active": "#2a1d22",
        "sidebar_text": "#e8e6e3", "sidebar_muted": "#8d8893",
        "text": "#e8e6e3", "muted": "#9a95a0", "line": "#2e2a33",
        "a": "#5b9dff", "a_soft": "#0f1b31", "b": "#ff5c61", "b_soft": "#2f1116",
        "ok": "#4cc38a", "ok_soft": "#0e2a1f", "warn": "#e5b53e", "warn_soft": "#2b2208",
        "bad": "#ff5555", "bad_soft": "#341117",
        "thumb_bg": "#1d1a21", "select": "#e0a458",
        "primary": "#e0a458", "primary_hover": "#f0bd78", "on_primary": "#1a1306",
        "danger_fill": "#d33a3a", "danger_hover": "#e04a4a", "success_hover": "#5fd29b",
        "ink": "#0e0d10", "white": "#ffffff",
        "slot": "#121014", "header": "#1c1920", "trough": "#25222a",
        "tip_bg": "#2a2630", "tip_fg": "#e8e6e3", "hover_raised": "#2b2730", "hover_quiet": "#1c1920",
    },
    "light": {
        "bg": "#f3f4f7", "panel": "#ffffff", "raised": "#ffffff", "field": "#ffffff",
        "sidebar": "#1d2230", "sidebar_hover": "#2b3248", "sidebar_active": "#3b4568",
        "sidebar_text": "#e8ebf5", "sidebar_muted": "#97a0bc",
        "text": "#1c2230", "muted": "#667085", "line": "#d9dce4",
        "a": "#2f6fed", "a_soft": "#e7eeff", "b": "#e5484d", "b_soft": "#fdecec",
        "ok": "#1f9d55", "ok_soft": "#e4f5ea", "warn": "#b46900", "warn_soft": "#fff2d9",
        "bad": "#d13438", "bad_soft": "#fde7e8",
        "thumb_bg": "#eceef3", "select": "#2f6fed",
        "primary": "#2f6fed", "primary_hover": "#2459c7", "on_primary": "#ffffff",
        "danger_fill": "#d13438", "danger_hover": "#a8282c", "success_hover": "#17803f",
        "ink": "#ffffff", "white": "#ffffff",
        "slot": "#fafbfd", "header": "#eef0f6", "trough": "#dfe3ec",
        "tip_bg": "#222839", "tip_fg": "#ffffff", "hover_raised": "#eef0f5", "hover_quiet": "#e4e7ef",
    },
}
DEFAULT_THEME = "dark"
THEME_NAMES = {"dark": "Dark (the default)", "light": "Light"}


def chosen_theme() -> str:
    """The theme to use: ``DUPLIO_THEME``, else what Settings saved, else dark."""
    name = os.environ.get("DUPLIO_THEME", "").strip().lower()
    if name in THEMES:
        return name
    with contextlib.suppress(Exception):
        from duplio.util.appdirs import get_app_dirs

        saved = json.loads((get_app_dirs().state / "window.json").read_text(encoding="utf-8")).get("theme")
        if saved in THEMES:
            return str(saved)
    return DEFAULT_THEME


THEME = chosen_theme()
PALETTE: dict[str, str] = dict(THEMES[THEME])
SIDE_COLOR = {0: PALETTE["a"], 1: PALETTE["b"]}
SIDE_SOFT = {0: PALETTE["a_soft"], 1: PALETTE["b_soft"]}
DARK = THEME == "dark"
