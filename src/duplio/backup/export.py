"""Write a snapshot out as one archive file (.tar, .tar.gz, .zip) for sharing or cold storage."""

from __future__ import annotations

import contextlib
import io
import os
import tarfile
import time
import zipfile
from pathlib import Path

from duplio.backup.repository import Repository, TreeEntry
from duplio.errors import ConfigError, RepositoryError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.scan.filters import GlobSet
from duplio.util.atomic import fsync_dir
from duplio.vfs.local import rename_noreplace

FORMATS = {".tar": "w", ".tar.gz": "w:gz", ".tgz": "w:gz", ".tar.xz": "w:xz", ".zip": "zip"}


def archive_mode(path: Path) -> str:
    name = path.name.lower()
    for suffix in sorted(FORMATS, key=len, reverse=True):
        if name.endswith(suffix):
            return FORMATS[suffix]
    raise ConfigError("the archive type is chosen by the file name: .zip, .tar, .tar.gz or .tar.xz")


class _ChunkStream(io.RawIOBase):
    """File-like view over a snapshot entry's chunks, for tarfile."""

    def __init__(self, repo: Repository, entry: TreeEntry, cancel: CancelToken, progress: ProgressSink) -> None:
        self._chunks = iter(entry.chunks)
        self._repo, self._cancel, self._progress = repo, cancel, progress
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            self._cancel.check()
            oid = next(self._chunks, None)
            if oid is None:
                break
            data = self._repo.get(oid)
            self._progress.advance(nbytes=len(data))
            self._buffer += data
        if size < 0:
            out, self._buffer = self._buffer, b""
        else:
            out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


def export_snapshot(
    repo: Repository,
    snapshot: str,
    target: str | Path,
    *,
    include: list[str] | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> tuple[int, int]:
    """Write ``snapshot`` to ``target``. Returns ``(files, bytes)``. The file appears only when complete."""
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    target = Path(target)
    mode = archive_mode(target)
    if target.exists():
        raise ConfigError(f"{target} already exists; choose another name")
    snap = repo.find_snapshot(snapshot)
    entries = repo.tree(snap.id)
    if include:
        globs = GlobSet(include)
        entries = [e for e in entries if globs.matches_path_or_parent(e.path)]
    progress.stage("Exporting", total_items=len(entries), total_bytes=sum(e.size for e in entries))
    tmp = target.with_name(f".{target.name}.duplio-tmp-{os.getpid()}")
    files = nbytes = 0
    try:
        if mode == "zip":
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for e in entries:
                    cancel.check()
                    _check_path(e.path)
                    stamp = time.gmtime(max(e.mtime_ns, 315_532_800 * 10**9) / 1e9)[:6]  # ZIP cannot hold dates before 1980
                    info = zipfile.ZipInfo(e.path, date_time=stamp)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    if e.mode is not None:
                        info.external_attr = (e.mode & 0xFFFF) << 16
                    with archive.open(info, "w", force_zip64=e.size > 2**31) as out:
                        for oid in e.chunks:
                            cancel.check()
                            data = repo.get(oid)
                            out.write(data)
                            progress.advance(nbytes=len(data))
                    files += 1
                    nbytes += e.size
                    progress.advance(items=1)
        else:
            with tarfile.open(tmp, mode) as archive:  # type: ignore[call-overload]
                for e in entries:
                    cancel.check()
                    _check_path(e.path)
                    member = tarfile.TarInfo(e.path)
                    member.size = e.size
                    member.mtime = e.mtime_ns // 10**9
                    member.mode = (e.mode if e.mode is not None else 0o644) & 0o7777
                    archive.addfile(member, _ChunkStream(repo, e, cancel, progress))  # type: ignore[arg-type]
                    files += 1
                    nbytes += e.size
                    progress.advance(items=1)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        rename_noreplace(str(tmp), str(target))  # fails rather than replace a file that appeared meanwhile
        fsync_dir(str(target.parent))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return files, nbytes


def _check_path(path: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise RepositoryError(f"unsafe path in snapshot: {path!r}")
