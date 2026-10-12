"""Bring over what the other folder does not have — and nothing it already has.

``plan_merge`` turns a folder comparison (:mod:`duplio.compare.diff`) into a list
of actions; ``run_merge`` carries them out with every safeguard Duplio has:

* content decides, not names: a photo that is already in the destination under
  another name or in another folder is **not** copied again;
* nothing in the destination is ever overwritten silently. A different file at
  the same path is handled by a policy: keep both (``rename``, the default),
  ``skip``, take the ``newer`` one, or ``replace`` — and a replaced file is
  moved to ``.duplio-versions`` first, so it can be recovered;
* every copy is written to a temporary file, checked by reading it back, and only
  then renamed into place (never over an existing file);
* the sources are re-checked just before copying; a file that changed since the
  comparison is skipped, not copied half-new;
* file names the destination cannot hold (``:`` on SMB, trailing dots, reserved
  Windows names, over-long names) are rewritten, skipped or refused, as chosen;
* identical files inside the source are copied once;
* ``--move`` removes a source file only after it was verified in the destination,
  by moving it into the quarantine (the same reversible mechanism as ``dedupe``);
* every step is written to the journal first, so ``duplio undo`` can reverse the
  whole merge: copies are removed only while the source still has the content.
"""

from __future__ import annotations

import contextlib
import posixpath
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from duplio.compare.diff import Basis, DiffItem, DiffResult, Kind
from duplio.compare.model import Confidence
from duplio.errors import CancelledError, ConfigError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.plan.executor import ApplyOptions, ApplyReport, Executor, new_apply_id
from duplio.plan.journal import Journal, exclusive_lock
from duplio.plan.model import ActionType, FileRef, Plan, PlanItem, RootSpec
from duplio.scan.inventory import FileEntry, Root
from duplio.transfer import copy_verified
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.fmt import iso_now
from duplio.util.names import fix_rel, rel_issues
from duplio.util.text import name_key
from duplio.util.throttle import RateLimiter
from duplio.vfs.base import MediaKind
from duplio.vfs.guards import check_destination
from duplio.vfs.location import BackendRegistry

VERSIONS_DIR = ".duplio-versions"
LAYOUT_TOKENS = {
    "rel": "path relative to the source folder (default)",
    "dir": "folder part of that path",
    "name": "file name with extension",
    "stem": "file name without extension",
    "ext": "extension without the dot",
    "year": "modification year (UTC)",
    "month": "modification month, 01-12",
    "day": "modification day, 01-31",
    "date": "modification date, YYYY-MM-DD",
}
_TOKEN_RE = re.compile(r"\{(\w+)\}")
CONFLICT_POLICIES = ("rename", "skip", "newer", "replace")
NAME_POLICIES = ("auto", "fix", "keep", "skip", "abort")


@dataclass
class MergeOptions:
    move: bool = False  # remove source files after they are verified in the destination
    clean_source: bool = False  # with move: also remove source files already identical to something in the destination
    on_conflict: str = "rename"
    layout: str = "{rel}"
    names: str = "auto"
    keep_dupes: bool = False  # copy every identical file of the source, not just one
    verify: bool = True  # read each copy back from the destination
    verify_bytes: bool = False  # before removing a source file, compare byte-for-byte
    prune_empty: bool = True  # after a move, remove source folders that became empty
    jobs: int | None = None  # parallel copies; None = automatic
    limiter: RateLimiter | None = None
    algorithm: str | None = None
    expect_mounted: bool | None = None
    sentinel: str | None = None
    tag: str = ""  # label used in the names of kept-both files; default: the source folder name

    def validate(self) -> None:
        if self.on_conflict not in CONFLICT_POLICIES:
            raise ConfigError(f"unknown conflict policy {self.on_conflict!r}; use one of {', '.join(CONFLICT_POLICIES)}")
        if self.names not in NAME_POLICIES:
            raise ConfigError(f"unknown names policy {self.names!r}; use one of {', '.join(NAME_POLICIES)}")
        for token in _TOKEN_RE.findall(self.layout):
            if token not in LAYOUT_TOKENS:
                raise ConfigError(f"unknown layout token {{{token}}}; known: " + ", ".join(f"{{{t}}}" for t in LAYOUT_TOKENS))
        if self.clean_source and not self.move:
            raise ConfigError("--clean-source only makes sense together with --move")
        if self.move and not self.verify:
            raise SafetyError("a move needs verified copies: it cannot be combined with --no-verify")


