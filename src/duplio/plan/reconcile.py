"""Settle operations that a crash or power loss left half-recorded.

Before every change the executor writes an *intent* record, with the names of the files the change will create; after
it, a *done* record. A process killed in between leaves an intent with no outcome. The file system still tells what
happened, and because the intent names every file involved, the answer can be read from facts instead of guessed:

* the change had not started (everything as before) — recorded as skipped;
* the change had finished, only its record was missing — recorded as done, so ``undo`` can reverse it;
* the change was half-way, and can be rolled back with certainty (a file moved aside, its replacement not yet
  written) — rolled back, and recorded as skipped;
* anything else is left as it is and reported, never guessed at.

Nothing here deletes data: the only file operations are renames that put a file back where it came from.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from duplio.errors import DuplioError
from duplio.hashing.algorithms import default_full_algorithm
from duplio.hashing.core import hash_file
from duplio.plan.journal import Journal, OpState, exclusive_lock, list_journals, summarize
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.vfs.location import Location


class Opener(Protocol):
    """Anything that turns a location string from the journal into a location (a registry, or a fixed set)."""

    def open(self, text: str) -> Location: ...


class KnownLocations:
    """An :class:`Opener` for code that already holds the locations a run uses."""

    def __init__(self, *locations: Location) -> None:
        self._by_uri = {loc.uri: loc for loc in locations}

    def open(self, text: str) -> Location:
        try:
            return self._by_uri[text]
        except KeyError:
            raise DuplioError(f"unknown location {text!r}") from None


COMPLETED = "completed"
ROLLED_BACK = "rolled-back"
NOT_STARTED = "not-started"
UNRESOLVED = "unresolved"


@dataclass
class Resolution:
    journal: Path
    seq: int
    op: str
    path: str
    outcome: str
    note: str


Verdict = tuple[str, str, str, dict[str, Any]]  # (outcome, journal event or "", note, fields for the event)


def _skipped(note: str, outcome: str = NOT_STARTED) -> Verdict:
    return outcome, "skipped", note, {"reason": note}


def _done(note: str, **fields: Any) -> Verdict:
    return COMPLETED, "done", note, fields


def _unresolved(note: str) -> Verdict:
    return UNRESOLVED, "", note, {}


def _quarantine(op: OpState, registry: Opener, dirs: AppDirs) -> Verdict:
    i = op.intent
    backend = registry.open(i["src_root"]).backend
    planned, fallback = i.get("planned_dst"), i.get("fallback_dst")
    if backend.stat(i["src"]) is not None:
        return _skipped("interrupted before the file was moved; nothing changed")
    if planned and backend.stat(planned) is not None:
        return _done("the file had been moved to the quarantine", dst=planned, fallback=False, recovered=True)
    if fallback and os.path.lexists(fallback):
        return _done("the file had been moved to the quarantine", dst=fallback, fallback=True,
                     dst_root=str(dirs.quarantine), recovered=True)
    return _unresolved("the original is gone and no quarantined copy was found: look in the .duplio-quarantine folders")


def _delete(op: OpState, registry: Opener) -> Verdict:
    i = op.intent
    backend = registry.open(i["src_root"]).backend
    probe = i.get("probe")
    present = backend.stat(i["src"]) is not None
    parked = bool(probe) and backend.stat(str(probe)) is not None
    if present and not parked:
        return _skipped("interrupted before the file was touched; nothing changed")
    if parked and not present:
        # The file was set aside under a temporary name and the run died before finishing. Putting it back returns
        # to the state before the operation; running the plan again repeats all the checks.
        backend.rename(str(probe), i["src"])
        return _skipped("interrupted while deleting: the file was put back where it was", ROLLED_BACK)
    if not present and not parked:
        return _done("the file had been deleted", recovered=True)
    return _unresolved(f"both {i['src']} and {probe} exist: check them by hand")


def _trash(op: OpState, registry: Opener) -> Verdict:
    i = op.intent
    backend = registry.open(i["src_root"]).backend
    if backend.stat(i["src"]) is not None:
        return _skipped("interrupted before the file was moved; nothing changed")
    return _done("the file had been moved to the system trash (restore it from there if needed)", recovered=True)


def _link(op: OpState, registry: Opener) -> Verdict:
    i = op.intent
    backend = registry.open(i["src_root"]).backend
    tmp = i.get("tmp")
    if tmp and backend.stat(str(tmp)) is not None:
        if not os.path.basename(str(tmp)).startswith(".duplio-link-"):
            return _unresolved(f"unexpected temporary file {tmp}")
        backend.remove(str(tmp))  # our own half-made link: the original was never replaced
        return _skipped("interrupted while linking: the half-made link was removed; the file is as before",
                        ROLLED_BACK)
    if i["op"] == "hardlink" and backend.same_file(i["src"], i["keeper"]) is True:
        return _done("the file had been replaced by a hard link", recovered=True)
    return _skipped("interrupted before the file was replaced (or replaced by an identical clone); nothing to undo")


def _sync_rename(op: OpState, registry: Opener) -> Verdict:
    i = op.intent
    backend = registry.open(i["src_root"]).backend
    renamed = backend.stat(i["dst"]) is not None
    original = backend.stat(i["src"]) is not None
    if renamed and not original:
        return _done("the file had been renamed", dst=i["dst"], recovered=True)
    if original and not renamed:
        return _skipped("interrupted before the file was renamed; nothing changed")
    return _unresolved("neither or both names exist: check the quarantine and versions folders")


def _digest_matches_source(i: dict[str, Any], registry: Opener, dst: str) -> tuple[str, str] | None:
    """The copy's digest, if the source still holds exactly that content (otherwise it cannot be vouched for)."""
    algorithm = default_full_algorithm()
    size = int(i["size"])
    db, sb = registry.open(i["dst_root"]).backend, registry.open(i["src_root"]).backend
    info, sinfo = db.stat(dst), sb.stat(i["src"])
    if info is None or sinfo is None or info.size != size or sinfo.size != size:
        return None
    try:
        digest = hash_file(db, dst, algorithm, expected_size=size)
        if hash_file(sb, i["src"], algorithm, expected_size=size) != digest:
            return None
    except OSError:
        return None
    return algorithm, digest


