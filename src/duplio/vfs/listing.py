"""Remembering folder listings for a short while, so browsing a slow drive does not list the same folder again.

Used where a person walks around a drive (the folder browser). It is deliberately never used for scans, comparisons or
anything that decides what to change: those always look at the drive itself. A listing here is only a convenience for
choosing where to go, and "refresh" always asks the drive again.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from duplio.vfs.base import Backend

Entries = list[tuple[str, bool]]


class ListingCache:
    def __init__(self, ttl: float = 120.0, max_folders: int = 256, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_folders = max_folders
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], tuple[float, Entries]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def list(self, backend: Backend, path: str, *, refresh: bool = False) -> Entries:
        """``(name, is_dir)`` for a folder. Fast local disks are always read fresh; slow, remote and cloud drives reuse a
        listing taken within ``ttl`` seconds unless ``refresh`` is set."""
        if backend.capabilities(path).read_cost == 0:
            return backend.listdir(path)
        key = (backend.id, backend.normalize(path))
        now = self._clock()
        if not refresh:
            with self._lock:
                found = self._entries.get(key)
                if found is not None and now - found[0] <= self.ttl:
                    self._entries.move_to_end(key)
                    self.hits += 1
                    return list(found[1])
        entries = backend.listdir(path)
        with self._lock:
            self.misses += 1
            self._entries[key] = (self._clock(), list(entries))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_folders:
                self._entries.popitem(last=False)
        return entries

    def invalidate(self, backend: Backend | None = None, path: str | None = None) -> None:
        """Forget one folder, everything on one drive, or (with no arguments) everything."""
        with self._lock:
            if backend is None:
                self._entries.clear()
                return
            for key in [k for k in self._entries if k[0] == backend.id and (path is None or k[1] == backend.normalize(path))]:
                del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
