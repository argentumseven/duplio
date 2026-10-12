"""Shared widgets: the logo, location helpers and dialogs."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from duplio.assets.logo import logo_text
from duplio.util.text import printable


class Logo(Static):
    """The Duplio mark (see ``duplio.assets.logo``)."""

    DEFAULT_CSS = """
    Logo { width: auto; height: auto; padding: 0 2; }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(logo_text(), **kwargs)  # type: ignore[arg-type]


def common_folders() -> list[tuple[str, str]]:
    """Well-known folders that exist on this machine."""
    home = Path.home()
    candidates = [
        ("Home", home),
        ("Pictures", home / "Pictures"),
        ("Downloads", home / "Downloads"),
        ("Documents", home / "Documents"),
        ("Videos", home / "Videos"),
        ("Music", home / "Music"),
        ("Desktop", home / "Desktop"),
    ]
    return [(label, str(path)) for label, path in candidates if path.is_dir()]


def location_suggestions() -> list[tuple[str, str]]:
    """``(label, location)`` for detected devices and common folders."""
    from duplio.devices import discover

    out = []
    try:
        for dev in discover():
            if dev.ready:
                out.append((f"{dev.kind}: {dev.name}", dev.location))
    except Exception:
        pass
    out.extend((f"folder: {label}", path) for label, path in common_folders())
    return out


def short(path: str, width: int = 70) -> str:
    home = os.path.expanduser("~")
    path = printable(path)
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= width else "…" + path[-(width - 1):]


def ptext(value: object, **kwargs: object) -> Text:
    """Text for untrusted content (file names, error messages): safe to draw on any terminal."""
    return Text(printable(str(value)), **kwargs)  # type: ignore[arg-type]


class ConfirmDialog(ModalScreen[bool]):
    """Yes/no question; optionally requires typing a word (for permanent deletion)."""

    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; }
    ConfirmDialog > Vertical { width: 76; height: auto; padding: 1 2; border: thick $error; background: $panel; }
    ConfirmDialog #cd-title { text-style: bold; margin-bottom: 1; }
    ConfirmDialog Horizontal { height: auto; margin-top: 1; align-horizontal: right; }
    ConfirmDialog Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, *, confirm_label: str = "Continue", require_word: str | None = None,
                 danger: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.confirm_label = confirm_label
        self.require_word = require_word
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, id="cd-title", markup=False)
            yield Static(Text(self.body))
            if self.require_word:
                yield Label(f"Type {self.require_word} to confirm:", markup=False)
                yield Input(id="cd-word")
            with Horizontal():
                yield Button("Cancel", id="cd-cancel")
                yield Button(self.confirm_label, id="cd-ok", variant="error" if self.danger else "primary",
                             disabled=bool(self.require_word))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cd-word":
            self.query_one("#cd-ok", Button).disabled = event.value.strip() != self.require_word

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "cd-ok")


class PasswordDialog(ModalScreen[str | None]):
    DEFAULT_CSS = """
    PasswordDialog { align: center middle; }
    PasswordDialog > Vertical { width: 60; height: auto; padding: 1 2; border: thick $primary; background: $panel; }
    PasswordDialog Horizontal { height: auto; margin-top: 1; align-horizontal: right; }
    """

    def __init__(self, server: str, username: str | None) -> None:
        super().__init__()
        self.server = server
        self.username = username

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Password for {self.username or 'guest'} on {self.server}", markup=False)
            yield Input(password=True, id="pw")
            with Horizontal():
                yield Button("Cancel", id="pw-cancel")
                yield Button("Connect", id="pw-ok", variant="primary")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one("#pw", Input).value if event.button.id == "pw-ok" else None)


def threaded_password_provider(app) -> Callable[[str, str | None], str | None]:  # type: ignore[no-untyped-def]
    """Credential callback usable from worker threads: shows a dialog and waits."""

    def provider(server: str, username: str | None) -> str | None:
        if threading.current_thread() is threading.main_thread():
            return None
        done = threading.Event()
        result: list[str | None] = [None]

        def show() -> None:
            def got(value: str | None) -> None:
                result[0] = value
                done.set()

            app.push_screen(PasswordDialog(server, username), got)

        app.call_from_thread(show)
        done.wait(timeout=600)
        return result[0]

    return provider


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 90; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $panel; }
    """
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        text = Text()
        text.append("How Duplio keeps your data safe\n\n", style="bold")
        for line in (
            "• Nothing changes until you review a plan and confirm it.",
            "• Copies in reference locations, protected paths (photo libraries, app bundles, Android/data…) "
            "and read-only storage are never removed.",
            "• Right before touching a file, Duplio re-checks it and re-hashes both it and the kept copy.",
            "• Every change is written to a journal first; 'History' can undo quarantines, hard links and clones.",
            "• Quarantine (the default) is an instant move into .duplio-quarantine next to your files.",
            "• If removing one path makes the kept copy vanish, the change is rolled back automatically.",
        ):
            text.append(line + "\n")
        text.append("\nKeys in the review screen\n\n", style="bold")
        for key, desc in (
            ("space", "toggle keep / remove for the highlighted file"),
            ("k", "keep only the highlighted file in this group"),
            ("x", "include / exclude the whole group from the plan"),
            ("r", "re-apply the keep rules to every group"),
            ("p", "review the plan"),
            ("tab", "move between the group list and the file list"),
        ):
            text.append(f"  {key:<7}", style="bold")
            text.append(desc + "\n")
        with Vertical():
            yield Static(text)
            yield Button("Close", id="help-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
