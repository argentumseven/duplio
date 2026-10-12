"""Rich progress display implementing :class:`duplio.events.ProgressSink`."""

from __future__ import annotations

import threading
import time

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from duplio.util.fmt import human_size


class _CountColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        items = task.fields.get("items", 0)
        total_items = task.fields.get("total_items")
        nbytes = task.fields.get("nbytes", 0)
        parts = []
        if total_items:
            parts.append(f"{items:,}/{total_items:,}")
        elif items:
            parts.append(f"{items:,}")
        if nbytes:
            parts.append(human_size(nbytes))
            elapsed = task.elapsed or 0
            if elapsed > 0.5:
                parts.append(f"{human_size(nbytes / elapsed)}/s")
        return Text(" · ".join(parts), style="cyan")


class _DetailColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        detail = str(task.fields.get("detail") or "")
        if len(detail) > 48:
            detail = "…" + detail[-47:]
        return Text(detail, style="dim")


class RichProgress:
    """Context manager; one bar per stage, previous stages stay visible as done."""

    def __init__(self, console: Console, *, enabled: bool = True) -> None:
        self.console = console
        self.enabled = enabled and console.is_terminal
        self._progress = Progress(
            SpinnerColumn(style="red"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None, complete_style="red", finished_style="green"),
            _CountColumn(),
            TimeElapsedColumn(),
            _DetailColumn(),
            console=console,
            transient=False,
            disable=not self.enabled,
            refresh_per_second=8,
        )
        self._task: TaskID | None = None
        self._by_bytes = False
        self._items = 0
        self._bytes = 0
        self._lock = threading.Lock()
        self._last_detail = 0.0
        self.messages: list[tuple[str, str]] = []

    def __enter__(self) -> RichProgress:
        self._progress.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._finish_task()
        self._progress.__exit__(*exc)  # type: ignore[arg-type]

    def _finish_task(self) -> None:
        if self._task is not None:
            task = self._progress.tasks[-1]
            if task.total is None:
                self._progress.update(self._task, total=max(task.completed, 1), completed=max(task.completed, 1))
            else:
                self._progress.update(self._task, completed=task.total)
            self._progress.update(self._task, detail="")
            self._task = None

    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        with self._lock:
            self._finish_task()
            self._by_bytes = bool(total_bytes)
            self._items = 0
            self._bytes = 0
            total = total_bytes if total_bytes else total_items
            self._task = self._progress.add_task(
                name, total=total or None, items=0, nbytes=0, total_items=total_items, detail=""
            )

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        with self._lock:
            if self._task is None:
                return
            self._items += items
            self._bytes += nbytes
            step = nbytes if self._by_bytes else items
            fields: dict[str, object] = {"items": self._items, "nbytes": self._bytes}
            now = time.monotonic()
            if detail is not None and now - self._last_detail > 0.2:
                fields["detail"] = detail
                self._last_detail = now
            self._progress.update(self._task, advance=step, **fields)

    def message(self, text: str, *, level: str = "info") -> None:
        self.messages.append((level, text))
        style = {"warning": "yellow", "error": "bold red"}.get(level, "dim")
        self._progress.console.print(Text(text, style=style))
