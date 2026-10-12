"""A folder browser for any drive: this computer, USB sticks, phones, and network servers."""

from __future__ import annotations

import os

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from duplio.tui.widgets import location_suggestions, ptext
from duplio.vfs.listing import ListingCache
from duplio.vfs.location import BackendRegistry, Location

UP = "\x00up"


class FolderPicker(ModalScreen[str | None]):
    """Walk through folders and choose one. Returns the folder's address, or ``None`` when cancelled.

    Folders on slow or remote drives are listed once and remembered for a couple of minutes, so going up and down a tree
    on a NAS or cloud drive does not ask it the same question again. ``r`` asks the drive again.
    """

    DEFAULT_CSS = """
    FolderPicker { align: center middle; }
    FolderPicker > Vertical { width: 90%; max-width: 110; height: 90%; padding: 1 2; border: thick $accent; background: $panel; }
    FolderPicker #fp-title { text-style: bold; margin-bottom: 1; }
    FolderPicker #fp-where { margin-bottom: 1; }
    FolderPicker #fp-status { height: auto; margin-bottom: 1; color: $text-muted; }
    FolderPicker OptionList { height: 1fr; margin-bottom: 1; }
    FolderPicker Horizontal { height: auto; align-horizontal: right; }
    FolderPicker Button { margin-left: 1; }
    FolderPicker #fp-places { width: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace", "up", "Up one folder", show=False),
        Binding("f5", "refresh", "Refresh"),
        Binding("ctrl+s", "choose", "Use this folder"),
    ]

    def __init__(self, registry: BackendRegistry, listings: ListingCache, *, start: str = "", title: str = "Choose a folder") -> None:
        super().__init__()
        self.registry = registry
        self.listings = listings
        self.start = start.strip() or os.path.expanduser("~")
        self.heading = title
        self.here: Location | None = None
        self._request = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, id="fp-title")
            yield Input(value=self.start, placeholder="A folder, or sftp://user@host/path, davs://host/path, smb://server/share ...", id="fp-where")
            yield Static("", id="fp-status")
            yield OptionList(id="fp-list")
            with Horizontal():
                yield Select([(label, loc) for label, loc in location_suggestions()], prompt="Places", id="fp-places", allow_blank=True)
                yield Button("Up", id="fp-up")
                yield Button("Refresh  (F5)", id="fp-refresh")
                yield Button("Cancel", id="fp-cancel")
                yield Button("Use this folder  (Ctrl+S)", id="fp-choose", variant="primary")

    def on_mount(self) -> None:
        self.go(self.start)
        self.query_one("#fp-list", OptionList).focus()

    # ---- going places -------------------------------------------------
    def go(self, text: str, *, refresh: bool = False) -> None:
        self._request += 1
        self.query_one("#fp-status", Static).update("Reading the folder…  (a network drive can take a moment)")
        self._load(text, self._request, refresh)

    @work(thread=True, exclusive=True, group="picker")
    def _load(self, text: str, request: int, refresh: bool) -> None:
        try:
            loc = self.registry.open(text)
            info = loc.backend.stat(loc.path)
            if info is None:
                raise FileNotFoundError(f"{loc.display} does not exist")
            if not info.is_dir:
                raise NotADirectoryError(f"{loc.display} is not a folder")
            entries = self.listings.list(loc.backend, loc.path, refresh=refresh)
        except Exception as exc:
            self.app.call_from_thread(self._failed, request, str(exc) or type(exc).__name__)
            return
        self.app.call_from_thread(self._show, request, loc, entries)

    def _failed(self, request: int, message: str) -> None:
        if request != self._request:
            return
        self.query_one("#fp-status", Static).update(ptext(f"Cannot open that: {message}", style="red"))

    def _show(self, request: int, loc: Location, entries: list[tuple[str, bool]]) -> None:
        if request != self._request:
            return
        self.here = loc
        folders = sorted((n for n, is_dir in entries if is_dir), key=lambda n: n.casefold())
        files = sum(1 for _, is_dir in entries if not is_dir)
        where = self.query_one("#fp-where", Input)
        where.value = loc.display
        options: list[Option] = []
        if loc.backend.parent(loc.path) != loc.path:
            options.append(Option(Text("↑  .. (up one folder)", style="bold"), id=UP))
        options.extend(Option(ptext(f"▸  {name}"), id=name) for name in folders)
        listing = self.query_one("#fp-list", OptionList)
        listing.clear_options()
        listing.add_options(options)
        if options:
            listing.highlighted = 1 if len(options) > 1 and options[0].id == UP else 0
        caps = loc.caps
        note = "" if caps.read_cost == 0 else "  ·  remembered for a couple of minutes (F5 to ask the drive again)"
        self.query_one("#fp-status", Static).update(
            ptext(f"{len(folders)} folders, {files} files here  ·  {caps.label}{note}")
        )

    # ---- actions ------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "fp-where":
            self.go(event.value)
            self.query_one("#fp-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.here is None:
            return
        if event.option.id == UP:
            self.action_up()
        elif event.option.id:
            child = self.here.backend.join(self.here.path, str(event.option.id))
            self.go(self.here.backend.uri(child))

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "fp-places" and isinstance(event.value, str):
            self.go(event.value)

    def action_up(self) -> None:
        if self.here is None:
            return
        above = self.here.backend.parent(self.here.path)
        if above != self.here.path:
            self.go(self.here.backend.uri(above))

    def action_refresh(self) -> None:
        if self.here is not None:
            self.listings.invalidate(self.here.backend, self.here.path)
            self.go(self.here.backend.uri(self.here.path), refresh=True)

    def action_choose(self) -> None:
        if self.here is None:
            self.query_one("#fp-status", Static).update(Text("Open a folder first.", style="yellow"))
            return
        self.dismiss(self.here.uri)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {"fp-up": self.action_up, "fp-refresh": self.action_refresh, "fp-choose": self.action_choose,
                   "fp-cancel": self.action_cancel}
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
