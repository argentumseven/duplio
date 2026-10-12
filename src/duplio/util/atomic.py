"""Crash-safe local file writes.

Writes go to a temporary file in the destination directory, are flushed and
fsynced, then renamed over the target with :func:`os.replace` (atomic on POSIX
and Windows). A crash therefore leaves either the old file or the new file —
never a half-written one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any


def fsync_dir(path: str | os.PathLike[str]) -> None:
    """Persist a directory entry change (rename/create) where the OS supports it."""
    if os.name == "nt":
        return
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_open(path: str | os.PathLike[str], mode: str = "wb") -> Iterator[IO[Any]]:
    if "w" not in mode:
        raise ValueError("atomic_open only supports write modes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        handle_cm = os.fdopen(fd, mode) if "b" in mode else os.fdopen(fd, mode, encoding="utf-8")
        with handle_cm as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        fsync_dir(target.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    with atomic_open(path, "wb") as handle:
        handle.write(data)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    with atomic_open(path, "w") as handle:
        handle.write(text)


def atomic_write_json(path: str | os.PathLike[str], data: Any, *, indent: int | None = 2) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=False) + "\n")
