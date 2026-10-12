"""Two-way synchronisation that cannot lose a file by accident.

``plan_sync`` compares two folders against what they looked like at the end of the
last sync (:mod:`duplio.backup.syncstate`) and decides, per path:

* new or changed on one side only -> copy it to the other side;
* changed on both sides, differently -> a conflict, settled by policy;
* gone from one side while unchanged on the other -> a deletion, which is *moved
  aside* (``.duplio-quarantine``), never removed;
* gone from one side but changed on the other -> the edit wins, the file is restored.

Safety rules, in the order they matter:

1. The very first sync of a pair has no history, so **nothing is ever deleted**.
2. A replaced file is archived in ``.duplio-versions`` before it is overwritten.
3. Deletions are not propagated at all if either side could not be fully read.
4. A side that is suddenly empty, or a plan that would delete more than a set
   fraction of a side, stops the run (``--force`` overrides) — the signature of an
   unmounted drive.
5. Every file is re-checked right before it is copied or moved aside; anything
   that changed since the scan is left for the next run.
6. Everything is journaled: ``duplio undo`` reverses a sync, step by step.
"""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from duplio.backup.merge import (
    MergeAction,
    TransferEnv,
    _with_suffix,
    auto_jobs_for,
    execute_action,
)
from duplio.backup.syncstate import Baseline, SyncState
from duplio.compare.diff import Basis, DiffOptions, Kind, diff_inventory
from duplio.errors import CancelledError, ConfigError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.hashing.cache import HashCache
from duplio.plan.executor import QUARANTINE_DIR, new_apply_id, safe_tag
from duplio.plan.journal import Journal, exclusive_lock
from duplio.scan.inventory import FileEntry, Inventory, Root
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.fmt import iso_now
from duplio.util.names import rel_issues
from duplio.util.text import name_key
from duplio.util.throttle import RateLimiter
from duplio.util.timecmp import is_racy, mtimes_match, tolerance_for, trusted_newer
from duplio.vfs.guards import check_destination
from duplio.vfs.location import BackendRegistry

CONFLICT_POLICIES = ("keep-both", "newer", "a", "b", "skip")
DELETION_POLICIES = ("propagate", "restore", "ignore")
SIDE = ("A", "B")


class SyncOp(StrEnum):
    COPY = "copy"  # new file: copy to the other side
    UPDATE = "update"  # changed on one side: replace the other side's (old version kept)
    DELETE = "delete"  # deleted on one side: move the other side's copy aside
    CONFLICT = "conflict"  # changed on both sides: keep both versions
    MARK = "mark"  # already identical: only refresh the history


@dataclass
class SyncOptions:
    basis: Basis = Basis.TIME_SIZE  # time-size (fast) | content (also confirms one-sided changes by content)
    on_conflict: str = "keep-both"
    deletions: str = "propagate"
    max_delete: float = 0.5  # stop if more than this fraction of a side would be deleted
    force: bool = False
    ignore_dst: bool = False
    ignore_ctime: bool = False  # do not treat a moved change time / new inode as a sign that content may have changed
    time_tolerance_ns: int | None = None
    verify: bool = True
    jobs: int | None = None
    limiter: RateLimiter | None = None
    algorithm: str | None = None
    expect_mounted: bool | None = None
    sentinel: str | None = None

    def validate(self) -> None:
        if self.on_conflict not in CONFLICT_POLICIES:
            raise ConfigError(f"unknown conflict policy {self.on_conflict!r}; use one of {', '.join(CONFLICT_POLICIES)}")
        if self.deletions not in DELETION_POLICIES:
            raise ConfigError(f"unknown deletions policy {self.deletions!r}; use one of {', '.join(DELETION_POLICIES)}")
        if self.basis is Basis.SIZE:
            raise ConfigError("sync compares by time and size or by content; --basis size is too weak to decide")


