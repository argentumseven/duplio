"""Retrying operations that fail because a network drive hiccuped.

Network shares fail in ways local disks do not: a stale NFS handle after the NAS
rebooted, a dropped SMB session, a timeout while a disk spins up. Those errors
usually clear on a second attempt. Errors that will *not* clear (no space,
permission denied, file exists, verification mismatch) are never retried.
"""

from __future__ import annotations

import errno
import time
from collections.abc import Callable
from typing import TypeVar

from duplio.errors import CancelledError, VerificationError
from duplio.events import CancelToken

T = TypeVar("T")

TRANSIENT_ERRNOS = frozenset(
    {
        errno.ESTALE, errno.EIO, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
        errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENOTCONN, errno.EAGAIN,
        errno.EINTR, errno.EBUSY,
    }
)  # fmt: skip
_TRANSIENT_NAMES = frozenset({"SMBConnectionClosed", "ConnectionClosed"})


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, CancelledError | VerificationError | FileExistsError | PermissionError):
        return False
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    if isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS:
        return True
    return type(exc).__name__ in _TRANSIENT_NAMES


def retry_transient(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    delay: float = 0.5,
    max_delay: float = 8.0,
    cancel: CancelToken | None = None,
    recover: Callable[[], None] | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``; retry up to ``attempts - 1`` more times on transient errors, with backoff."""
    attempt = 0
    while True:
        try:
            return fn()
        except BaseException as exc:
            attempt += 1
            if attempt >= attempts or not is_transient(exc):
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            wait = min(max_delay, delay * (2 ** (attempt - 1)))
            remaining = wait
            while remaining > 0:
                if cancel is not None:
                    cancel.check()
                step = min(remaining, 0.25)
                sleep(step)
                remaining -= step
            if recover is not None:
                try:
                    recover()
                except Exception:  # recovery is best effort; the next attempt reports the real error
                    pass