@dataclass
class MergeAction:
    kind: str  # copy | rename | replace
    entry: FileEntry
    dest_rel: str
    reason: str
    note: str = ""
    previous: FileEntry | None = None  # the different file already at the path (replace / rename)
    group: int | None = None


@dataclass
class MergeSkip:
    entry: FileEntry
    reason: str  # present | moved | unreadable | duplicate-in-source | conflict | dest-newer | name
    detail: str = ""
    keeper: FileEntry | None = None  # destination file that already holds the same content
    group: int | None = None
    dest_rel: str | None = None  # for duplicates-in-source: where the representative copy goes


@dataclass
class MergePlan:
    diff: DiffResult
    options: MergeOptions
    actions: list[MergeAction] = field(default_factory=list)
    skipped: list[MergeSkip] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def src(self) -> Root:
        return self.diff.inventory.roots[0]

    @property
    def dest(self) -> Root:
        return self.diff.inventory.roots[1]

    @property
    def bytes_to_copy(self) -> int:
        return sum(a.entry.size for a in self.actions)

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)

    def skipped_count(self, reason: str) -> int:
        return sum(1 for s in self.skipped if s.reason == reason)

    @property
    def has_work(self) -> bool:
        if self.actions:
            return True
        return self.options.move and self.options.clean_source and any(
            s.reason in ("present", "moved") for s in self.skipped)

    @property
    def disruptive(self) -> bool:
        """Does applying this plan change or remove anything that already exists?"""
        return self.count("replace") > 0 or self.options.move


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def render_layout(template: str, entry: FileEntry) -> str:
    rel = entry.rel
    directory, _, name = rel.rpartition("/")
    stem, ext = posixpath.splitext(name)
    moment = datetime.fromtimestamp(max(entry.mtime_ns, 0) / 1e9, tz=UTC)
    values = {
        "rel": rel, "dir": directory, "name": name, "stem": stem, "ext": ext.lstrip("."),
        "year": f"{moment.year:04d}", "month": f"{moment.month:02d}", "day": f"{moment.day:02d}",
        "date": moment.strftime("%Y-%m-%d"),
    }  # fmt: skip
    text = _TOKEN_RE.sub(lambda m: values[m.group(1)], template)
    parts = [p for p in text.replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ConfigError(f"the layout {template!r} produced an unusable path for {rel!r}")
    return "/".join(parts)


def _with_suffix(rel: str, suffix: str) -> str:
    directory, _, name = rel.rpartition("/")
    stem, ext = posixpath.splitext(name)
    new = f"{stem}{suffix}{ext}"
    return f"{directory}/{new}" if directory else new


def _unique(rel: str, taken: set[str], key: Callable[[str], str]) -> str:
    if key(rel) not in taken:
        return rel
    counter = 2
    while True:
        candidate = _with_suffix(rel, f" ({counter})")
        if key(candidate) not in taken:
            return candidate
        counter += 1


def plan_merge(diff: DiffResult, options: MergeOptions | None = None) -> MergePlan:
    """Decide what to do with every file of the first location (the source)."""
    options = options or MergeOptions()
    options.validate()
    plan = MergePlan(diff=diff, options=options)
    src, dest = plan.src, plan.dest
    default_layout = options.layout == "{rel}"
    if not default_layout and diff.options.basis is not Basis.CONTENT:
        raise ConfigError("--layout needs --basis content (paths no longer line up once files are rearranged)")
    if options.move and not src.caps.can_delete:
        raise SafetyError(f"{src.location.display} is read-only, so files cannot be moved out of it")
    if not dest.caps.can_write:
        raise SafetyError(f"{dest.location.display} is read-only")

    fold = dest.caps.case_insensitive

    def key(rel: str) -> str:
        return name_key(rel, fold=fold)

    taken = {key(i.entry.rel) for i in diff.items if i.side == 1}
    windows = dest.caps.windows_names or options.names == "fix"
    tag = options.tag or (src.location.backend.basename(src.path) or "source")
    represented: dict[int, str] = {}  # identical-content group -> destination path of the copy being made
    problems: list[str] = []

    mine = sorted((i for i in diff.items if i.side == 0), key=lambda i: i.entry.rel)
    for item in mine:
        entry = item.entry
        if item.kind is Kind.SAME:
            plan.skipped.append(MergeSkip(entry, "present", "identical file at the same path",
                                          keeper=item.others[0] if item.others else None, group=item.group))
            continue
        if item.kind is Kind.MOVED:
            there = item.others[0].rel if item.others else ""
            plan.skipped.append(MergeSkip(entry, "moved", f"already in the destination as {there}",
                                          keeper=item.others[0] if item.others else None, group=item.group))
            continue
        if item.kind is Kind.UNREADABLE:
            plan.skipped.append(MergeSkip(entry, "unreadable", item.note or "could not be read"))
            continue

        # ONLY or CHANGED: something may have to be copied.
        if not options.keep_dupes and item.group is not None and item.group in represented:
            plan.skipped.append(MergeSkip(entry, "duplicate-in-source", f"identical to {represented[item.group]}",
                                          group=item.group, dest_rel=represented[item.group]))
            continue

        target = render_layout(options.layout, entry)
        note = ""
        issues = rel_issues(target, windows=windows)
        if issues:
            problems.append(f"{entry.rel}: {issues[0]}")
            if options.names == "keep":
                note = "the destination may refuse this name"
            elif options.names == "skip":
                plan.skipped.append(MergeSkip(entry, "name", str(issues[0])))
                continue
            elif options.names != "abort":
                fixed = fix_rel(target, windows=windows)
                note = f"renamed for the destination: {target} → {fixed}"
                target = fixed

        previous = item.others[0] if (item.kind is Kind.CHANGED and default_layout and item.others) else None
        if previous is not None:
            action = _conflict(item, previous, target, options, tag, taken, key)
            if action is None:
                reason = "conflict" if options.on_conflict == "skip" else "dest-newer"
                detail = "different file at the same path (policy: skip)" if reason == "conflict" else \
                    "the destination's version is newer"
                plan.skipped.append(MergeSkip(entry, reason, detail))
                continue
            action.note = "; ".join(x for x in (note, action.note) if x)
            action.group = item.group
        else:
            final = _unique(target, taken, key)
            if final != target:
                note = "; ".join(x for x in (note, "name already taken in the destination: kept both") if x)
            action = MergeAction("copy", entry, final, "not in the destination", note, group=item.group)
        taken.add(key(action.dest_rel))
        if item.group is not None:
            represented.setdefault(item.group, action.dest_rel)
        plan.actions.append(action)

    if options.names == "abort" and problems:
        shown = "; ".join(problems[:3])
        raise SafetyError(
            f"{len(problems)} file name(s) cannot be stored on {dest.location.display} (for example {shown}). "
            "Use --names fix to rename them safely, or --names skip to leave them out."
        )
    if problems and options.names == "keep":
        plan.warnings.append(f"{len(problems)} names may be refused by the destination; those copies will fail")
    return plan


def _conflict(item: DiffItem, previous: FileEntry, target: str, options: MergeOptions, tag: str,
              taken: set[str], key: Callable[[str], str]) -> MergeAction | None:
    entry = item.entry
    policy = options.on_conflict
    if policy == "newer":
        if item.newer == 1:
            policy = "replace"
        elif item.newer == -1:
            return None
        else:
            policy = "rename"  # same time, different content: ambiguous, so keep both
    if policy == "skip":
        return None
    if policy == "replace" and previous.protected:
        policy = "rename"  # a protected path (app bundle, photo library, …) is never replaced; keep both
    if policy == "replace":
        return MergeAction("replace", entry, previous.rel, "different file at the same path (old version kept)",
                           "previous version is kept in .duplio-versions", previous=previous)
    stamp = datetime.fromtimestamp(max(entry.mtime_ns, 0) / 1e9, tz=UTC).strftime("%Y-%m-%d")
    renamed = _unique(_with_suffix(target, f" (from {tag} {stamp})"), taken, key)
    return MergeAction("rename", entry, renamed, "different file at the same path: both kept",
                       previous=previous)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    action: MergeAction
    status: str  # done | skipped | failed | cancelled
    message: str = ""
    dest_path: str | None = None
    digest: str | None = None
    algorithm: str | None = None


@dataclass
class MergeReport:
    apply_id: str
    journal: Path | None
    results: list[MergeResult] = field(default_factory=list)
    moved: ApplyReport | None = None
    bytes_copied: int = 0
    cancelled: bool = False
    seconds: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failures(self) -> list[MergeResult]:
        return [r for r in self.results if r.status == "failed"]


def auto_jobs(plan: MergePlan) -> int:
    return auto_jobs_for(plan.src, plan.dest)


def auto_jobs_for(src: Root, dest: Root) -> int:
    kinds = {src.caps.kind, dest.caps.kind}
    if MediaKind.DEVICE in kinds or MediaKind.ROTATIONAL in kinds or MediaKind.REMOVABLE in kinds:
        return 1
    return 3 if MediaKind.NETWORK in kinds else 2


def run_merge(
    plan: MergePlan,
    registry: BackendRegistry,
    *,
    dirs: AppDirs | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    dry_run: bool = False,
) -> MergeReport:
    dirs = (dirs or get_app_dirs()).ensure()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    options = plan.options
    options.validate()
    src, dest = plan.src, plan.dest
    apply_id = new_apply_id()
    report = MergeReport(apply_id, None)
    if dry_run or not plan.has_work:
        return report
    started = time.monotonic()
    check_destination(dest.location, expect_mounted=options.expect_mounted, sentinel=options.sentinel,
                      needed_bytes=plan.bytes_to_copy)
    algorithm = options.algorithm or default_full_algorithm()
    jobs = max(1, options.jobs or auto_jobs(plan))

    with exclusive_lock(dirs.state / "apply.lock", purpose="merge"):
        journal = Journal(
            dirs.journals / f"{apply_id}.jsonl",
            header={
                "apply_id": apply_id, "plan_id": "", "session_id": "", "action": "merge", "created": iso_now(),
                "roots": [{"index": 0, "uri": src.location.uri, "role": "normal"},
                          {"index": 1, "uri": dest.location.uri, "role": "destination"}],
            },
        )
        report.journal = journal.path
        progress.stage("Copying", total_items=len(plan.actions), total_bytes=plan.bytes_to_copy)
        env = TransferEnv(src, dest, journal, apply_id, algorithm, cancel, progress, verify=options.verify,
                          limiter=options.limiter)

        def job(action: MergeAction) -> MergeResult:
            if cancel.cancelled:
                return MergeResult(action, "cancelled")
            try:
                result = execute_action(action, env)
            except CancelledError:
                return MergeResult(action, "cancelled")
            except Exception as exc:  # one bad file must not stop the others
                return MergeResult(action, "failed", str(exc) or type(exc).__name__)
            progress.advance(items=1, detail=action.dest_rel)
            return result

        try:
            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="duplio-merge") as pool:
                futures = [pool.submit(job, a) for a in plan.actions]
                try:
                    report.results = [f.result() for f in futures]
                except KeyboardInterrupt:
                    cancel.cancel()  # before the pool waits for running copies, so they stop promptly
                    raise
        finally:
            report.cancelled = cancel.cancelled
            report.bytes_copied = sum(r.action.entry.size for r in report.results if r.status == "done")
            journal.note(event="end", done=report.count("done"), skipped=report.count("skipped"),
                         failed=report.count("failed"), cancelled=report.cancelled)
            journal.close()

    if options.move and not report.cancelled:
        report.moved = _remove_sources(plan, report, registry, dirs, apply_id, progress, cancel)
    report.seconds = time.monotonic() - started
    return report


