"""The Duplio window: a sidebar of things to do, and a page for each."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from duplio import __version__
from duplio.assets.logo import TAGLINE, WORDMARK
from duplio.config import Settings, load_settings
from duplio.gui import style
from duplio.gui.dialogs import ConfirmDialog, MessageDialog, PasswordDialog, ProgressDialog
from duplio.gui.jobs import Job, explain
from duplio.gui.listing import BrowseCache
from duplio.gui.prefetch import FolderPrefetcher
from duplio.gui.state import GuiState
from duplio.gui.style import PALETTE, Fonts
from duplio.gui.thumbs import Thumbnailer
from duplio.gui.widgets import Tooltip, autowrap, bind_to
from duplio.hashing.cache import HashCache, NullCache
from duplio.preview.service import PreviewService
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.vfs.listing import ListingCache
from duplio.vfs.location import BackendRegistry

log = logging.getLogger("duplio.gui")


@dataclass
class Context:
    """What every page shares: the drives, the picture cache, the settings, and ways to talk to the person."""

    window: DuplioWindow
    fonts: Fonts
    dirs: AppDirs
    settings: Settings
    registry: BackendRegistry
    previews: PreviewService
    thumbs: Thumbnailer
    listings: ListingCache
    state: GuiState
    tooltip: Tooltip
    prefetcher: FolderPrefetcher
    browse_cache: BrowseCache

    @property
    def cache(self) -> HashCache | NullCache:
        """Remembered digests, for finding duplicates (the removal step always re-reads the files, so this can never cause a loss)."""
        if self.window.hash_cache is None:
            try:
                self.window.hash_cache = HashCache(self.dirs.hash_cache) if self.settings.cache else NullCache()
            except Exception:
                self.window.hash_cache = NullCache()
        return self.window.hash_cache

    @property
    def fresh_cache(self) -> NullCache:
        """No remembered digests: "is it safe elsewhere?" must reflect what is on the disks now."""
        return NullCache()

    # ---- background work --------------------------------------------------
    def run_job(self, title: str, fn: Callable[[Any, Any], Any], on_done: Callable[[Any], None], *,
                on_error: Callable[[BaseException], None] | None = None, on_cancel: Callable[[], None] | None = None) -> Job:
        job = Job(title, fn)
        job.start()
        ProgressDialog(self, job, on_done, on_error or self.show_error, on_cancel)
        return job

    def call_in_ui(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the window's thread and wait for its result (for worker threads that need to ask something)."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        box: list[Any] = []
        self.window.ui_calls.put((fn, done, box))
        done.wait(timeout=900)
        return box[0] if box else None

    def password_provider(self, server: str, username: str | None) -> str | None:
        return self.call_in_ui(lambda: PasswordDialog(self, server, username).show())

    # ---- talking to the person ----------------------------------------------
    def show_error(self, error: BaseException | str, title: str = "Duplio could not do that") -> None:
        text = error if isinstance(error, str) else explain(error)
        MessageDialog(self, title, text, kind="error")

    def info(self, title: str, headline: str, lines: tuple[str, ...] | list[str] = (), kind: str = "info", buttons=()) -> None:  # type: ignore[no-untyped-def]
        MessageDialog(self, title, headline, list(lines), kind=kind, buttons=buttons)

    def confirm(self, title: str, headline: str, details: list[str] | tuple[str, ...] = (), rows=(), confirm: str = "Continue",  # type: ignore[no-untyped-def]
                danger: bool = False, columns: tuple[str, str] = ("File", "What happens")) -> bool:
        return ConfirmDialog(self, title, headline, details, rows, confirm, danger, columns).show()


class Page(tk.Frame):
    """A screen of the window. Subclasses build their widgets in ``__init__`` and may react to ``on_show``."""

    title = ""
    subtitle = ""

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, bg=PALETTE["bg"])
        self.ctx = ctx
        head = tk.Frame(self, bg=PALETTE["bg"])
        head.pack(fill="x", padx=28, pady=(16, 4))
        tk.Label(head, text=self.title, font=ctx.fonts.title, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w").pack(fill="x")
        tk.Frame(head, bg=PALETTE["primary"], height=2, width=44).pack(anchor="w", pady=(2, 4))
        if self.subtitle:
            sub = tk.Label(head, text=self.subtitle, font=ctx.fonts.base, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w",
                           justify="left", wraplength=900)
            sub.pack(fill="x", pady=(2, 0))
            autowrap(sub, margin=10, minimum=300)

    def on_show(self) -> None:
        """Called each time the page is brought to the front."""

    def on_hide(self) -> None:
        """Called when another page is brought to the front."""


NAV = (
    ("home", "⌂", "Home"),
    ("browse", "▦", "Browse a folder"),
    ("compare", "⇆", "Compare two folders"),
    ("backup", "☑", "Is it backed up?"),
    ("dupes", "❐", "Find duplicates"),
    ("sync", "⟳", "Keep in sync"),
    ("import", "⇩", "Import from phone"),
    ("history", "↶", "History & undo"),
    ("settings", "⚙", "Settings"),
)


def _page_classes() -> dict[str, type[Page]]:
    from duplio.gui.pages.backedup import BackedUpPage
    from duplio.gui.pages.browse import BrowsePage
    from duplio.gui.pages.compare import ComparePage
    from duplio.gui.pages.dupes import DuplicatesPage
    from duplio.gui.pages.history import HistoryPage
    from duplio.gui.pages.home import HomePage
    from duplio.gui.pages.ingest import ImportPage
    from duplio.gui.pages.settings import SettingsPage
    from duplio.gui.pages.sync import SyncPage

    return {"home": HomePage, "browse": BrowsePage, "compare": ComparePage, "backup": BackedUpPage, "dupes": DuplicatesPage, "sync": SyncPage,
            "import": ImportPage, "history": HistoryPage, "settings": SettingsPage}


class DuplioWindow(tk.Tk):
    def __init__(self, dirs: AppDirs | None = None) -> None:
        style.enable_hidpi()
        super().__init__()
        self.title(f"Duplio {__version__}")
        self.minsize(1040, 660)
        self.ui_calls: queue.SimpleQueue[tuple[Callable[[], Any], threading.Event, list[Any]]] = queue.SimpleQueue()
        dirs = (dirs or get_app_dirs()).ensure()
        self.fonts = style.apply_style(self)
        settings = load_settings(dirs)
        registry = BackendRegistry()
        previews = PreviewService(dirs, settings.previews)
        thumbs = Thumbnailer(self, previews, image_budget=settings.previews.memory_bytes or None)
        self.ctx = Context(self, self.fonts, dirs, settings, registry, previews, thumbs, ListingCache(),
                           GuiState(dirs.state / "window.json"), Tooltip(self, self.fonts), FolderPrefetcher(previews), BrowseCache())
        registry.credential_provider = self.ctx.password_provider
        self.hash_cache: HashCache | NullCache | None = None
        self.pages: dict[str, Page] = {}
        self._classes = _page_classes()
        self.current = ""
        self._build()
        width = min(1440, max(1100, int(self.winfo_screenwidth() * 0.86)))
        height = min(960, max(700, int(self.winfo_screenheight() * 0.9)))
        self.geometry(self.ctx.state.get("geometry") or f"{width}x{height}+{(self.winfo_screenwidth() - width) // 2}+"
                      f"{max((self.winfo_screenheight() - height) // 3, 0)}")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, self._drain_ui_calls)
        self.show("home")

    # ---- layout ---------------------------------------------------------------
    def _build(self) -> None:
        f = self.fonts
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        side = tk.Frame(self, bg=PALETTE["sidebar"], width=244)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        self._logo = self._make_logo()
        if self._logo is not None:
            tk.Label(side, image=self._logo, bg=PALETTE["sidebar"]).pack(pady=(20, 4))
            try:
                self.iconphoto(True, self._logo)
            except tk.TclError:
                pass
        tk.Label(side, text=WORDMARK, font=f.heading, bg=PALETTE["sidebar"], fg=PALETTE["sidebar_text"]).pack()
        tk.Label(side, text=TAGLINE.replace(". ", ".\n"), font=f.mono, bg=PALETTE["sidebar"], fg=PALETTE["sidebar_muted"],
                 justify="center").pack(pady=(2, 18))
        self._nav: dict[str, tuple[tk.Frame, tk.Frame, tk.Label]] = {}
        for key, glyph, label in NAV:
            row = tk.Frame(side, bg=PALETTE["sidebar"], cursor="hand2")
            row.pack(fill="x")
            bar = tk.Frame(row, bg=PALETTE["sidebar"], width=3)  # lights up on the page that is open
            bar.pack(side="left", fill="y")
            item = tk.Label(row, text=f"  {glyph}   {label}", font=f.base, bg=PALETTE["sidebar"], fg=PALETTE["sidebar_text"], anchor="w",
                            padx=12, pady=11, cursor="hand2")
            item.pack(side="left", fill="x", expand=True)
            for widget in (row, bar, item):
                bind_to(widget, "<Button-1>", partial(self.show, key))
                bind_to(widget, "<Enter>", partial(self._hover, key, True))
                bind_to(widget, "<Leave>", partial(self._hover, key, False))
            self._nav[key] = (row, bar, item)
        tk.Label(side, text="Nothing is removed without\nyour OK, and everything\nis undoable.", font=f.mono, bg=PALETTE["sidebar"],
                 fg=PALETTE["sidebar_muted"], justify="left").pack(side="bottom", anchor="w", padx=20, pady=18)
        self.content = tk.Frame(self, bg=PALETTE["bg"])
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def _make_logo(self) -> tk.PhotoImage | None:
        try:
            from duplio.assets.logo import composite, rgb_of

            scale = 3
            bg = rgb_of(PALETTE["sidebar"])
            pixels = bytearray()
            grid = composite(PALETTE["sidebar"])  # the glow fades into the sidebar's own colour
            for row in grid:
                line = bytearray()
                for rgb in row:
                    line += bytes(rgb or bg) * scale
                pixels += bytes(line) * scale
            width, height = len(grid[0]) * scale, len(grid) * scale
            return tk.PhotoImage(master=self, data=b"P6\n%d %d\n255\n" % (width, height) + bytes(pixels), format="ppm")
        except Exception:
            return None

    def _hover(self, key: str, inside: bool) -> None:
        if key != self.current:
            row, bar, item = self._nav[key]
            color = PALETTE["sidebar_hover"] if inside else PALETTE["sidebar"]
            for widget in (row, bar, item):
                widget.configure(bg=color)

    # ---- pages ----------------------------------------------------------------
    def show(self, key: str) -> None:
        previous = self.pages.get(self.current)
        if previous is not None and self.current != key:
            previous.on_hide()
        if key not in self.pages:
            self.pages[key] = self._classes[key](self.content, self.ctx)
            self.pages[key].grid(row=0, column=0, sticky="nsew")
        self.pages[key].tkraise()
        self.current = key
        for name, (row, bar, item) in self._nav.items():
            active = name == key
            for widget in (row, item):
                widget.configure(bg=PALETTE["sidebar_active"] if active else PALETTE["sidebar"])
            bar.configure(bg=PALETTE["primary"] if active else PALETTE["sidebar"])
        self.pages[key].on_show()

    def _drain_ui_calls(self) -> None:
        try:
            while True:
                fn, done, box = self.ui_calls.get_nowait()
                try:
                    box.append(fn())
                except Exception:
                    log.exception("a request from a background task failed")
                finally:
                    done.set()
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_calls)

    def close(self) -> None:
        try:
            self.ctx.state.set("geometry", self.geometry())
            self.ctx.state.save()
            self.ctx.prefetcher.close()
            self.ctx.thumbs.close()
            self.ctx.previews.close()
            if self.hash_cache is not None:
                self.hash_cache.close()
            self.ctx.registry.close()
        finally:
            self._cancel_timers()
            self.destroy()

    def _cancel_timers(self) -> None:
        """Stop the window's own timers so none fires after the window is gone."""
        try:
            for ident in self.tk.splitlist(self.tk.call("after", "info")):
                self.tk.call("after", "cancel", ident)  # (not after_cancel: that also deletes commands the widgets delete again)
        except tk.TclError:
            pass



def run(dirs: AppDirs | None = None) -> str | None:
    """Open the window and keep it open until it is closed. Returns why it could not open, or ``None``."""
    try:
        window = DuplioWindow(dirs)
    except tk.TclError as exc:
        return f"The window could not open: {exc}"
    window.mainloop()
    return None
