"""Reverse an applied plan, list and purge quarantine, recover from crashes."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duplio.errors import DuplioError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm
from duplio.hashing.core import hash_file
from duplio.plan.journal import (
    Journal,
    JournalSummary,
    OpState,
    exclusive_lock,
    list_journals,
    summarize,
)
from duplio.plan.reconcile import reconcile_journal
from duplio.transfer import copy_verified
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.faults import crashpoint
from duplio.util.fmt import parse_duration
from duplio.vfs.location import BackendRegistry


@dataclass
class UndoLine:
    seq: int
    op: str
    path: str
    status: str  # restored | conflict | not-undoable | failed | already-undone | would-restore
    message: str = ""


@dataclass
class UndoReport:
    apply_id: str
    lines: list[UndoLine] = field(default_factory=list)
    dry_run: bool = False

    def count(self, status: str) -> int:
        return sum(1 for line in self.lines if line.status == status)


def find_journal(dirs: AppDirs, apply_id: str | None) -> Path:
    journals = list_journals(dirs.journals)
    if not journals:
        raise DuplioError("no applied plans found in the history")
    if apply_id is None:
        return journals[0]
    matches = [j for j in journals if j.stem == apply_id or j.stem.startswith(apply_id)]
    if not matches:
        raise DuplioError(f"no applied plan matches {apply_id!r} (see 'duplio history')")
    if len(matches) > 1:
        raise DuplioError(f"{apply_id!r} is ambiguous: {', '.join(m.stem for m in matches)}")
    return matches[0]


def _restore_quarantine(op: OpState, registry: BackendRegistry, dry_run: bool) -> UndoLine:
    intent, done = op.intent, op.done_record or {}
    src_loc = registry.open(intent["src_root"])
    backend = src_loc.backend
    original = intent["src"]
    dst = done.get("dst")
    if not dst:
        return UndoLine(op.seq, "quarantine", original, "failed", "journal lacks the quarantine path")
    fallback = bool(done.get("fallback"))
    q_backend = registry.local() if fallback else backend
    if q_backend.stat(dst) is None:
        return UndoLine(op.seq, "quarantine", original, "failed", "quarantined copy no longer exists (purged?)")
    if backend.stat(original) is not None:
        return UndoLine(op.seq, "quarantine", original, "conflict", "a file now exists at the original path; left both")
    if dry_run:
        return UndoLine(op.seq, "quarantine", original, "would-restore")
    if fallback:
        copy_verified(
            (q_backend, dst),
            (backend, original),
            algorithm=default_full_algorithm(),
            expected_size=int(intent["size"]),
            mtime_ns=int(intent["mtime_ns"]),
        )
        q_backend.remove(dst)
    else:
        backend.rename(dst, original)
        with contextlib.suppress(Exception):
            backend.remove_empty_dirs(backend.parent(dst), src_loc.path)
    return UndoLine(op.seq, "quarantine", original, "restored")


def _unshare(op: OpState, registry: BackendRegistry, dry_run: bool) -> UndoLine:
    """Turn a hard link / clone back into an independent file with its old metadata."""
    intent = op.intent
    path = intent["src"]
    backend = registry.open(intent["src_root"]).backend
    info = backend.stat(path)
    if info is None:
        return UndoLine(op.seq, intent["op"], path, "failed", "file no longer exists")
    if dry_run:
        return UndoLine(op.seq, intent["op"], path, "would-restore")
    tmp = os.path.join(os.path.dirname(path), f".duplio-link-{secrets.token_hex(4)}")
    try:
        with open(path, "rb") as src, open(tmp, "xb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        if intent.get("mode") is not None:
            os.chmod(tmp, int(intent["mode"]))
        os.utime(tmp, ns=(int(intent["mtime_ns"]), int(intent["mtime_ns"])))
        crashpoint("undo.before-replace")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return UndoLine(op.seq, intent["op"], path, "restored", "independent copy re-created")


def _merge_still_safe(op: OpState, registry: BackendRegistry) -> tuple[str, str] | UndoLine:
    """Check that a merged copy is untouched and the source still holds the same content.

    Returns ``(dst, digest)`` when it is safe to remove the copy, else the line to report.
    """
    intent, done = op.intent, op.done_record or {}
    kind = str(intent.get("op"))
    src = str(intent["src"])
    dst, digest, algorithm = done.get("dst"), done.get("digest"), done.get("algorithm")
    if not dst or not digest or not algorithm:
        return UndoLine(op.seq, kind, src, "failed", "the journal lacks the copy's details")
    db = registry.open(intent["dst_root"]).backend
    sb = registry.open(intent["src_root"]).backend
    size = int(done.get("size", intent.get("size", 0)))
    info = db.stat(dst)
    if info is None:
        return UndoLine(op.seq, kind, src, "failed", "the copy no longer exists")
    if info.size != size or hash_file(db, dst, algorithm, expected_size=size) != digest:
        return UndoLine(op.seq, kind, src, "conflict", "the copy was modified after the merge; left in place")
    sinfo = sb.stat(src)
    if sinfo is None or sinfo.size != size or hash_file(sb, src, algorithm, expected_size=size) != digest:
        return UndoLine(op.seq, kind, src, "conflict",
                        "the source no longer holds this content, so the copy is the only one; left in place")
    return str(dst), str(digest)


def _undo_merge_copy(op: OpState, registry: BackendRegistry, dry_run: bool) -> UndoLine:
    checked = _merge_still_safe(op, registry)
    if isinstance(checked, UndoLine):
        return checked
    dst, _ = checked
    src = str(op.intent["src"])
    if dry_run:
        return UndoLine(op.seq, "merge-copy", src, "would-restore", f"remove the copy {dst}")
    dst_loc = registry.open(op.intent["dst_root"])
    dst_loc.backend.remove(dst)
    with contextlib.suppress(Exception):
        dst_loc.backend.remove_empty_dirs(dst_loc.backend.parent(dst), dst_loc.path)
    return UndoLine(op.seq, "merge-copy", src, "restored", "copy removed")


def _undo_merge_replace(op: OpState, registry: BackendRegistry, dry_run: bool) -> UndoLine:
    checked = _merge_still_safe(op, registry)
    if isinstance(checked, UndoLine):
        return checked
    dst, _ = checked
    src = str(op.intent["src"])
    archived = (op.done_record or {}).get("archived")
    dst_loc = registry.open(op.intent["dst_root"])
    db = dst_loc.backend
    if not archived or db.stat(str(archived)) is None:
        return UndoLine(op.seq, "merge-replace", src, "failed", "the previous version is no longer in .duplio-versions")
    if dry_run:
        return UndoLine(op.seq, "merge-replace", src, "would-restore", f"bring back the previous version of {dst}")
    db.remove(dst)
    db.rename(str(archived), dst)
    with contextlib.suppress(Exception):
        db.remove_empty_dirs(db.parent(str(archived)), dst_loc.path)
    return UndoLine(op.seq, "merge-replace", src, "restored", "previous version put back")


def _undo_sync_rename(op: OpState, registry: BackendRegistry, dry_run: bool) -> UndoLine:
    """Give a conflict's set-aside file its old name back (the winner's copy was removed just before)."""
    intent = op.intent
    original, renamed = str(intent["src"]), str(op.done_record.get("dst") if op.done_record else intent["dst"])
    backend = registry.open(intent["src_root"]).backend
    if backend.stat(renamed) is None:
        return UndoLine(op.seq, "sync-rename", original, "failed", "the renamed file no longer exists")
    if backend.stat(original) is not None:
        return UndoLine(op.seq, "sync-rename", original, "conflict", "a file exists at the original name; left as is")
    if dry_run:
        return UndoLine(op.seq, "sync-rename", original, "would-restore", f"rename {renamed} back")
    backend.rename(renamed, original)
    return UndoLine(op.seq, "sync-rename", original, "restored", "original name restored")


def _forget_sync_history(summary: JournalSummary, dirs: AppDirs) -> None:
    """After undoing a sync, the recorded 'last identical state' is no longer true: start over safely.

    Without history the next sync is a first run, which never deletes anything.
    """
    from duplio.backup.syncstate import pair_id

    uris = [str(r.get("uri", "")) for r in summary.header.get("roots", [])]
    if len(uris) != 2:
        return
    base = dirs.state / "sync" / f"{pair_id(uris[0], uris[1])}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(str(base) + suffix).unlink()


def undo(
    apply_id: str | None,
    registry: BackendRegistry,
    *,
    dirs: AppDirs | None = None,
    dry_run: bool = False,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> UndoReport:
    dirs = (dirs or get_app_dirs()).ensure()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    path = find_journal(dirs, apply_id)
    summary = summarize(path)
    report = UndoReport(summary.session, dry_run=dry_run)
    with exclusive_lock(dirs.state / "apply.lock", purpose="undo"):
        # A run that was killed leaves operations without an outcome. Their files say what happened; settle them first
        # so that what took effect is reversed too (with --dry-run only the conclusion is used, nothing is written).
        for resolution in reconcile_journal(path, registry, dirs, write=not dry_run, summary=summary):
            report.lines.append(UndoLine(resolution.seq, resolution.op, resolution.path,
                                         "interrupted-" + resolution.outcome, resolution.note))
        ops = [op for op in reversed(summary.ops)]
        progress.stage("Undoing", total_items=len(ops))
        journal = None if dry_run else Journal(path)
        try:
            for op in ops:
                cancel.check()
                kind = op.intent.get("op", "")
                status = op.status
                src = str(op.intent.get("src", ""))
                if status == "undone":
                    report.lines.append(UndoLine(op.seq, kind, src, "already-undone"))
                elif status == "purged":
                    report.lines.append(UndoLine(op.seq, kind, src, "failed", "the quarantined copy was purged"))
                elif status != "done":
                    continue
                elif kind == "quarantine":
                    line = _safe(lambda op=op: _restore_quarantine(op, registry, dry_run), op, kind, src)
                    report.lines.append(line)
                elif kind == "sync-rename":
                    report.lines.append(_safe(lambda op=op: _undo_sync_rename(op, registry, dry_run), op, kind, src))
                elif kind == "merge-copy":
                    report.lines.append(_safe(lambda op=op: _undo_merge_copy(op, registry, dry_run), op, kind, src))
                elif kind == "merge-replace":
                    report.lines.append(_safe(lambda op=op: _undo_merge_replace(op, registry, dry_run), op, kind, src))
                elif kind in ("hardlink", "reflink"):
                    line = _safe(lambda op=op: _unshare(op, registry, dry_run), op, kind, src)
                    report.lines.append(line)
                elif kind == "trash":
                    report.lines.append(UndoLine(op.seq, kind, src, "not-undoable", "restore it from the system trash"))
                elif kind == "delete":
                    report.lines.append(UndoLine(op.seq, kind, src, "not-undoable", "permanently deleted"))
                if journal is not None and report.lines and report.lines[-1].seq == op.seq:
                    last = report.lines[-1]
                    if last.status == "restored":
                        journal.event(op.seq, "undone")
                    elif last.status == "failed":
                        journal.event(op.seq, "undo-failed", error=last.message)
                progress.advance(items=1)
        finally:
            if journal is not None:
                journal.close()
        if not dry_run and summary.header.get("action") == "sync" and report.count("restored"):
            _forget_sync_history(summary, dirs)
    return report


def _safe(fn, op: OpState, kind: str, src: str) -> UndoLine:  # type: ignore[no-untyped-def]
    try:
        return fn()  # type: ignore[no-any-return]
    except (OSError, DuplioError) as exc:
        return UndoLine(op.seq, kind, src, "failed", str(exc))


# ---------------------------------------------------------------------------
# Quarantine management
# ---------------------------------------------------------------------------


@dataclass
class QuarantineSession:
    apply_id: str
    created: str
    files: int
    bytes: int
    journal: Path


def list_quarantine(dirs: AppDirs | None = None) -> list[QuarantineSession]:
    dirs = (dirs or get_app_dirs()).ensure()
    sessions = []
    for path in list_journals(dirs.journals):
        summary = summarize(path)
        held = [op for op in summary.ops if op.intent.get("op") == "quarantine" and op.status == "done"]
        if held:
            sessions.append(
                QuarantineSession(
                    apply_id=summary.session,
                    created=str(summary.header.get("created", "")),
                    files=len(held),
                    bytes=sum(int(op.intent.get("size", 0)) for op in held),
                    journal=path,
                )
            )
    return sessions


def purge_quarantine(
    registry: BackendRegistry,
    *,
    apply_id: str | None = None,
    older_than: str | None = None,
    dirs: AppDirs | None = None,
    dry_run: bool = False,
) -> tuple[int, int, list[str]]:
    """Permanently delete quarantined files. Returns ``(files, bytes, errors)``."""
    import time
    from datetime import datetime

    dirs = (dirs or get_app_dirs()).ensure()
    if apply_id is None and older_than is None:
        raise SafetyError("say which quarantine to purge: a session id or --older-than")
    cutoff = time.time() - parse_duration(older_than) if older_than else None
    files = nbytes = 0
    errors: list[str] = []
    with exclusive_lock(dirs.state / "apply.lock", purpose="purge"):
        for session in list_quarantine(dirs):
            if apply_id is not None and not session.apply_id.startswith(apply_id):
                continue
            if cutoff is not None:
                try:
                    created = datetime.fromisoformat(session.created).timestamp()
                except ValueError:
                    continue
                if created > cutoff:
                    continue
            summary: JournalSummary = summarize(session.journal)
            journal = None if dry_run else Journal(session.journal)
            try:
                for op in summary.ops:
                    if op.intent.get("op") != "quarantine" or op.status != "done":
                        continue
                    done: dict[str, Any] = op.done_record or {}
                    dst = done.get("dst")
                    if not dst:
                        continue
                    try:
                        backend = registry.local() if done.get("fallback") else registry.open(op.intent["src_root"]).backend
                        if backend.stat(dst) is None:
                            continue
                        if not dry_run:
                            backend.remove(dst)
                            if journal is not None:
                                journal.event(op.seq, "purged")
                            stop = str(dirs.quarantine) if done.get("fallback") else registry.open(op.intent["src_root"]).path
                            with contextlib.suppress(Exception):
                                backend.remove_empty_dirs(backend.parent(dst), stop)
                        files += 1
                        nbytes += int(op.intent.get("size", 0))
                    except (OSError, DuplioError) as exc:
                        errors.append(f"{dst}: {exc}")
            finally:
                if journal is not None:
                    journal.close()
    return files, nbytes, errors


def inspect_incomplete(registry: BackendRegistry, dirs: AppDirs | None = None) -> list[tuple[Path, OpState, str]]:
    """Operations that were in flight when Duplio stopped, with their real state."""
    dirs = (dirs or get_app_dirs()).ensure()
    findings = []
    for path in list_journals(dirs.journals):
        for op in summarize(path).incomplete:
            try:
                state = _describe_incomplete(op.intent, registry)
            except DuplioError as exc:
                state = f"cannot check: {exc}"
            findings.append((path, op, state))
    return findings


def _describe_incomplete(intent: dict[str, Any], registry: BackendRegistry) -> str:
    kind = str(intent.get("op", ""))
    if kind in ("merge-copy", "merge-replace"):
        backend = registry.open(intent["dst_root"]).backend
        written = backend.stat(intent["dst"]) is not None
        archived = intent.get("archived")
        kept = bool(archived) and backend.stat(str(archived)) is not None
        if kind == "merge-replace" and kept:
            if written:
                return f"the new version is in place; the previous version is kept at {archived}"
            return (f"the previous version was set aside at {archived} and the new one was not written: run the "
                    "merge again, or move the previous version back")
        if written:
            return f"a file exists at {intent['dst']}, but the copy was not confirmed in the journal: verify it or copy again"
        return "nothing was written (the destination is as before)"
    if kind == "sync-rename":
        backend = registry.open(intent["src_root"]).backend
        renamed = backend.stat(intent["dst"]) is not None
        original = backend.stat(intent["src"]) is not None
        if renamed and not original:
            return f"the file was renamed to {intent['dst']}; the next sync completes the conflict"
        return "the file was not renamed" if original else "neither name exists: check the quarantine and versions folders"
    backend = registry.open(intent["src_root"]).backend
    exists = backend.stat(intent["src"]) is not None
    return "original still in place (nothing happened)" if exists else "original is gone: check the quarantine folder"
