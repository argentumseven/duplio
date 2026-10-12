"""Progress reporting and cancellation shared by every long-running operation.

Engines never print. They report through a :class:`ProgressSink` so the same
code drives the Rich CLI, the Textual TUI, JSON output and tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from duplio.errors import CancelledError


class ProgressSink(Protocol):
    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        """A new stage starts; totals may be unknown (``None``)."""

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        """Work completed within the current stage."""

    def message(self, text: str, *, level: str = "info") -> None:
        """A human-readable note: ``info``, ``warning`` or ``error``."""


class NullProgress:
    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        pass

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        pass

    def message(self, text: str, *, level: str = "info") -> None:
        pass


@dataclass
class RecordingProgress:
    """Collects everything; handy for tests and for post-run summaries."""

    stages: list[str] = field(default_factory=list)
    items: int = 0
    nbytes: int = 0
    messages: list[tuple[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        with self._lock:
            self.stages.append(name)

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        with self._lock:
            self.items += items
            self.nbytes += nbytes

    def message(self, text: str, *, level: str = "info") -> None:
        with self._lock:
            self.messages.append((level, text))

    def warnings(self) -> list[str]:
        return [text for level, text in self.messages if level in ("warning", "error")]


class CancelToken:
    """Cooperative cancellation; workers call :meth:`check` between units of work."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled by user") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CancelledError(self.reason or "cancelled")


class Stopwatch:
    def __init__(self) -> None:
        self.start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start
