"""A bounded on-disk cache of thumbnails.

Thumbnails are kept as small image files named by a hash, with a SQLite index of sizes and last use. The index holds no
file names or paths (only a hashed drive id), so a leftover cache does not list what you browsed. The store trims itself
to its size limit, least recently used first (but the pictures of the folder on screen go last).

A *session* store lives in a fresh temporary folder and is deleted when the program ends; a *persistent* store lives in
Duplio's cache folder and survives. Neither is ever used to decide anything about a file: they only save a trip to the
drive when a picture is shown again.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs (
    key TEXT PRIMARY KEY, drive TEXT NOT NULL, fmt TEXT NOT NULL,
    width INTEGER, height INTEGER, orientation INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL,
    original_w INTEGER, original_h INTEGER, bytes INTEGER NOT NULL, created REAL NOT NULL, used REAL NOT NULL);
CREATE INDEX IF NOT EXISTS thumbs_used ON thumbs (used);
"""


@dataclass(frozen=True)
class StoredThumb:
    data: bytes
    fmt: str  # "jpg" or "png"
    width: int | None
    height: int | None
    orientation: int
    source: str  # "embedded" (taken from the photo's own thumbnail) or "decoded" (made from the picture)
    original: tuple[int, int] | None


@dataclass(frozen=True)
class StoreStats:
    count: int
    bytes: int
    persistent: bool
    path: str