@dataclass
class SyncAction:
    op: SyncOp
    key: str
    a: FileEntry | None
    b: FileEntry | None
    frm: int | None = None  # side the data comes from (copy / update / conflict winner)
    side: int | None = None  # side acted on (delete)
    reason: str = ""
    note: str = ""
    conflict_rel: str | None = None  # keep-both: the visible name given to the losing version

    @property
    def source(self) -> FileEntry | None:
        return None if self.frm is None else (self.a, self.b)[self.frm]

    @property
    def target(self) -> FileEntry | None:
        return None if self.frm is None else (self.b, self.a)[self.frm]

    @property
    def entry(self) -> FileEntry:
        e = self.source if self.frm is not None else (self.a, self.b)[self.side or 0]
        assert e is not None
        return e

    @property
    def rel(self) -> str:
        return self.entry.rel

    @property
    def bytes(self) -> int:
        if self.op in (SyncOp.COPY, SyncOp.UPDATE):
            return self.entry.size
        if self.op is SyncOp.CONFLICT:
            loser = self.target
            return self.entry.size + (loser.size if loser else 0)
        return 0


@dataclass
class SyncSkip:
    key: str
    rel: str
    reason: str


@dataclass
class SyncPlan:
    inventory: Inventory
    options: SyncOptions
    first_run: bool
    actions: list[SyncAction] = field(default_factory=list)
    skipped: list[SyncSkip] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unchanged: int = 0
    gone: list[str] = field(default_factory=list)  # baseline keys whose files are gone from both sides
    case_folded: bool = False

    @property
    def roots(self) -> tuple[Root, Root]:
        return self.inventory.roots[0], self.inventory.roots[1]

    def count(self, op: SyncOp, frm: int | None = None) -> int:
        return sum(1 for a in self.actions if a.op is op and (frm is None or a.frm == frm or a.side == frm))

    def bytes_towards(self, side: int) -> int:
        """Bytes that will be written to ``side``."""
        total = 0
        for a in self.actions:
            if a.op in (SyncOp.COPY, SyncOp.UPDATE) and a.frm == 1 - side:
                total += a.entry.size
            elif a.op is SyncOp.CONFLICT and a.frm is not None:
                loser = a.target
                total += a.entry.size if side == 1 - a.frm else (loser.size if loser else 0)
        return total

    @property
    def has_work(self) -> bool:
        return any(a.op is not SyncOp.MARK for a in self.actions)

    @property
    def disruptive(self) -> bool:
        return any(a.op in (SyncOp.UPDATE, SyncOp.DELETE, SyncOp.CONFLICT) for a in self.actions)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_sync(
    inventory: Inventory,
    baseline: dict[str, Baseline],
    options: SyncOptions | None = None,
    *,
    cache: HashCache | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> SyncPlan:
    options = options or SyncOptions()
    options.validate()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    ra, rb = inventory.roots
    fold = ra.caps.case_insensitive or rb.caps.case_insensitive
    plan = SyncPlan(inventory, options, first_run=not baseline, case_folded=fold)
    roots = (ra, rb)

    def key_of(rel: str) -> str:
        return name_key(rel, fold=fold)

    by_side: tuple[dict[str, list[FileEntry]], dict[str, list[FileEntry]]] = (defaultdict(list), defaultdict(list))
    for e in inventory.entries:
        by_side[e.root][key_of(e.rel)].append(e)

    tol_side = [tolerance_for(r.caps.mtime_resolution_ns, floor_ns=1_000) for r in roots]
    tol_pair = options.time_tolerance_ns or max(tol_side)

    def changed(side: int, e: FileEntry, base: Baseline) -> bool:
        size, mtime = (base.size_a, base.mtime_a) if side == 0 else (base.size_b, base.mtime_b)
        return e.size != size or not mtimes_match(e.mtime_ns, mtime, tolerance_ns=tol_side[side],
                                                  dst_shift=options.ignore_dst)

    def doubtful(side: int, e: FileEntry, base: Baseline) -> bool:
        """Same size and time as recorded, yet the change time or identity moved: the content may have been edited
        and the modification time put back (or the file replaced by another with the same size and time)."""
        if options.ignore_ctime:
            return False
        ctime, ino = (base.ctime_a, base.ino_a) if side == 0 else (base.ctime_b, base.ino_b)
        if ctime is not None and e.ctime_ns is not None and ctime != e.ctime_ns:
            return True
        return bool(roots[side].caps.stable_inodes and ino is not None and e.inode is not None and ino != e.inode[1])

    def unproven(side: int, base: Baseline) -> bool:
        """Was this side's file recorded so soon after its last modification that "same size and time" proves nothing?"""
        mtime = base.mtime_a if side == 0 else base.mtime_b
        return bool(base.recorded_ns) and is_racy(mtime, base.recorded_ns, roots[side].caps.mtime_resolution_ns)

    def same_by_time_size(a: FileEntry, b: FileEntry) -> bool:
        return a.size == b.size and mtimes_match(a.mtime_ns, b.mtime_ns, tolerance_ns=tol_pair,
                                                  dst_shift=options.ignore_dst)

    # Pairs whose fate depends on whether their content is identical: resolved in one engine pass below.
    undecided: list[tuple[str, FileEntry, FileEntry, SyncAction | None]] = []
    pending_actions: dict[str, SyncAction] = {}

    keys = sorted(set(by_side[0]) | set(by_side[1]) | set(baseline))
    progress.stage("Deciding what to sync", total_items=len(keys))
    for key in keys:
        cancel.check()
        as_, bs_ = by_side[0].get(key, []), by_side[1].get(key, [])
        base = baseline.get(key)
        if len(as_) > 1 or len(bs_) > 1:
            shown = (as_ + bs_)[0].rel
            plan.skipped.append(SyncSkip(key, shown, "several names collapse into one path here (case or Unicode "
                                                     "form differs); resolve by hand"))
            continue
        a = as_[0] if as_ else None
        b = bs_[0] if bs_ else None
        if a is None and b is None:
            plan.gone.append(key)
            continue
        if a is not None and b is not None:
            if base is not None:
                ca, cb = changed(0, a, base), changed(1, b, base)
                da = not ca and doubtful(0, a, base)
                db = not cb and doubtful(1, b, base)
                if not ca and not cb:
                    if unproven(0, base) or unproven(1, base):
                        # Edited within the same timestamp step as the last sync? Only the content can say.
                        undecided.append((key, a, b, None))
                        continue
                    if da or db:
                        # Size and time say "unchanged" but the change time or identity moved. If only one side did,
                        # that side is the one that may have changed; identical content settles it either way.
                        if da != db:
                            frm = 0 if da else 1
                            undecided.append((key, a, b, SyncAction(
                                SyncOp.UPDATE, key, a, b, frm=frm,
                                reason=f"changed on {SIDE[frm]} (same size and time, but edited in place)")))
                        else:
                            undecided.append((key, a, b, None))
                        continue
                    plan.unchanged += 1
                    continue
                if ca != cb and not (db if ca else da):
                    frm = 0 if ca else 1
                    action = SyncAction(SyncOp.UPDATE, key, a, b, frm=frm,
                                        reason=f"changed on {SIDE[frm]}")
                    if options.basis is Basis.CONTENT and a.size == b.size:
                        undecided.append((key, a, b, action))  # a touched file with unchanged content needs no copy
                    else:
                        pending_actions[key] = action
                    continue
            # both changed, or no history for this path
            if same_by_time_size(a, b):
                plan.actions.append(SyncAction(SyncOp.MARK, key, a, b, reason="identical"))
            elif a.size == b.size:
                undecided.append((key, a, b, None))
            else:
                _add(pending_actions, key, _conflict_action(key, a, b, options, tol_pair, plan))
            continue
        # present on one side only
        side = 0 if a is not None else 1
        present = a if a is not None else b
        assert present is not None
        if base is not None:
            # It was in sync before. Missing from the other side's scan does not mean deleted there: it may be
            # outside the filters on that side (size, type, hidden, include) after an edit. Ask the file system.
            other_root = roots[1 - side]
            other_rel = base.rel_b if side == 0 else base.rel_a
            if other_root.backend.stat(other_root.backend.join(other_root.path, other_rel)) is not None:
                plan.skipped.append(SyncSkip(
                    key, present.rel,
                    f"still exists on {SIDE[1 - side]} but is outside the filters there; neither side is touched"))
                continue
        if base is None:
            pending_actions[key] = SyncAction(SyncOp.COPY, key, a, b, frm=side, reason=f"new on {SIDE[side]}")
        elif changed(side, present, base):
            pending_actions[key] = SyncAction(SyncOp.COPY, key, a, b, frm=side,
                                              reason=f"deleted on {SIDE[1 - side]} but changed on {SIDE[side]}: kept",
                                              note="restored")
        elif options.deletions == "propagate":
            pending_actions[key] = SyncAction(SyncOp.DELETE, key, a, b, side=side,
                                              reason=f"deleted on {SIDE[1 - side]}")
        elif options.deletions == "restore":
            pending_actions[key] = SyncAction(SyncOp.COPY, key, a, b, frm=side,
                                              reason=f"deleted on {SIDE[1 - side]}: copied back", note="restored")
        else:
            plan.skipped.append(SyncSkip(key, present.rel, f"deleted on {SIDE[1 - side]}; left alone (--deletions ignore)"))

    # Content comparison for the undecided pairs (only files whose sizes already match are ever read).
    if undecided:
        sub = Inventory(roots=list(roots), entries=[e for _, a, b, _ in undecided for e in (a, b)])
        result = diff_inventory(sub, DiffOptions(basis=Basis.CONTENT, detect_moves=False), cache=cache,
                                progress=progress, cancel=cancel)
        same_ids = {i.entry.id for i in result.items if i.kind is Kind.SAME}
        unreadable = {i.entry.id for i in result.items if i.kind is Kind.UNREADABLE}
        for key, a, b, fallback in undecided:
            if a.id in unreadable or b.id in unreadable:
                plan.skipped.append(SyncSkip(key, a.rel, "could not be read, so it was not compared"))
            elif a.id in same_ids and b.id in same_ids:
                plan.actions.append(SyncAction(SyncOp.MARK, key, a, b, reason="identical content"))
            elif fallback is not None:
                pending_actions[key] = fallback
            else:
                _add(pending_actions, key, _conflict_action(key, a, b, options, tol_pair, plan))

    # Names the other side cannot store are never rewritten in a sync (the two sides would drift apart).
    for key, action in pending_actions.items():
        victim = action.entry if action.op is SyncOp.DELETE else action.target
        if action.op in (SyncOp.DELETE, SyncOp.UPDATE, SyncOp.CONFLICT) and victim is not None and victim.protected:
            plan.skipped.append(SyncSkip(key, victim.rel, "protected path: never removed or replaced"))
            continue
        if action.op is SyncOp.COPY and action.frm is not None:
            issues = rel_issues(action.rel, windows=roots[1 - action.frm].caps.windows_names)
            if issues:
                plan.skipped.append(
                    SyncSkip(key, action.rel, f"{SIDE[1 - action.frm]} cannot store this name ({issues[0]})"))
                continue
        plan.actions.append(action)

    _assign_conflict_names(plan, by_side, key_of)
    _apply_brakes(plan, baseline, by_side)
    plan.actions.sort(key=lambda act: (_ORDER[act.op], act.key))
    return plan


_ORDER = {SyncOp.MARK: 0, SyncOp.CONFLICT: 1, SyncOp.COPY: 2, SyncOp.UPDATE: 3, SyncOp.DELETE: 4}


def _add(actions: dict[str, SyncAction], key: str, action: SyncAction | None) -> None:
    if action is not None:
        actions[key] = action


def _conflict_action(key: str, a: FileEntry, b: FileEntry, options: SyncOptions, tol: int,
                     plan: SyncPlan) -> SyncAction | None:
    policy = options.on_conflict
    if policy == "skip":
        plan.skipped.append(SyncSkip(key, a.rel, "changed differently on both sides (--on-conflict skip)"))
        return None
    direction = trusted_newer(a.mtime_ns, b.mtime_ns, now_ns=time.time_ns(), tolerance_ns=tol,
                              dst_shift=options.ignore_dst)
    if policy == "newer" and direction == 0:
        # Equal times, or a time that cannot be believed: no side can be called newer, so nothing is replaced.
        policy = "keep-both"
    if policy == "a":
        winner = 0
    elif policy == "b":
        winner = 1
    elif direction != 0:
        winner = 0 if direction > 0 else 1
    else:
        winner = 0  # keep-both with no usable times: A keeps the name, B's version is kept alongside
    why = "changed on both sides"
    if policy in ("newer", "a", "b"):
        return SyncAction(SyncOp.UPDATE, key, a, b, frm=winner,
                          reason=f"{why}: {SIDE[winner]} wins ({'newer' if policy == 'newer' else '--on-conflict'}); "
                                 "the other version is kept in .duplio-versions")
    return SyncAction(SyncOp.CONFLICT, key, a, b, frm=winner,
                      reason=f"{why}: {SIDE[winner]}'s version keeps the name, the other is kept alongside")


def _assign_conflict_names(plan: SyncPlan, by_side, key_of) -> None:  # type: ignore[no-untyped-def]
    taken = {k for side in by_side for k in side}
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    for action in plan.actions:
        if action.op is not SyncOp.CONFLICT or action.frm is None:
            continue
        loser = 1 - action.frm
        loser_entry = action.target
        assert loser_entry is not None
        counter = 1
        while True:
            suffix = f" (conflict {SIDE[loser]} {stamp})" + (f" {counter}" if counter > 1 else "")
            candidate = _with_suffix(loser_entry.rel, suffix)
            if key_of(candidate) not in taken:
                break
            counter += 1
        taken.add(key_of(candidate))
        action.conflict_rel = candidate


def _apply_brakes(plan: SyncPlan, baseline: dict[str, Baseline], by_side) -> None:  # type: ignore[no-untyped-def]
    options = plan.options
    errors = [bool(st.errors) for st in plan.inventory.walk_stats] or [False, False]
    deletes = [a for a in plan.actions if a.op is SyncOp.DELETE]
    if any(errors) and deletes:
        which = " and ".join(SIDE[i] for i, e in enumerate(errors) if e)
        plan.warnings.append(f"{which} could not be read completely, so no deletions are propagated this time")
        for a in deletes:
            plan.skipped.append(SyncSkip(a.key, a.rel, "deletion not propagated: a side was not fully readable"))
        plan.actions = [a for a in plan.actions if a.op is not SyncOp.DELETE]
        deletes = []
    if options.force:
        return
    for side in (0, 1):
        known = len(baseline)
        on_side = sum(len(v) for v in by_side[side].values())
        on_other = sum(len(v) for v in by_side[1 - side].values())
        if known >= 3 and on_side == 0 and on_other > 0:
            raise SafetyError(
                f"{plan.roots[side].location.display} (side {SIDE[side]}) is empty, but it held {known:,} files at the "
                "last sync. Is the drive mounted? Nothing was changed. If this is intended, run again with --force."
            )
    for side in (0, 1):
        count = sum(1 for a in deletes if a.side == side)
        known = len(baseline)
        if count and known and count > max(5, options.max_delete * known):
            raise SafetyError(
                f"this sync would remove {count:,} of {known:,} files from side {SIDE[side]} "
                f"({count / known:.0%}; the limit is {options.max_delete:.0%}). That usually means a folder or drive "
                "went missing on the other side. Nothing was changed. Check, or run again with --force."
            )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    action: SyncAction
    status: str  # done | skipped | failed | cancelled
    message: str = ""


@dataclass
class SyncReport:
    apply_id: str
    journal: Path | None
    results: list[SyncResult] = field(default_factory=list)
    bytes_copied: int = 0
    cancelled: bool = False
    seconds: float = 0.0

    def count(self, status: str, op: SyncOp | None = None) -> int:
        return sum(1 for r in self.results if r.status == status and (op is None or r.action.op is op))

    @property
    def failures(self) -> list[SyncResult]:
        return [r for r in self.results if r.status == "failed"]


def run_sync(
    plan: SyncPlan,
    registry: BackendRegistry,
    state: SyncState,
    *,
    dirs: AppDirs | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> SyncReport:
    dirs = (dirs or get_app_dirs()).ensure()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    options = plan.options
    ra, rb = plan.roots
    apply_id = new_apply_id()
    report = SyncReport(apply_id, None)
    started = time.monotonic()

    for side, root in ((0, ra), (1, rb)):
        check_destination(root.location, expect_mounted=options.expect_mounted, sentinel=options.sentinel,
                          needed_bytes=plan.bytes_towards(side))

    todo = [a for a in plan.actions if a.op is not SyncOp.MARK]
    # History for files that are already identical, and forgetting files gone from both sides.
    for action in plan.actions:
        if action.op is SyncOp.MARK and action.a and action.b:
            state.put(action.key, _baseline(action.a, action.b))
    for key in plan.gone:
        state.drop(key)
    if not todo:
        state.commit(finished=True)
        report.seconds = time.monotonic() - started
        return report

    algorithm = options.algorithm or default_full_algorithm()
    jobs = max(1, options.jobs or auto_jobs_for(ra, rb))
    with exclusive_lock(dirs.state / "apply.lock", purpose="sync"):
        journal = Journal(
            dirs.journals / f"{apply_id}.jsonl",
            header={"apply_id": apply_id, "plan_id": "", "session_id": "", "action": "sync", "created": iso_now(),
                    "roots": [{"index": 0, "uri": ra.location.uri, "role": "A"},
                              {"index": 1, "uri": rb.location.uri, "role": "B"}]},
        )
        report.journal = journal.path
        progress.stage("Synchronising", total_items=len(todo), total_bytes=sum(a.bytes for a in todo))
        envs = {
            0: TransferEnv(ra, rb, journal, apply_id, algorithm, cancel, progress, verify=options.verify,
                           limiter=options.limiter, rename_on_collision=False),
            1: TransferEnv(rb, ra, journal, apply_id, algorithm, cancel, progress, verify=options.verify,
                           limiter=options.limiter, rename_on_collision=False),
        }  # keyed by the side the data comes from

        def job(action: SyncAction) -> SyncResult:
            if cancel.cancelled:
                return SyncResult(action, "cancelled")
            try:
                result = _do(action, envs, plan, state, dirs, registry, journal, apply_id)
            except CancelledError:
                return SyncResult(action, "cancelled")
            except Exception as exc:  # one bad file must not stop the others
                return SyncResult(action, "failed", str(exc) or type(exc).__name__)
            progress.advance(items=1, detail=action.rel)
            return result

        try:
            copies = [a for a in todo if a.op is not SyncOp.DELETE]
            deletions = [a for a in todo if a.op is SyncOp.DELETE]
            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="duplio-sync") as pool:
                futures = [pool.submit(job, a) for a in copies]
                try:
                    report.results = [f.result() for f in futures]
                except KeyboardInterrupt:
                    cancel.cancel()
                    raise
            # Deletions come last, so that nothing is moved aside before the other changes are safely in place:
            # if any copy failed or the run was cancelled, they wait for the next run.
            postpone = bool(report.failures) or cancel.cancelled or any(r.status == "cancelled" for r in report.results)
            for action in deletions:
                if postpone:
                    report.results.append(SyncResult(action, "skipped", "postponed: some copies did not finish"))
                else:
                    report.results.append(job(action))
        finally:
            report.cancelled = cancel.cancelled
            report.bytes_copied = sum(r.action.bytes for r in report.results
                                      if r.status == "done" and r.action.op in (SyncOp.COPY, SyncOp.UPDATE, SyncOp.CONFLICT))
            journal.note(event="end", done=report.count("done"), skipped=report.count("skipped"),
                         failed=report.count("failed"), cancelled=report.cancelled)
            journal.close()
            state.commit(finished=not report.cancelled and not report.failures)
    report.seconds = time.monotonic() - started
    return report


def _baseline(a: FileEntry, b: FileEntry) -> Baseline:
    return Baseline(a.rel, a.size, a.mtime_ns, b.rel, b.size, b.mtime_ns, time.time_ns(),
                    a.ctime_ns, a.inode[1] if a.inode else None, b.ctime_ns, b.inode[1] if b.inode else None)


def _record(state: SyncState, key: str, plan: SyncPlan, src_side: int, src: FileEntry, dest_path: str,
            dest_rel: str) -> None:
    """Store the history of a file that was just copied: re-read the destination for what it really holds."""
    dest_root = plan.roots[1 - src_side]
    info = dest_root.backend.stat(dest_path)
    if info is None:
        return
    dest = FileEntry(-1, 1 - src_side, dest_path, dest_rel, info.size, info.mtime_ns, info.inode, info.nlink,
                     ctime_ns=info.ctime_ns)
    a, b = (src, dest) if src_side == 0 else (dest, src)
    state.put(key, _baseline(a, b))


def _do(action: SyncAction, envs: dict[int, TransferEnv], plan: SyncPlan, state: SyncState, dirs: AppDirs,
        registry: BackendRegistry, journal: Journal, apply_id: str) -> SyncResult:
    if action.op is SyncOp.DELETE:
        return _delete(action, plan, state, dirs, registry, journal, apply_id)
    assert action.frm is not None
    env = envs[action.frm]
    src = action.source
    assert src is not None
    if action.op is SyncOp.CONFLICT:
        return _keep_both(action, envs, plan, state)
    target = action.target
    if action.op is SyncOp.UPDATE:
        assert target is not None
        merge = MergeAction("replace", src, target.rel, action.reason, previous=target)
    else:
        merge = MergeAction("copy", src, src.rel, action.reason)
    result = execute_action(merge, env)
    if result.status != "done":
        return SyncResult(action, result.status, result.message)
    assert result.dest_path is not None
    _record(state, action.key, plan, action.frm, src, result.dest_path, target.rel if target else src.rel)
    return SyncResult(action, "done", result.message)


def _keep_both(action: SyncAction, envs: dict[int, TransferEnv], plan: SyncPlan, state: SyncState) -> SyncResult:
    """Winner keeps the name; the loser's version is renamed on its side and copied to the winner's side."""
    assert action.frm is not None and action.conflict_rel is not None
    winner_side = action.frm
    loser_side = 1 - winner_side
    winner, loser = action.source, action.target
    assert winner is not None and loser is not None
    lroot = plan.roots[loser_side]
    lb = lroot.backend
    env_w2l, env_l2w = envs[winner_side], envs[loser_side]
    journal = env_w2l.journal

    # The loser must still be exactly what the plan saw.
    info = lb.stat(loser.path)
    if info is None or info.size != loser.size or info.mtime_ns != loser.mtime_ns:
        return SyncResult(action, "skipped", "changed since the scan")
    renamed = lb.join(lroot.path, action.conflict_rel)
    lb.makedirs(lb.parent(renamed))
    seq = journal.intent(op="sync-rename", src_root=lroot.location.uri, src=loser.path, rel=loser.rel,
                         dst_root=lroot.location.uri, dst=renamed, size=loser.size, mtime_ns=loser.mtime_ns)
    try:
        lb.rename(loser.path, renamed)
    except BaseException as exc:
        journal.failed(seq, str(exc) or type(exc).__name__)
        raise
    journal.done(seq, dst=renamed)
    # 1. winner's version takes the original name on the loser's side
    res = execute_action(MergeAction("copy", winner, loser.rel, action.reason), env_w2l)
    if res.status != "done":
        return SyncResult(action, "failed", f"the other version was set aside as {action.conflict_rel} but the "
                                            f"winning version could not be copied: {res.message}")
    assert res.dest_path is not None
    _record(state, action.key, plan, winner_side, winner, res.dest_path, loser.rel)
    # 2. the set-aside version is copied to the winner's side so both sides stay identical
    aside = FileEntry(-1, loser_side, renamed, action.conflict_rel, loser.size, loser.mtime_ns)
    res2 = execute_action(MergeAction("copy", aside, action.conflict_rel, action.reason), env_l2w)
    if res2.status != "done":
        return SyncResult(action, "failed", f"{action.conflict_rel} could not be copied to {SIDE[winner_side]}: "
                                            f"{res2.message} (it stays on {SIDE[loser_side]}; the next sync retries)")
    assert res2.dest_path is not None
    _record(state, name_key(action.conflict_rel, fold=plan.case_folded), plan, loser_side, aside, res2.dest_path,
            action.conflict_rel)
    return SyncResult(action, "done", f"both versions kept; the other one is now '{action.conflict_rel}'")


def _delete(action: SyncAction, plan: SyncPlan, state: SyncState, dirs: AppDirs, registry: BackendRegistry,
            journal: Journal, apply_id: str) -> SyncResult:
    """Move a file aside (quarantine) instead of deleting it."""
    assert action.side is not None
    side = action.side
    root = plan.roots[side]
    entry = action.entry
    backend = root.backend
    info = backend.stat(entry.path)
    if info is None:
        state.drop(action.key)
        return SyncResult(action, "done", "already gone")
    if info.size != entry.size or info.mtime_ns != entry.mtime_ns:
        return SyncResult(action, "skipped", "changed since the scan")
    if not root.caps.can_delete:
        return SyncResult(action, "skipped", "this location is read-only")
    q_root = backend.join(root.path, f"{QUARANTINE_DIR}/{apply_id}")
    dst = backend.join(q_root, entry.rel)
    seq = journal.intent(op="quarantine", src_root=root.location.uri, src=entry.path, rel=entry.rel,
                         size=entry.size, mtime_ns=entry.mtime_ns, reason="sync")
    fallback = False
    dst_root_uri = root.location.uri
    try:
        try:
            if not root.caps.can_rename:
                raise OSError("rename not supported")
            backend.makedirs(backend.parent(dst))
            backend.rename(entry.path, dst)
        except FileExistsError:
            raise
        except OSError:
            # Phones and some network layers cannot rename: copy to the local quarantine, verify, then remove.
            from duplio.transfer import copy_verified

            fallback = True
            local = registry.local()
            dst = str(dirs.quarantine / apply_id / safe_tag(backend.id) / Path(*entry.rel.split("/")))
            dst_root_uri = str(dirs.quarantine)
            copy_verified((backend, entry.path), (local, dst), algorithm=default_full_algorithm(),
                          expected_size=entry.size, mtime_ns=entry.mtime_ns)
            try:
                backend.remove(entry.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    local.remove(dst)
                raise
    except BaseException as exc:
        journal.failed(seq, str(exc) or type(exc).__name__)
        raise
    journal.done(seq, dst_root=dst_root_uri, dst=dst, fallback=fallback, reclaimed=0)
    state.drop(action.key)
    return SyncResult(action, "done", "moved to the quarantine (duplio undo restores it)")
