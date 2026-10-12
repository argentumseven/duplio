"""One-way mirror with versioning: make DEST look like SOURCE, losing nothing.

* New files are copied (verified, atomic, timestamps preserved).
* Changed files: the old destination copy is moved to
  ``DEST/.duplio-versions/<run>/`` before the new one is written.
* Files missing from the source stay in the destination unless
  ``delete_extraneous`` is set — and even then they are moved to the versions
  folder, never deleted.
* Renames/moves in the source are detected by content and replayed as a move
  in the destination instead of a re-copy.
* A **mass-change brake** stops the run when the source looks empty or when
  most of the destination would change. A locked phone presents an empty
  storage over MTP; without the brake a mirror would "archive" an entire
  backup because of it.
"""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict
from dataclasses import dataclass, field

from duplio.errors import SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.hashing.cache import HashCache, NullCache
from duplio.hashing.core import hash_file
from duplio.scan.filters import ScanFilter
from duplio.transfer import copy_verified
from duplio.util.fmt import utc_stamp
from duplio.util.text import name_key, printable
from duplio.util.throttle import RateLimiter
from duplio.util.timecmp import is_racy
from duplio.vfs.base import RawEntry, WalkStats
from duplio.vfs.location import Location

VERSIONS_DIR = ".duplio-versions"


@dataclass
class MirrorOptions:
    delete_extraneous: bool = False
    checksum: bool = False
    verify: bool = True
    dry_run: bool = False
    detect_moves: bool = True
    max_change_ratio: float = 0.5
    force: bool = False
    keep_versions: str | None = None  # e.g. "90d": prune older version folders after the run
    limiter: RateLimiter | None = None


@dataclass
class MirrorAction:
    kind: str  # copy | update | archive | move
    rel: str
    size: int
    reason: str = ""
    from_rel: str | None = None  # for moves
    dest_rel: str | None = None  # the existing destination file this action replaces (may be spelled differently)


@dataclass
class MirrorPlan:
    source: Location
    dest: Location
    actions: list[MirrorAction] = field(default_factory=list)
    unchanged: int = 0
    source_files: int = 0
    dest_files: int = 0
    warnings: list[str] = field(default_factory=list)
    _src: dict[str, RawEntry] = field(default_factory=dict, repr=False)
    _dst: dict[str, RawEntry] = field(default_factory=dict, repr=False)

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)

    @property
    def bytes_to_copy(self) -> int:
        return sum(a.size for a in self.actions if a.kind in ("copy", "update"))


@dataclass
class MirrorReport:
    plan: MirrorPlan
    done: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    errors: list[tuple[str, str]] = field(default_factory=list)
    bytes_copied: int = 0
    versions_dir: str | None = None
    pruned_versions: int = 0


def _key(rel: str, fold: bool) -> str:
    return name_key(rel, fold=fold)