def _merge(op: OpState, registry: Opener) -> Verdict:
    i = op.intent
    db = registry.open(i["dst_root"]).backend
    dst, archived = str(i["dst"]), i.get("archived")
    written = db.stat(dst) is not None
    kept = bool(archived) and db.stat(str(archived)) is not None
    replace = i["op"] == "merge-replace"
    if replace and kept and not written:
        db.rename(str(archived), dst)  # the previous version goes back under its own name
        return _skipped("interrupted while replacing: the previous version was put back", ROLLED_BACK)
    if not written:
        return _skipped("nothing was written; the destination is as before")
    if replace and not kept:
        return _skipped("interrupted before the previous version was set aside; nothing changed")
    vouched = _digest_matches_source(i, registry, dst)
    if vouched is None:
        return _unresolved(f"a file exists at {dst} but it could not be confirmed to be the copy: verify it by hand")
    algorithm, digest = vouched
    fields: dict[str, Any] = {"dst": dst, "digest": digest, "algorithm": algorithm, "size": int(i["size"]),
                              "recovered": True}
    if replace:
        fields["archived"] = archived
    return _done("the copy had been written and verified against the source", **fields)


_RESOLVERS = {"trash": _trash, "hardlink": _link, "reflink": _link, "sync-rename": _sync_rename,
              "merge-copy": _merge, "merge-replace": _merge}


