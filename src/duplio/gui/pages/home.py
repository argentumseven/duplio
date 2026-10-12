"""The first screen: the things Duplio does, as big buttons."""

from __future__ import annotations

import tkinter as tk
from functools import partial
from typing import TYPE_CHECKING

from duplio.gui.app import Page
from duplio.gui.style import PALETTE, SIDE_COLOR
from duplio.gui.widgets import autowrap, bind_to

if TYPE_CHECKING:
    from duplio.gui.app import Context

CARDS = (
    ("compare", "Compare two folders", "Put folder A next to folder B and see what is in one and not the other, with pictures. "
                                       "Renamed files are recognised.", SIDE_COLOR[0]),
    ("backup", "Is it backed up?", "Check that everything on your phone or in a folder has a safe copy in your backup, "
                                   "and copy over what is missing.", PALETTE["ok"]),
    ("dupes", "Find duplicates", "Find identical files inside one folder or between two, see them side by side, "
                                 "and clear the extra copies safely.", SIDE_COLOR[1]),
    ("sync", "Keep in sync", "Make two folders match, in one direction or both, without ever destroying anything.", PALETTE["warn"]),
    ("import", "Import from phone", "Bring only the new photos from a phone or camera card into your library.", PALETTE["a"]),
    ("history", "History & undo", "Everything Duplio changed, and the button to put it back.", PALETTE["muted"]),
)


class HomePage(Page):
    title = "What would you like to do?"
    subtitle = "Every screen puts two folders next to each other. Nothing is removed without your OK, and everything can be undone."

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        grid = tk.Frame(self, bg=PALETTE["bg"])
        grid.pack(fill="both", expand=True, padx=28, pady=20)
        for n, (key, title, text, color) in enumerate(CARDS):
            card = tk.Frame(grid, bg=PALETTE["panel"], highlightbackground=PALETTE["line"], highlightthickness=1, cursor="hand2")
            card.grid(row=n // 3, column=n % 3, sticky="new", padx=10, pady=10)
            strip = tk.Frame(card, bg=color, height=6)
            strip.pack(fill="x")
            head = tk.Label(card, text=title, font=f.heading, bg=PALETTE["panel"], fg=PALETTE["text"], anchor="w", justify="left")
            head.pack(fill="x", padx=18, pady=(16, 6))
            body = tk.Label(card, text=text, font=f.base, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="nw", justify="left", height=4)
            body.pack(fill="x", padx=18, pady=(0, 18))
            for label in (head, body):
                autowrap(label, margin=4, minimum=120)
            for widget in (card, strip, *card.winfo_children()):
                bind_to(widget, "<Button-1>", partial(ctx.window.show, key))
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="cards")
        grid.rowconfigure(2, weight=1)  # the cards keep their natural height; the spare room goes below them
