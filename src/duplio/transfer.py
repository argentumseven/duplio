"""Verified copying between any two backends.

``copy_verified`` is the only way Duplio copies file data. It hashes while
streaming, writes through the destination's atomic ``open_write`` (temp file
+ no-clobber rename), checks the byte count, and then re-reads the
destination from storage to confirm the digest before reporting success.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from duplio.errors import VerificationError
from duplio.events import CancelToken
from duplio.hashing.algorithms import get_algorithm
from duplio.hashing.core import READ_BUFFER, SizeMismatch, hash_file
from duplio.util.throttle import RateLimiter
from duplio.vfs.base import Backend
from duplio.vfs.retry import retry_transient


def copy_verified(
    src: tuple[Backend, str],
    dst: tuple[Backend, str],
    *,
    algorithm: str,
    expected_size: int | None = None,
    expected_digest: str | None = None,
    mtime_ns: int | None = None,
    overwrite: bool = False,
    verify: bool = True,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
) -> str:
    """Copy ``src`` to ``dst``; return the content digest.

    Raises :class:`~duplio.errors.VerificationError` (and removes the
    destination) if the copy does not match what was read, or does not match
    ``expected_digest`` when one is given. Transient network errors (stale
    handle, dropped session, timeout) are retried with backoff; each retry
    starts the file again from scratch, so a partial copy is never kept.
    ``limiter`` caps the transfer rate, including the read-back verification.
    """

    def once() -> str:
        return _copy_once(src, dst, algorithm=algorithm, expected_size=expected_size,
                          expected_digest=expected_digest, mtime_ns=mtime_ns, overwrite=overwrite,
                          verify=verify, cancel=cancel, on_bytes=on_bytes, limiter=limiter)

    def recover() -> None:
        src[0].recover()
        dst[0].recover()

    return retry_transient(once, attempts=attempts, cancel=cancel, recover=recover)


def _copy_once(
    src: tuple[Backend, str],
    dst: tuple[Backend, str],
    *,
    algorithm: str,
    expected_size: int | None,
    expected_digest: str | None,
    mtime_ns: int | None,
    overwrite: bool,
    verify: bool,
    cancel: CancelToken | None,
    on_bytes: Callable[[int], None] | None,
    limiter: RateLimiter | None,
) -> str:
    src_backend, src_path = src
    dst_backend, dst_path = dst
    hasher = get_algorithm(algorithm).new()
    total = 0
    buf = bytearray(READ_BUFFER)
    view = memoryview(buf)
    with src_backend.open_read(src_path) as reader, dst_backend.open_write(dst_path, overwrite=overwrite) as writer:
        readinto = getattr(reader, "readinto", None)
        while True:
            if cancel is not None:
                cancel.check()
            if readinto is not None:
                n = readinto(view)
                if not n:
                    break
                chunk = view[:n]
            else:
                data = reader.read(READ_BUFFER)
                if not data:
                    break
                n = len(data)
                chunk = memoryview(data)
            hasher.update(chunk)
            if limiter is not None:
                limiter.acquire(n, cancel)
            writer.write(chunk)
            total += n
            if on_bytes is not None:
                on_bytes(n)
        if expected_size is not None and total != expected_size:
            raise SizeMismatch(f"{src_path}: read {total} bytes, expected {expected_size} (file changed?)")
        digest = hasher.hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise VerificationError(f"{src_path}: content no longer matches the scanned digest")
    if verify:
        try:
            written = hash_file(
                dst_backend, dst_path, algorithm, expected_size=total, cancel=cancel,
                on_bytes=(lambda n: limiter.acquire(n, cancel)) if limiter is not None else None,
            )
        except Exception:
            with contextlib.suppress(Exception):
                dst_backend.remove(dst_path)
            raise
        if written != digest:
            with contextlib.suppress(Exception):
                dst_backend.remove(dst_path)
            raise VerificationError(f"verification failed: {dst_backend.display(dst_path)} differs from its source")
    if mtime_ns is not None:
        dst_backend.set_mtime(dst_path, mtime_ns)
    return digest
