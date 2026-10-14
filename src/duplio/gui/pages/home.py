"""The first screen: the big mark, what Duplio promises, and the things it does as numbered cards (press ctrl+the number)."""

from __future__ import annotations

import tkinter as tk
from functools import partial
from typing import TYPE_CHECKING

from duplio.assets.logo import TAGLINE, WORDMARK
from duplio.gui.app import Page
from duplio.gui.style import PALETTE
from duplio.gui.widgets import autowrap, bind_to

if TYPE_CHECKING:
    from duplio.gui.app import Context

CARDS = (  # (page, number, title, what it does)
    ("browse", "1", "Browse a folder", "Look inside any folder, on this computer, a phone, a NAS or a cloud drive, as a wall of pictures."),
    ("favorites", "2", "Favorites", "Star the pictures and movies you love, wherever they are, and gather copies of them into an album."),
    ("compare", "3", "Compare two folders", "Put folder A next to folder B: what is in one and not the other. Renamed files are recognised."),
    ("backup", "4", "Is it backed up?", "Check that everything in a folder has a safe copy in your backup, and copy over what is missing."),
    ("dupes", "5", "Find duplicates", "Scan folders, phones and shares for identical files; review them side by side; clean up safely."),
    ("sync", "6", "Keep in sync", "Make two folders match, in one direction or both, without ever destroying anything."),
    ("import", "7", "Import from phone", "Copy new photos and videos into your library, skipping the ones it already has."),
    ("history", "8", "History & undo", "Everything Duplio changed, and the button to put it back."),
    ("settings", "9", "Settings", "Colours, the picture buffer, and what to skip when searching."),
)
SAFETY = ("Nothing is removed without your review. Files are re-verified the moment before any change, every change is "
          "journaled, and the default action — quarantine — is instantly undoable.")


class HomePage(Page):
    title = "What would you like to do?"
    subtitle = ""

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        hero = tk.Frame(self, bg=PALETTE["bg"])
        hero.pack(fill="x", padx=28, pady=(6, 14))
        self._logo = ctx.window.make_logo(5, PALETTE["bg"])
        if self._logo is not None:
            tk.Label(hero, image=self._logo, bg=PALETTE["bg"]).pack(side="left", padx=(0, 30))
        text = tk.Frame(hero, bg=PALETTE["bg"])
        text.pack(side="left", fill="both", expand=True, anchor="n")
        tk.Label(text, text=WORDMARK, font=f.title, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w").pack(fill="x", pady=(14, 0))
        tk.Label(text, text=TAGLINE, font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w").pack(fill="x")
        tk.Label(text, text="Safety", font=f.heading, bg=PALETTE["bg"], fg=PALETTE["heading"], anchor="w").pack(fill="x", pady=(26, 2))
        blurb = tk.Label(text, text=SAFETY, font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left", wraplength=560)
        blurb.pack(fill="x")
        autowrap(blurb, margin=10, minimum=240)

        grid = tk.Frame(self, bg=PALETTE["bg"])
        grid.pack(fill="x", padx=28, pady=(0, 10))
        columns = 3
        for n, (key, number, title, what) in enumerate(CARDS):
            card = tk.Frame(grid, bg=PALETTE["panel"], highlightthickness=1, highlightbackground=PALETTE["line"], cursor="hand2")
            card.grid(row=n // columns, column=n % columns, sticky="nsew", padx=6, pady=6)
            head = tk.Label(card, text=f"{number}  {title}", font=f.bold, bg=PALETTE["panel"], fg=PALETTE["text"], anchor="w", justify="left")
            head.pack(fill="x", padx=14, pady=(12, 8))
            rule = tk.Frame(card, bg=PALETTE["line"], height=1)
            rule.pack(fill="x", padx=14)
            body = tk.Label(card, text=what, font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="nw", justify="left")
            body.pack(fill="x", padx=14, pady=(8, 12))
            for label in (head, body):
                autowrap(label, margin=4, minimum=120)
            for widget in (card, head, rule, body):
                bind_to(widget, "<Button-1>", partial(ctx.window.show, key))
                bind_to(widget, "<Enter>", partial(card.configure, highlightbackground=PALETTE["primary"]))
                bind_to(widget, "<Leave>", partial(card.configure, highlightbackground=PALETTE["line"]))
        for c in range(columns):
            grid.columnconfigure(c, weight=1, uniform="cards")