def drive_tag(backend_id: str) -> str:
    return hashlib.sha256(backend_id.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def thumb_key(backend_id: str, path: str, size: int, mtime_ns: int) -> str:
    """Identify a picture by where it is and what it looked like: a changed file gets a new thumbnail."""
    raw = f"{backend_id}\0{path}\0{size}\0{mtime_ns}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest()[:32]


class ThumbStore:
    """The cache itself. It keeps its own running totals (no table scan per insert), writes "last used" times in batches
    (no database write per picture shown), and can be told which pictures to protect while a folder is on screen."""

    TOUCH_FLUSH_EVERY = 200  # remembered "last used" times written out after this many pictures ...
    TOUCH_FLUSH_SECONDS = 5.0  # ... or this long

    def __init__(self, root: Path | None, *, max_bytes: int, persistent: bool) -> None:
        self.persistent = persistent
        self.max_bytes = max(0, max_bytes)
        self._temporary = root is None
        self.root = Path(tempfile.mkdtemp(prefix="duplio-previews-")) if root is None else root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = self._open_index()
        self._count, self._total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM thumbs").fetchone()
        self._touched: dict[str, float] = {}
        self._last_flush = time.monotonic()
        self._pinned: frozenset[str] = frozenset()
        self._pinned_cache: tuple[frozenset[str], float, int] | None = None
        self._closed = False

    def _open_index(self) -> sqlite3.Connection:
        """Open the index; a damaged one is only a cache that was lost, so it is started again rather than failing."""
        path = self.root / "index.sqlite3"
        for attempt in (1, 2):
            db = None
            try:
                db = sqlite3.connect(path, check_same_thread=False, timeout=30)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.executescript(_SCHEMA)
                db.commit()
                return db
            except sqlite3.DatabaseError:
                if db is not None:
                    with contextlib.suppress(sqlite3.Error):
                        db.close()
                if attempt == 2:
                    raise
                self._discard_index()
        raise AssertionError("unreachable")  # pragma: no cover

    def _discard_index(self) -> None:
        for name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm"):
            with contextlib.suppress(OSError):
                (self.root / name).unlink()
        for child in self.root.iterdir():
            if child.is_dir() and len(child.name) == 2:
                shutil.rmtree(child, ignore_errors=True)

    # ------------------------------------------------------------------ access
    def _file(self, key: str, fmt: str) -> Path:
        return self.root / key[:2] / f"{key}.{fmt}"

    def get(self, key: str) -> StoredThumb | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fmt, width, height, orientation, source, original_w, original_h, bytes FROM thumbs WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            fmt, width, height, orientation, source, ow, oh, size = row
            try:
                data = self._file(key, fmt).read_bytes()
            except OSError:  # the file was removed behind our back: forget the entry
                self._db.execute("DELETE FROM thumbs WHERE key=?", (key,))
                self._db.commit()
                self._count -= 1
                self._total -= size
                return None
            self._touched[key] = time.time()
            self._flush_touched(force=False)
        return StoredThumb(data, fmt, width, height, orientation, source, (ow, oh) if ow and oh else None)

    def has(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM thumbs WHERE key=?", (key,)).fetchone() is not None

    def put(self, key: str, drive: str, thumb: StoredThumb) -> None:
        path = self._file(key, thumb.fmt)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
        try:
            tmp.write_bytes(thumb.data)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        now = time.time()
        ow, oh = thumb.original if thumb.original else (None, None)
        with self._lock:
            old = self._db.execute("SELECT bytes FROM thumbs WHERE key=?", (key,)).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO thumbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, drive, thumb.fmt, thumb.width, thumb.height, thumb.orientation, thumb.source, ow, oh,
                 len(thumb.data), now, now),
            )
            self._db.commit()
            self._total += len(thumb.data) - (old[0] if old else 0)
            self._count += 0 if old else 1
            over = self._total > self.max_bytes
        if over:
            self.trim()

    # ------------------------------------------------------------------ housekeeping
    def pin(self, keys: set[str] | frozenset[str]) -> None:
        """Pictures to keep when the store is trimmed (the folder on screen), for as long as anything else can go instead."""
        self._pinned = frozenset(keys)

    def pinned_bytes(self) -> int:
        """How much of the store the pinned pictures (the folder on screen) take up. Cached for a few seconds."""
        now = time.monotonic()
        with self._lock:
            pinned = self._pinned
            if self._pinned_cache and self._pinned_cache[0] is pinned and now - self._pinned_cache[1] < 3.0:
                return self._pinned_cache[2]
            keys = list(pinned)
            total = 0
            for start in range(0, len(keys), 500):  # SQLite takes a limited number of values in one query
                chunk = keys[start : start + 500]
                marks = ",".join("?" * len(chunk))
                total += int(self._db.execute(f"SELECT COALESCE(SUM(bytes), 0) FROM thumbs WHERE key IN ({marks})", chunk).fetchone()[0])
            self._pinned_cache = (pinned, now, total)
            return total

    def expire(self, days: int) -> int:
        """Remove pictures nobody has looked at for ``days`` days (the point of keeping them is to have them at hand
        again, and a folder not opened for months is not coming back). Returns how many went."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            self._flush_touched(force=True)
            rows = self._db.execute("SELECT key, fmt, bytes FROM thumbs WHERE used < ?", (cutoff,)).fetchall()
            for key, fmt, size in rows:
                with contextlib.suppress(OSError):
                    self._file(key, fmt).unlink()
                self._total -= size
                self._count -= 1
            if rows:
                self._db.execute("DELETE FROM thumbs WHERE used < ?", (cutoff,))
                self._db.commit()
        return len(rows)

    @property
    def total_bytes(self) -> int:
        return int(self._total)

    @property
    def count(self) -> int:
        return int(self._count)

    @property
    def fullness(self) -> float:
        """How much of the size limit is used (1.0 = full)."""
        return 1.0 if self.max_bytes <= 0 else min(self._total / self.max_bytes, 9.99)

    def free_disk(self) -> int | None:
        """Free space where the store lives, or ``None`` when the system will not say."""
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    def _flush_touched(self, *, force: bool) -> None:
        """Write the remembered "last used" times (caller holds the lock)."""
        if not self._touched:
            return
        due = len(self._touched) >= self.TOUCH_FLUSH_EVERY or time.monotonic() - self._last_flush >= self.TOUCH_FLUSH_SECONDS
        if not (force or due):
            return
        with contextlib.suppress(sqlite3.Error):
            self._db.executemany("UPDATE thumbs SET used=? WHERE key=?", [(t, k) for k, t in self._touched.items()])
            self._db.commit()
        self._touched.clear()
        self._last_flush = time.monotonic()

    def trim(self) -> int:
        """Remove the least recently used thumbnails until the store fits its limit. Returns how many went.

        Pictures that are pinned (see :meth:`pin`) go last: only when everything else is gone and the store is still over
        its limit are they removed too, so the limit holds whatever is pinned."""
        removed = 0
        with self._lock:
            if self._total <= self.max_bytes:
                return 0
            self._flush_touched(force=True)
            target = self.max_bytes * 0.9  # trim a little below the limit so every insert does not trim
            rows = self._db.execute("SELECT key, fmt, bytes FROM thumbs ORDER BY used ASC").fetchall()
            pinned = self._pinned
            for second_pass in (False, True):
                for key, fmt, size in rows:
                    if self._total <= target:
                        break
                    if (key in pinned) != second_pass:
                        continue
                    with contextlib.suppress(OSError):
                        self._file(key, fmt).unlink()
                    self._db.execute("DELETE FROM thumbs WHERE key=?", (key,))
                    self._total -= size
                    self._count -= 1
                    removed += 1
                if self._total <= self.max_bytes:
                    break  # it fits again without touching the pinned pictures (or after the first of them)
            self._db.commit()
        return removed

    def clear(self, drive: str | None = None) -> int:
        with self._lock:
            self._touched.clear()
            if drive is None:
                rows = self._db.execute("SELECT key, fmt FROM thumbs").fetchall()
                self._db.execute("DELETE FROM thumbs")
            else:
                rows = self._db.execute("SELECT key, fmt FROM thumbs WHERE drive=?", (drive,)).fetchall()
                self._db.execute("DELETE FROM thumbs WHERE drive=?", (drive,))
            self._db.commit()
            self._count, self._total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM thumbs").fetchone()
        for key, fmt in rows:
            with contextlib.suppress(OSError):
                self._file(key, fmt).unlink()
        if drive is None:  # everything: also sweep up files no entry knows about (left by a stop in the middle of an earlier clear)
            with contextlib.suppress(OSError):
                for child in self.root.iterdir():
                    if child.is_dir() and len(child.name) == 2:
                        shutil.rmtree(child, ignore_errors=True)
        return len(rows)

    def stats(self) -> StoreStats:
        with self._lock:
            return StoreStats(int(self._count), int(self._total), self.persistent, str(self.root))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._flush_touched(force=True)
            with contextlib.suppress(sqlite3.Error):
                self._db.close()
        if self._temporary:
            shutil.rmtree(self.root, ignore_errors=True)
