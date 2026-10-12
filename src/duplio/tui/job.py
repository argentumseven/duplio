"""Generic screen that runs a long job in a worker thread with live progress."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import RenderableType
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from duplio.errors import CancelledError, DuplioError
from duplio.events import CancelToken
from duplio.tui.progress import PolledProgress, ProgressPanel

JobFn = Callable[[PolledProgress, CancelToken], Any]
Summarise = Callable[[Any], RenderableType]


class JobScreen(Screen[Any]):
    """Run ``fn(progress, cancel)``; show ``summarise(result)`` when it finishes.

    ``on_done(result)`` (optional) may replace this screen with a follow-up.
    """

    BINDINGS = [("escape", "cancel_or_back", "Cancel / back")]

    def __init__(self, title: str, fn: JobFn, *, summarise: Summarise | None = None,
                 on_done: Callable[[Any], None] | None = None, subtitle: str = "") -> None:
        super().__init__()
        self.job_title = title
        self.subtitle = subtitle
        self.fn = fn
        self.summarise = summarise
        self.on_done = on_done
        self.progress = PolledProgress()
        self.cancel = CancelToken()
        self.finished = False
        self.result: Any = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="job"):
            yield Label(self.job_title, id="job-title", markup=False)
            if self.subtitle:
                yield Label(self.subtitle, id="job-subtitle", markup=False)
            yield ProgressPanel(self.progress, id="job-progress")
            with VerticalScroll(id="job-result-wrap"):
                yield Static("", id="job-result")
            with Horizontal(id="job-buttons"):
                yield Button("Cancel", id="job-cancel", variant="warning")
                yield Button("Done", id="job-done", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_job()

    @work(thread=True, exclusive=True)
    def run_job(self) -> None:
        try:
            result = self.fn(self.progress, self.cancel)
        except CancelledError:
            self.app.call_from_thread(self._finish, None, Text("Cancelled. Work already finished is kept and journaled.", style="yellow"))
            return
        except DuplioError as exc:
            self.app.call_from_thread(self._finish, None, Text(f"Stopped: {exc}", style="bold red"))
            return
        except Exception as exc:  # unexpected: show it rather than crash the UI
            self.app.call_from_thread(self._finish, None, Text(f"Unexpected error: {type(exc).__name__}: {exc}", style="bold red"))
            return
        rendered = self.summarise(result) if self.summarise else Text("Finished.", style="green")
        self.app.call_from_thread(self._finish, result, rendered)

    def _finish(self, result: Any, rendered: RenderableType) -> None:
        self.finished = True
        self.result = result
        self.query_one("#job-progress", ProgressPanel).refresh_view()
        self.query_one("#job-result", Static).update(rendered)
        self.query_one("#job-cancel", Button).disabled = True
        done = self.query_one("#job-done", Button)
        done.disabled = False
        done.focus()
        if self.on_done is not None and result is not None:
            self.on_done(result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "job-cancel":
            self.cancel.cancel()
            event.button.disabled = True
            event.button.label = "Cancelling…"
        elif event.button.id == "job-done":
            self.dismiss(self.result)

    def action_cancel_or_back(self) -> None:
        if self.finished:
            self.dismiss(self.result)
        else:
            self.cancel.cancel()
