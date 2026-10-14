"""Albums: copies of starred pictures, gathered into one new folder.

An album is an ordinary folder of ordinary copies. It is made with the same care as every other copy Duplio makes
(:mod:`duplio.backup.merge`): each file is written to a temporary name, read back and compared, and only then given its
name; nothing is ever overwritten (a name already taken gets " (2)"); the originals are not touched; and the whole thing is
journaled, so *History & undo* can take the album's copies away again (while the originals still hold the same content).

The folder's name is chosen by the person: words of their own, a date-and-time stamp before or after them, or just the stamp.
"""

from __future__ import annotations

import contextlib
import posixpath
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from duplio.backup.merge import (
    MergeAction,
    MergeReport,
    MergeResult,
    TransferEnv,
    _unique,
    auto_jobs_for,
    execute_action,
)
from duplio.errors import CancelledError, ConfigError, SafetyError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.favorites import Favorite
from duplio.hashing.algorithms import default_full_algorithm
from duplio.plan.executor import new_apply_id
from duplio.plan.journal import Journal, exclusive_lock
from duplio.scan.inventory import FileEntry, Root, RootRole
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.fmt import iso_now
from duplio.util.names import component_issues, fix_component, fix_rel
from duplio.util.text import name_key
from duplio.vfs.guards import check_destination
from duplio.vfs.location import BackendRegistry, Location

STAMP_FORMATS = {"date": "%Y-%m-%d", "minutes": "%Y-%m-%d %H-%M", "seconds": "%Y-%m-%d %H-%M-%S"}
STAMP_PLACES = ("none", "before", "after")
MAX_NAME = 120


@dataclass
class AlbumOptions:
    name: str = ""  # the person's own words for the album ("" for none)
    stamp: str = "none"  # none | before | after: a date and time in front of the name, or behind it
    stamp_format: str = "date"  # date | minutes | seconds
    separator: str = " "

    def validate(self) -> None:
        if self.stamp not in STAMP_PLACES:
            raise ConfigError(f"the stamp goes none, before or after the name, not {self.stamp!r}")
        if self.stamp_format not in STAMP_FORMATS:
            raise ConfigError(f"unknown stamp format {self.stamp_format!r}")


def album_name(options: AlbumOptions, moment: datetime) -> str:
    """The album's folder name from the person's words and the time. Names the destination could not store are made safe."""
    options.validate()
    words = " ".join(options.name.replace("/", "_").replace("\\", "_").split())  # a slash would make a folder inside a folder
    stamp = moment.strftime(STAMP_FORMATS[options.stamp_format]) if options.stamp != "none" else ""
    if not words and not stamp:
        raise ConfigError("give the album a name, or choose to name it by the date and time")
    parts = [stamp, words] if options.stamp == "before" else [words, stamp]
    text = options.separator.join(p for p in parts if p)
    text = text[:MAX_NAME].strip()
    if component_issues(text, windows=True):
        text = fix_component(text, windows=True)  # albums travel between computers, so Windows' rules are used everywhere
    return text


@dataclass
class AlbumAction:
    favorite: Favorite
    entry: FileEntry
    folder: Location  # the folder the original is in
    dest_rel: str  # the album folder, a slash, and the file's name there
    note: str = ""


