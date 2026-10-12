"""Garbage collection for a repository: find what nothing needs, set it aside first, delete it later.

Three rules keep a bug, a crash or a flaky disk from turning "unused" into "lost":

1. **References come from the file lists (trees), not from the summaries.** A snapshot is a summary plus a file
   list; if the summary is lost or unreadable but the file list survives, its data is still referenced. Any file list
   that cannot be read stops the run — "I could not look" is never treated as "nothing is there".
2. **A safety margin.** Data written recently, and file lists whose summary has not been written yet, are never
   collected. Another machine's backup that is still running (or one whose lock was lost) is protected by age,
   whatever the lock files say. Kopia works the same way for the same reason.
3. **Deferred deletion.** Unused data moves into ``trash/<batch>/`` and is deleted for good only by a later run, once
   it has sat there for the grace period. ``duplio backup trash restore`` undoes a run until then.
"""

from __future__ import annotations

import calendar
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from duplio.errors import RepositoryError
from duplio.events import NullProgress, ProgressSink
from duplio.util.fmt import utc_stamp_ms

if TYPE_CHECKING:
    from duplio.backup.repository import Repository

TRASH_DIR = "trash"
DEFAULT_GRACE_S = 24 * 3600  # collected data stays recoverable this long
DEFAULT_MIN_AGE_S = 24 * 3600  # data newer than this is never collected
ORPHAN_TREE_GRACE_S = 7 * 24 * 3600  # a file list without a summary is protected this long
_TREE_SUFFIX = ".json.gz"


@dataclass
class PruneReport:
    objects: int = 0  # chunks moved to the trash (deleted outright when the grace period is 0)
    bytes: int = 0
    purged_objects: int = 0  # chunks deleted for good from batches older than the grace period
    purged_bytes: int = 0
    too_young: int = 0  # unused, but written too recently to touch
    trees_set_aside: list[str] = field(default_factory=list)  # file lists without a summary, past their protection
    trees_protected: list[str] = field(default_factory=list)  # file lists without a summary, still protected
    trees_skipped: list[str] = field(default_factory=list)  # damaged file lists of lost snapshots, left in place
    batch: str | None = None  # name of the trash batch this run created
    dry_run: bool = False


@dataclass
class TrashBatch:
    name: str
    time: float | None
    objects: int
    bytes: int
    trees: int


def id_time(name: str) -> float | None:
    """The creation time encoded in a snapshot id or trash batch name (``20260102T030405123Z...``), if it has one."""
    try:
        whole = calendar.timegm(time.strptime(name[:15], "%Y%m%dT%H%M%S"))
        return whole + int(name[15:18]) / 1000
    except (ValueError, OverflowError):
        return None


def _age_s(now: float, *times: float | None) -> float:
    """Age by the *youngest* of several clues; a clue from the future counts as brand new."""
    ages = [max(0.0, now - t) for t in times if t is not None]
    return min(ages) if ages else 0.0


def _remove_tree(repo: Repository, path: str) -> tuple[int, int]:
    """Delete a folder of trashed data. Returns (files, bytes)."""
    b = repo.b
    files = size = 0
    for name, is_dir in b.listdir(path):
        child = b.join(path, name)
        if is_dir:
            f, s = _remove_tree(repo, child)
            files, size = files + f, size + s
            continue
        info = b.stat(child)
        b.remove(child)
        files += 1
        size += info.size if info else 0
    with contextlib.suppress(OSError):
        b.remove_empty_dirs(path, b.parent(path))  # the folder itself, now that it is empty
    return files, size


def _move(repo: Repository, src: str, dst: str) -> None:
    repo.b.makedirs(repo.b.parent(dst))
    repo.b.rename(src, dst)


def trash_batches(repo: Repository) -> list[TrashBatch]:
    b = repo.b
    base = repo.p(TRASH_DIR)
    out: list[TrashBatch] = []
    if not b.exists(base):
        return out
    for name, is_dir in sorted(b.listdir(base)):
        if not is_dir:
            continue
        objects = size = trees = 0
        objects_dir = b.join(b.join(base, name), "objects")
        if b.exists(objects_dir):
            for sub, sub_dir in b.listdir(objects_dir):
                if not sub_dir:
                    continue
                for obj, obj_dir in b.listdir(b.join(objects_dir, sub)):
                    if obj_dir:
                        continue
                    info = b.stat(b.join(b.join(objects_dir, sub), obj))
                    objects += 1
                    size += info.size if info else 0
        trees_dir = b.join(b.join(base, name), "trees")
        if b.exists(trees_dir):
            trees = sum(1 for n, d in b.listdir(trees_dir) if not d)
        out.append(TrashBatch(name, id_time(name), objects, size, trees))
    return out