@dataclass
class TransferEnv:
    """Everything one copy needs; ``duplio sync`` builds one per direction."""

    src: Root
    dest: Root
    journal: Journal
    apply_id: str
    algorithm: str
    cancel: CancelToken
    progress: ProgressSink
    verify: bool = True
    limiter: RateLimiter | None = None
    rename_on_collision: bool = True  # merge: keep both; sync: a collision is an error
    lock: threading.Lock = field(default_factory=threading.Lock)


def execute_action(action: MergeAction, env: TransferEnv) -> MergeResult:
    """Copy (or replace) one file, journaled and verified; never overwrites without archiving first."""
    journal, apply_id, algorithm = env.journal, env.apply_id, env.algorithm
    src, dest = env.src, env.dest
    sb, db = src.backend, dest.backend
    entry = action.entry
    info = sb.stat(entry.path)
    if info is None:
        return MergeResult(action, "skipped", "the source file is gone")
    if not info.is_file or info.is_symlink:
        return MergeResult(action, "skipped", "no longer a regular file")
    if info.size != entry.size or info.mtime_ns != entry.mtime_ns:
        return MergeResult(action, "skipped", "changed since the comparison")

    dest_rel = action.dest_rel
    archived: str | None = None
    seq = journal.intent(
        op="merge-replace" if action.kind == "replace" else "merge-copy",
        src_root=src.location.uri, src=entry.path, rel=entry.rel,
        dst_root=dest.location.uri, dst=db.join(dest.path, dest_rel), dest_rel=dest_rel,
        size=entry.size, mtime_ns=entry.mtime_ns, kind=action.kind,
    )  # fmt: skip
    try:
        if action.kind == "replace":
            prev = action.previous
            assert prev is not None
            target = db.join(dest.path, prev.rel)
            current = db.stat(target)
            if current is None or current.size != prev.size or current.mtime_ns != prev.mtime_ns:
                journal.event(seq, "skipped", reason="destination file changed since the comparison")
                return MergeResult(action, "skipped", "the destination file changed since the comparison")
            archived = db.join(dest.path, f"{VERSIONS_DIR}/{apply_id}/{prev.rel}")
            db.makedirs(db.parent(archived))
            db.rename(target, archived)
            try:
                digest = _copy(action, target, env)
                moved = _changed_during_copy(sb, entry)
                if moved:
                    raise _SourceMoved(moved)
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    if isinstance(exc, _SourceMoved):
                        db.remove(target)  # the new copy may be torn: discard it
                    db.rename(archived, target)  # put the old version back
                if isinstance(exc, _SourceMoved):
                    journal.event(seq, "skipped", reason=str(exc))
                    return MergeResult(action, "skipped", str(exc))
                raise
            journal.done(seq, dst=target, archived=archived, digest=digest, algorithm=algorithm, size=entry.size)
            return MergeResult(action, "done", f"replaced (old version: {VERSIONS_DIR}/{apply_id}/{prev.rel})",
                               target, digest, algorithm)

        target = db.join(dest.path, dest_rel)
        for attempt in range(6):
            try:
                digest = _copy(action, target, env)
                break
            except FileExistsError:
                # Something appeared at the path since the comparison (or a filtered-out file sits there).
                if not env.rename_on_collision:
                    raise
                dest_rel = _with_suffix(action.dest_rel, f" ({attempt + 2})")
                target = db.join(dest.path, dest_rel)
        else:
            raise FileExistsError(f"{action.dest_rel}: the name keeps being taken in the destination")
        moved = _changed_during_copy(sb, entry)
        if moved:  # a file written to while it was copied may be neither its old nor its new content
            with contextlib.suppress(Exception):
                db.remove(target)
            journal.event(seq, "skipped", reason=moved)
            return MergeResult(action, "skipped", moved)
        journal.done(seq, dst=target, digest=digest, algorithm=algorithm, size=entry.size)
        return MergeResult(action, "done", "" if dest_rel == action.dest_rel else f"saved as {dest_rel}", target,
                           digest, algorithm)
    except CancelledError:
        journal.event(seq, "skipped", reason="cancelled")
        raise
    except BaseException as exc:
        journal.failed(seq, str(exc) or type(exc).__name__)
        raise


