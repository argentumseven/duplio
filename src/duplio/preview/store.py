"""A bounded on-disk cache of thumbnails.

Thumbnails are kept as small image files named by a hash, with a SQLite index of sizes and last use. The index holds no
file names or paths (only a hashed drive id), so a leftover cache does not list what you browsed. The store trims itself
to its size limit, least recently used first.

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
    def __init__(self, root: Path | None, *, max_bytes: int, persistent: bool) -> None:
        self.persistent = persistent
        self.max_bytes = max(0, max_bytes)
        self._temporary = root is None
        self.root = Path(tempfile.mkdtemp(prefix="duplio-previews-")) if root is None else root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False, timeout=30)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._closed = False

    # ------------------------------------------------------------------ access
    def _file(self, key: str, fmt: str) -> Path:
        return self.root / key[:2] / f"{key}.{fmt}"

    def get(self, key: str) -> StoredThumb | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fmt, width, height, orientation, source, original_w, original_h FROM thumbs WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            fmt, width, height, orientation, source, ow, oh = row
            try:
                data = self._file(key, fmt).read_bytes()
            except OSError:  # the file was removed behind our back: forget the entry
                self._db.execute("DELETE FROM thumbs WHERE key=?", (key,))
                self._db.commit()
                return None
            self._db.execute("UPDATE thumbs SET used=? WHERE key=?", (time.time(), key))
            self._db.commit()
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
            self._db.execute(
                "INSERT OR REPLACE INTO thumbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, drive, thumb.fmt, thumb.width, thumb.height, thumb.orientation, thumb.source, ow, oh,
                 len(thumb.data), now, now),
            )
            self._db.commit()
        self.trim()

    # ------------------------------------------------------------------ housekeeping
    def trim(self) -> int:
        """Remove the least recently used thumbnails until the store fits its limit. Returns how many went."""
        removed = 0
        with self._lock:
            total = self._db.execute("SELECT COALESCE(SUM(bytes), 0) FROM thumbs").fetchone()[0]
            if total <= self.max_bytes:
                return 0
            rows = self._db.execute("SELECT key, fmt, bytes FROM thumbs ORDER BY used ASC").fetchall()
            for key, fmt, size in rows:
                if total <= self.max_bytes * 0.9:  # trim a little below the limit so every insert does not trim
                    break
                with contextlib.suppress(OSError):
                    self._file(key, fmt).unlink()
                self._db.execute("DELETE FROM thumbs WHERE key=?", (key,))
                total -= size
                removed += 1
            self._db.commit()
        return removed

    def clear(self, drive: str | None = None) -> int:
        with self._lock:
            if drive is None:
                rows = self._db.execute("SELECT key, fmt FROM thumbs").fetchall()
                self._db.execute("DELETE FROM thumbs")
            else:
                rows = self._db.execute("SELECT key, fmt FROM thumbs WHERE drive=?", (drive,)).fetchall()
                self._db.execute("DELETE FROM thumbs WHERE drive=?", (drive,))
            self._db.commit()
        for key, fmt in rows:
            with contextlib.suppress(OSError):
                self._file(key, fmt).unlink()
        return len(rows)

    def stats(self) -> StoreStats:
        with self._lock:
            count, total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM thumbs").fetchone()
        return StoreStats(int(count), int(total), self.persistent, str(self.root))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._db.close()
        if self._temporary:
            shutil.rmtree(self.root, ignore_errors=True)