def trashed_objects(repo: Repository) -> dict[str, str]:
    """Every object id currently in the trash -> its path there (the newest batch wins)."""
    b = repo.b
    found: dict[str, str] = {}
    for batch in trash_batches(repo):
        objects_dir = b.join(b.join(repo.p(TRASH_DIR), batch.name), "objects")
        if not b.exists(objects_dir):
            continue
        for sub, sub_dir in b.listdir(objects_dir):
            if not sub_dir:
                continue
            for obj, obj_dir in b.listdir(b.join(objects_dir, sub)):
                if not obj_dir and len(obj) == 64:
                    found[obj] = b.join(b.join(objects_dir, sub), obj)
    return found


def purge_trash(repo: Repository, *, older_than_s: float = 0.0, dry_run: bool = False) -> tuple[int, int]:
    """Permanently delete trash batches older than ``older_than_s`` (0 = all). Returns (files, bytes)."""
    now = time.time()
    files = size = 0
    for batch in trash_batches(repo):
        if older_than_s > 0 and (batch.time is None or _age_s(now, batch.time) < older_than_s):
            continue  # unreadable name or too recent: when in doubt, keep
        files += batch.objects + batch.trees
        size += batch.bytes
        if not dry_run:
            _remove_tree(repo, repo.b.join(repo.p(TRASH_DIR), batch.name))
    return files, size


def restore_trash(repo: Repository, batch: str | None = None) -> tuple[int, int]:
    """Bring trashed chunks and file lists back. Returns (chunks restored, snapshots brought back)."""
    b = repo.b
    restored = adopted = 0
    base = repo.p(TRASH_DIR)
    names = [x.name for x in trash_batches(repo) if batch in (None, x.name)]
    if batch is not None and not names:
        raise RepositoryError(f"no trash batch named {batch!r}")
    for name in names:
        root = b.join(base, name)
        objects_dir = b.join(root, "objects")
        if b.exists(objects_dir):
            for sub, sub_dir in b.listdir(objects_dir):
                if not sub_dir:
                    continue
                for obj, obj_dir in b.listdir(b.join(objects_dir, sub)):
                    if obj_dir:
                        continue
                    src = b.join(b.join(objects_dir, sub), obj)
                    dst = repo.object_path(obj)
                    if b.exists(dst):
                        continue  # a fresh copy was written meanwhile; the trashed one is redundant
                    _move(repo, src, dst)
                    restored += 1
        trees_dir = b.join(root, "trees")
        if b.exists(trees_dir):
            for tree_name, tree_dir in b.listdir(trees_dir):
                if tree_dir or not tree_name.endswith(_TREE_SUFFIX):
                    continue
                dst = repo.p(f"trees/{tree_name}")
                if not b.exists(dst):
                    _move(repo, b.join(trees_dir, tree_name), dst)
    repo._index = None
    adopted = len(repo.adopt_orphan_trees())
    return restored, adopted


