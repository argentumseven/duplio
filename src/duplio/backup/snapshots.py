"""Browsable point-in-time snapshots built from hard links (rsnapshot / Time Machine style).

Every snapshot is an ordinary folder holding the complete tree of the source as it was.
Files that did not change since the previous snapshot are *hard links* to the previous
snapshot's files, so they take no extra space, yet every snapshot can be opened, searched
and copied from with any file manager, and any one of them can be deleted without
affecting the others.

::

    DEST/
      2026-10-12T183750Z/           full tree
      2026-10-13T020000Z/           unchanged files are hard links to the folder above
      .duplio-snapshots/<name>.json one small record per snapshot (source, label, counts)

Safety:

* a snapshot is built in ``.incomplete-<id>`` and renamed into place only when finished,
  so an interrupted run never leaves something that looks like a snapshot;
* files in a snapshot are made read-only: because links share data, editing one file in
  one snapshot would silently change it in every snapshot that links to it;
* the destination must really support hard links (this is tested, not assumed);
* copies are verified by reading them back, like every other copy Duplio makes.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from duplio.backup import lockfile
from duplio.backup.repository import SnapshotMeta
from duplio.compare.scheduler import Job, run_jobs
from duplio.errors import RepositoryError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.scan.filters import ScanFilter
from duplio.transfer import copy_verified
from duplio.util.fmt import iso_now
from duplio.util.text import json_dumps
from duplio.util.throttle import RateLimiter
from duplio.util.timecmp import mtimes_match, tolerance_for
from duplio.vfs.base import RawEntry, WalkStats
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import Location

META_DIR = ".duplio-snapshots"
INCOMPLETE = ".incomplete-"
NAME_FORMAT = "%Y-%m-%dT%H%M%SZ"


@dataclass
class SnapshotReport:
    meta: SnapshotMeta
    name: str
    linked: int = 0
    copied: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    changed_during: list[str] = field(default_factory=list)


class SnapshotSet:
    """The snapshots stored under one destination folder."""

    def __init__(self, location: Location) -> None:
        self.loc = location
        self.b = location.backend

    # ------------------------------------------------------------------ records
    def _meta_path(self, name: str) -> str:
        return self.b.join(self.loc.path, f"{META_DIR}/{name}.json")

    def snapshots(self) -> list[tuple[str, SnapshotMeta]]:
        """Complete snapshots, oldest first, as ``(folder name, meta)``."""
        folder = self.b.join(self.loc.path, META_DIR)
        if self.b.stat(folder) is None:
            return []
        out = []
        for entry, is_dir in self.b.listdir(folder):
            if is_dir or not entry.endswith(".json"):
                continue
            name = entry[: -len(".json")]
            try:
                with self.b.open_read(self.b.join(folder, entry)) as handle:
                    meta = SnapshotMeta.from_dict(json.loads(handle.read()))
            except (OSError, ValueError, TypeError):
                continue
            meta.id = name  # the file name is the identity; an edited record cannot point at another folder
            if self.b.stat(self.b.join(self.loc.path, name)) is not None:
                out.append((name, meta))
        out.sort(key=lambda item: (item[1].time, item[0]))
        return out

    def _write_meta(self, name: str, meta: SnapshotMeta) -> None:
        folder = self.b.join(self.loc.path, META_DIR)
        self.b.makedirs(folder)
        payload = json_dumps(meta.to_dict(), indent=1).encode()
        with self.b.open_write(self._meta_path(name)) as handle:
            handle.write(payload)

    def _lock(self, purpose: str):  # type: ignore[no-untyped-def]
        """Only one snapshot create/forget at a time per destination (a scheduled run may start while one is busy)."""
        return lockfile.exclusive(self.b, self.b.join(self.loc.path, f"{META_DIR}/.lock"), purpose,
                                  hint="If that process is gone, delete the file '.lock' in the .duplio-snapshots folder.")

    # ------------------------------------------------------------------ support check
    def require_hardlinks(self) -> None:
        caps = self.loc.caps
        if not caps.can_hardlink:
            raise SafetyError(
                f"{self.loc.display} ({caps.label}) does not support hard links, which snapshots need. "
                "Use 'duplio backup run' (deduplicated repository) or 'duplio mirror' there instead."
            )
        probe = self.b.join(self.loc.path, f"{INCOMPLETE}probe-{secrets.token_hex(3)}")
        self.b.makedirs(probe)
        first, second = self.b.join(probe, "a"), self.b.join(probe, "b")
        try:
            with self.b.open_write(first) as handle:
                handle.write(b"x")
            self.b.hardlink(first, second)
            info = self.b.stat(second)
            if info is None or info.nlink < 2:
                raise OSError("link count did not increase")
        except (OSError, NotImplementedError) as exc:
            raise SafetyError(f"hard links do not work on {self.loc.display}: {exc}") from exc
        finally:
            for path in (second, first):
                with contextlib.suppress(Exception):
                    self.b.remove(path)
            with contextlib.suppress(Exception):
                self.b.remove_empty_dirs(probe, self.loc.path)

    # ------------------------------------------------------------------ create
    def create(
        self,
        source: Location,
        *,
        scan_filter: ScanFilter | None = None,
        label: str = "",
        tags: list[str] | None = None,
        one_file_system: bool = True,
        limiter: RateLimiter | None = None,
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
        now: float | None = None,
    ) -> SnapshotReport:
        info = source.backend.stat(source.path)
        if info is None or not info.is_dir:
            raise RepositoryError(f"source folder not found: {source.display}")
        self.require_hardlinks()
        with self._lock("snapshot"):
            return self._create_locked(source, scan_filter=scan_filter, label=label, tags=tags,
                                       one_file_system=one_file_system, limiter=limiter, progress=progress,
                                       cancel=cancel, now=now)

    def _create_locked(
        self,
        source: Location,
        *,
        scan_filter: ScanFilter | None,
        label: str,
        tags: list[str] | None,
        one_file_system: bool,
        limiter: RateLimiter | None,
        progress: ProgressSink | None,
        cancel: CancelToken | None,
        now: float | None,
    ) -> SnapshotReport:
        progress = progress or NullProgress()
        cancel = cancel or CancelToken()
        scan_filter = scan_filter or ScanFilter.for_backup()
        sb, db = source.backend, self.b
        if sb.id == db.id and (db.is_within(self.loc.path, source.path) or db.is_within(source.path, self.loc.path)):
            raise RepositoryError("the snapshot folder cannot be inside the folder being backed up (or vice versa)")
        self._remove_leftovers()

        previous = None
        for name, meta in reversed(self.snapshots()):
            if meta.source == source.uri and meta.label == label:
                previous = (name, meta)
                break

        stamp = time.time() if now is None else now
        name = datetime.fromtimestamp(stamp, UTC).strftime(NAME_FORMAT) + (f"-{_slug(label)}" if label else "")
        if db.stat(db.join(self.loc.path, name)) is not None or self._meta_exists(name):
            name += f"-{secrets.token_hex(2)}"  # two snapshots in the same second
        build_id = secrets.token_hex(4)
        building = db.join(self.loc.path, f"{INCOMPLETE}{build_id}")
        db.makedirs(building)
        try:
            progress.stage("Listing files")
            walk = WalkStats()
            files: list[RawEntry] = []
            for raw in sb.walk(source.path, walk_filter=scan_filter, stats=walk, one_file_system=one_file_system,
                               should_stop=lambda: cancel.cancelled):
                if scan_filter.uses_time and scan_filter.skip_by_time(raw.mtime_ns):
                    continue
                files.append(raw)
            cancel.check()

            meta = SnapshotMeta(
                id=name, time=iso_now(), host=socket.gethostname(), source=source.uri, label=label,
                tags=sorted(set(tags or [])), parent=previous[0] if previous else None, files=len(files),
                bytes=sum(f.size for f in files),
            )
            report = SnapshotReport(meta=meta, name=name)
            report.errors.extend(walk.errors)
            prev_dir = db.join(self.loc.path, previous[0]) if previous else None
            tolerance = tolerance_for(self.loc.caps.mtime_resolution_ns, source.caps.mtime_resolution_ns, floor_ns=1_000)
            lock = threading.Lock()
            algorithm = default_full_algorithm()
            progress.stage("Snapshotting", total_items=len(files), total_bytes=sum(f.size for f in files))

            def make(raw: RawEntry):  # type: ignore[no-untyped-def]
                def job() -> None:
                    target = db.join(building, raw.rel)
                    try:
                        current = sb.stat(raw.path)
                        src_mode = None if current is None or current.mode is None else current.mode & 0o7777
                        linked = False
                        if prev_dir is not None:
                            old = db.join(prev_dir, raw.rel)
                            old_info = db.stat(old)
                            if (old_info is not None and old_info.is_file and not old_info.is_symlink
                                    and old_info.size == raw.size
                                    and mtimes_match(old_info.mtime_ns, raw.mtime_ns, tolerance_ns=tolerance)
                                    and _same_permissions(old_info.mode, src_mode)):
                                db.makedirs(db.parent(target))
                                db.hardlink(old, target)
                                linked = True
                        if not linked:
                            copy_verified((sb, raw.path), (db, target), algorithm=algorithm, expected_size=raw.size,
                                          mtime_ns=raw.mtime_ns, cancel=cancel, limiter=limiter,
                                          on_bytes=lambda n: progress.advance(nbytes=n))
                            _make_read_only(db, target, src_mode)
                        after = sb.stat(raw.path)
                        with lock:
                            if linked:
                                report.linked += 1
                                progress.advance(nbytes=raw.size)
                            else:
                                report.copied += 1
                                meta.new_bytes += raw.size
                            if after is not None and (after.size != raw.size or after.mtime_ns != raw.mtime_ns):
                                report.changed_during.append(raw.rel)
                    except Exception as exc:
                        from duplio.errors import CancelledError

                        if isinstance(exc, CancelledError):
                            raise
                        with lock:
                            report.errors.append((sb.display(raw.path), str(exc)))
                    progress.advance(items=1)

                return job

            device = sb.device_key(source.path)
            run_jobs([Job(device, source.caps.max_parallel_reads, make(f)) for f in files], cancel=cancel)
            cancel.check()
            meta.reused_files = report.linked
            meta.errors = len(report.errors)
            meta.files = report.linked + report.copied
            db.rename(building, db.join(self.loc.path, name))
        except BaseException:
            with contextlib.suppress(Exception):
                _remove_tree(db, building)
            raise
        self._write_meta(name, meta)
        self._point_latest(name)
        return report

    def _meta_exists(self, name: str) -> bool:
        return self.b.stat(self._meta_path(name)) is not None

    def _point_latest(self, name: str) -> None:
        if not isinstance(self.b, LocalBackend) or os.name == "nt":
            return
        link = os.path.join(self.loc.path, "latest")
        tmp = f"{link}.tmp-{secrets.token_hex(3)}"
        try:
            os.symlink(name, tmp)
            os.replace(tmp, link)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _remove_leftovers(self) -> None:
        """Folders from interrupted runs: they only hold links and fresh copies, never the only copy of anything."""
        for entry, is_dir in self.b.listdir(self.loc.path):
            if is_dir and entry.startswith(INCOMPLETE):
                with contextlib.suppress(Exception):
                    _remove_tree(self.b, self.b.join(self.loc.path, entry))

    # ------------------------------------------------------------------ forget
    def forget(self, name: str) -> int:
        """Delete one snapshot. Returns the bytes freed (files that no other snapshot links to)."""
        if not name or name.startswith(".") or "/" in name or "\\" in name or name in (".", ".."):
            raise SafetyError(f"not a snapshot folder name: {name!r}")
        with self._lock("forget"):
            return self._forget_locked(name)

    def _forget_locked(self, name: str) -> int:
        record = self._meta_path(name)
        if self.b.stat(record) is not None:
            self.b.remove(record)  # first: from here on the snapshot no longer exists
        folder = self.b.join(self.loc.path, name)
        freed = _remove_tree(self.b, folder)
        link = os.path.join(self.loc.path, "latest")
        if isinstance(self.b, LocalBackend) and os.path.islink(link) and os.readlink(link) == name:
            remaining = self.snapshots()
            with contextlib.suppress(OSError):
                os.unlink(link)
            if remaining:
                self._point_latest(remaining[-1][0])
        return freed


def _slug(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]


_WRITE_BITS = 0o222


def _same_permissions(snapshot_mode: int | None, source_mode: int | None) -> bool:
    """Snapshot files are read-only, so only the other bits (read, execute, setuid…) must match the source."""
    if snapshot_mode is None or source_mode is None:
        return True
    return ((snapshot_mode ^ source_mode) & 0o7777 & ~_WRITE_BITS) == 0


def _make_read_only(backend, path: str, source_mode: int | None = None) -> None:  # type: ignore[no-untyped-def]
    """Keep the source's permissions (a 0600 key stays private, a script stays executable) minus the write bits."""
    if isinstance(backend, LocalBackend):
        with contextlib.suppress(OSError):
            mode = source_mode if source_mode is not None else os.stat(path).st_mode & 0o7777
            os.chmod(path, mode & ~_WRITE_BITS)


def _remove_tree(backend, folder: str) -> int:  # type: ignore[no-untyped-def]
    """Delete a snapshot folder; returns bytes freed (only files whose last link this was)."""
    freed = 0
    for raw in list(backend.walk(folder, walk_filter=None)):
        if raw.nlink <= 1:
            freed += raw.size
        if os.name == "nt":  # pragma: no cover - read-only files cannot be deleted on Windows
            with contextlib.suppress(OSError):
                os.chmod(raw.path, 0o666)
        backend.remove(raw.path)
    _remove_dirs(backend, folder)
    return freed


def _remove_dirs(backend, folder: str) -> None:  # type: ignore[no-untyped-def]
    for name, is_dir in backend.listdir(folder):
        if is_dir:
            _remove_dirs(backend, backend.join(folder, name))
    backend.remove_empty_dirs(folder, backend.parent(folder))
