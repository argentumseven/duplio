"""A lock file that lives next to the data it protects, on any backend.

Repositories and snapshot folders can be shared between machines or between two runs on one machine
(a scheduled job starting while the previous one is still busy), so the lock is a file in the destination:
created without clobbering, with the owner's pid, host and time, and reclaimed only when the owner is
provably gone (same host, dead pid) or the lock is very old.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from collections.abc import Iterator
from typing import Any

from duplio.errors import LockError
from duplio.vfs.base import Backend

STALE_LOCK_S = 12 * 3600


def read_lock(backend: Backend, path: str) -> dict[str, Any]:
    try:
        with backend.open_read(path) as handle:
            return dict(json.loads(handle.read() or b"{}"))
    except (OSError, ValueError):
        return {}


def is_stale(held: dict[str, Any]) -> bool:
    if not held:
        return False
    if time.time() - float(held.get("time", 0)) > STALE_LOCK_S:
        return True
    if held.get("host") == socket.gethostname():
        from duplio.plan.journal import _pid_alive

        return not _pid_alive(int(held.get("pid", -1)))
    return False


@contextlib.contextmanager
def exclusive(backend: Backend, path: str, purpose: str, *, hint: str = "") -> Iterator[None]:
    """Hold ``path`` as a lock for the duration of the block; raises :class:`LockError` if someone else has it."""
    info = {"pid": os.getpid(), "host": socket.gethostname(), "time": time.time(), "purpose": purpose}
    backend.makedirs(backend.parent(path))
    for _ in range(2):
        try:
            with backend.open_write(path) as handle:
                handle.write(json.dumps(info).encode())
            break
        except FileExistsError:
            held = read_lock(backend, path)
            if is_stale(held):
                with contextlib.suppress(Exception):
                    backend.remove(path)
                continue
            raise LockError(
                f"{backend.display(path)} is locked by {held.get('purpose', 'another operation')} "
                f"(pid {held.get('pid')} on {held.get('host')}). {hint or 'If that process is gone, delete the lock file.'}"
            ) from None
    else:  # pragma: no cover
        raise LockError(f"could not lock {backend.display(path)}")
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            backend.remove(path)
