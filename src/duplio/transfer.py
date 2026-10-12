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

    state = {"committed": False}

    def discard_unverified() -> None:
        # We created the destination (overwrite=False means nothing was there) but could not confirm it.
        state["committed"] = False
        with contextlib.suppress(FileNotFoundError):
            dst[0].remove(dst[1])

    def once() -> str:
        if state["committed"] and not overwrite:
            # An earlier attempt finished writing, then failed (for instance the read-back dropped) and could not
            # remove its output. Retrying would meet our own file and treat it as someone else's.
            discard_unverified()
        return _copy_once(src, dst, algorithm=algorithm, expected_size=expected_size,
                          expected_digest=expected_digest, mtime_ns=mtime_ns, overwrite=overwrite,
                          verify=verify, cancel=cancel, on_bytes=on_bytes, limiter=limiter, state=state)

    def recover() -> None:
        src[0].recover()
        dst[0].recover()

    try:
        return retry_transient(once, attempts=attempts, cancel=cancel, recover=recover)
    except BaseException:
        if state["committed"] and not overwrite:
            with contextlib.suppress(Exception):
                discard_unverified()  # last chance: never leave an unverified copy under the real name
        raise


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
    state: dict[str, bool],
) -> str:
    src_backend, src_path = src
    dst_backend, dst_path = dst
    hasher = get_algorithm(algorithm).new()
    view = memoryview(bytearray(READ_BUFFER))
    streamed = False
    try:
        with src_backend.open_read(src_path) as reader, dst_backend.open_write(dst_path, overwrite=overwrite) as writer:
            total, digest = _stream(reader, writer, hasher, view, src_path, expected_size, expected_digest, cancel,
                                    on_bytes, limiter)
            streamed = True
    except BaseException:
        if streamed and not overwrite:
            # Everything was written, and the destination's commit (the rename) may or may not have happened before
            # the error. From here on, whatever sits under the real name is our own unverified output.
            state["committed"] = True
        raise
    state["committed"] = True  # the destination now exists under its real name; verification is still to come
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
    state["committed"] = False  # verified: this copy is now the result
    if mtime_ns is not None:
        dst_backend.set_mtime(dst_path, mtime_ns)
    return digest


def _stream(reader, writer, hasher, view, src_path: str, expected_size: int | None, expected_digest: str | None,  # type: ignore[no-untyped-def]
            cancel: CancelToken | None, on_bytes: Callable[[int], None] | None,
            limiter: RateLimiter | None) -> tuple[int, str]:
    """Copy everything from ``reader`` to ``writer`` while hashing; returns ``(bytes, digest)``."""
    total = 0
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
    return total, digest
