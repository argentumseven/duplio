"""Apply a plan — paranoid by design.

For every file to be removed or replaced, the executor:

1. re-reads its metadata and skips it if size or modification time changed
   since the scan;
2. re-verifies the *kept* copy: it must still exist and still hash to the
   group's digest (computed fresh — never from cache);
3. re-verifies the copy to remove the same way, or compares both
   byte-for-byte (``--verify bytes``, and always for non-cryptographic
   digests);
4. writes an ``intent`` record to the journal and fsyncs it;
5. performs the action using the safest primitive available — a no-clobber
   rename into quarantine, an atomic ``rename(2)`` over the original for hard
   links and clones;
6. checks that the kept copy *still exists afterwards*. If removing one path
   made the other disappear, the two paths were secretly the same file (a
   mount seen twice, a phone reached through ADB and MTP at once) and the
   action is rolled back immediately;
7. records ``done`` in the journal.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from duplio.compare.model import Confidence
from duplio.errors import (
    CancelledError,
    ConfigError,
    DuplioError,
    SafetyError,
    UnsupportedOperation,
    VerificationError,
)
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm, get_algorithm
from duplio.hashing.core import files_identical, hash_file
from duplio.plan.journal import Journal, exclusive_lock
from duplio.plan.model import ActionType, FileRef, Plan, PlanItem
from duplio.transfer import copy_verified
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.atomic import atomic_write_json
from duplio.util.fmt import iso_now
from duplio.util.text import json_dumps
from duplio.util.timecmp import is_racy
from duplio.vfs.base import Backend
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import BackendRegistry, Location

QUARANTINE_DIR = ".duplio-quarantine"


@dataclass
class ApplyOptions:
    verify: str = "hash"  # "hash" or "bytes"
    confirm_delete: bool = False  # must be True to apply a DELETE plan
    prune_empty_dirs: bool = False
    dry_run: bool = False  # run every check, change nothing


@dataclass
class ItemOutcome:
    item: PlanItem
    status: str  # done | skipped | failed | would-do
    message: str = ""
    reclaimed: int = 0
    dst: str | None = None


@dataclass
class ApplyReport:
    apply_id: str
    action: ActionType
    journal: Path | None
    outcomes: list[ItemOutcome] = field(default_factory=list)
    dry_run: bool = False
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def reclaimed(self) -> int:
        return sum(o.reclaimed for o in self.outcomes)


def new_apply_id() -> str:
    from duplio.util.fmt import utc_stamp_ms

    return f"{utc_stamp_ms()}-{secrets.token_hex(2)}"


def safe_tag(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:80]


_REVERSIBLE = frozenset({ActionType.QUARANTINE, ActionType.TRASH})  # steps that can be undone afterwards


@dataclass(frozen=True)
class _KeeperVerdict:
    """A kept copy that was hashed and matched: what it looked like then, and when."""

    state: tuple[int, int, int | None, tuple[int, int] | None]  # size, mtime, change time, identity
    verified_ns: int


class _Skip(Exception):
    """Item skipped for a benign, expected reason (e.g. file changed)."""


class Executor:
    def __init__(
        self,
        plan: Plan,
        registry: BackendRegistry,
        *,
        options: ApplyOptions | None = None,
        dirs: AppDirs | None = None,
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.plan = plan
        self.registry = registry
        self.opts = options or ApplyOptions()
        self.dirs = (dirs or get_app_dirs()).ensure()
        self.progress = progress or NullProgress()
        self.cancel = cancel or CancelToken()
        self.apply_id = new_apply_id()
        self.locations: dict[int, Location] = {}
        self.unavailable: dict[int, str] = {}
        self._keeper_ok: dict[tuple[int, str], _KeeperVerdict] = {}
        self._quarantined: dict[int, list[dict[str, object]]] = {}
        self._removed: set[tuple[int, str]] = set()
        self.journal: Journal | None = None

    # ------------------------------------------------------------------
    def run(self) -> ApplyReport:
        if self.plan.action is ActionType.DELETE and not self.opts.confirm_delete and not self.opts.dry_run:
            raise SafetyError("permanent deletion requires explicit confirmation")
        report = ApplyReport(self.apply_id, self.plan.action, None, dry_run=self.opts.dry_run)
        for spec in self.plan.roots:
            try:
                self.locations[spec.index] = self.registry.open(spec.uri)
            except DuplioError as exc:
                self.unavailable[spec.index] = str(exc)
        reference_roots = {r.index for r in self.plan.roots if r.role == "reference"}

        self.progress.stage(
            "Verifying and applying" if not self.opts.dry_run else "Verifying (dry run)",
            total_items=len(self.plan.items),
        )
        lock_path = self.dirs.state / "apply.lock"
        with exclusive_lock(lock_path, purpose="apply"):
            if not self.opts.dry_run:
                self.journal = Journal(
                    self.dirs.journals / f"{self.apply_id}.jsonl",
                    header={
                        "apply_id": self.apply_id,
                        "plan_id": self.plan.plan_id,
                        "session_id": self.plan.session_id,
                        "action": self.plan.action.value,
                        "created": iso_now(),
                        "roots": [r.__dict__ for r in self.plan.roots],
                    },
                )
                report.journal = self.journal.path
            try:
                for item in self.plan.items:
                    if self.cancel.cancelled:
                        report.cancelled = True
                        break
                    if item.victim.root in reference_roots:
                        # Belt and braces: the planner never does this.
                        report.outcomes.append(ItemOutcome(item, "failed", "refusing to modify a reference location"))
                        continue
                    outcome = self._process(item)
                    report.outcomes.append(outcome)
                    self.progress.advance(items=1, detail=outcome.status)
                if not self.opts.dry_run:
                    self._write_manifests()
                    if self.opts.prune_empty_dirs:
                        self._prune_dirs(report)
            finally:
                if self.journal is not None:
                    self.journal.note(
                        event="end",
                        done=report.count("done"),
                        skipped=report.count("skipped"),
                        failed=report.count("failed"),
                        reclaimed=report.reclaimed,
                        cancelled=report.cancelled,
                    )
                    self.journal.close()
        return report

    # ------------------------------------------------------------------
    def _loc(self, root: int) -> Location:
        if root in self.unavailable:
            raise _Skip(f"location unavailable: {self.unavailable[root]}")
        return self.locations[root]

    def _process(self, item: PlanItem) -> ItemOutcome:
        try:
            return self._process_inner(item)
        except _Skip as skip:
            return ItemOutcome(item, "skipped", str(skip))
        except CancelledError:
            raise
        except (OSError, DuplioError) as exc:
            return ItemOutcome(item, "failed", str(exc))

    def _process_inner(self, item: PlanItem) -> ItemOutcome:
        self.cancel.check()
        victim_loc = self._loc(item.victim.root)
        keeper_loc = self._loc(item.keeper.root)
        vb, kb = victim_loc.backend, keeper_loc.backend
        v, k = item.victim, item.keeper

        if (k.root, k.path) in self._removed:
            raise SafetyError("the kept copy was removed earlier in this run")

        # 1. The file to remove must be exactly as scanned.
        info = vb.stat(v.path)
        if info is None:
            raise _Skip("already gone")
        if not info.is_file or info.is_symlink:
            raise _Skip("no longer a regular file")
        if info.size != v.size or info.mtime_ns != v.mtime_ns:
            raise _Skip("changed since the scan")

        # 2. Never act on two names for one file.
        if vb is kb and vb.same_file(v.path, k.path):
            raise _Skip("same physical file as the kept copy")

        # 3. Fresh content verification.
        similar = item.confidence == int(Confidence.SIMILAR)
        if similar:
            if not self._keeper_present(kb, k):
                raise SafetyError("the kept image is missing")
        else:
            self._verify_content(item, vb, kb)

        if self.opts.dry_run:
            return ItemOutcome(item, "would-do", "verified")

        # 4-7. Journal, act, post-check.
        assert self.journal is not None
        mode = None
        if isinstance(vb, LocalBackend):
            with contextlib.suppress(OSError):
                mode = os.stat(v.path).st_mode & 0o7777
        seq = self.journal.intent(
            op=item.op.value,
            item=item.id,
            group=item.group_id,
            src_root=self.plan.roots[v.root].uri,
            src=v.path,
            rel=v.rel,
            keeper_root=self.plan.roots[k.root].uri,
            keeper=k.path,
            size=v.size,
            mtime_ns=v.mtime_ns,
            mode=mode,
            digest=item.digest,
            algorithm=item.algorithm,
        )
        try:
            outcome = self._act(item, victim_loc, keeper_loc, seq)
        except _Skip as skip:
            self.journal.event(seq, "skipped", reason=str(skip))
            raise
        except BaseException as exc:
            self.journal.failed(seq, str(exc) or type(exc).__name__)
            raise
        return outcome

    # ------------------------------------------------------------------
    def _keeper_present(self, kb: Backend, k: FileRef) -> bool:
        info = kb.stat(k.path)
        return info is not None and info.is_file and info.size == k.size

    def _keeper_verified(self, item: PlanItem, kb: Backend) -> bool:
        """Is the kept copy, right now, the content the plan says it is?

        The verdict for a kept copy is not simply remembered for the rest of the run: a long run can outlast a change
        to the file. Before an irreversible step (delete, replace by a link) it is hashed again every time. For
        steps that can be undone (quarantine, system trash) an earlier verdict is reused only while the file still has
        the same size, times and identity, and was not modified so close to the earlier check that a further edit
        could have left them unchanged.
        """
        k = item.keeper
        assert item.algorithm is not None
        info = kb.stat(k.path)
        if info is None or not info.is_file or info.size != k.size:
            self._keeper_ok.pop((k.root, k.path), None)
            return False
        key = (k.root, k.path)
        now_ns = time.time_ns()
        cached = self._keeper_ok.get(key)
        reusable = (
            item.op in _REVERSIBLE
            and cached is not None
            and info.ctime_ns is not None  # without a change time there is nothing that cannot be forged
            and cached.state == (info.size, info.mtime_ns, info.ctime_ns, info.inode)
            and not is_racy(info.mtime_ns, cached.verified_ns, kb.capabilities(k.path).mtime_resolution_ns)
        )
        if reusable:
            return True
        ok = self._fresh_digest(kb, k.path, item.algorithm, k.size) == item.digest
        if ok:
            self._keeper_ok[key] = _KeeperVerdict((info.size, info.mtime_ns, info.ctime_ns, info.inode), now_ns)
        else:
            self._keeper_ok.pop(key, None)
        return ok

    def _fresh_digest(self, backend: Backend, path: str, algorithm: str, size: int) -> str:
        caps = backend.capabilities(path)
        if algorithm in caps.remote_hash_algorithms:
            remote = backend.remote_hash([path], algorithm).get(path)
            if remote is None:
                raise VerificationError(f"could not hash {backend.display(path)} on the device")
            if remote.size is not None and remote.size != size:
                raise _Skip("changed since the scan")
            return remote.digest
        return hash_file(backend, path, algorithm, expected_size=size, cancel=self.cancel)

    def _verify_content(self, item: PlanItem, vb: Backend, kb: Backend) -> None:
        v, k = item.victim, item.keeper
        if item.digest is None or item.algorithm is None:
            raise SafetyError("plan item has no digest; refusing to act on unverified content")
        bytes_mode = self.opts.verify == "bytes" or not get_algorithm(item.algorithm).cryptographic
        if not bytes_mode:
            if not self._keeper_verified(item, kb):
                raise SafetyError("the kept copy is missing or its content changed since the scan")
            if self._fresh_digest(vb, v.path, item.algorithm, v.size) != item.digest:
                raise _Skip("content changed since the scan")
            return
        if not self._keeper_present(kb, k):
            raise SafetyError("the kept copy is missing or changed since the scan")
        if not files_identical((vb, v.path), (kb, k.path), cancel=self.cancel):
            raise _Skip("not byte-for-byte identical to the kept copy")

    # ------------------------------------------------------------------
    def _act(self, item: PlanItem, victim_loc: Location, keeper_loc: Location, seq: int) -> ItemOutcome:
        assert self.journal is not None
        op = item.op
        if op is ActionType.QUARANTINE:
            return self._quarantine(item, victim_loc, keeper_loc, seq)
        if op is ActionType.DELETE:
            return self._delete(item, victim_loc, keeper_loc, seq)
        if op is ActionType.TRASH:
            return self._trash(item, victim_loc, keeper_loc, seq)
        if op in (ActionType.HARDLINK, ActionType.REFLINK):
            return self._replace_with_link(item, victim_loc, keeper_loc, seq)
        raise ConfigError(f"unknown action {op}")

    def _post_check(self, keeper_loc: Location, k: FileRef, rollback: Callable[[], None]) -> None:
        if not self._keeper_present(keeper_loc.backend, k):
            try:
                rollback()
            except Exception as exc:  # pragma: no cover - extremely unlikely double failure
                raise SafetyError(
                    f"DANGER: the kept copy vanished and the rollback failed ({exc}); "
                    "see the journal to recover the file manually"
                ) from exc
            raise SafetyError(
                "the kept copy disappeared when the duplicate was moved — both paths are the same file. "
                "The change was rolled back."
            )

    def _quarantine(self, item: PlanItem, victim_loc: Location, keeper_loc: Location, seq: int) -> ItemOutcome:
        assert self.journal is not None
        v = item.victim
        vb = victim_loc.backend
        root_loc = self._loc(v.root)
        q_root = vb.join(root_loc.path, f"{QUARANTINE_DIR}/{self.apply_id}")
        dst = vb.join(q_root, v.rel)
        fallback = False
        try:
            if not vb.capabilities(v.path).can_rename:
                raise UnsupportedOperation("rename not supported")
            vb.rename(v.path, dst)
            dst_root_uri = self.plan.roots[v.root].uri
        except (UnsupportedOperation, OSError) as exc:
            if isinstance(exc, FileExistsError):
                raise
            # Different filesystem or no rename support: copy, verify, then delete.
            fallback = True
            local = self.registry.local()
            q_local = self.dirs.quarantine / self.apply_id / safe_tag(vb.id) / Path(*v.rel.split("/"))
            dst = str(q_local)
            dst_root_uri = str(self.dirs.quarantine)
            if item.algorithm and get_algorithm(item.algorithm).cryptographic:
                algorithm = item.algorithm
                expected = item.digest if item.confidence == int(Confidence.CERTAIN) else None
            else:
                algorithm, expected = default_full_algorithm(), None
            copy_verified(
                (vb, v.path),
                (local, dst),
                algorithm=algorithm,
                expected_size=v.size,
                expected_digest=expected,
                mtime_ns=v.mtime_ns,
                cancel=self.cancel,
            )
            try:
                vb.remove(v.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(dst)
                raise

        def rollback() -> None:
            if fallback:
                copy_verified(
                    (self.registry.local(), dst), (vb, v.path), algorithm=default_full_algorithm(), mtime_ns=v.mtime_ns
                )
            else:
                vb.rename(dst, v.path)

        self._post_check(keeper_loc, item.keeper, rollback)
        self._removed.add((v.root, v.path))
        self.journal.done(seq, dst_root=dst_root_uri, dst=dst, fallback=fallback, reclaimed=item.reclaim)
        if not fallback:
            self._quarantined.setdefault(v.root, []).append({"original": v.path, "rel": v.rel, "quarantined": dst})
        return ItemOutcome(item, "done", "quarantined", reclaimed=item.reclaim, dst=dst)

    def _delete(self, item: PlanItem, victim_loc: Location, keeper_loc: Location, seq: int) -> ItemOutcome:
        assert self.journal is not None
        v = item.victim
        vb = victim_loc.backend
        probe = vb.join(vb.parent(v.path), f".duplio-del-{secrets.token_hex(4)}")
        probed = False
        if vb.capabilities(v.path).can_rename:
            try:
                vb.rename(v.path, probe)
                probed = True
            except (UnsupportedOperation, OSError):
                probed = False
        if probed:
            self._post_check(keeper_loc, item.keeper, lambda: vb.rename(probe, v.path))
            vb.remove(probe)
        else:
            if victim_loc.backend is not keeper_loc.backend or vb.same_file(v.path, item.keeper.path) is False:
                vb.remove(v.path)
            else:
                raise _Skip("cannot prove this is a different file from the kept copy on this storage")
            if not self._keeper_present(keeper_loc.backend, item.keeper):
                raise SafetyError("DANGER: the kept copy vanished after deletion; check the journal")
        self._removed.add((v.root, v.path))
        self.journal.done(seq, reclaimed=item.reclaim)
        return ItemOutcome(item, "done", "deleted", reclaimed=item.reclaim)

    def _trash(self, item: PlanItem, victim_loc: Location, keeper_loc: Location, seq: int) -> ItemOutcome:
        assert self.journal is not None
        vb = victim_loc.backend
        if vb.same_file(item.victim.path, item.keeper.path) is not False and vb is keeper_loc.backend:
            raise _Skip("cannot prove this is a different file from the kept copy")
        vb.trash(item.victim.path)
        if not self._keeper_present(keeper_loc.backend, item.keeper):
            raise SafetyError("DANGER: the kept copy vanished; restore it from the system trash")
        self._removed.add((item.victim.root, item.victim.path))
        self.journal.done(seq, reclaimed=item.reclaim)
        return ItemOutcome(item, "done", "moved to trash", reclaimed=item.reclaim)

    def _replace_with_link(self, item: PlanItem, victim_loc: Location, keeper_loc: Location, seq: int) -> ItemOutcome:
        assert self.journal is not None
        vb, kb = victim_loc.backend, keeper_loc.backend
        if not isinstance(vb, LocalBackend) or vb is not kb:
            raise _Skip(f"{item.op.value} needs both files on the same local filesystem")
        v, k = item.victim.path, item.keeper.path
        tmp = os.path.join(os.path.dirname(v), f".duplio-link-{secrets.token_hex(4)}")
        try:
            if item.op is ActionType.HARDLINK:
                os.link(k, tmp)
                st_tmp, st_keep = os.stat(tmp), os.stat(k)
                if (st_tmp.st_dev, st_tmp.st_ino) != (st_keep.st_dev, st_keep.st_ino):
                    raise VerificationError("hard link does not point at the kept copy")
            else:
                vb.reflink(k, tmp)
                shutil.copystat(v, tmp, follow_symlinks=False)
                if os.stat(tmp).st_size != item.keeper.size:
                    raise VerificationError("clone has the wrong size")
            # Last-moment check: the original must still be untouched.
            st_v = os.lstat(v)
            if st_v.st_size != item.victim.size or st_v.st_mtime_ns != item.victim.mtime_ns:
                raise _Skip("changed while being processed")
            os.replace(tmp, v)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self.journal.done(seq, reclaimed=item.reclaim)
        label = "hard-linked" if item.op is ActionType.HARDLINK else "cloned"
        return ItemOutcome(item, "done", label, reclaimed=item.reclaim)

    # ------------------------------------------------------------------
    def _write_manifests(self) -> None:
        for root_idx, moves in self._quarantined.items():
            loc = self.locations[root_idx]
            b = loc.backend
            path = b.join(loc.path, f"{QUARANTINE_DIR}/{self.apply_id}/MANIFEST.json")
            payload = {
                "format": "duplio-quarantine",
                "apply_id": self.apply_id,
                "plan_id": self.plan.plan_id,
                "created": iso_now(),
                "location": self.plan.roots[root_idx].uri,
                "restore_hint": f"duplio undo {self.apply_id}",
                "files": moves,
            }
            try:

                with b.open_write(path) as handle:
                    handle.write(json_dumps(payload, indent=1).encode())
            except Exception as exc:
                self.progress.message(f"could not write quarantine manifest {b.display(path)}: {exc}", level="warning")
        # Fallback quarantine on the local state disk keeps its own manifest too.
        local_dir = self.dirs.quarantine / self.apply_id
        if local_dir.exists():
            atomic_write_json(
                local_dir / "MANIFEST.json",
                {"format": "duplio-quarantine", "apply_id": self.apply_id, "restore_hint": f"duplio undo {self.apply_id}"},
            )

    def _prune_dirs(self, report: ApplyReport) -> None:
        for outcome in report.outcomes:
            if outcome.status != "done" or outcome.item.op not in (ActionType.QUARANTINE, ActionType.DELETE, ActionType.TRASH):
                continue
            loc = self.locations[outcome.item.victim.root]
            with contextlib.suppress(Exception):
                loc.backend.remove_empty_dirs(loc.backend.parent(outcome.item.victim.path), loc.path)


def apply_plan(
    plan: Plan,
    registry: BackendRegistry,
    *,
    options: ApplyOptions | None = None,
    dirs: AppDirs | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> ApplyReport:
    return Executor(plan, registry, options=options, dirs=dirs, progress=progress, cancel=cancel).run()
