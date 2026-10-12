"""Write-ahead journal for every change Duplio makes.

Before touching a file the executor appends an ``intent`` record and fsyncs
it; afterwards it appends ``done`` (or ``failed``/``skipped``). If the process
dies mid-operation the journal shows exactly which operation was in flight,
and :func:`inspect_incomplete` reports whether it took effect. ``undo`` reads
the same journal backwards.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duplio.errors import LockError
from duplio.util.faults import crashpoint, torn_write
from duplio.util.fmt import iso_now
from duplio.util.text import json_dumps

JOURNAL_FORMAT = "duplio-journal"


class Journal:
    def __init__(self, path: str | Path, *, header: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        existing = self.path.exists()
        if existing:
            for rec in read_journal(self.path):
                self._seq = max(self._seq, int(rec.get("seq", 0)))
        self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        if existing and not _ends_with_newline(self.path):
            # The last write of an earlier run was cut short. Start a new line so that the next record is not glued
            # onto the fragment (which would make both unreadable).
            self._fh.write("\n")
            self._fh.flush()
        if not existing:
            self._write({"type": "header", "format": JOURNAL_FORMAT, "version": 1, **(header or {})})

    def _write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", iso_now())
        crashpoint("journal.before")
        line = json_dumps(record) + "\n"
        torn_write("journal.torn", self._fh, line)
        self._fh.write(line)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        crashpoint("journal.after")

    def intent(self, **fields: Any) -> int:
        with self._lock:
            self._seq += 1
            self._write({"type": "op", "event": "intent", "seq": self._seq, **fields})
            return self._seq

    def event(self, seq: int, event: str, **fields: Any) -> None:
        with self._lock:
            self._write({"type": "op", "event": event, "seq": seq, **fields})

    def done(self, seq: int, **fields: Any) -> None:
        self.event(seq, "done", **fields)

    def failed(self, seq: int, error: str) -> None:
        self.event(seq, "failed", error=error)

    def note(self, **fields: Any) -> None:
        with self._lock:
            self._write({"type": "note", **fields})

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a crash (or damaged): the records around it are still good
    return records


def _ends_with_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return True
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:
        return True


@dataclass
class OpState:
    seq: int
    intent: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Last terminal event: ``done``, ``failed``, ``skipped``, ``undone``... or ``incomplete``."""
        for ev in reversed(self.events):
            if ev["event"] in ("done", "failed", "skipped", "undone", "undo-failed", "purged", "recovered"):
                return str(ev["event"])
        return "incomplete"

    @property
    def done_record(self) -> dict[str, Any] | None:
        for ev in self.events:
            if ev["event"] == "done":
                return ev
        return None


@dataclass
class JournalSummary:
    path: Path
    header: dict[str, Any]
    ops: list[OpState]
    notes: list[dict[str, Any]]

    @property
    def session(self) -> str:
        return str(self.header.get("apply_id", self.path.stem))

    def count(self, status: str) -> int:
        return sum(1 for op in self.ops if op.status == status)

    @property
    def incomplete(self) -> list[OpState]:
        return [op for op in self.ops if op.status == "incomplete"]

    @property
    def reclaimed(self) -> int:
        return sum(int((op.done_record or {}).get("reclaimed", 0)) for op in self.ops if op.status == "done")


def summarize(path: str | Path) -> JournalSummary:
    records = read_journal(path)
    header: dict[str, Any] = {}
    ops: dict[int, OpState] = {}
    notes = []
    for rec in records:
        kind = rec.get("type")
        if kind == "header":
            header = rec
        elif kind == "note":
            notes.append(rec)
        elif kind == "op":
            seq = int(rec["seq"])
            if rec.get("event") == "intent":
                ops[seq] = OpState(seq=seq, intent=rec)
            elif seq in ops:
                ops[seq].events.append(rec)
    return JournalSummary(Path(path), header, [ops[k] for k in sorted(ops)], notes)


def list_journals(directory: str | Path) -> list[Path]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl"), reverse=True)


# ---------------------------------------------------------------------------
# Process lock: only one apply/undo at a time per state directory.
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows only
        # os.kill(pid, 0) would send CTRL_C_EVENT on Windows; query the process instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def exclusive_lock(path: str | Path, *, purpose: str = "operation") -> Iterator[None]:
    """Create ``path`` exclusively; stale locks from dead local processes are reclaimed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": os.getpid(), "host": socket.gethostname(), "time": time.time(), "purpose": purpose}
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                held = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                held = {}
            same_host = held.get("host") == socket.gethostname()
            if same_host and not _pid_alive(int(held.get("pid", -1))):
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            raise LockError(
                f"another Duplio {held.get('purpose', 'operation')} is running "
                f"(pid {held.get('pid')} on {held.get('host')}); lock file: {path}"
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info, handle)
        break
    else:  # pragma: no cover
        raise LockError(f"could not acquire lock {path}")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
