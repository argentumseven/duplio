"""Settings: how pictures are kept, what to skip when searching, and a way to the text screen."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from duplio.config import save_settings
from duplio.errors import ConfigError
from duplio.gui.app import Page
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn
from duplio.util.fmt import human_size, parse_size

if TYPE_CHECKING:
    from duplio.gui.app import Context


class SettingsPage(Page):
    title = "Settings"
    subtitle = "The defaults are safe. Nothing here can make Duplio remove a file without asking."

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        s = ctx.settings
        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(10, 20))

        def heading(text: str) -> None:
            tk.Label(body, text=text, font=f.heading, bg=PALETTE["bg"], anchor="w").pack(fill="x", pady=(14, 4))

        def note(text: str) -> None:
            tk.Label(body, text=text, font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left", wraplength=900).pack(fill="x")

        heading("Pictures from slow drives")
        note("Duplio takes the small preview that is already inside a photo and remembers it, so looking at pictures on a phone, "
             "a NAS or a cloud drive does not download the photos again and again.")
        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", pady=6)
        tk.Label(row, text="Remember pictures", font=f.base, bg=PALETTE["bg"]).pack(side="left")
        self.keep = ttk.Combobox(row, state="readonly", width=44, font=f.base,
                                 values=["only until Duplio is closed (recommended)", "between runs (kept on this computer, NOT encrypted)"])
        self.keep.current(0 if s.previews.keep == "session" else 1)
        self.keep.pack(side="left", padx=10)
        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="Use the picture memory for", font=f.base, bg=PALETTE["bg"]).pack(side="left")
        self.mode = ttk.Combobox(row, state="readonly", width=40, font=f.base,
                                 values=["slow, remote and cloud drives (recommended)", "every drive", "nothing (always read the drive)"])
        self.mode.current({"auto": 0, "always": 1, "off": 2}[s.previews.mode])
        self.mode.pack(side="left", padx=10)
        self.cloud = tk.BooleanVar(value=s.previews.online_only)
        ttk.Checkbutton(body, text="Also show pictures of files that are only stored in the cloud (this downloads them)",
                        variable=self.cloud).pack(anchor="w", pady=2)
        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", pady=6)
        self.cache_info = tk.Label(row, text="", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"])
        Btn(row, "Delete the remembered pictures", self.clear_pictures, fonts=f, padx=12, pady=6).pack(side="left")
        self.cache_info.pack(side="left", padx=12)

        heading("Searching for duplicates")
        self.hidden = tk.BooleanVar(value=s.skip_hidden)
        ttk.Checkbutton(body, text="Skip hidden files and folders", variable=self.hidden).pack(anchor="w", pady=2)
        self.remember = tk.BooleanVar(value=s.cache)
        ttk.Checkbutton(body, text="Remember file fingerprints between runs (makes repeat searches much faster; removal always re-checks the files)",
                        variable=self.remember).pack(anchor="w", pady=2)
        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Ignore files smaller than", font=f.base, bg=PALETTE["bg"]).pack(side="left")
        self.min_size = ttk.Entry(row, width=10)
        self.min_size.insert(0, human_size(s.min_size).replace(" B", "B") if s.min_size > 1 else "1")
        self.min_size.pack(side="left", padx=10)
        tk.Label(row, text="(for example 1, 100KB, 1MB)", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="left")
        for label, attr, values in (("Never look in (comma-separated patterns such as *.tmp, **/cache/**)", "exclude", s.exclude),
                                    ("Never remove files matching (for example **/Originals/**)", "protect", s.protect)):
            tk.Label(body, text=label, font=f.base, bg=PALETTE["bg"], anchor="w").pack(fill="x", pady=(8, 2))
            entry = ttk.Entry(body)
            entry.insert(0, ", ".join(values))
            entry.pack(fill="x")
            setattr(self, attr + "_entry", entry)

        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(22, 0))
        Btn(row, "Save settings", self.save, kind="primary", fonts=f, padx=20, pady=9).pack(side="left")
        self.saved = tk.Label(row, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["ok"])
        self.saved.pack(side="left", padx=14)

    def on_show(self) -> None:
        count = sum(c for _k, c, _b, _p in self.ctx.previews.storage_summary())
        size = sum(b for _k, _c, b, _p in self.ctx.previews.storage_summary())
        self.cache_info.configure(text=f"{count:,} pictures remembered ({human_size(size)})" if count else "no pictures remembered yet")

    def clear_pictures(self) -> None:
        removed = self.ctx.previews.clear()
        self.cache_info.configure(text=f"{removed:,} remembered pictures deleted")

    def save(self) -> None:
        s = self.ctx.settings
        p = s.previews
        try:
            p.keep = "session" if self.keep.current() == 0 else "persistent"
            p.mode = ("auto", "always", "off")[self.mode.current()]
            p.online_only = bool(self.cloud.get())
            p.validate()
            s.skip_hidden = bool(self.hidden.get())
            s.cache = bool(self.remember.get())
            text = self.min_size.get().strip() or "1"
            s.min_size = int(text) if text.isdigit() else parse_size(text)
            s.exclude = [x.strip() for x in self.exclude_entry.get().split(",") if x.strip()]  # type: ignore[attr-defined]
            s.protect = [x.strip() for x in self.protect_entry.get().split(",") if x.strip()]  # type: ignore[attr-defined]
            save_settings(s)
        except (ConfigError, ValueError) as exc:
            self.ctx.show_error(f"These settings cannot be saved: {exc}")
            return
        self.ctx.previews.settings = p
        self.saved.configure(text="Saved.")
        self.after(3000, lambda: self.saved.configure(text=""))