@dataclass
class AlbumPlan:
    folder_name: str
    destination: Location  # the folder the album folder is made in
    options: AlbumOptions
    actions: list[AlbumAction] = field(default_factory=list)
    skipped: list[tuple[Favorite, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bytes_to_copy(self) -> int:
        return sum(a.entry.size for a in self.actions)

    @property
    def album_location(self) -> Location:
        return Location(self.destination.backend, self.destination.backend.join(self.destination.path, self.folder_name))


def plan_album(favorites: Sequence[Favorite], registry: BackendRegistry, destination: Location, options: AlbumOptions, *,
               now: datetime | None = None) -> AlbumPlan:
    """Decide what goes into the album, and under what names. Reads (stats) each file; changes nothing."""
    moment = now or datetime.now()
    base = album_name(options, moment)
    backend = destination.backend
    info = backend.stat(destination.path)
    if info is None or not info.is_dir:
        raise SafetyError(f"{destination.display} is not a folder that exists, so the album cannot be made there")
    caps = destination.caps
    if not caps.can_write:
        raise SafetyError(f"{destination.display} is read-only, so the album cannot be made there")
    fold = caps.case_insensitive
    existing = {name_key(name, fold=fold) for name, _is_dir in backend.listdir(destination.path)}
    folder = _unique(base, existing, lambda text: name_key(text, fold=fold))
    plan = AlbumPlan(folder, destination, options)
    if folder != base:
        plan.warnings.append(f"There is already something called '{base}' there, so the album is called '{folder}'.")
    windows = caps.windows_names
    taken: set[str] = set()
    for fav in favorites:
        try:
            where = registry.open(fav.uri)
            stat = where.backend.stat(where.path)
        except Exception as exc:
            plan.skipped.append((fav, f"could not be reached ({exc})"))
            continue
        if stat is None:
            plan.skipped.append((fav, "was not found: its drive is not connected, or it was moved or deleted"))
            continue
        if not stat.is_file or stat.is_symlink:
            plan.skipped.append((fav, "is no longer a regular file"))
            continue
        name = where.backend.basename(where.path) or fav.name
        note = ""
        if component_issues(name, windows=windows):
            fixed = fix_rel(name, windows=True)
            note = f"renamed for the destination: {name} → {fixed}"
            name = fixed
        final = _unique(name, taken, lambda text: name_key(text, fold=fold))
        if final != name:
            note = "; ".join(x for x in (note, "another favorite has this name: both kept") if x)
        taken.add(name_key(final, fold=fold))
        if stat.offline:
            note = "; ".join(x for x in (note, "stored only in the cloud: copying it downloads it") if x)
        elif stat.size != fav.size or stat.mtime_ns != fav.mtime_ns:
            note = "; ".join(x for x in (note, "changed since it was starred: the file as it is now is copied") if x)
        parent = Location(where.backend, where.backend.parent(where.path))
        entry = FileEntry(len(plan.actions) + 1, 0, where.path, name, stat.size, stat.mtime_ns, offline=stat.offline)
        plan.actions.append(AlbumAction(fav, entry, parent, posixpath.join(folder, final), note))
    return plan


def run_album(plan: AlbumPlan, registry: BackendRegistry, *, dirs: AppDirs | None = None, progress: ProgressSink | None = None,
              cancel: CancelToken | None = None) -> MergeReport:
    """Copy the album's files. Every copy is verified, journaled and never overwrites anything."""
    dirs = (dirs or get_app_dirs()).ensure()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    apply_id = new_apply_id()
    report = MergeReport(apply_id, None)
    if not plan.actions:
        return report
    started = time.monotonic()
    dest_root = Root(1, plan.destination, RootRole.NORMAL, "", plan.destination.caps)
    check_destination(plan.destination, needed_bytes=plan.bytes_to_copy)
    algorithm = default_full_algorithm()
    with exclusive_lock(dirs.state / "apply.lock", purpose="album"):
        journal = Journal(
            dirs.journals / f"{apply_id}.jsonl",
            header={
                "apply_id": apply_id, "plan_id": "", "session_id": "", "action": "album", "created": iso_now(),
                "roots": [{"index": 0, "uri": "favorites", "role": "normal"},
                          {"index": 1, "uri": plan.album_location.uri, "role": "destination"}],
            },
        )
        report.journal = journal.path
        progress.stage("Copying the album", total_items=len(plan.actions), total_bytes=plan.bytes_to_copy)
        envs: dict[tuple[str, str], TransferEnv] = {}

        def env_for(action: AlbumAction) -> TransferEnv:
            key = (action.folder.backend.id, action.folder.path)
            if key not in envs:
                src = Root(0, action.folder, RootRole.NORMAL, "", action.folder.caps)
                envs[key] = TransferEnv(src, dest_root, journal, apply_id, algorithm, cancel, progress, verify=True)
            return envs[key]

        for action in plan.actions:  # the environments are made here, on one thread, before the copies run in several
            env_for(action)
        jobs = max(1, min(4, auto_jobs_for(next(iter(envs.values())).src, dest_root)))

        def job(action: AlbumAction) -> MergeResult:
            move = MergeAction("copy", action.entry, action.dest_rel, "starred", action.note)
            if cancel.cancelled:
                return MergeResult(move, "cancelled")
            try:
                result = execute_action(move, env_for(action))
            except CancelledError:
                return MergeResult(move, "cancelled")
            except Exception as exc:  # one bad file must not stop the others
                return MergeResult(move, "failed", str(exc) or type(exc).__name__)
            progress.advance(items=1, detail=action.dest_rel)
            return result

        try:
            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="duplio-album") as pool:
                futures = [pool.submit(job, a) for a in plan.actions]
                try:
                    report.results = [f.result() for f in futures]
                except KeyboardInterrupt:
                    cancel.cancel()
                    raise
        finally:
            report.cancelled = cancel.cancelled
            report.bytes_copied = sum(r.action.entry.size for r in report.results if r.status == "done")
            journal.note(event="end", done=report.count("done"), skipped=report.count("skipped"), failed=report.count("failed"),
                         cancelled=report.cancelled)
            journal.close()
    report.seconds = time.monotonic() - started
    with contextlib.suppress(Exception):
        progress.stage("Done")
    return report