def plan_mirror(
    source: Location,
    dest: Location,
    *,
    scan_filter: ScanFilter | None = None,
    options: MirrorOptions | None = None,
    cache: HashCache | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> MirrorPlan:
    options = options or MirrorOptions()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    cache = cache or NullCache()
    scan_filter = scan_filter or ScanFilter.for_backup()
    sb, db = source.backend, dest.backend
    if sb.id == db.id and (sb.is_within(dest.path, source.path) or sb.is_within(source.path, dest.path)):
        raise SafetyError("source and destination overlap; mirroring would copy a folder into itself")
    src_info = sb.stat(source.path)
    if src_info is None or not src_info.is_dir:
        raise SafetyError(f"source folder not found: {source.display}")
    if db.stat(dest.path) is None and not options.dry_run:
        db.makedirs(dest.path)

    progress.stage("Listing source and destination")
    plan = MirrorPlan(source=source, dest=dest)
    fold = source.caps.case_insensitive or dest.caps.case_insensitive
    src_stats, dst_stats = WalkStats(), WalkStats()
    for raw in sb.walk(source.path, walk_filter=scan_filter, stats=src_stats, should_stop=lambda: cancel.cancelled):
        plan._src[raw.rel] = raw
    if db.stat(dest.path) is not None:
        for raw in db.walk(dest.path, walk_filter=scan_filter, stats=dst_stats, should_stop=lambda: cancel.cancelled):
            plan._dst[raw.rel] = raw
    cancel.check()
    plan.source_files, plan.dest_files = len(plan._src), len(plan._dst)

    # Names are matched exactly first. Only when a source name has no exact twin is a destination
    # file with the *same name in another spelling* (NFC/NFD accents, and case on case-insensitive
    # storage) accepted as its counterpart. Two source files that merely collide on that looser key
    # (legal on Linux) are therefore never merged, and no source file can silently disappear.
    dst_by_key: dict[str, list[RawEntry]] = defaultdict(list)
    for existing in plan._dst.values():
        dst_by_key[_key(existing.rel, fold)].append(existing)
    claimed: set[str] = set()
    src_keys: dict[str, str] = {}
    for rel in sorted(plan._src):
        other = src_keys.setdefault(_key(rel, fold), rel)
        if other != rel and fold:
            plan.warnings.append(
                f"'{printable(other)}' and '{printable(rel)}' differ only by case or Unicode form, "
                "which this destination cannot store separately; only one of them can be copied"
            )

    def counterpart(s: RawEntry) -> RawEntry | None:
        exact = plan._dst.get(s.rel)
        if exact is not None:
            return exact
        for cand in dst_by_key.get(_key(s.rel, fold), ()):
            if cand.rel not in plan._src and cand.rel not in claimed:  # never take a name another source file owns
                return cand
        return None
    for path, err in src_stats.errors[:20]:
        plan.warnings.append(f"source: {path}: {err}")
    if src_stats.errors:
        # Unreadable source folders must not look like deletions.
        options.delete_extraneous = False
        plan.warnings.append("some source folders could not be read; extraneous files will not be archived this run")

    tolerance = max(source.caps.mtime_resolution_ns, dest.caps.mtime_resolution_ns, 1)
    algo = default_full_algorithm()

    def digest(loc: Location, raw: RawEntry) -> str:
        b = loc.backend
        cached = cache.get(b.id, raw.path, f"full:{algo}", raw.size, raw.mtime_ns)
        if cached is not None:
            return cached
        value = hash_file(b, raw.path, algo, expected_size=raw.size, cancel=cancel)
        cache.put(b.id, raw.path, f"full:{algo}", raw.size, raw.mtime_ns, value,
                  mtime_resolution_ns=loc.caps.mtime_resolution_ns)
        return value

    now_ns = time.time_ns()
    resolutions = (source.caps.mtime_resolution_ns, dest.caps.mtime_resolution_ns)
    progress.stage("Comparing", total_items=len(plan._src))
    new_files: list[RawEntry] = []
    for _rel, s in sorted(plan._src.items()):
        cancel.check()
        d = counterpart(s)
        if d is not None:
            claimed.add(d.rel)
        if d is None:
            new_files.append(s)
        elif s.size != d.size:
            plan.actions.append(MirrorAction("update", s.rel, s.size, "size differs", dest_rel=d.rel))
        elif options.checksum or is_racy(s.mtime_ns, now_ns, *resolutions):
            # Files changed within the last few seconds are compared by content even without --checksum: an
            # edit in the same timestamp step as the copy would keep the same size and time.
            if digest(source, s) == digest(dest, d):
                plan.unchanged += 1
            else:
                plan.actions.append(MirrorAction("update", s.rel, s.size, "content differs", dest_rel=d.rel))
        elif abs(s.mtime_ns - d.mtime_ns) <= tolerance:
            plan.unchanged += 1
        else:
            plan.actions.append(MirrorAction("update", s.rel, s.size, "modified time differs", dest_rel=d.rel))
        progress.advance(items=1)

    extraneous = [d for d in plan._dst.values() if d.rel not in claimed]
    moved_from: set[str] = set()
    if options.delete_extraneous and options.detect_moves and new_files and extraneous:
        by_size: dict[int, list[RawEntry]] = defaultdict(list)
        for d in extraneous:
            by_size[d.size].append(d)
        for s in list(new_files):
            candidates = [d for d in by_size.get(s.size, []) if d.rel not in moved_from]
            if not candidates or s.size == 0:
                continue
            try:
                sd = digest(source, s)
                match = next((d for d in candidates if digest(dest, d) == sd), None)
            except OSError:
                match = None
            if match is not None:
                moved_from.add(match.rel)
                new_files.remove(s)
                plan.actions.append(MirrorAction("move", s.rel, s.size, "moved in source", from_rel=match.rel))
    for s in new_files:
        plan.actions.append(MirrorAction("copy", s.rel, s.size, "new"))
    if options.delete_extraneous:
        for d in sorted(extraneous, key=lambda e: e.rel):
            if d.rel not in moved_from:
                plan.actions.append(MirrorAction("archive", d.rel, d.size, "not in source"))

    # The brake.
    if not options.force:
        if plan.source_files == 0 and plan.dest_files > 0:
            raise SafetyError(
                "the source is empty but the destination is not. If this is a phone, unlock it and "
                "choose 'File transfer' mode; use --force if the source really is empty."
            )
        disruptive = plan.count("update") + plan.count("archive")
        if plan.dest_files >= 20 and disruptive / plan.dest_files > options.max_change_ratio:
            raise SafetyError(
                f"{disruptive} of {plan.dest_files} destination files would be replaced or archived "
                f"(more than {options.max_change_ratio:.0%}). Check the source, then use --force to proceed."
            )
    return plan


def run_mirror(
    plan: MirrorPlan,
    *,
    options: MirrorOptions | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> MirrorReport:
    options = options or MirrorOptions()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    report = MirrorReport(plan=plan)
    if options.dry_run:
        return report
    sb, db = plan.source.backend, plan.dest.backend
    stamp = utc_stamp()
    versions_root = db.join(plan.dest.path, f"{VERSIONS_DIR}/{stamp}")
    algo = default_full_algorithm()
    progress.stage("Mirroring", total_items=len(plan.actions), total_bytes=plan.bytes_to_copy)

    def archive(rel: str) -> str:
        src = db.join(plan.dest.path, rel)
        dst = db.join(versions_root, rel)
        report.versions_dir = versions_root
        try:
            db.rename(src, dst)
        except FileExistsError:
            raise
        except OSError:
            info = db.stat(src)
            copy_verified((db, src), (db, dst), algorithm=algo, expected_size=info.size if info else None,
                          mtime_ns=info.mtime_ns if info else None, cancel=cancel, limiter=options.limiter)
            db.remove(src)
        return dst

    for action in plan.actions:
        cancel.check()
        try:
            src_raw = plan._src.get(action.rel)
            if action.kind == "copy" and src_raw is not None:
                copy_verified((sb, src_raw.path), (db, db.join(plan.dest.path, action.rel)), algorithm=algo,
                              expected_size=src_raw.size, mtime_ns=src_raw.mtime_ns, verify=options.verify,
                              cancel=cancel, on_bytes=lambda n: progress.advance(nbytes=n), limiter=options.limiter)
                report.bytes_copied += src_raw.size
            elif action.kind == "update" and src_raw is not None:
                target_rel = action.dest_rel or action.rel
                archived = archive(target_rel)
                try:
                    copy_verified((sb, src_raw.path), (db, db.join(plan.dest.path, action.rel)), algorithm=algo,
                                  expected_size=src_raw.size, mtime_ns=src_raw.mtime_ns, verify=options.verify,
                                  cancel=cancel, on_bytes=lambda n: progress.advance(nbytes=n), limiter=options.limiter)
                except BaseException:
                    # Put the previous version back so the destination is never left without the file.
                    with contextlib.suppress(Exception):
                        db.rename(archived, db.join(plan.dest.path, target_rel))
                    raise
                report.bytes_copied += src_raw.size
            elif action.kind == "archive":
                archive(action.rel)
            elif action.kind == "move" and action.from_rel:
                db.rename(db.join(plan.dest.path, action.from_rel), db.join(plan.dest.path, action.rel))
            else:
                continue
            report.done[action.kind] += 1
        except (OSError, SafetyError) as exc:
            report.errors.append((action.rel, str(exc)))
        except Exception as exc:
            from duplio.errors import CancelledError, DuplioError

            if isinstance(exc, CancelledError) or not isinstance(exc, DuplioError):
                raise
            report.errors.append((action.rel, str(exc)))
        progress.advance(items=1)

    if options.keep_versions:
        report.pruned_versions = prune_versions(plan.dest, options.keep_versions)
    return report


def prune_versions(dest: Location, keep_for: str) -> int:
    """Delete version folders older than ``keep_for`` (e.g. ``90d``). Returns folders removed."""
    from duplio.backup.versions import prune_versions as prune

    return len(prune(dest, older_than=keep_for))
