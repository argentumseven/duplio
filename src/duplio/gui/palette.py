"""The window's colours. Folder A is blue and folder B is red throughout, the colours of the two figures in Duplio's icon,
so a glance at the border of a thumbnail says which folder it is in. (Kept apart from the Tk code so plain logic can use it.)"""

from __future__ import annotations

PALETTE = {
    "bg": "#f3f4f7",
    "panel": "#ffffff",
    "sidebar": "#1d2230",
    "sidebar_hover": "#2b3248",
    "sidebar_active": "#3b4568",
    "sidebar_text": "#e8ebf5",
    "sidebar_muted": "#97a0bc",
    "text": "#1c2230",
    "muted": "#667085",
    "line": "#d9dce4",
    "a": "#2f6fed",
    "a_soft": "#e7eeff",
    "b": "#e5484d",
    "b_soft": "#fdecec",
    "ok": "#1f9d55",
    "ok_soft": "#e4f5ea",
    "warn": "#b46900",
    "warn_soft": "#fff2d9",
    "bad": "#d13438",
    "bad_soft": "#fde7e8",
    "thumb_bg": "#eceef3",
    "select": "#2f6fed",
}
SIDE_COLOR = {0: PALETTE["a"], 1: PALETTE["b"]}
SIDE_SOFT = {0: PALETTE["a_soft"], 1: PALETTE["b_soft"]}