def prune(
    repo: Repository,
    *,
    dry_run: bool = False,
    grace_s: float = DEFAULT_GRACE_S,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    orphan_grace_s: float = ORPHAN_TREE_GRACE_S,
    progress: ProgressSink | None = None,
    check_lease=None,  # type: ignore[no-untyped-def]
) -> PruneReport:
    """Set aside what no snapshot needs. The caller holds the repository lock."""
    progress = progress or NullProgress()
    b = repo.b
    now = time.time()
    report = PruneReport(dry_run=dry_run)
    guard = check_lease or (lambda: None)

    snaps, unreadable = repo._read_summaries()
    if unreadable:
        raise RepositoryError(
            f"{len(unreadable)} snapshot summary file(s) cannot be read ({unreadable[0]}); refusing to prune, "
            "because their data would look unused. Run 'duplio backup check' and try again."
        )
    live = {s.id for s in snaps}

    # 1. Everything that could reference data: the file list of every snapshot, and of every file list that has
    #    no summary yet. Reading all of them is mandatory.
    referenced: set[str] = set()
    tree_names = [n for n, d in b.listdir(repo.p("trees")) if not d and n.endswith(_TREE_SUFFIX)]
    lost = sorted(live - {n[: -len(_TREE_SUFFIX)] for n in tree_names})
    if lost:
        raise RepositoryError(
            f"snapshot {lost[0]} has a summary but its file list is missing; refusing to prune, because the data "
            "it describes would look unused. Restore trees/ from a copy, or forget the snapshot first."
        )
    progress.stage("Reading file lists", total_items=len(tree_names))
    orphans_to_set_aside: list[str] = []
    for name in sorted(tree_names):
        sid = name[: -len(_TREE_SUFFIX)]
        path = repo.p(f"trees/{name}")
        info = b.stat(path)
        orphan_age = _age_s(now, id_time(sid), (info.mtime_ns / 1e9) if info else None)
        try:
            entries = repo.tree(sid)
        except (OSError, ValueError, RepositoryError) as exc:
            is_old_orphan = sid not in live and orphan_age >= orphan_grace_s and not isinstance(exc, OSError)
            if is_old_orphan:
                report.trees_skipped.append(f"{sid}: {exc}")  # nothing live depends on it; leave it for the owner
                continue
            raise RepositoryError(
                f"the file list of snapshot {sid} cannot be read ({exc}); refusing to prune until "
                "'duplio backup check' passes (otherwise the data it describes would look unused)"
            ) from exc
        if sid in live or orphan_age < orphan_grace_s:
            referenced.update(c for e in entries for c in e.chunks)
            if sid not in live:
                report.trees_protected.append(sid)
        else:
            orphans_to_set_aside.append(sid)
        progress.advance(items=1)
    report.trees_set_aside = orphans_to_set_aside

    # 2. What is unused, and old enough to touch.
    repo._index = None
    index = repo.index()
    garbage = sorted(index - referenced)
    progress.stage("Pruning", total_items=len(garbage))
    batch = utc_stamp_ms()
    batch_root = b.join(repo.p(TRASH_DIR), batch)
    keep_in_place = 0
    for count, oid in enumerate(garbage):
        if count % 256 == 0:
            guard()
        path = repo.object_path(oid)
        info = b.stat(path)
        if info is None:
            index.discard(oid)
            progress.advance(items=1)
            continue
        if min_age_s > 0 and _age_s(now, info.mtime_ns / 1e9) < min_age_s:
            keep_in_place += 1
            progress.advance(items=1)
            continue
        report.objects += 1
        report.bytes += info.size
        if not dry_run:
            if grace_s <= 0:
                b.remove(path)
            else:
                _move(repo, path, b.join(b.join(b.join(batch_root, "objects"), oid[:2]), oid))
            index.discard(oid)
        progress.advance(items=1)
    report.too_young = keep_in_place
    if not dry_run:
        for sid in orphans_to_set_aside:
            guard()
            src = repo.p(f"trees/{sid}{_TREE_SUFFIX}")
            if grace_s <= 0:
                b.remove(src)
            else:
                _move(repo, src, b.join(b.join(batch_root, "trees"), f"{sid}{_TREE_SUFFIX}"))
        if report.objects or orphans_to_set_aside:
            report.batch = batch
        _clean_temp_files(repo, now, min_age_s)

    # 3. Deferred deletion: batches that have been in the trash long enough.
    guard()
    purged, purged_bytes = purge_trash(repo, older_than_s=max(grace_s, 1.0), dry_run=dry_run)
    report.purged_objects, report.purged_bytes = purged, purged_bytes
    return report


def _clean_temp_files(repo: Repository, now: float, min_age_s: float) -> None:
    """Leftovers of interrupted writes. Recent ones may belong to a writer that is still running; leave them."""
    b = repo.b

    def stale(path: str) -> bool:
        info = b.stat(path)
        return info is not None and _age_s(now, info.mtime_ns / 1e9) >= min_age_s

    for name, is_dir in b.listdir(repo.p("trees")):
        if not is_dir and ".duplio-tmp-" in name and stale(repo.p(f"trees/{name}")):
            with contextlib.suppress(OSError):
                b.remove(repo.p(f"trees/{name}"))
    objects_dir = repo.p("objects")
    for name, is_dir in b.listdir(objects_dir):
        if not is_dir:
            continue
        sub = b.join(objects_dir, name)
        for obj, _ in b.listdir(sub):
            if ".duplio-tmp-" in obj and stale(b.join(sub, obj)):
                with contextlib.suppress(OSError):
                    b.remove(b.join(sub, obj))