def resolve(op: OpState, registry: Opener, dirs: AppDirs) -> Verdict:
    """What the file system says happened to one journalled operation (see the module documentation)."""
    kind = str(op.intent.get("op", ""))
    try:
        if kind == "quarantine":
            return _quarantine(op, registry, dirs)
        if kind == "delete":
            return _delete(op, registry)
        resolver = _RESOLVERS.get(kind)
        if resolver is None:
            return _unresolved(f"unknown operation {kind!r}")
        return resolver(op, registry)
    except (OSError, DuplioError, KeyError) as exc:
        return _unresolved(f"cannot tell: {exc}")


def reconcile_journal(path: Path, registry: Opener, dirs: AppDirs, *, write: bool = True,
                      summary=None) -> list[Resolution]:  # type: ignore[no-untyped-def]
    """Settle the interrupted operations of one journal. With ``write=False`` nothing is changed or recorded."""
    summary = summary or summarize(path)
    out: list[Resolution] = []
    journal = Journal(path) if write and summary.incomplete else None
    try:
        for op in summary.incomplete:
            if not write:
                outcome, event, note, fields = _preview(op, registry, dirs)
            else:
                outcome, event, note, fields = resolve(op, registry, dirs)
                if event and journal is not None:
                    journal.event(op.seq, event, **fields)
            if event:
                op.events.append({"type": "op", "event": event, "seq": op.seq, **fields})
            out.append(Resolution(path, op.seq, str(op.intent.get("op", "")), str(op.intent.get("src", "")), outcome,
                                  note))
    finally:
        if journal is not None:
            journal.close()
    return out


def _preview(op: OpState, registry: Opener, dirs: AppDirs) -> Verdict:
    """What :func:`_resolve` would conclude, without the file renames it may perform."""
    kind = str(op.intent.get("op", ""))
    if kind in ("delete", "merge-replace"):
        with contextlib.suppress(Exception):
            i = op.intent
            backend = registry.open(i["src_root" if kind == "delete" else "dst_root"]).backend
            if kind == "delete" and backend.stat(i["src"]) is None and backend.stat(str(i.get("probe"))) is not None:
                return ROLLED_BACK, "skipped", "interrupted while deleting: the file would be put back", {"reason": "preview"}
            if (kind == "merge-replace" and backend.stat(str(i["dst"])) is None
                    and backend.stat(str(i.get("archived"))) is not None):
                return (ROLLED_BACK, "skipped", "interrupted while replacing: the previous version would be put back",
                        {"reason": "preview"})
    return resolve(op, registry, dirs)


def reconcile(registry: Opener, dirs: AppDirs | None = None, *, apply_id: str | None = None,
              write: bool = True) -> list[Resolution]:
    """Settle interrupted operations in every journal (or the one named). Refuses while a run is in progress."""
    dirs = (dirs or get_app_dirs()).ensure()
    paths = [p for p in list_journals(dirs.journals) if apply_id is None or p.stem.startswith(apply_id)]
    out: list[Resolution] = []
    if not write:
        for path in paths:
            out.extend(reconcile_journal(path, registry, dirs, write=False))
        return out
    with exclusive_lock(dirs.state / "apply.lock", purpose="recover"):
        for path in paths:
            out.extend(reconcile_journal(path, registry, dirs))
    return out


def record_failure(journal: Journal, seq: int, intent: Mapping[str, Any], exc: BaseException, opener: Opener,
                   dirs: AppDirs | None = None) -> None:
    """Journal an error that interrupted an operation.

    If the change had in fact taken effect (the error came after it, or only the confirmation was lost, as with a
    network share that times out), it is recorded as done so that ``undo`` still reverses it; otherwise as failed.
    """
    verdict = None
    with contextlib.suppress(Exception):
        record = {"type": "op", "event": "intent", "seq": seq, **intent}
        verdict = resolve(OpState(seq, record), opener, dirs or get_app_dirs())
    if verdict is not None and verdict[0] == COMPLETED:
        journal.event(seq, "done", note=f"finished; an error followed: {exc}", **verdict[3])
    else:
        journal.failed(seq, str(exc) or type(exc).__name__)
