"""Thread-safe progress model polled by the UI (no event flood from workers)."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ProgressBar, RichLog

from duplio.util.fmt import human_duration, human_size


@dataclass
class Snapshot:
    stage: str
    total_items: int | None
    total_bytes: int | None
    items: int
    nbytes: int
    detail: str
    stage_elapsed: float
    finished_stages: list[str]
    messages: list[tuple[str, str]] = field(default_factory=list)


class PolledProgress:
    """Implements :class:`duplio.events.ProgressSink`; the UI reads :meth:`snapshot`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = "Starting…"
        self._total_items: int | None = None
        self._total_bytes: int | None = None
        self._items = 0
        self._bytes = 0
        self._detail = ""
        self._started = time.monotonic()
        self._finished: list[str] = []
        self._pending: deque[tuple[str, str]] = deque(maxlen=500)
        self.all_messages: list[tuple[str, str]] = []

    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        with self._lock:
            if self._stage and self._stage != "Starting…":
                self._finished.append(f"{self._stage} ({human_duration(time.monotonic() - self._started)})")
            self._stage = name
            self._total_items = total_items
            self._total_bytes = total_bytes
            self._items = 0
            self._bytes = 0
            self._detail = ""
            self._started = time.monotonic()

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        with self._lock:
            self._items += items
            self._bytes += nbytes
            if detail is not None:
                self._detail = detail

    def message(self, text: str, *, level: str = "info") -> None:
        with self._lock:
            self._pending.append((level, text))
            self.all_messages.append((level, text))

    def snapshot(self) -> Snapshot:
        with self._lock:
            msgs = list(self._pending)
            self._pending.clear()
            return Snapshot(
                stage=self._stage,
                total_items=self._total_items,
                total_bytes=self._total_bytes,
                items=self._items,
                nbytes=self._bytes,
                detail=self._detail,
                stage_elapsed=time.monotonic() - self._started,
                finished_stages=list(self._finished),
                messages=msgs,
            )


class ProgressPanel(Vertical):
    """Stage title, bar, counters, and a scrolling log of warnings."""

    DEFAULT_CSS = """
    ProgressPanel { height: auto; padding: 1 2; border: round $primary; }
    ProgressPanel #pp-stage { text-style: bold; color: $text; }
    ProgressPanel #pp-done { color: $text-muted; }
    ProgressPanel #pp-counts { color: $secondary; }
    ProgressPanel #pp-detail { color: $text-muted; }
    ProgressPanel ProgressBar { width: 100%; margin: 1 0; }
    ProgressPanel RichLog { height: 8; border: none; background: $surface; }
    """

    def __init__(self, progress: PolledProgress, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.progress = progress

    def compose(self) -> ComposeResult:
        yield Label("", id="pp-done", markup=False)
        yield Label("Starting…", id="pp-stage", markup=False)
        yield ProgressBar(total=None, show_eta=True, id="pp-bar")
        yield Label("", id="pp-counts", markup=False)
        yield Label("", id="pp-detail", markup=False)
        yield RichLog(id="pp-log", wrap=True, markup=False, highlight=False)

    def on_mount(self) -> None:
        self.set_interval(0.15, self.refresh_view)

    def refresh_view(self) -> None:
        snap = self.progress.snapshot()
        self.query_one("#pp-stage", Label).update(snap.stage)
        self.query_one("#pp-done", Label).update("  ✓ ".join(["", *snap.finished_stages]).strip() if snap.finished_stages else "")
        bar = self.query_one("#pp-bar", ProgressBar)
        if snap.total_bytes:
            bar.update(total=snap.total_bytes, progress=min(snap.nbytes, snap.total_bytes))
        elif snap.total_items:
            bar.update(total=snap.total_items, progress=min(snap.items, snap.total_items))
        else:
            bar.update(total=None)
        parts = []
        if snap.total_items:
            parts.append(f"{snap.items:,} / {snap.total_items:,} files")
        elif snap.items:
            parts.append(f"{snap.items:,} files")
        if snap.nbytes:
            parts.append(human_size(snap.nbytes))
            if snap.stage_elapsed > 0.5:
                parts.append(f"{human_size(snap.nbytes / snap.stage_elapsed)}/s")
        parts.append(human_duration(snap.stage_elapsed))
        self.query_one("#pp-counts", Label).update(" · ".join(parts))
        detail = snap.detail
        if len(detail) > 90:
            detail = "…" + detail[-89:]
        self.query_one("#pp-detail", Label).update(detail)
        log = self.query_one("#pp-log", RichLog)
        for level, text in snap.messages:
            style = {"warning": "yellow", "error": "bold red"}.get(level, "dim")
            log.write(Text(text, style=style))
