"""Import photos and videos from a phone (or camera card) into a dated library.

``duplio ingest adb:/sdcard/DCIM ~/Pictures/Library`` does, per file:

1. **Skip instantly** if this exact source file (path, size, mtime) was
   imported before — recorded in a local ledger, no read needed.
2. Otherwise **stream it once** from the device into a temporary file inside
   the library, hashing while copying, then verify the temp copy.
3. **Skip** if that content was imported before (even if you have since moved
   or deleted it in the library — "don't resurrect what I deleted"), or if an
   identical file already exists anywhere in the library.
4. Otherwise read the capture date (EXIF, video metadata, file name, mtime)
   from the *local* temp copy and move it to its final place, e.g.
   ``2024/2024-07/PXL_20240712_101500.jpg``. Name collisions with different
   content get `` (2)``, `` (3)``…

Optionally, ``free_source`` then deletes from the phone exactly those files
whose content is verified to be in the library.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from duplio.errors import CancelledError, DuplioError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.hashing.cache import HashCache, NullCache
from duplio.hashing.core import hash_file
from duplio.scan.filters import MEDIA_EXTENSIONS, ScanFilter, extension
from duplio.transfer import copy_verified
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.vfs.base import RawEntry, WalkStats
from duplio.vfs.location import Location

DEFAULT_LAYOUT = "{year}/{year}-{month}/{name}"
INCOMING_DIR = ".duplio/incoming"
LAYOUT_TOKENS = ("year", "month", "day", "date", "time", "name", "stem", "ext", "kind", "device", "source_dir")


@dataclass
class IngestOptions:
    layout: str = DEFAULT_LAYOUT
    media_only: bool = True
    dry_run: bool = False
    free_source: bool = False
    reimport_deleted: bool = False
    device_label: str = ""


@dataclass
class IngestItem:
    source_rel: str
    size: int
    status: str  # imported | duplicate | previously-imported | would-import | maybe-duplicate | failed
    dest_rel: str | None = None
    date_source: str | None = None
    message: str = ""


@dataclass
class IngestReport:
    items: list[IngestItem] = field(default_factory=list)
    freed: int = 0
    freed_bytes: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    @property
    def imported_bytes(self) -> int:
        return sum(i.size for i in self.items if i.status == "imported")


class Ledger:
    """Local SQLite memory of what was imported into a library."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS imported (
                digest TEXT PRIMARY KEY, size INTEGER, dest_rel TEXT, source TEXT, imported_at INTEGER);
            CREATE TABLE IF NOT EXISTS seen (
                source_key TEXT PRIMARY KEY, digest TEXT NOT NULL, seen_at INTEGER);
            """
        )

    @staticmethod
    def source_key(source_uri: str, rel: str, size: int, mtime_ns: int) -> str:
        return hashlib.blake2b(f"{source_uri}\0{rel}\0{size}\0{mtime_ns}".encode(), digest_size=16).hexdigest()

    def seen_digest(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT digest FROM seen WHERE source_key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def imported(self, digest: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT dest_rel FROM imported WHERE digest=?", (digest,)).fetchone()
        return str(row[0]) if row else None

    def record(self, key: str, digest: str, size: int, dest_rel: str | None, source: str) -> None:
        now = int(time.time())
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (key, digest, now))
            if dest_rel is not None:
                self._db.execute(
                    "INSERT OR IGNORE INTO imported VALUES (?,?,?,?,?)", (digest, size, dest_rel, source, now)
                )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def ledger_path(dirs: AppDirs, library: Location) -> Path:
    tag = hashlib.blake2b(library.uri.encode(), digest_size=8).hexdigest()
    return dirs.state / "ingest" / f"{tag}.sqlite3"


def _clean(token: str) -> str:
    out = "".join("_" if c in '/\\:*?"<>|\0' else c for c in token).strip()
    return out if out not in ("", ".", "..") else "_"


def render_layout(template: str, *, name: str, when, kind: str, device: str, source_dir: str) -> str:  # type: ignore[no-untyped-def]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    values = {
        "year": f"{when.year:04d}",
        "month": f"{when.month:02d}",
        "day": f"{when.day:02d}",
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H%M%S"),
        "name": name,
        "stem": stem,
        "ext": ext.lower(),
        "kind": kind,
        "device": device or "device",
        "source_dir": source_dir or "_",
    }
    try:
        rendered = template.format(**{k: _clean(v) for k, v in values.items()})
    except (KeyError, IndexError, ValueError) as exc:
        raise DuplioError(f"invalid layout {template!r}: unknown token {exc}; use {', '.join(LAYOUT_TOKENS)}") from exc
    parts = [p for p in rendered.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise DuplioError(f"layout {template!r} produced an empty path")
    return "/".join(parts)


def _numbered(rel: str, n: int) -> str:
    folder, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    new = f"{stem} ({n}).{ext}" if dot and stem else f"{name} ({n})"
    return f"{folder}/{new}" if folder else new


def ingest(
    source: Location,
    library: Location,
    *,
    options: IngestOptions | None = None,
    scan_filter: ScanFilter | None = None,
    cache: HashCache | None = None,
    dirs: AppDirs | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    confirm_free: bool = False,
) -> IngestReport:
    options = options or IngestOptions()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    cache = cache or NullCache()
    dirs = (dirs or get_app_dirs()).ensure()
    sb, lb = source.backend, library.backend
    if sb.id == lb.id and (sb.is_within(library.path, source.path) or sb.is_within(source.path, library.path)):
        raise SafetyError("the library and the source overlap")
    if options.free_source and not confirm_free and not options.dry_run:
        raise SafetyError("freeing space on the source deletes files there; it must be confirmed explicitly")
    # Validate the layout before touching anything.
    render_layout(options.layout, name="x.jpg", when=datetime.now(), kind="Photos", device="d", source_dir="s")

    base_filter = scan_filter or ScanFilter.for_backup()
    report = IngestReport()

    progress.stage("Listing source")
    stats = WalkStats()
    sources: list[RawEntry] = []
    for raw in sb.walk(source.path, walk_filter=base_filter, stats=stats, should_stop=lambda: cancel.cancelled):
        if options.media_only and extension(raw.rel) not in MEDIA_EXTENSIONS:
            continue
        if raw.size == 0:
            continue
        sources.append(raw)
    report.errors.extend(stats.errors)
    cancel.check()

    progress.stage("Indexing library")
    if lb.stat(library.path) is None and not options.dry_run:
        lb.makedirs(library.path)
    library_by_size: dict[int, list[RawEntry]] = defaultdict(list)
    if lb.stat(library.path) is not None:
        for raw in lb.walk(library.path, walk_filter=ScanFilter.for_backup(), should_stop=lambda: cancel.cancelled):
            library_by_size[raw.size].append(raw)
    cancel.check()

    algo = default_full_algorithm()
    ledger = Ledger(ledger_path(dirs, library))
    lib_lock = threading.Lock()
    verified_sources: list[tuple[RawEntry, str]] = []

    def library_match(size: int, digest: str) -> str | None:
        with lib_lock:
            candidates = list(library_by_size.get(size, ()))
        for cand in candidates:
            try:
                kind = f"full:{algo}"
                # Freeing the source is destructive: then only fresh reads count.
                value = None if options.free_source else cache.get(lb.id, cand.path, kind, cand.size, cand.mtime_ns)
                if value is None:
                    value = hash_file(lb, cand.path, algo, expected_size=cand.size, cancel=cancel)
                    cache.put(lb.id, cand.path, kind, cand.size, cand.mtime_ns, value,
                              mtime_resolution_ns=library.caps.mtime_resolution_ns)
            except OSError:
                continue
            if value == digest:
                return cand.rel
        return None

    progress.stage("Importing", total_items=len(sources), total_bytes=sum(r.size for r in sources))
    try:
        for raw in sources:
            cancel.check()
            key = Ledger.source_key(source.uri, raw.rel, raw.size, raw.mtime_ns)
            known = ledger.seen_digest(key)
            if known is not None and not options.reimport_deleted and ledger.imported(known) is not None:
                report.items.append(IngestItem(raw.rel, raw.size, "previously-imported", ledger.imported(known)))
                progress.advance(items=1, nbytes=raw.size)
                continue
            if options.dry_run:
                status = "maybe-duplicate" if raw.size in library_by_size else "would-import"
                report.items.append(IngestItem(raw.rel, raw.size, status))
                progress.advance(items=1, nbytes=raw.size)
                continue
            try:
                item = _import_one(
                    raw, source, library, options, algo, ledger, key, library_match,
                    library_by_size, lib_lock, cancel, progress,
                )
            except CancelledError:
                raise
            except (OSError, DuplioError) as exc:
                item = IngestItem(raw.rel, raw.size, "failed", message=str(exc))
                report.errors.append((raw.rel, str(exc)))
            report.items.append(item)
            # Only content verified in the library *during this run* may be freed on the source.
            if item.status in ("imported", "duplicate") and item.dest_rel:
                verified_sources.append((raw, item.dest_rel))
            progress.advance(items=1)
    finally:
        ledger.close()
        cache.flush()
        with contextlib.suppress(Exception):
            lb.remove_empty_dirs(lb.join(library.path, INCOMING_DIR), library.path)

    if options.free_source and not options.dry_run:
        _free_source(source, library, verified_sources, report, cancel, progress)
    return report


def _import_one(raw, source, library, options, algo, ledger, key, library_match, library_by_size, lib_lock, cancel, progress):  # type: ignore[no-untyped-def]
    from duplio.backup.media import capture_date, media_kind

    sb, lb = source.backend, library.backend
    tmp = lb.join(library.path, f"{INCOMING_DIR}/{secrets.token_hex(8)}.part")
    digest = copy_verified(
        (sb, raw.path), (lb, tmp), algorithm=algo, expected_size=raw.size, mtime_ns=raw.mtime_ns,
        cancel=cancel, on_bytes=lambda n: progress.advance(nbytes=n),
    )
    try:
        previous = ledger.imported(digest)
        if previous is not None and not options.reimport_deleted:
            ledger.record(key, digest, raw.size, None, source.uri)
            return IngestItem(raw.rel, raw.size, "previously-imported", previous, message="imported before")
        existing = library_match(raw.size, digest)
        if existing is not None:
            ledger.record(key, digest, raw.size, existing, source.uri)
            return IngestItem(raw.rel, raw.size, "duplicate", existing, message="already in library")
        name = raw.rel.rsplit("/", 1)[-1]
        try:
            with lb.open_read(tmp) as handle:
                seekable = getattr(handle, "seekable", None)
                when, date_source = capture_date(name, handle if seekable and seekable() else None, raw.mtime_ns)
        except OSError:
            when, date_source = capture_date(name, None, raw.mtime_ns)
        source_dir = raw.rel.rsplit("/", 1)[0] if "/" in raw.rel else ""
        rel = render_layout(
            options.layout, name=name, when=when, kind=media_kind(name),
            device=options.device_label, source_dir=source_dir.replace("/", "-"),
        )
        final_rel = rel
        for n in range(2, 10_000):
            target = lb.join(library.path, final_rel)
            if not lb.exists(target):
                try:
                    lb.rename(tmp, target)
                    break
                except FileExistsError:
                    pass
            final_rel = _numbered(rel, n)
        else:  # pragma: no cover
            raise DuplioError(f"too many name collisions for {rel}")
        lb.set_mtime(lb.join(library.path, final_rel), raw.mtime_ns)
        info = lb.stat(lb.join(library.path, final_rel))
        with lib_lock:
            if info is not None:
                library_by_size[raw.size].append(
                    RawEntry(lb.join(library.path, final_rel), final_rel, raw.size, info.mtime_ns)
                )
        ledger.record(key, digest, raw.size, final_rel, source.uri)
        return IngestItem(raw.rel, raw.size, "imported", final_rel, date_source)
    finally:
        with contextlib.suppress(Exception):
            if lb.exists(tmp):
                lb.remove(tmp)


def _free_source(
    source: Location,
    library: Location,
    verified: list[tuple[RawEntry, str]],
    report: IngestReport,
    cancel: CancelToken,
    progress: ProgressSink,
) -> None:
    sb, lb = source.backend, library.backend
    progress.stage("Freeing space on source", total_items=len(verified))
    for raw, dest_rel in verified:
        cancel.check()
        progress.advance(items=1)
        kept = lb.stat(lb.join(library.path, dest_rel))
        if kept is None or kept.size != raw.size:
            report.errors.append((raw.rel, "library copy missing; not deleted"))
            continue
        info = sb.stat(raw.path)
        if info is None or info.size != raw.size or info.mtime_ns != raw.mtime_ns:
            report.errors.append((raw.rel, "changed since import; not deleted"))
            continue
        try:
            sb.remove(raw.path)
            report.freed += 1
            report.freed_bytes += raw.size
        except (OSError, DuplioError) as exc:
            report.errors.append((raw.rel, f"could not delete: {exc}"))
