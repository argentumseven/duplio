"""A lock file that lives next to the data it protects, on any backend — held as a *lease*.

Repositories and snapshot folders can be shared between machines, or between two runs on one machine (a scheduled
job starting while the previous one is still busy), so the lock is a file in the destination: created without
clobbering, with an owner token, the owner's pid and host.

A lock that is only ever "taken" and "released" has two failure modes that both end in two writers at once: a
crashed owner leaves it behind forever (so people force-break it by hand), or a very old lock is declared stale
while its owner is merely slow (a backup running for hours, a laptop that slept). Backup tools such as restic
solve this with a lease, and so does this module:

* the owner **refreshes** the file every ``REFRESH_S`` from a heartbeat thread;
* anyone else treats it as stale only after ``STALE_S`` without a refresh, measured against the *file server's* clock
  (the age of the lock file against a probe file written moments ago on the same share), so clocks that disagree
  between machines cannot make a live lock look dead;
* the owner **fences** itself: :meth:`Lease.check` raises :class:`LockLostError` when the lock was taken over or could
  not be confirmed for too long, and :meth:`Lease.verify` re-reads the lock right before an irreversible step. A
  process that wakes from sleep therefore stops (or re-confirms the lock) instead of carrying on blindly;
* breaking a stale lock renames it away and checks that it really was the stale one that was moved, instead of
  deleting whatever happens to be at the path.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from duplio.errors import LockError, LockLostError
from duplio.vfs.base import Backend

REFRESH_S = 60.0  # how often the owner rewrites the lock
STALE_S = 20 * 60.0  # a lock not refreshed for this long (by the share's own clock) is abandoned
STALE_LOCK_S = STALE_S  # older name, kept for callers that import it
_CLOCK_SLACK_S = 5.0
VERIFY_ATTEMPTS = 3  # reads/writes of the lock tried before it is declared unconfirmable


def read_lock(backend: Backend, path: str) -> dict[str, Any]:
    try:
        with backend.open_read(path) as handle:
            data = json.loads(handle.read() or b"{}")
        return dict(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def storage_now_ns(backend: Backend, folder: str) -> int:
    """The current time according to whatever stamps files in ``folder`` (the file server, for a share)."""
    probe = backend.join(folder, f".clock-{secrets.token_hex(4)}")
    try:
        with backend.open_write(probe) as handle:
            handle.write(b"1")
        info = backend.stat(probe)
        if info is not None and info.mtime_ns:
            return int(info.mtime_ns)
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            backend.remove(probe)
    return time.time_ns()


def lock_age_s(backend: Backend, path: str, held: dict[str, Any]) -> float | None:
    """Seconds since the lock was last refreshed, or ``None`` when that cannot be told."""
    info = backend.stat(path)
    if info is None:
        return None
    return (storage_now_ns(backend, backend.parent(path)) - info.mtime_ns) / 1e9


def is_stale(backend: Backend, path: str, held: dict[str, Any], *, stale_s: float | None = None) -> bool:
    if held.get("host") == socket.gethostname():
        from duplio.plan.journal import _pid_alive

        if not _pid_alive(int(held.get("pid", -1))):
            return True
    age = lock_age_s(backend, path, held)  # an unreadable lock file is judged by age alone
    if age is None:
        return False
    return age > (STALE_S if stale_s is None else stale_s) + _CLOCK_SLACK_S


class Lease:
    """A held lock. Long operations call :meth:`check` regularly and :meth:`verify` before irreversible steps."""

    def __init__(self, backend: Backend, path: str, token: str, info: dict[str, Any], *, refresh_s: float,
                 stale_s: float) -> None:
        self.backend = backend
        self.path = path
        self.token = token
        self._info = info
        self.refresh_s = refresh_s
        self.stale_s = stale_s
        self.limit_s = stale_s / 2  # unconfirmed for longer than this and we stop trusting the lock
        self._guard = threading.Lock()
        self._lost = threading.Event()
        self.lost_reason = ""
        self._ok_wall = time.time()
        self._ok_mono = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._callbacks: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- state
    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def on_lost(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def _mark_lost(self, reason: str) -> None:
        if self._lost.is_set():
            return
        self.lost_reason = reason
        self._lost.set()
        for callback in self._callbacks:
            with contextlib.suppress(Exception):
                callback()

    def _unconfirmed_s(self) -> float:
        # Wall clock catches a suspended machine (the monotonic clock stops while asleep); monotonic catches a
        # wall clock set backwards. The larger of the two is the cautious reading.
        return max(time.time() - self._ok_wall, time.monotonic() - self._ok_mono)

    def _fail(self) -> LockLostError:
        return LockLostError(
            f"the lock {self.backend.display(self.path)} was lost ({self.lost_reason}); stopping before anything "
            "more is changed. Nothing is left half-done: run the command again."
        )

    # ---------------------------------------------------------------- checks
    def check(self) -> None:
        """Cheap: raise if the lock is known lost, or has gone unconfirmed for too long (then confirm it now)."""
        if self._lost.is_set():
            raise self._fail()
        if self._unconfirmed_s() > self.limit_s:
            self.verify()

    def verify(self) -> None:
        """Re-read the lock and refresh it. Raises :class:`LockLostError` if it is no longer ours."""
        if self._lost.is_set():
            raise self._fail()
        with self._guard:
            failure: OSError | None = None
            for attempt in range(VERIFY_ATTEMPTS):
                if attempt:
                    time.sleep(0.2 * attempt)  # a share that hiccups once should not cost a whole backup
                try:
                    held = read_lock(self.backend, self.path) if self.backend.exists(self.path) else {}
                    if held.get("token") != self.token:
                        self._mark_lost("another process took it over" if held else "the lock file is gone")
                        raise self._fail()
                    self._write()
                    return
                except OSError as exc:
                    failure = exc
            self._mark_lost(f"it cannot be confirmed: {failure}")
            raise self._fail() from failure

    def _write(self) -> None:
        self._seq += 1
        body = json.dumps({**self._info, "token": self.token, "time": time.time(), "seq": self._seq}).encode()
        with self.backend.open_write(self.path, overwrite=True) as handle:
            handle.write(body)
        self._ok_wall = time.time()
        self._ok_mono = time.monotonic()

    # ---------------------------------------------------------------- heartbeat
    def start(self) -> None:
        self._thread = threading.Thread(target=self._beat, name="duplio-lock-heartbeat", daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.refresh_s):
            if self._lost.is_set():
                return
            try:
                self.verify()
            except LockLostError:
                return
            except Exception:  # a transient error: try again next time; check() fences if it lasts
                continue

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._guard, contextlib.suppress(Exception):
            if read_lock(self.backend, self.path).get("token") == self.token:  # never remove someone else's lock
                self.backend.remove(self.path)


def _break_stale(backend: Backend, path: str, stale_token: object) -> None:
    """Move an abandoned lock out of the way. If what moved turns out not to be the abandoned one, put it back."""
    side = f"{path}.broken-{secrets.token_hex(4)}"
    try:
        backend.rename(path, side)
    except OSError:
        return  # someone else got there first
    moved = read_lock(backend, side)
    if moved.get("token") != stale_token and moved:
        with contextlib.suppress(OSError):
            backend.rename(side, path)  # we grabbed a live lock created in the meantime: restore it
            return
    with contextlib.suppress(Exception):
        backend.remove(side)


@contextlib.contextmanager
def exclusive(backend: Backend, path: str, purpose: str, *, hint: str = "", refresh_s: float | None = None,
              stale_s: float | None = None) -> Iterator[Lease]:
    """Hold ``path`` as a lock for the block; raises :class:`LockError` if someone else has it."""
    refresh = REFRESH_S if refresh_s is None else refresh_s
    stale = STALE_S if stale_s is None else stale_s
    token = secrets.token_hex(8)
    info = {"pid": os.getpid(), "host": socket.gethostname(), "purpose": purpose, "started": time.time()}
    backend.makedirs(backend.parent(path))
    lease = Lease(backend, path, token, info, refresh_s=refresh, stale_s=stale)
    for _ in range(3):
        try:
            with backend.open_write(path) as handle:
                handle.write(json.dumps({**info, "token": token, "time": time.time(), "seq": 0}).encode())
            break
        except FileExistsError:
            held = read_lock(backend, path)
            if is_stale(backend, path, held, stale_s=stale):
                _break_stale(backend, path, held.get("token"))
                continue
            raise LockError(
                f"{backend.display(path)} is locked by {held.get('purpose', 'another operation')} "
                f"(pid {held.get('pid')} on {held.get('host')}). "
                f"{hint or 'If that process is gone, delete the lock file.'}"
            ) from None
    else:  # pragma: no cover
        raise LockError(f"could not lock {backend.display(path)}")
    lease.start()
    try:
        yield lease
    finally:
        lease.release()
