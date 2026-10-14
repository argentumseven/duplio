"""Settings: how pictures are kept, what to skip when searching, and a way to the text screen."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from duplio.config import save_settings
from duplio.errors import ConfigError
from duplio.gui import extras
from duplio.gui.app import Page
from duplio.gui.memory import GIB, MIB, default_image_budget, describe
from duplio.gui.palette import THEME, THEME_NAMES
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, ScrollFrame, autowrap
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
        scroll = ScrollFrame(self, PALETTE["bg"])
        scroll.pack(fill="both", expand=True, padx=(28, 12), pady=(10, 14))
        body = scroll.inner
        bg = PALETTE["bg"]

        def heading(text: str) -> None:
            tk.Label(body, text=text, font=f.heading, bg=bg, fg=PALETTE["heading"], anchor="w").pack(fill="x", pady=(16, 4))

        def note(text: str, pady: tuple[int, int] = (0, 6)) -> None:
            label = tk.Label(body, text=text, font=f.small, bg=bg, fg=PALETTE["muted"], anchor="w", justify="left")
            label.pack(fill="x", pady=pady)
            autowrap(label, margin=24, minimum=300)

        def choice(text: str, values: list[str], current: str) -> ttk.Combobox:
            row = tk.Frame(body, bg=bg)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=text, font=f.base, bg=bg, fg=PALETTE["text"], width=32, anchor="w").pack(side="left")
            box = ttk.Combobox(row, state="readonly", font=f.base, values=values, width=max(len(v) for v in values) + 3)
            box.set(current)
            box.pack(side="left")
            return box

        def check(text: str, variable: tk.BooleanVar) -> None:
            ttk.Checkbutton(body, text=text, variable=variable).pack(anchor="w", pady=2)

        heading("Appearance")
        self.theme = choice("Colours", list(THEME_NAMES.values()), THEME_NAMES[THEME])
        note("Applies the next time Duplio starts.", (2, 4))

        heading("Pictures from slow drives")
        note("Duplio takes the small preview that is already inside a photo (or a picture of the first moments of a movie) and keeps it, "
             "so looking at pictures on a phone, a NAS or a cloud drive does not download the photos again and again.")
        keeps = ["between runs (kept unencrypted; recommended)", "until Duplio closes"]
        self.keep = choice("Remember pictures", keeps, keeps[0 if s.previews.keep == "persistent" else 1])
        self.days_choices = [(7, "a week"), (30, "30 days"), (60, "60 days"), (180, "6 months"), (365, "a year"), (0, "never")]
        shown_days = next((label for n, label in self.days_choices if n == s.previews.keep_days), f"{s.previews.keep_days} days")
        self.keep_days = choice("Forget pictures not looked at for", [label for _n, label in self.days_choices]
                                if shown_days in [label for _n, label in self.days_choices] else [shown_days, *[label for _n, label in self.days_choices]],
                                shown_days)
        modes = ["slow, remote and cloud drives (recommended)", "every drive", "nothing (always read the drive)"]
        self.mode = choice("Use the picture memory for", modes, modes[{"auto": 0, "always": 1, "off": 2}[s.previews.mode]])
        self.buffer_sizes = [(GIB, "1 GB"), (2 * GIB, "2 GB"), (4 * GIB, "4 GB"), (8 * GIB, "8 GB"), (16 * GIB, "16 GB"),
                             (32 * GIB, "32 GB"), (64 * GIB, "64 GB"), (128 * GIB, "128 GB")]
        sizes = [label for _n, label in self.buffer_sizes]
        shown = next((label for n, label in self.buffer_sizes if n == s.previews.max_bytes), describe(s.previews.max_bytes))
        self.buffer = choice("Picture buffer on disk, up to", sizes if shown in sizes else [shown, *sizes], shown)
        note("Roughly 1 to 4 MB per hundred pictures (a little more without Pillow). Nothing is reserved: this is only the ceiling. "
             "When it is reached the pictures nobody has looked at for the longest are let go first, never the folder you are looking at.",
             (2, 6))
        self.memory_sizes = [(0, f"Automatic ({describe(default_image_budget())})"), (256 * MIB, "256 MB"), (512 * MIB, "512 MB"), (GIB, "1 GB"),
                             (2 * GIB, "2 GB"), (4 * GIB, "4 GB")]
        labels = [label for _n, label in self.memory_sizes]
        self.memory = choice("Memory for pictures on screen", labels,
                             next((label for n, label in self.memory_sizes if n == s.previews.memory_bytes), labels[0]))
        note("Shrinks by itself if the computer runs short of memory.", (2, 6))
        self.whole_folder = tk.BooleanVar(value=s.previews.prefetch_folder)
        check("Browse: fetch a folder's pictures in the background", self.whole_folder)
        note("From the part you are looking at to the end of the folder, then back to the start. Jump ahead and it carries on from there.",
             (0, 2))
        self.download_whole = tk.BooleanVar(value=s.previews.download_whole)
        check("Read whole pictures and movies that have no small preview", self.download_whole)
        note("Needed for most movies and for photos with no thumbnail inside. It downloads those files once (up to 64 MB each) and keeps "
             "the picture, so it uses the network the first time you look at a folder.", (0, 6))
        self.cloud = tk.BooleanVar(value=s.previews.online_only)
        check("Fetch pictures of cloud-only files too (this downloads them)", self.cloud)
        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=8)
        Btn(row, "Delete pictures", self.clear_pictures, fonts=f, padx=12, pady=6).pack(side="left")
        self.cache_info = tk.Label(row, text="", font=f.small, bg=bg, fg=PALETTE["muted"])
        self.cache_info.pack(side="left", padx=12)

        heading("Picture support")
        note("Duplio reads JPEG, PNG, GIF, BMP and a few more formats by itself. These two programs add the rest and make it faster. "
             "Neither is needed, and nothing is installed unless you press a button.")
        self.pillow_label = tk.Label(body, text="", font=f.base, bg=bg, fg=PALETTE["text"], anchor="w", justify="left")
        self.pillow_label.pack(fill="x", pady=(4, 2))
        autowrap(self.pillow_label, margin=24, minimum=300)
        self.pillow_button = Btn(body, "Install Pillow…", self.install_pillow, fonts=f, padx=12, pady=6)
        self.ffmpeg_label = tk.Label(body, text="", font=f.base, bg=bg, fg=PALETTE["text"], anchor="w", justify="left")
        self.ffmpeg_label.pack(fill="x", pady=(10, 2))
        autowrap(self.ffmpeg_label, margin=24, minimum=300)
        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="ffmpeg program (empty: look for it)", font=f.base, bg=bg, fg=PALETTE["text"], width=32, anchor="w").pack(side="left")
        self.ffmpeg_entry = ttk.Entry(row, font=f.base)
        self.ffmpeg_entry.insert(0, s.previews.ffmpeg)
        self.ffmpeg_entry.pack(side="left", fill="x", expand=True)
        Btn(row, "Look again", self.refresh_support, fonts=f, padx=12, pady=4).pack(side="left", padx=(8, 0))
        self.refresh_support()

        heading("Searching for duplicates")
        self.hidden = tk.BooleanVar(value=s.skip_hidden)
        check("Skip hidden files and folders", self.hidden)
        self.remember = tk.BooleanVar(value=s.cache)
        check("Remember file fingerprints between runs", self.remember)
        note("Makes repeat searches much faster; removing a file always re-checks it first.", (0, 6))
        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Ignore files smaller than", font=f.base, bg=bg, fg=PALETTE["text"], width=32, anchor="w").pack(side="left")
        self.min_size = ttk.Entry(row, width=10, font=f.base)
        self.min_size.insert(0, human_size(s.min_size).replace(" B", "B") if s.min_size > 1 else "1")
        self.min_size.pack(side="left")
        tk.Label(row, text="for example 1, 100KB, 1MB", font=f.small, bg=bg, fg=PALETTE["muted"]).pack(side="left", padx=12)
        for text, attr, values in (("Never look in (patterns, comma-separated: *.tmp, **/cache/**)", "exclude", s.exclude),
                                   ("Never remove files matching (for example **/Originals/**)", "protect", s.protect)):
            tk.Label(body, text=text, font=f.base, bg=bg, fg=PALETTE["text"], anchor="w").pack(fill="x", pady=(10, 2))
            entry = ttk.Entry(body, font=f.base)
            entry.insert(0, ", ".join(values))
            entry.pack(fill="x")
            setattr(self, attr + "_entry", entry)

        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=(22, 8))
        Btn(row, "Save settings", self.save, kind="primary", fonts=f, padx=20, pady=9).pack(side="left")
        self.saved = tk.Label(row, text="", font=f.base, bg=bg, fg=PALETTE["ok"])
        self.saved.pack(side="left", padx=14)

    def refresh_support(self) -> None:
        """Show whether Pillow and ffmpeg are there, using the program named in the box if there is one."""
        self.pillow_label.configure(text=extras.pillow_status())
        if extras.pillow_available():
            self.pillow_button.pack_forget()
        elif not self.pillow_button.winfo_ismapped():
            self.pillow_button.pack(anchor="w", pady=(2, 6), after=self.pillow_label)
        self.ffmpeg_label.configure(text=extras.ffmpeg_status(self.ffmpeg_entry.get().strip()))

    def install_pillow(self) -> None:
        ctx = self.ctx
        if not ctx.confirm("Install Pillow", "Install the Pillow picture library?",
                           [f"This runs:  {extras.pip_command_text()}  (adding --user when Python allows it).",
                            "It downloads about 5 MB from pypi.org and installs it for this Python only. Nothing else is changed."],
                           confirm="Install"):
            return

        def done(result: tuple[bool, str]) -> None:
            worked, message = result
            self.refresh_support()
            ctx.info("Pillow", "Done." if worked else "Pillow was not installed.", [message], "ok" if worked else "warn")

        ctx.run_job("Installing Pillow", lambda _progress, _cancel: extras.install_pillow(), done)

    def on_show(self) -> None:
        self.refresh_support()
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
            p.keep = "persistent" if self.keep.current() == 0 else "session"
            shown_days = self.keep_days.get()
            p.keep_days = next((n for n, label in self.days_choices if label == shown_days), p.keep_days)
            p.download_whole = bool(self.download_whole.get())
            p.ffmpeg = self.ffmpeg_entry.get().strip()
            p.mode = ("auto", "always", "off")[self.mode.current()]
            p.online_only = bool(self.cloud.get())
            p.max_bytes = next((n for n, label in self.buffer_sizes if label == self.buffer.get()), p.max_bytes)
            p.memory_bytes = next((n for n, label in self.memory_sizes if label == self.memory.get()), p.memory_bytes)
            p.prefetch_folder = bool(self.whole_folder.get())
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
        self.ctx.previews.apply_settings()
        self.ctx.thumbs.set_budget(p.memory_bytes or None)
        chosen = next((name for name, label in THEME_NAMES.items() if label == self.theme.get()), THEME)
        self.ctx.state.set("theme", chosen)
        self.ctx.state.save()
        self.saved.configure(text="Saved." if chosen == THEME else "Saved. The new colours appear the next time Duplio starts.")
        self.after(3000, lambda: self.saved.configure(text=""))
