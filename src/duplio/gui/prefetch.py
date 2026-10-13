"""Filling the picture buffer with a whole folder in the background, so scrolling a folder of thousands is smooth.

While a folder is on screen its pictures are fetched (nearest the viewer first) into the on-disk buffer. Fetching only
stores small pictures; making them into images happens only for what is on screen. It never downloads more than the
picture service would for one picture, and it stops, saying why, instead of thrashing: when the buffer is full (going on
would throw away pictures of this very folder, to be fetched again), when the disk is almost full, or when the drive stops
answering. Nothing here needs Tk.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from duplio.errors import CancelledError
from duplio.events import CancelToken
from duplio.preview.service import PICTURE_EXT, PreviewItem, Unavailable, extension

GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class PrefetchStatus:
    state: str  # idle | running | paused | finished | stopped
    total: int = 0  # pictures in the folder
    fetched: int = 0  # taken from the drive during this run
    had: int = 0  # already in the buffer
    skipped: int = 0  # left alone on purpose (cloud-only files, pictures with no small preview, too large)
    failed: int = 0  # the drive refused or failed
    reason: str = ""  # why it stopped, when it did

    @property
    def handled(self) -> int:
        return self.fetched + self.had + self.skipped + self.failed

    @property
    def ready(self) -> int:
        """Pictures that are in the buffer now."""
        return self.fetched + self.had


class FolderPrefetcher:
    def __init__(self, previews: Any, *, workers: int = 2, stop_at_fullness: float = 0.97, min_free_disk: int = GIB,
                 max_failures_in_a_row: int = 25) -> None:
        self._previews = previews
        self._stop_at = stop_at_fullness
        self._min_free = min_free_disk
        self._max_failures = max_failures_in_a_row
        self._cond = threading.Condition()
        self._items: list[PreviewItem] = []
        self._pending: deque[int] = deque()
        self._state = "idle"
        self._reason = ""
        self._generation = 0
        self._token = CancelToken()
        self._allow_online_only = False
        self._busy = 0
        self._in_a_row = 0
        self._counts = {"fetched": 0, "had": 0, "skipped": 0, "failed": 0}
        self._closing = False
        self._workers = [threading.Thread(target=self._work, name=f"duplio-folder-fetch-{n}", daemon=True) for n in range(workers)]
        for worker in self._workers:
            worker.start()

    # ---- control --------------------------------------------------------------
    def start(self, items: Sequence[PreviewItem], *, first: int = 0, allow_online_only: bool = False) -> None:
        """Fetch the pictures among ``items``, those nearest position ``first`` first. Replaces any earlier run."""
        pictures = [i for i in items if extension(i.name) in PICTURE_EXT]
        with self._cond:
            self._token.cancel("a new folder")
            self._token = CancelToken()
            self._generation += 1
            self._items = pictures
            self._allow_online_only = allow_online_only
            self._counts = {"fetched": 0, "had": 0, "skipped": 0, "failed": 0}
            self._in_a_row = 0
            self._reason = ""
            self._pending = deque(self._by_distance(range(len(pictures)), self._index_near(items, pictures, first)))
            self._state = "running" if pictures else "idle"
            self._cond.notify_all()

    @staticmethod
    def _index_near(items: Sequence[PreviewItem], pictures: Sequence[PreviewItem], first: int) -> int:
        """Where position ``first`` of the folder falls among its pictures."""
        if not pictures or first <= 0:
            return 0
        seen = 0
        for item in items[:first]:
            if extension(item.name) in PICTURE_EXT:
                seen += 1
        return min(seen, len(pictures) - 1)

    @staticmethod
    def _by_distance(indices: Any, centre: int) -> list[int]:
        return sorted(indices, key=lambda i: (abs(i - centre), i))

    def move_to(self, first: int) -> None:
        """The viewer scrolled: fetch what is near the new position next."""
        with self._cond:
            if self._pending:
                self._pending = deque(self._by_distance(self._pending, max(0, min(first, len(self._items) - 1))))

    def pause(self) -> None:
        with self._cond:
            if self._state == "running":
                self._state = "paused"

    def resume(self) -> None:
        with self._cond:
            if self._state == "paused":
                self._state = "running"
                self._cond.notify_all()

    def stop(self, reason: str = "stopped") -> None:
        with self._cond:
            self._token.cancel(reason)
            self._pending.clear()
            if self._state in ("running", "paused"):
                self._state = "stopped"
                self._reason = reason

    def status(self) -> PrefetchStatus:
        with self._cond:
            return PrefetchStatus(self._state, len(self._items), self._counts["fetched"], self._counts["had"], self._counts["skipped"],
                                  self._counts["failed"], self._reason)

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._token.cancel("closing")
            self._cond.notify_all()
        for worker in self._workers:
            worker.join(timeout=2)

    # ---- the workers ----------------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._closing and (self._state != "running" or not self._pending):
                    self._cond.wait()
                if self._closing:
                    return
                index = self._pending.popleft()
                item = self._items[index]
                generation, token = self._generation, self._token
                self._busy += 1
            try:
                self._one(item, generation, token)
            finally:
                with self._cond:
                    self._busy -= 1
                    if self._state == "running" and not self._pending and self._busy == 0 and generation == self._generation:
                        self._state = "finished"

    def _halt(self, generation: int, reason: str) -> None:
        with self._cond:
            if generation == self._generation and self._state in ("running", "paused"):
                self._state = "stopped"
                self._reason = reason
                self._pending.clear()
                self._token.cancel(reason)

    def _count(self, generation: int, name: str) -> None:
        with self._cond:
            if generation == self._generation:
                self._counts[name] += 1

    def _one(self, item: PreviewItem, generation: int, token: CancelToken) -> None:
        if token.cancelled:
            return
        try:
            store = self._previews.buffer_for(item)
            if store is None:
                self._halt(generation, "pictures from this drive are not being kept (see Settings), so there is nothing to fill")
                return
            if store.fullness >= self._stop_at:
                self._halt(generation, "the picture buffer is full; fetching more would throw away pictures of this folder to be "
                                       "fetched again. Make the buffer larger in Settings.")
                return
            free = store.free_disk()
            if free is not None and free < self._min_free:
                self._halt(generation, "the disk the pictures are kept on is almost full")
                return
            if self._previews.cached(item):
                self._count(generation, "had")
                return
            self._previews.get(item, cancel=token, allow_online_only=self._allow_online_only)
        except CancelledError:
            return
        except Unavailable:
            self._count(generation, "skipped")  # as ever: cloud-only files, huge pictures and the like are not forced
            with self._cond:
                self._in_a_row = 0
            return
        except Exception as exc:  # the drive failed this one; many in a row means it is gone
            self._count(generation, "failed")
            with self._cond:
                self._in_a_row += 1
                tripped = self._in_a_row >= self._max_failures
            if tripped:
                self._halt(generation, f"the drive stopped answering ({self._max_failures} reads in a row failed: {exc})")
            return
        self._count(generation, "fetched")
        with self._cond:
            self._in_a_row = 0
