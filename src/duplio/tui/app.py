"""The Duplio terminal application."""

from __future__ import annotations

import threading

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from duplio import __version__
from duplio.config import Settings, load_settings
from duplio.hashing.cache import HashCache, NullCache
from duplio.tui.widgets import HelpScreen, Logo, threaded_password_provider
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.text import printable
from duplio.vfs.location import BackendRegistry

DUPLIO_DARK = Theme(
    name="duplio",
    primary="#d33a3a",
    secondary="#e0a458",
    accent="#8f5bd6",
    warning="#e5b53e",
    error="#ff4d4d",
    success="#4cc38a",
    foreground="#e8e6e3",
    background="#0e0d10",
    surface="#17151a",
    panel="#211e25",
    dark=True,
)
DUPLIO_LIGHT = Theme(
    name="duplio-light",
    primary="#b3262c",
    secondary="#9a5b13",
    accent="#6b3fb3",
    warning="#a87400",
    error="#c4161c",
    success="#1d7d4f",
    foreground="#1b1a1d",
    background="#f6f4f2",
    surface="#ffffff",
    panel="#ece8e4",
    dark=False,
)

CARDS = [
    ("dedupe", "1", "Find duplicates", "Scan folders, phones and shares; review; clean up safely."),
    ("compare", "2", "Is it backed up?", "List files that have no copy in your backup locations."),
    ("ingest", "3", "Import from phone", "Copy new photos & videos into a dated library, skipping known ones."),
    ("mirror", "4", "Mirror a folder", "Keep an exact copy elsewhere; older versions are kept."),
    ("backup", "5", "Snapshot backup", "Versioned, deduplicated backups with retention and restore."),
    ("history", "6", "History & undo", "Every change Duplio made — undo, or empty the quarantine."),
]


class HomeScreen(Screen[None]):
    BINDINGS = [
        Binding("1", "open('dedupe')", "Duplicates", show=False),
        Binding("2", "open('compare')", "Backed up?", show=False),
        Binding("3", "open('ingest')", "Import", show=False),
        Binding("4", "open('mirror')", "Mirror", show=False),
        Binding("5", "open('backup')", "Backup", show=False),
        Binding("6", "open('history')", "History", show=False),
        Binding("f5", "refresh_devices", "Refresh devices"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="home"):
            with Vertical(id="home-left"):
                yield Logo(id="logo")
                with Grid(id="cards"):
                    for key, number, title, desc in CARDS:
                        with Vertical(classes="card"):
                            yield Button(f"{number}  {title}", id=f"card-{key}", classes="card-button")
                            yield Static(Text(desc), classes="card-desc")
            with Vertical(id="home-right"):
                yield Label("Connected devices", classes="section-title")
                yield DataTable(id="devices", cursor_type="row", zebra_stripes=True)
                yield Static("", id="devices-tip", classes="muted")
                yield Button("Refresh  (F5)", id="refresh-devices")
                yield Label("Safety", classes="section-title")
                yield Static(
                    Text(
                        "Nothing is removed without your review. Files are re-verified the moment before any change, "
                        "every change is journaled, and the default action — quarantine — is instantly undoable.",
                    ),
                    classes="muted",
                )
        yield Footer()

    def on_resize(self, event: events.Resize) -> None:
        # The mark is 20 rows tall; on short terminals the workflow cards matter more.
        self.query_one("#logo", Logo).display = event.size.height >= 42

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.add_columns("Kind", "Device", "Location")
        self.action_refresh_devices()
        self.query_one("#card-dedupe", Button).focus()

    def action_refresh_devices(self) -> None:
        self.load_devices()

    @work(thread=True, exclusive=True, group="devices")
    def load_devices(self) -> None:
        from duplio.devices import discover, platform_tip

        try:
            found = discover()
        except Exception as exc:  # discovery must never break the home screen
            found = []
            tip = f"Device discovery failed: {exc}"
        else:
            tip = platform_tip()
        self.app.call_from_thread(self._show_devices, found, tip)

    def _show_devices(self, found, tip: str) -> None:  # type: ignore[no-untyped-def]
        table = self.query_one("#devices", DataTable)
        table.clear()
        for dev in found:
            table.add_row(Text(dev.kind, style="green" if dev.ready else "yellow"), Text(dev.name), Text(dev.location))
        if not found:
            table.add_row(Text("—", style="dim"), Text("nothing detected", style="dim"), Text(""))
        self.query_one("#devices-tip", Static).update(Text(tip))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("card-"):
            self.action_open(button_id[5:])
        elif button_id == "refresh-devices":
            self.action_refresh_devices()

    def action_open(self, key: str) -> None:
        from duplio.tui.dedupe import SetupScreen
        from duplio.tui.tools import BackupScreen, HistoryScreen, IngestScreen, MirrorScreen

        screens = {
            "dedupe": lambda: SetupScreen("dedupe"),
            "compare": lambda: SetupScreen("compare"),
            "ingest": IngestScreen,
            "mirror": MirrorScreen,
            "backup": BackupScreen,
            "history": HistoryScreen,
        }
        self.app.push_screen(screens[key]())


class DuplioApp(App[None]):
    CSS_PATH = "duplio.tcss"
    TITLE = "Duplio"
    SUB_TITLE = f"safe duplicate finder & backup · v{__version__}"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("question_mark", "help", "Help"),
        Binding("f2", "toggle_theme", "Light/Dark"),
        Binding("ctrl+h", "home", "Home", show=False),
    ]

    def __init__(self, *, dirs: AppDirs | None = None) -> None:
        super().__init__()
        self.dirs = (dirs or get_app_dirs()).ensure()
        self.settings: Settings = load_settings(self.dirs)
        self.registry = BackendRegistry(credential_provider=threaded_password_provider(self))
        self._cache: HashCache | None = None
        self._cache_lock = threading.Lock()

    @property
    def cache(self) -> HashCache:
        with self._cache_lock:
            if self._cache is None:
                try:
                    self._cache = HashCache(self.dirs.hash_cache) if self.settings.cache else NullCache()
                except Exception:
                    self._cache = NullCache()
            return self._cache

    def on_mount(self) -> None:
        self.register_theme(DUPLIO_DARK)
        self.register_theme(DUPLIO_LIGHT)
        self.theme = "duplio"
        self.push_screen(HomeScreen())

    def notify(self, message: str, *, markup: bool = False, **kwargs: object) -> None:  # type: ignore[override]
        """Messages carry file names and OS errors (``[Errno 2]``…): never parse them as markup."""
        super().notify(printable(message, keep_newlines=True), markup=False, **kwargs)  # type: ignore[arg-type]

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_theme(self) -> None:
        self.theme = "duplio-light" if self.theme == "duplio" else "duplio"

    def action_home(self) -> None:
        self.go_home()

    def go_home(self) -> None:
        while len(self.screen_stack) > 2 and not isinstance(self.screen, HomeScreen):
            self.pop_screen()

    def on_unmount(self) -> None:
        with self._cache_lock:
            if self._cache is not None:
                try:
                    self._cache.close()
                except Exception:
                    pass
        self.registry.close()


def run(dirs: AppDirs | None = None) -> None:
    DuplioApp(dirs=dirs).run()


__all__ = ["DuplioApp", "HomeScreen", "run"]
