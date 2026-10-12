"""The two folder boxes every page starts with: where folder A is, where folder B is."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

from duplio.gui.dialogs import ConnectDialog, FolderPicker
from duplio.gui.style import PALETTE, SIDE_COLOR, SIDE_SOFT
from duplio.gui.widgets import Btn, autowrap
from duplio.vfs.location import location_kind

if TYPE_CHECKING:
    from duplio.gui.app import Context

READ_COST = {0: "fast", 1: "slower (USB stick or spinning disk)", 2: "slow: pictures are fetched once and remembered"}


def describe_folder(ctx: Context, text: str) -> tuple[str, str]:
    """``(sentence, colour)`` saying what Duplio makes of a folder address, without connecting to anything."""
    text = text.strip()
    if not text:
        return "", PALETTE["muted"]
    try:
        kind = location_kind(text)
        if kind == "local":
            path = os.path.abspath(os.path.expanduser(text))
            if not os.path.isdir(path):
                return "This folder was not found.", PALETTE["bad"]
        loc = ctx.registry.open(text)
        caps = loc.caps
    except Exception as exc:
        return str(exc), PALETTE["bad"]
    speed = READ_COST[caps.read_cost]
    note = "  Not encrypted!" if "not encrypted" in caps.label.lower() else ""
    color = PALETTE["bad"] if note else PALETTE["muted"]
    if caps.virtual:
        return f"{caps.label} · a cloud drive: files are not downloaded unless you ask · {speed}", color
    return f"{caps.label} · reading is {speed}{note}", color


class FolderBox(tk.Frame):
    """One folder: a coloured letter, a path box with recent folders, and the buttons that fill it in."""

    def __init__(self, master: tk.Misc, ctx: Context, side: int, title: str, hint: str, on_change=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(master, bg=PALETTE["bg"])
        self.ctx, self.side, self._on_change = ctx, side, on_change
        f = ctx.fonts
        head = tk.Frame(self, bg=PALETTE["bg"])
        head.pack(fill="x")
        badge = tk.Label(head, text="AB"[side], bg=SIDE_COLOR[side], fg="#ffffff", font=f.big, width=2)
        badge.pack(side="left")
        text = tk.Frame(head, bg=PALETTE["bg"])
        text.pack(side="left", padx=10)
        tk.Label(text, text=title, font=f.bold, bg=PALETTE["bg"], anchor="w").pack(fill="x")
        tk.Label(text, text=hint, font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w").pack(fill="x")
        self.var = tk.StringVar()
        self.entry = ttk.Combobox(self, textvariable=self.var, values=ctx.state.recent, font=f.base)
        self.entry.pack(fill="x", pady=(8, 0))
        self.entry.bind("<Return>", lambda _e: self._changed())
        self.entry.bind("<FocusOut>", lambda _e: self._changed())
        self.entry.bind("<<ComboboxSelected>>", lambda _e: self.after(10, self._changed))
        row = tk.Frame(self, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(6, 0))
        Btn(row, "Choose folder…", self.choose, fonts=f, padx=10, pady=4).pack(side="left")
        Btn(row, "Phone or server…", self.connect, fonts=f, padx=10, pady=4).pack(side="left", padx=(6, 0))
        self.about = tk.Label(self, text="", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.about.pack(fill="x", pady=(4, 0))
        autowrap(self.about, margin=6, minimum=120)
        self._shown = ""

    # ---- the value --------------------------------------------------------
    def get(self) -> str:
        return self.var.get().strip()

    def set(self, text: str) -> None:
        self.var.set(text)
        self._changed()

    def refresh_recent(self) -> None:
        self.entry.configure(values=self.ctx.state.recent)

    def _changed(self) -> None:
        text = self.get()
        if text == self._shown:
            return
        self._shown = text
        sentence, color = describe_folder(self.ctx, text)
        self.about.configure(text=sentence, fg=color)
        if self._on_change:
            self._on_change()

    # ---- filling it in ------------------------------------------------------
    def choose(self) -> None:
        current = self.get()
        if current and location_kind(current) != "local":
            picked = FolderPicker(self.ctx, current, "Choose a folder").show()
        else:
            start = os.path.expanduser(current) if current and os.path.isdir(os.path.expanduser(current)) else os.path.expanduser("~")
            picked = filedialog.askdirectory(parent=self.ctx.window, initialdir=start, title="Choose a folder", mustexist=True) or None
        if picked:
            self.set(picked)

    def connect(self) -> None:
        address = ConnectDialog(self.ctx).show()
        if not address:
            return
        picked = FolderPicker(self.ctx, address, "Choose a folder").show()
        self.set(picked or address)


class FolderPair(tk.Frame):
    """Folder A and folder B next to each other, with a button that swaps them."""

    def __init__(self, master: tk.Misc, ctx: Context, a_title: str, a_hint: str, b_title: str, b_hint: str, on_change=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(master, bg=PALETTE["bg"])
        self.a = FolderBox(self, ctx, 0, a_title, a_hint, on_change)
        self.b = FolderBox(self, ctx, 1, b_title, b_hint, on_change)
        self.a.grid(row=0, column=0, sticky="we")
        self.b.grid(row=0, column=2, sticky="we")
        swap = tk.Label(self, text="⇄", font=ctx.fonts.title, bg=PALETTE["bg"], fg=PALETTE["muted"], cursor="hand2", padx=14)
        swap.grid(row=0, column=1)
        swap.bind("<Button-1>", lambda _e: self.swap())
        self.columnconfigure(0, weight=1, uniform="pair")
        self.columnconfigure(2, weight=1, uniform="pair")
        self._soft = SIDE_SOFT

    def values(self) -> tuple[str, str]:
        return self.a.get(), self.b.get()

    def swap(self) -> None:
        a, b = self.a.get(), self.b.get()
        self.a.set(b)
        self.b.set(a)
