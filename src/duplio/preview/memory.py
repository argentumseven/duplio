"""The hot tier of the picture buffer: finished thumbnails held in memory.

A picture that has been fetched goes here first. Only when memory is full does a picture go on to the disk buffer (the warm
tier, see ``store.py``), and only when that is full are the oldest ones let go. A picture is in one tier or the other, not
both. Pictures of the folder on screen are never pushed out to make room for each other: when memory is full of them, the
next one goes straight to disk instead, so the part nearest the viewer (which is fetched first) stays at hand in memory.

Nothing here touches the disk or Tk; the caller writes what this hands back to the disk buffer.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import NamedTuple

from duplio.preview.store import StoredThumb, ThumbStore

OVERHEAD = 200  # bytes counted for each held picture besides its data (the bookkeeping around it)


class Spilled(NamedTuple):
    """A picture that left memory: to go on to the disk buffer when ``demote`` (its drive is one that is kept on disk)."""

    key: str
    drive: str
    thumb: StoredThumb
    demote: bool


class _Held(NamedTuple):
    thumb: StoredThumb
    drive: str
    demote: bool
    cost: int


class MemoryTier:
    """Thumbnails in memory, up to a byte limit, least recently used first out (the folder on screen last)."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        self._items: OrderedDict[str, _Held] = OrderedDict()  # oldest first
        self._bytes = 0
        self._pinned: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ access
    def get(self, key: str) -> StoredThumb | None:
        with self._lock:
            held = self._items.get(key)
            if held is None:
                return None
            self._items.move_to_end(key)
            return held.thumb

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._items

    def put(self, key: str, drive: str, thumb: StoredThumb, *, demote: bool) -> list[Spilled]:
        """Hold a picture. Returns the pictures that no longer fit (possibly this one), for the caller to move on or drop."""
        cost = len(thumb.data) + OVERHEAD
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old.cost
            self._items[key] = _Held(thumb, drive, demote, cost)
            self._bytes += cost
            if self._bytes <= self.max_bytes:
                return []
            return self._make_room(newcomer=key, allow_pinned=False)

    def _make_room(self, *, newcomer: str | None, allow_pinned: bool) -> list[Spilled]:
        """Let pictures go until the tier is a little under its limit (caller holds the lock).

        The ones not on screen go first, least recently used first. A picture of the folder on screen is only ever let go
        when ``allow_pinned`` (the limit itself was lowered); otherwise, if nothing else is left to go, the newcomer is the one
        that moves on, so what is already here stays."""
        target = int(self.max_bytes * 0.9)
        out: list[Spilled] = []
        pinned = self._pinned
        for key in list(self._items):
            if self._bytes <= target:
                break
            if key == newcomer or key in pinned:
                continue
            out.append(self._drop(key))
        if self._bytes > self.max_bytes and newcomer is not None and newcomer in self._items:
            out.append(self._drop(newcomer))  # nothing else could go: this one does not fit
        if allow_pinned and self._bytes > self.max_bytes:
            for key in list(self._items):
                if self._bytes <= target:
                    break
                out.append(self._drop(key))
        return out

    def _drop(self, key: str) -> Spilled:
        held = self._items.pop(key)
        self._bytes -= held.cost
        return Spilled(key, held.drive, held.thumb, held.demote)

    # ------------------------------------------------------------------ housekeeping
    def set_limit(self, max_bytes: int) -> list[Spilled]:
        """Change the limit; a lower one pushes out what no longer fits (the folder on screen last of all)."""
        with self._lock:
            self.max_bytes = max(0, max_bytes)
            if self._bytes <= self.max_bytes:
                return []
            return self._make_room(newcomer=None, allow_pinned=True)

    def pin(self, keys: set[str] | frozenset[str]) -> None:
        """The pictures of the folder on screen: kept while anything else can go instead."""
        with self._lock:
            self._pinned = frozenset(keys)

    def pinned_bytes(self) -> int:
        with self._lock:
            pinned = self._pinned
            return sum(held.cost for key, held in self._items.items() if key in pinned)

    def take_all(self, *, newest_first: bool = True) -> list[Spilled]:
        """Empty the tier and hand back everything in it (to move it on to the disk buffer when the program closes)."""
        with self._lock:
            keys = list(self._items)
            if newest_first:
                keys.reverse()
            out = [self._drop(key) for key in keys]
            self._bytes = 0
            return out

    def clear(self) -> int:
        with self._lock:
            removed = len(self._items)
            self._items.clear()
            self._bytes = 0
            return removed

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._bytes


class BufferView:
    """The memory tier and the disk buffer of a drive's pictures seen as one buffer: what the background fetch (is it full?)
    and the status line (how much is in each?) ask about."""

    def __init__(self, hot: MemoryTier, warm: ThumbStore) -> None:
        self.hot = hot
        self.warm = warm

    @property
    def count(self) -> int:
        return self.hot.count + self.warm.count

    @property
    def total_bytes(self) -> int:
        return self.hot.total_bytes + self.warm.total_bytes

    @property
    def max_bytes(self) -> int:
        return self.hot.max_bytes + self.warm.max_bytes

    @property
    def fullness(self) -> float:
        """How much of the two together is used (1.0 = full)."""
        return 1.0 if self.max_bytes <= 0 else min(self.total_bytes / self.max_bytes, 9.99)

    @property
    def persistent(self) -> bool:
        return self.warm.persistent

    def pinned_bytes(self) -> int:
        return self.hot.pinned_bytes() + self.warm.pinned_bytes()

    def free_disk(self) -> int | None:
        return self.warm.free_disk()
