"""Persistent digest cache (SQLite).

Rescanning a 500 GB photo archive should take seconds, not hours. Digests are
cached per ``(backend, path, kind)`` and are only reused while the file's size
and modification time are unchanged.

Safety notes:

* Files modified very recently are not cached (the "racy timestamp" problem:
  a file rewritten within the same mtime tick would otherwise keep a stale
  digest).
* The cache only ever *accelerates discovery*. Before any destructive action
  the executor re-reads the bytes, so a stale cache entry can never cause a
  wrong deletion.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
    backend   TEXT    NOT NULL,
    path      TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    digest    TEXT    NOT NULL,
    stored_at INTEGER NOT NULL,
    PRIMARY KEY (backend, path, kind)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA_VERSION = "1"


class HashCache:
    """Thread-safe digest cache. Use :class:`NullCache` to disable caching."""

    def __init__(self, path: str | Path, *, racy_window_s: float = 3.0, flush_every: int = 500) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            self._conn.commit()
        elif row[0] != SCHEMA_VERSION:
            self._conn.execute("DELETE FROM digests")
            self._conn.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
            self._conn.commit()
        self._pending: list[tuple[str, str, str, int, int, str, int]] = []
        self._racy_ns = int(racy_window_s * 1e9)
        self._flush_every = flush_every
        self.hits = 0
        self.misses = 0

    def get(self, backend: str, path: str, kind: str, size: int, mtime_ns: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime_ns, digest FROM digests WHERE backend=? AND path=? AND kind=?",
                (backend, path, kind),
            ).fetchone()
            if row is not None and row[0] == size and row[1] == mtime_ns:
                self.hits += 1
                return str(row[2])
            # Pending (not yet flushed) entries are found too.
            for item in reversed(self._pending):
                if item[0] == backend and item[1] == path and item[2] == kind:
                    if item[3] == size and item[4] == mtime_ns:
                        self.hits += 1
                        return item[5]
                    break
            self.misses += 1
            return None

    def put(
        self,
        backend: str,
        path: str,
        kind: str,
        size: int,
        mtime_ns: int,
        digest: str,
        *,
        mtime_resolution_ns: int = 1,
    ) -> None:
        now_ns = time.time_ns()
        window = max(self._racy_ns, 2 * mtime_resolution_ns)
        if now_ns - mtime_ns < window:
            return  # too fresh to trust its timestamp
        with self._lock:
            self._pending.append((backend, path, kind, size, mtime_ns, digest, now_ns // 1_000_000_000))
            if len(self._pending) >= self._flush_every:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        self._conn.executemany("INSERT OR REPLACE INTO digests VALUES (?,?,?,?,?,?,?)", self._pending)
        self._conn.commit()
        self._pending.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def invalidate(self, backend: str, path: str) -> None:
        with self._lock:
            self._pending = [p for p in self._pending if not (p[0] == backend and p[1] == path)]
            self._conn.execute("DELETE FROM digests WHERE backend=? AND path=?", (backend, path))
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._flush_locked()
            count = self._conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"entries": int(count), "bytes": size, "hits": self.hits, "misses": self.misses}

    def prune(self, older_than_s: float) -> int:
        cutoff = int(time.time() - older_than_s)
        with self._lock:
            self._flush_locked()
            cur = self._conn.execute("DELETE FROM digests WHERE stored_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._conn.execute("DELETE FROM digests")
            self._conn.commit()
            self._conn.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_locked()
            finally:
                self._conn.close()

    def __enter__(self) -> HashCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class NullCache(HashCache):
    """A cache that remembers nothing (``--no-cache``)."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    def get(self, backend: str, path: str, kind: str, size: int, mtime_ns: int) -> str | None:
        return None

    def put(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None

    def flush(self) -> None:
        pass

    def invalidate(self, backend: str, path: str) -> None:
        pass

    def stats(self) -> dict[str, int]:
        return {"entries": 0, "bytes": 0, "hits": 0, "misses": 0}

    def prune(self, older_than_s: float) -> int:
        return 0

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass
