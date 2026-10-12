"""Deduplicated, versioned snapshot repository.

Layout (on any writable backend: local disk, USB drive, NAS via SMB)::

    REPO/
      duplio-repo.json            format, id, chunk size, hash
      objects/ab/ab12…ef          content-addressed chunks ("DPO1" header, optional zlib)
      trees/<snapshot>.json.gz    full file list of a snapshot
      snapshots/<snapshot>.json   small summary; written LAST, so a snapshot
                                  only exists once all its data is durable
      locks/repo.lock             exclusive writer lock

Design choices:

* **Content addressing** (BLAKE2b-256 of the plaintext) gives deduplication
  across files, snapshots and machines: a photo backed up from the phone and
  from the laptop is stored once.
* **Fixed-size chunks** (default 4 MiB) dedupe in-place edits of large files
  (disk images, databases) without the CPU cost of content-defined chunking
  in pure Python.
* **Incremental by metadata**: files whose size and mtime match the parent
  snapshot reuse its chunk list without being read — the same trick restic
  uses — unless ``rehash`` is requested.
* **Self-verifying reads**: every chunk is re-hashed when read, so restore
  and ``check --read-data`` detect bit rot and tampering.
* BLAKE2b ships with Python, so a repository can always be restored without
  optional packages.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import random
import secrets
import socket
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from duplio import __version__
from duplio.backup.crypto import PasswordRequired, RepoCrypto, require_library
from duplio.compare.scheduler import Job, run_jobs
from duplio.errors import LockError, RepositoryError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.scan.filters import ScanFilter, extension
from duplio.util.fmt import iso_now, utc_stamp_ms
from duplio.util.text import json_dumps
from duplio.util.throttle import RateLimiter
from duplio.vfs.base import RawEntry
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import Location

REPO_FORMAT = "duplio-repo"
REPO_VERSION = 2  # 2: may carry an 'encryption' block; version-1 repositories (unencrypted) still open
CONFIG_NAME = "duplio-repo.json"
OBJECT_MAGIC = b"DPO1"
FLAG_RAW = 0
FLAG_ZLIB = 1
DEFAULT_CHUNK = 4 * 1024 * 1024
STALE_LOCK_S = 12 * 3600

# Already-compressed formats: compressing them again wastes CPU for nothing.
INCOMPRESSIBLE = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif", "mp4", "m4v", "mov", "mkv", "webm",
        "avi", "3gp", "mp3", "m4a", "aac", "ogg", "opus", "flac", "zip", "gz", "bz2", "xz", "zst", "7z",
        "rar", "jar", "apk", "ipa", "docx", "xlsx", "pptx", "odt", "pdf", "dng", "cr2", "cr3", "nef", "arw",
    }
)  # fmt: skip


def password_from_environment() -> str | None:
    """DUPLIO_REPO_PASSWORD, or the first line of the file named by DUPLIO_REPO_PASSWORD_FILE."""
    value = os.environ.get("DUPLIO_REPO_PASSWORD")
    if value:
        return value
    path = os.environ.get("DUPLIO_REPO_PASSWORD_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.readline().rstrip("\r\n") or None
        except OSError as exc:
            raise RepositoryError(f"cannot read the password file {path}: {exc}") from exc
    return None


def object_id(data: bytes | memoryview) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def encode_object(data: bytes, compress: bool) -> bytes:
    if compress and len(data) > 512:
        packed = zlib.compress(data, 3)
        if len(packed) < len(data) * 0.9:
            return OBJECT_MAGIC + bytes([FLAG_ZLIB]) + packed
    return OBJECT_MAGIC + bytes([FLAG_RAW]) + data


def decode_object(blob: bytes) -> bytes:
    if blob[:4] != OBJECT_MAGIC or len(blob) < 5:
        raise RepositoryError("object has an invalid header")
    flag = blob[4]
    if flag == FLAG_RAW:
        return blob[5:]
    if flag == FLAG_ZLIB:
        try:
            return zlib.decompress(blob[5:])
        except zlib.error as exc:
            raise RepositoryError(f"object cannot be decompressed: {exc}") from exc
    raise RepositoryError(f"object uses unknown encoding {flag}")


@dataclass
class TreeEntry:
    path: str  # POSIX, relative to the backed-up source
    size: int
    mtime_ns: int
    chunks: list[str]
    mode: int | None = None

    def to_list(self) -> list[Any]:
        return [self.path, self.size, self.mtime_ns, self.chunks, self.mode]

    @classmethod
    def from_list(cls, row: list[Any]) -> TreeEntry:
        return cls(str(row[0]), int(row[1]), int(row[2]), list(row[3]), row[4] if len(row) > 4 else None)


@dataclass
class SnapshotMeta:
    id: str
    time: str
    host: str
    source: str
    label: str
    tags: list[str]
    parent: str | None
    files: int
    bytes: int
    new_bytes: int = 0
    stored_bytes: int = 0
    reused_files: int = 0
    errors: int = 0
    duplio_version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return {"format": "duplio-snapshot", "version": 1, **self.__dict__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SnapshotMeta:
        fields = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**fields)

    @property
    def timestamp(self) -> float:
        from datetime import datetime

        return datetime.fromisoformat(self.time).timestamp()


@dataclass
class FileVersion:
    """One distinct content of a path, and the snapshots it appears in."""

    size: int
    mtime_ns: int
    chunks: tuple[str, ...]
    snapshots: list[str] = field(default_factory=list)
    times: list[str] = field(default_factory=list)


@dataclass
class FoundFile:
    path: str
    versions: list[FileVersion] = field(default_factory=list)

    def add(self, snap: SnapshotMeta, entry: TreeEntry) -> None:
        key = tuple(entry.chunks)
        for v in self.versions:
            if v.chunks == key:
                v.snapshots.append(snap.id)
                v.times.append(snap.time)
                return
        self.versions.append(FileVersion(entry.size, entry.mtime_ns, key, [snap.id], [snap.time]))

    @property
    def snapshot_count(self) -> int:
        return sum(len(v.snapshots) for v in self.versions)


@dataclass
class BackupReport:
    snapshot: SnapshotMeta
    errors: list[tuple[str, str]] = field(default_factory=list)
    changed_during_backup: list[str] = field(default_factory=list)


@dataclass
class CheckReport:
    snapshots: int = 0
    objects: int = 0
    missing: dict[str, list[str]] = field(default_factory=dict)  # snapshot -> missing oids
    corrupt: list[str] = field(default_factory=list)
    unreadable_trees: list[str] = field(default_factory=list)
    orphans: int = 0
    read_objects: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt and not self.unreadable_trees


@dataclass
class RestoreReport:
    restored: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    bytes: int = 0


class Repository:
    def __init__(self, location: Location, config: dict[str, Any], crypto: RepoCrypto | None = None) -> None:
        self.loc = location
        self.b = location.backend
        self.config = config
        self.crypto = crypto
        self.chunk_size = int(config.get("chunk_size", DEFAULT_CHUNK))
        self.compression = str(config.get("compression", "auto"))
        self._index: set[str] | None = None
        self._index_lock = threading.Lock()
        self.limiter: RateLimiter | None = None  # set by the caller to cap upload speed

    # ------------------------------------------------------------------ setup
    def p(self, rel: str) -> str:
        return self.b.join(self.loc.path, rel)

    @classmethod
    def init(
        cls,
        location: Location,
        *,
        chunk_size: int = DEFAULT_CHUNK,
        compression: str = "auto",
        password: str | None = None,
        scrypt_n: int | None = None,
    ) -> Repository:
        """Create a repository; with ``password`` everything stored in it is encrypted."""
        b = location.backend
        if password is not None:
            require_library()
        if compression not in ("auto", "always", "never"):
            raise RepositoryError("compression must be auto, always or never")
        if not 64 * 1024 <= chunk_size <= 64 * 1024 * 1024:
            raise RepositoryError("chunk size must be between 64 KiB and 64 MiB")
        info = b.stat(location.path)
        if info is not None:
            if not info.is_dir:
                raise RepositoryError(f"{location.display} exists and is not a folder")
            existing = [n for n, _ in b.listdir(location.path)]
            if CONFIG_NAME in existing:
                raise RepositoryError(f"{location.display} already contains a Duplio repository")
            if existing:
                raise RepositoryError(f"{location.display} is not empty; choose an empty or new folder")
        b.makedirs(location.path)
        repo_id = secrets.token_hex(16)
        config: dict[str, Any] = {
            "format": REPO_FORMAT,
            "version": REPO_VERSION if password is not None else 1,
            "id": repo_id,
            "created": iso_now(),
            "hash": "blake2b-256" if password is None else "hmac-sha256",
            "chunk_size": chunk_size,
            "compression": compression,
            "created_by": f"duplio {__version__}",
        }
        crypto = None
        if password is not None:
            from duplio.backup.crypto import DEFAULT_SCRYPT_N

            crypto, config["encryption"] = RepoCrypto.create(password, repo_id, scrypt_n=scrypt_n or DEFAULT_SCRYPT_N)
        for sub in ("objects", "trees", "snapshots", "locks"):
            b.makedirs(b.join(location.path, sub))
        with b.open_write(b.join(location.path, CONFIG_NAME)) as handle:
            handle.write(json.dumps(config, indent=2).encode())
        return cls(location, config, crypto)

    @classmethod
    def open(cls, location: Location, *, password: str | None = None) -> Repository:
        """Open a repository. An encrypted one needs ``password`` (or DUPLIO_REPO_PASSWORD / _FILE)."""
        b = location.backend
        try:
            with b.open_read(b.join(location.path, CONFIG_NAME)) as handle:
                config = json.loads(handle.read())
        except FileNotFoundError:
            raise RepositoryError(
                f"{location.display} is not a Duplio repository (run 'duplio backup init' first)"
            ) from None
        except (OSError, ValueError) as exc:
            if not b.exists(b.join(location.path, CONFIG_NAME)):
                raise RepositoryError(
                    f"{location.display} is not a Duplio repository (run 'duplio backup init' first)"
                ) from None
            raise RepositoryError(f"cannot read repository config: {exc}") from exc
        if config.get("format") != REPO_FORMAT:
            raise RepositoryError(f"{location.display} is not a Duplio repository")
        if int(config.get("version", 0)) > REPO_VERSION:
            raise RepositoryError("repository was created by a newer Duplio; please upgrade")
        crypto = None
        if "encryption" in config:
            password = password if password is not None else password_from_environment()
            if password is None:
                raise PasswordRequired(
                    f"{location.display} is encrypted: enter its password, or set DUPLIO_REPO_PASSWORD "
                    "(or DUPLIO_REPO_PASSWORD_FILE)"
                )
            crypto = RepoCrypto.unlock(password, config["encryption"], str(config["id"]))
        return cls(location, config, crypto)

    @property
    def encrypted(self) -> bool:
        return self.crypto is not None

    def change_password(self, new_password: str, *, scrypt_n: int | None = None) -> None:
        """Re-wrap the master key under a new password. Data is untouched; the old password stops working."""
        if self.crypto is None:
            raise RepositoryError("this repository is not encrypted")
        from duplio.backup.crypto import DEFAULT_SCRYPT_N

        config = dict(self.config)
        config["encryption"] = self.crypto.rewrap(new_password, str(config["id"]), scrypt_n=scrypt_n or DEFAULT_SCRYPT_N)
        with self.lock("change-password"):
            with self.b.open_write(self.p(CONFIG_NAME), overwrite=True) as handle:
                handle.write(json.dumps(config, indent=2).encode())
            self.config = config

    # ------------------------------------------------------------------ locking
    @contextlib.contextmanager
    def lock(self, purpose: str) -> Iterator[None]:
        path = self.p("locks/repo.lock")
        info = {"pid": os.getpid(), "host": socket.gethostname(), "time": time.time(), "purpose": purpose}
        for _ in range(2):
            try:
                with self.b.open_write(path) as handle:
                    handle.write(json.dumps(info).encode())
                break
            except FileExistsError:
                held = self._read_lock(path)
                if self._is_stale(held):
                    with contextlib.suppress(Exception):
                        self.b.remove(path)
                    continue
                raise LockError(
                    f"repository is locked by {held.get('purpose', 'another operation')} "
                    f"(pid {held.get('pid')} on {held.get('host')}). If that process is gone, run "
                    f"'duplio backup unlock'."
                ) from None
        else:  # pragma: no cover
            raise LockError("could not lock the repository")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                self.b.remove(path)

    def _read_lock(self, path: str) -> dict[str, Any]:
        try:
            with self.b.open_read(path) as handle:
                return dict(json.loads(handle.read() or b"{}"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _is_stale(held: dict[str, Any]) -> bool:
        if not held:
            return False
        if time.time() - float(held.get("time", 0)) > STALE_LOCK_S:
            return True
        if held.get("host") == socket.gethostname():
            from duplio.plan.journal import _pid_alive

            return not _pid_alive(int(held.get("pid", -1)))
        return False

    def force_unlock(self) -> bool:
        path = self.p("locks/repo.lock")
        if self.b.exists(path):
            self.b.remove(path)
            return True
        return False

    # ------------------------------------------------------------------ objects
    def object_path(self, oid: str) -> str:
        return self.p(f"objects/{oid[:2]}/{oid}")

    def index(self) -> set[str]:
        with self._index_lock:
            if self._index is None:
                found: set[str] = set()
                objects_dir = self.p("objects")
                for name, is_dir in self.b.listdir(objects_dir):
                    if not is_dir or len(name) != 2:
                        continue
                    for obj, obj_is_dir in self.b.listdir(self.b.join(objects_dir, name)):
                        if not obj_is_dir and len(obj) == 64 and obj.startswith(name):
                            found.add(obj)
                self._index = found
            return self._index

    def put(self, data: bytes, *, compress: bool) -> tuple[str, int, bool]:
        """Store a chunk; returns ``(oid, stored_bytes, newly_written)``."""
        oid = self.oid(data)
        index = self.index()
        with self._index_lock:
            if oid in index:
                return oid, 0, False
        blob = encode_object(data, compress)
        if self.crypto is not None:
            blob = self.crypto.seal(blob, oid.encode())
        path = self.object_path(oid)
        if self.limiter is not None:
            self.limiter.acquire(len(blob))
        try:
            with self.b.open_write(path) as handle:
                handle.write(blob)
        except FileExistsError:
            pass  # written concurrently by another worker: identical content by definition
        with self._index_lock:
            index.add(oid)
        return oid, len(blob), True

    def get(self, oid: str) -> bytes:
        with self.b.open_read(self.object_path(oid)) as handle:
            blob = handle.read()
        if self.crypto is not None:
            blob = self.crypto.open(blob, oid.encode())
        data = decode_object(blob)
        if self.oid(data) != oid:
            raise RepositoryError(f"object {oid[:12]}… is corrupt (content does not match its id)")
        return data

    def oid(self, data: bytes | memoryview) -> str:
        """The id a chunk is stored under: its BLAKE2b hash, or a keyed HMAC in an encrypted repository."""
        return object_id(data) if self.crypto is None else self.crypto.object_id(data)

    # ---- small metadata files (file lists, snapshot summaries)
    def _seal_meta(self, kind: str, name: str, payload: bytes) -> bytes:
        return payload if self.crypto is None else self.crypto.seal(payload, f"{kind}:{name}".encode())

    def _open_meta(self, kind: str, name: str, blob: bytes) -> bytes:
        return blob if self.crypto is None else self.crypto.open(blob, f"{kind}:{name}".encode())

    # ------------------------------------------------------------------ snapshots
    def snapshots(self) -> list[SnapshotMeta]:
        out = []
        for name, is_dir in self.b.listdir(self.p("snapshots")):
            if is_dir or not name.endswith(".json"):
                continue
            try:
                with self.b.open_read(self.p(f"snapshots/{name}")) as handle:
                    raw = self._open_meta("snap", name[: -len(".json")], handle.read())
                out.append(SnapshotMeta.from_dict(json.loads(raw)))
            except (OSError, ValueError, TypeError, RepositoryError):
                continue
        out.sort(key=lambda s: (s.time, s.id))
        return out

    def find_snapshot(self, ref: str) -> SnapshotMeta:
        snaps = self.snapshots()
        if not snaps:
            raise RepositoryError("the repository has no snapshots yet")
        if ref == "latest":
            return snaps[-1]
        matches = [s for s in snaps if s.id == ref or s.id.startswith(ref)]
        if not matches:
            raise RepositoryError(f"no snapshot matches {ref!r}")
        if len(matches) > 1:
            raise RepositoryError(f"{ref!r} is ambiguous; use more characters")
        return matches[0]

    def tree(self, snapshot_id: str) -> list[TreeEntry]:
        with self.b.open_read(self.p(f"trees/{snapshot_id}.json.gz")) as handle:
            data = json.loads(gzip.decompress(self._open_meta("tree", snapshot_id, handle.read())))
        return [TreeEntry.from_list(row) for row in data["entries"]]

    def _write_snapshot(self, meta: SnapshotMeta, entries: list[TreeEntry]) -> None:
        tree = {"format": "duplio-tree", "version": 1, "snapshot": meta.id, "entries": [e.to_list() for e in entries]}
        with self.b.open_write(self.p(f"trees/{meta.id}.json.gz")) as handle:
            handle.write(self._seal_meta("tree", meta.id, gzip.compress(json_dumps(tree).encode(), 6)))
        with self.b.open_write(self.p(f"snapshots/{meta.id}.json")) as handle:
            handle.write(self._seal_meta("snap", meta.id, json_dumps(meta.to_dict(), indent=1).encode()))

    def delete_snapshot(self, snapshot_id: str) -> None:
        # Summary first: once it is gone the snapshot no longer exists, even if
        # removing the tree is interrupted (prune will collect the leftovers).
        self.b.remove(self.p(f"snapshots/{snapshot_id}.json"))
        with contextlib.suppress(OSError):
            self.b.remove(self.p(f"trees/{snapshot_id}.json.gz"))

    # ------------------------------------------------------------------ backup
    def backup(
        self,
        source: Location,
        *,
        scan_filter: ScanFilter | None = None,
        label: str = "",
        tags: list[str] | None = None,
        rehash: bool = False,
        one_file_system: bool = True,
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
    ) -> BackupReport:
        progress = progress or NullProgress()
        cancel = cancel or CancelToken()
        scan_filter = scan_filter or ScanFilter.for_backup()
        if source.backend.id == self.b.id and (
            self.b.is_within(self.loc.path, source.path) or self.b.is_within(source.path, self.loc.path)
        ):
            raise RepositoryError("the repository cannot be inside the folder being backed up (or vice versa)")
        info = source.backend.stat(source.path)
        if info is None or not info.is_dir:
            raise RepositoryError(f"source folder not found: {source.display}")

        with self.lock("backup"):
            parent = None
            for snap in reversed(self.snapshots()):
                if snap.source == source.uri and snap.label == label:
                    parent = snap
                    break
            parent_tree: dict[str, TreeEntry] = {}
            if parent is not None:
                try:
                    parent_tree = {e.path: e for e in self.tree(parent.id)}
                except (OSError, ValueError):
                    progress.message("parent snapshot tree unreadable; doing a full backup", level="warning")

            progress.stage("Listing files")
            from duplio.vfs.base import WalkStats

            stats = WalkStats()
            files: list[RawEntry] = []
            for raw in source.backend.walk(
                source.path,
                walk_filter=scan_filter,
                stats=stats,
                one_file_system=one_file_system,
                should_stop=lambda: cancel.cancelled,
            ):
                files.append(raw)
                if len(files) % 512 == 0:
                    progress.advance(items=512, detail=raw.rel)
            cancel.check()
            index = self.index()
            report = BackupReport(
                snapshot=SnapshotMeta(
                    id=f"{utc_stamp_ms()}-{secrets.token_hex(2)}",
                    time=iso_now(),
                    host=socket.gethostname(),
                    source=source.uri,
                    label=label,
                    tags=sorted(set(tags or [])),
                    parent=parent.id if parent else None,
                    files=0,
                    bytes=0,
                )
            )
            report.errors.extend(stats.errors)
            entries: dict[str, TreeEntry] = {}
            lock = threading.Lock()
            todo: list[RawEntry] = []
            for raw in files:
                prev = parent_tree.get(raw.rel)
                if (
                    not rehash
                    and prev is not None
                    and prev.size == raw.size
                    and prev.mtime_ns == raw.mtime_ns
                    and all(c in index for c in prev.chunks)
                ):
                    entries[raw.rel] = TreeEntry(raw.rel, raw.size, raw.mtime_ns, list(prev.chunks), prev.mode)
                    report.snapshot.reused_files += 1
                else:
                    todo.append(raw)
            progress.stage("Backing up", total_items=len(todo), total_bytes=sum(r.size for r in todo))
            caps = source.backend.capabilities(source.path)
            device = source.backend.device_key(source.path)

            def make_job(raw: RawEntry):  # type: ignore[no-untyped-def]
                def job() -> None:
                    try:
                        entry, new_bytes, stored, changed = self._store_file(source, raw, cancel, progress)
                    except Exception as exc:
                        from duplio.errors import CancelledError

                        if isinstance(exc, CancelledError):
                            raise
                        with lock:
                            report.errors.append((source.backend.display(raw.path), str(exc)))
                            report.snapshot.errors += 1
                        progress.advance(items=1)
                        return
                    with lock:
                        entries[raw.rel] = entry
                        report.snapshot.new_bytes += new_bytes
                        report.snapshot.stored_bytes += stored
                        if changed:
                            report.changed_during_backup.append(raw.rel)
                    progress.advance(items=1)

                return job

            run_jobs([Job(device, caps.max_parallel_reads, make_job(r)) for r in todo], cancel=cancel)
            ordered = [entries[k] for k in sorted(entries)]
            report.snapshot.files = len(ordered)
            report.snapshot.bytes = sum(e.size for e in ordered)
            self._write_snapshot(report.snapshot, ordered)
            return report

    def _store_file(
        self, source: Location, raw: RawEntry, cancel: CancelToken, progress: ProgressSink
    ) -> tuple[TreeEntry, int, int, bool]:
        b = source.backend
        compress = self.compression == "always" or (
            self.compression == "auto" and extension(raw.rel) not in INCOMPRESSIBLE
        )
        mode = None
        if isinstance(b, LocalBackend):
            with contextlib.suppress(OSError):
                mode = os.stat(raw.path).st_mode & 0o7777
        changed = False
        for attempt in range(3):
            chunks: list[str] = []
            new_bytes = stored = total = 0
            with b.open_read(raw.path) as handle:
                while True:
                    cancel.check()
                    data = _read_exact(handle, self.chunk_size)
                    if not data:
                        break
                    oid, nstored, is_new = self.put(data, compress=compress)
                    chunks.append(oid)
                    total += len(data)
                    if is_new:
                        new_bytes += len(data)
                        stored += nstored
                    progress.advance(nbytes=len(data))
            after = b.stat(raw.path)
            if after is not None and after.size == total and after.mtime_ns == raw.mtime_ns and total == raw.size:
                return TreeEntry(raw.rel, total, raw.mtime_ns, chunks, mode), new_bytes, stored, False
            changed = True
            if after is not None:
                raw = RawEntry(raw.path, raw.rel, after.size, after.mtime_ns, raw.inode, raw.nlink)
            if attempt < 2:
                continue
        # Still changing after retries: keep what was read, flagged as such.
        return TreeEntry(raw.rel, total, raw.mtime_ns, chunks, mode), new_bytes, stored, changed

    # ------------------------------------------------------------------ restore
    def restore(
        self,
        snapshot_id: str,
        target: Location,
        *,
        include: list[str] | None = None,
        conflict: str = "skip",
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
    ) -> RestoreReport:
        if conflict not in ("skip", "rename", "overwrite"):
            raise RepositoryError("conflict policy must be skip, rename or overwrite")
        progress = progress or NullProgress()
        cancel = cancel or CancelToken()
        snap = self.find_snapshot(snapshot_id)
        entries = self.tree(snap.id)
        if include:
            from duplio.scan.filters import GlobSet

            globs = GlobSet(include)
            entries = [e for e in entries if globs.matches_path_or_parent(e.path)]
        report = RestoreReport()
        tb = target.backend
        progress.stage("Restoring", total_items=len(entries), total_bytes=sum(e.size for e in entries))
        for entry in entries:
            cancel.check()
            if ".." in entry.path.split("/") or entry.path.startswith("/"):
                report.errors.append((entry.path, "unsafe path in snapshot; skipped"))
                continue
            dst = tb.join(target.path, entry.path)
            try:
                existing = tb.stat(dst)
                overwrite = False
                if existing is not None:
                    if existing.is_file and existing.size == entry.size and self._matches(tb, dst, entry):
                        report.unchanged += 1
                        progress.advance(items=1, nbytes=entry.size)
                        continue
                    if conflict == "skip":
                        report.conflicts.append(entry.path)
                        progress.advance(items=1, nbytes=entry.size)
                        continue
                    if conflict == "rename":
                        stem, dot, ext = entry.path.rpartition(".")
                        suffix = f".restored-{snap.id[:15]}"
                        alt = f"{stem}{suffix}.{ext}" if dot and "/" not in ext else f"{entry.path}{suffix}"
                        dst = tb.join(target.path, alt)
                        if tb.exists(dst):
                            report.conflicts.append(entry.path)
                            progress.advance(items=1, nbytes=entry.size)
                            continue
                        report.renamed.append(alt)
                    else:
                        overwrite = True
                with tb.open_write(dst, overwrite=overwrite) as handle:
                    for oid in entry.chunks:
                        handle.write(self.get(oid))
                tb.set_mtime(dst, entry.mtime_ns)
                if entry.mode is not None and isinstance(tb, LocalBackend):
                    with contextlib.suppress(OSError):
                        os.chmod(dst, entry.mode)
                report.restored += 1
                report.bytes += entry.size
            except (OSError, RepositoryError) as exc:
                report.errors.append((entry.path, str(exc)))
            progress.advance(items=1, nbytes=entry.size)
        return report

    def _matches(self, backend, path: str, entry: TreeEntry) -> bool:  # type: ignore[no-untyped-def]
        """Does an existing file already hold exactly this snapshot entry?"""
        try:
            with backend.open_read(path) as handle:
                for oid in entry.chunks:
                    if self.oid(_read_exact(handle, self.chunk_size)) != oid:
                        return False
                return not handle.read(1)
        except OSError:
            return False

    # ------------------------------------------------------------------ maintenance
    def check(
        self,
        *,
        read_data: bool = False,
        subset: float | None = None,
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
    ) -> CheckReport:
        progress = progress or NullProgress()
        cancel = cancel or CancelToken()
        report = CheckReport()
        self._index = None
        index = self.index()
        report.objects = len(index)
        referenced: set[str] = set()
        snaps = self.snapshots()
        report.snapshots = len(snaps)
        progress.stage("Checking snapshots", total_items=len(snaps))
        for snap in snaps:
            cancel.check()
            try:
                tree = self.tree(snap.id)
            except (OSError, ValueError, RepositoryError) as exc:
                report.unreadable_trees.append(f"{snap.id}: {exc}")
                continue
            missing = sorted({c for e in tree for c in e.chunks if c not in index})
            if missing:
                report.missing[snap.id] = missing
            referenced.update(c for e in tree for c in e.chunks)
            progress.advance(items=1)
        report.orphans = len(index - referenced)
        if read_data:
            to_read = sorted(referenced & index)
            if subset is not None and 0 < subset < 1:
                to_read = random.sample(to_read, max(1, int(len(to_read) * subset))) if to_read else []
            progress.stage("Reading data", total_items=len(to_read))
            for oid in to_read:
                cancel.check()
                try:
                    self.get(oid)
                except (OSError, RepositoryError):
                    report.corrupt.append(oid)
                report.read_objects += 1
                progress.advance(items=1)
        return report

    def prune(
        self, *, dry_run: bool = False, progress: ProgressSink | None = None
    ) -> tuple[int, int]:
        """Delete chunks no snapshot references. Returns ``(objects, bytes)``."""
        progress = progress or NullProgress()
        with self.lock("prune"):
            self._index = None
            index = self.index()
            referenced: set[str] = set()
            snaps = self.snapshots()
            for snap in snaps:
                try:
                    referenced.update(c for e in self.tree(snap.id) for c in e.chunks)
                except (OSError, ValueError, RepositoryError) as exc:
                    raise RepositoryError(
                        f"snapshot {snap.id} cannot be read ({exc}); refusing to prune until 'duplio backup check' passes"
                    ) from exc
            garbage = sorted(index - referenced)
            freed = 0
            progress.stage("Pruning", total_items=len(garbage))
            for oid in garbage:
                path = self.object_path(oid)
                info = self.b.stat(path)
                freed += info.size if info else 0
                if not dry_run:
                    self.b.remove(path)
                    index.discard(oid)
                progress.advance(items=1)
            if not dry_run:
                self._clean_leftovers({s.id for s in snaps})
            return len(garbage), freed

    def _clean_leftovers(self, live: set[str]) -> None:
        """Remove trees without a summary and temp files from interrupted writes."""
        for name, is_dir in self.b.listdir(self.p("trees")):
            if is_dir:
                continue
            if (name.endswith(".json.gz") and name[: -len(".json.gz")] not in live) or ".duplio-tmp-" in name:
                with contextlib.suppress(OSError):
                    self.b.remove(self.p(f"trees/{name}"))
        objects_dir = self.p("objects")
        for name, is_dir in self.b.listdir(objects_dir):
            if not is_dir:
                continue
            for obj, _ in self.b.listdir(self.b.join(objects_dir, name)):
                if ".duplio-tmp-" in obj:
                    with contextlib.suppress(OSError):
                        self.b.remove(self.b.join(self.b.join(objects_dir, name), obj))

    def find(self, pattern: str, *, snapshot: str | None = None, regex: bool = False,
             progress: ProgressSink | None = None) -> list[FoundFile]:
        """Files whose path matches ``pattern`` (a glob, or a regex) in one snapshot or in all of them.

        Each result lists the distinct versions (by content) and where they occur, so a deleted file or an
        earlier edition is easy to find.
        """
        import re

        from duplio.scan.filters import compile_glob

        matcher = re.compile(pattern, re.IGNORECASE) if regex else compile_glob(pattern)
        search = matcher.search if regex else matcher.match
        progress = progress or NullProgress()
        snaps = [self.find_snapshot(snapshot)] if snapshot else self.snapshots()
        progress.stage("Searching snapshots", total_items=len(snaps))
        found: dict[str, FoundFile] = {}
        for snap in snaps:
            try:
                entries = self.tree(snap.id)
            except (OSError, ValueError, RepositoryError):
                progress.advance(items=1)
                continue
            for e in entries:
                if not search(e.path):
                    continue
                hit = found.setdefault(e.path, FoundFile(e.path))
                hit.add(snap, e)
            progress.advance(items=1)
        return sorted(found.values(), key=lambda f: f.path)

    def history(self, path: str) -> FoundFile | None:
        """Every version of one exact path across all snapshots (None if it never existed)."""
        result = None
        for snap in self.snapshots():
            try:
                entries = self.tree(snap.id)
            except (OSError, ValueError, RepositoryError):
                continue
            for e in entries:
                if e.path == path:
                    result = result or FoundFile(path)
                    result.add(snap, e)
        return result

    def diff(self, a: str, b: str) -> dict[str, list[str]]:
        ta = {e.path: e for e in self.tree(self.find_snapshot(a).id)}
        tb = {e.path: e for e in self.tree(self.find_snapshot(b).id)}
        out: dict[str, list[str]] = defaultdict(list)
        for path in sorted(set(ta) | set(tb)):
            ea, eb = ta.get(path), tb.get(path)
            if ea is None:
                out["added"].append(path)
            elif eb is None:
                out["removed"].append(path)
            elif ea.chunks != eb.chunks:
                out["modified"].append(path)
            elif ea.mtime_ns != eb.mtime_ns or ea.mode != eb.mode:
                out["metadata"].append(path)
        return {k: out.get(k, []) for k in ("added", "removed", "modified", "metadata")}

    def stats(self) -> dict[str, int]:
        index = self.index()
        total = 0
        for oid in index:
            info = self.b.stat(self.object_path(oid))
            total += info.size if info else 0
        snaps = self.snapshots()
        return {
            "snapshots": len(snaps),
            "objects": len(index),
            "stored_bytes": total,
            "logical_bytes": sum(s.bytes for s in snaps),
        }


def _read_exact(handle, size: int) -> bytes:  # type: ignore[no-untyped-def]
    parts = []
    remaining = size
    while remaining:
        chunk = handle.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)
