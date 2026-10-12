"""Work that takes a while runs on a thread; the window shows its progress and can ask it to stop."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from duplio.errors import CancelledError, DuplioError
from duplio.events import CancelToken

log = logging.getLogger("duplio.gui")


@dataclass(frozen=True)
class Snapshot:
    stage: str
    done_items: int
    total_items: int | None
    done_bytes: int
    total_bytes: int | None
    detail: str
    messages: tuple[tuple[str, str], ...]
    elapsed: float

    @property
    def fraction(self) -> float | None:
        if self.total_items:
            return min(self.done_items / self.total_items, 1.0)
        if self.total_bytes:
            return min(self.done_bytes / self.total_bytes, 1.0)
        return None


class GuiProgress:
    """A ``ProgressSink`` that only remembers, so the window can read it from its own thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stage = "Starting"
        self._total_items: int | None = None
        self._total_bytes: int | None = None
        self._items = 0
        self._bytes = 0
        self._detail = ""
        self._messages: deque[tuple[str, str]] = deque(maxlen=200)
        self._started = time.monotonic()

    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        with self._lock:
            self._stage, self._total_items, self._total_bytes = name, total_items, total_bytes
            self._items = self._bytes = 0
            self._detail = ""

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        with self._lock:
            self._items += items
            self._bytes += nbytes
            if detail is not None:
                self._detail = detail

    def message(self, text: str, *, level: str = "info") -> None:
        with self._lock:
            self._messages.append((level, text))

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._stage, self._items, self._total_items, self._bytes, self._total_bytes, self._detail,
                            tuple(self._messages), time.monotonic() - self._started)


class Job:
    """One piece of background work and what became of it."""

    def __init__(self, title: str, fn: Callable[[GuiProgress, CancelToken], Any]) -> None:
        self.title = title
        self.progress = GuiProgress()
        self.cancel = CancelToken()
        self.result: Any = None
        self.error: BaseException | None = None
        self.cancelled = False
        self._fn = fn
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"duplio-job-{title}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def _run(self) -> None:
        try:
            self.result = self._fn(self.progress, self.cancel)
        except CancelledError:
            self.cancelled = True
        except BaseException as exc:  # reported to the window, never lost
            self.error = exc
            if not isinstance(exc, DuplioError):
                log.error("job %r failed\n%s", self.title, traceback.format_exc())
        finally:
            self._done.set()


def explain(error: BaseException) -> str:
    """What to tell a person about an error (Duplio's own errors are already written for people)."""
    if isinstance(error, DuplioError):
        return str(error)
    if isinstance(error, PermissionError):
        return f"Permission was refused: {error}"
    if isinstance(error, FileNotFoundError):
        return f"A folder or file could not be found: {error}"
    if isinstance(error, OSError):
        return f"The drive reported a problem: {error}"
    return f"Something unexpected went wrong ({type(error).__name__}: {error}). Details are in Duplio's log file."