class _SourceMoved(Exception):
    """The source file changed (or vanished) while it was being copied."""


def _changed_during_copy(backend, entry: FileEntry) -> str | None:  # type: ignore[no-untyped-def]
    after = backend.stat(entry.path)
    if after is None:
        return "the source file disappeared while it was being copied"
    if after.size != entry.size or after.mtime_ns != entry.mtime_ns:
        return "the source file changed while it was being copied; it will be picked up next time"
    return None


def _copy(action: MergeAction, target: str, env: TransferEnv) -> str:
    entry = action.entry
    return copy_verified(
        (env.src.backend, entry.path), (env.dest.backend, target),
        algorithm=env.algorithm, expected_size=entry.size, mtime_ns=entry.mtime_ns, verify=env.verify,
        cancel=env.cancel, on_bytes=lambda n: env.progress.advance(nbytes=n), limiter=env.limiter,
    )


def _remove_sources(plan: MergePlan, report: MergeReport, registry: BackendRegistry, dirs: AppDirs, apply_id: str,
                    progress: ProgressSink, cancel: CancelToken) -> ApplyReport | None:
    """Quarantine source files whose content is now verified in the destination."""
    options = plan.options
    algorithm = options.algorithm or default_full_algorithm()
    src, dest = plan.src, plan.dest
    items: list[PlanItem] = []
    notes: list[str] = []

    def add(entry: FileEntry, keeper_path: str, keeper_rel: str, keeper_size: int, keeper_mtime: int, digest: str,
            algo: str, group: int) -> None:
        if entry.protected:
            notes.append(f"{entry.rel}: protected, left in the source")
            return
        items.append(PlanItem(
            id=len(items) + 1, group_id=group, op=ActionType.QUARANTINE,
            victim=FileRef(0, entry.path, entry.rel, entry.size, entry.mtime_ns),
            keeper=FileRef(1, keeper_path, keeper_rel, keeper_size, keeper_mtime),
            digest=digest, algorithm=algo, confidence=int(Confidence.CERTAIN), reclaim=0,
            note="merged into the destination",
        ))  # fmt: skip

    copied: dict[str, MergeResult] = {}
    for r in report.results:
        if r.status == "done" and r.dest_path and r.digest:
            copied[r.action.dest_rel] = r
            e = r.action.entry
            add(e, r.dest_path, r.action.dest_rel, e.size, e.mtime_ns, r.digest, r.algorithm or algorithm, r.action.group or 0)
    for s in plan.skipped:
        if s.reason == "duplicate-in-source" and s.dest_rel in copied:
            r = copied[s.dest_rel]
            e = s.entry
            add(e, r.dest_path or "", s.dest_rel, r.action.entry.size, r.action.entry.mtime_ns,
                r.digest or "", r.algorithm or algorithm, s.group or 0)
        elif s.reason in ("present", "moved") and options.clean_source:
            group = next((g for g in plan.diff.groups if g.id == s.group), None)
            if group is None or not group.digest or s.keeper is None:
                notes.append(f"{s.entry.rel}: identity was not verified by content; left in the source")
                continue
            add(s.entry, s.keeper.path, s.keeper.rel, s.keeper.size, s.keeper.mtime_ns, group.digest,
                group.algorithm or algorithm, group.id)
    if not items:
        return None
    move_plan = Plan(
        action=ActionType.QUARANTINE,
        roots=[RootSpec(0, src.location.uri, "normal"), RootSpec(1, dest.location.uri, "reference")],
        items=items, verify="bytes" if options.verify_bytes else "hash", notes=notes,
    )
    executor = Executor(move_plan, registry, options=ApplyOptions(
        verify="bytes" if options.verify_bytes else "hash", prune_empty_dirs=options.prune_empty), dirs=dirs,
        progress=progress, cancel=cancel)
    executor.apply_id = apply_id  # same journal: one 'duplio undo' reverses the copies and the removals together
    return executor.run()
