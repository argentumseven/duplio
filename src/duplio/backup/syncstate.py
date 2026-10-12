"""What two synchronised folders looked like at the end of the last successful sync.

Two-way sync cannot work from a single snapshot of each side: a file present on A
but not on B could be *new on A* or *deleted on B*. The baseline tells them apart:
if the file was in sync last time and B no longer has it, B deleted it; if it was
never in the baseline, it is new. Without a baseline (the first run) Duplio never
deletes anything.

One small SQLite file per pair of locations, in the state folder. The pair is
identified regardless of argument order (``sync A B`` and ``sync B A`` share it).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from duplio.util.appdirs import AppDirs
from duplio.util.fmt import iso_now
from duplio.util.text import fs_bytes, fs_str

SCHEMA_VERSION = "2"  # v2: each row records when it was written (see the racy-timestamp rule in sync.py)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key      BLOB PRIMARY KEY,
    rel_l    BLOB    NOT NULL,
    size_l   INTEGER NOT NULL,
    mtime_l  INTEGER NOT NULL,
    rel_r    BLOB    NOT NULL,
    size_r   INTEGER NOT NULL,
    mtime_r  INTEGER NOT NULL,
    recorded_ns INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Baseline:
    """A file as it was on both sides when they were last identical."""

    rel_a: str
    size_a: int
    mtime_a: int
    rel_b: str
    size_b: int
    mtime_b: int
    recorded_ns: int = 0  # when this row was written; 0 = unknown


def pair_id(uri_a: str, uri_b: str) -> str:
    first, second = sorted((uri_a, uri_b))
    return hashlib.sha256(f"{first}\n{second}".encode()).hexdigest()[:16]


class SyncState:
    def __init__(self, path: Path, uri_a: str, uri_b: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        left = min(uri_a, uri_b)
        self.flipped = uri_a != left  # our "A" is the stored right-hand side
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is not None and row[0] != SCHEMA_VERSION:
            # An older or unknown format: forget it and behave as a first run, which never deletes anything.
            self._conn.execute("DROP TABLE IF EXISTS files")
            self._conn.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
        self._conn.executescript(_SCHEMA)
        if row is None:
            self._conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("schema", SCHEMA_VERSION), ("left", left),
                                                                                  ("right", max(uri_a, uri_b))])
        self._conn.commit()

    @classmethod
    def open(cls, dirs: AppDirs, uri_a: str, uri_b: str) -> SyncState:
        return cls(dirs.state / "sync" / f"{pair_id(uri_a, uri_b)}.sqlite3", uri_a, uri_b)

    # ------------------------------------------------------------------
    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    @property
    def last_run(self) -> str | None:
        return self._meta("last_run")

    def load(self) -> dict[str, Baseline]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, rel_l, size_l, mtime_l, rel_r, size_r, mtime_r, recorded_ns FROM files").fetchall()
        out: dict[str, Baseline] = {}
        for key, rl, sl, ml, rr, sr, mr, rec in rows:
            if self.flipped:
                out[fs_str(key)] = Baseline(fs_str(rr), sr, mr, fs_str(rl), sl, ml, rec)
            else:
                out[fs_str(key)] = Baseline(fs_str(rl), sl, ml, fs_str(rr), sr, mr, rec)
        return out

    def put(self, key: str, base: Baseline) -> None:
        if self.flipped:
            row = (fs_bytes(key), fs_bytes(base.rel_b), base.size_b, base.mtime_b, fs_bytes(base.rel_a),
                   base.size_a, base.mtime_a, base.recorded_ns)
        else:
            row = (fs_bytes(key), fs_bytes(base.rel_a), base.size_a, base.mtime_a, fs_bytes(base.rel_b),
                   base.size_b, base.mtime_b, base.recorded_ns)
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)

    def drop(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE key=?", (fs_bytes(key),))

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files")
            self._conn.commit()

    def commit(self, *, finished: bool = False) -> None:
        with self._lock:
            if finished:
                self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (iso_now(),))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
