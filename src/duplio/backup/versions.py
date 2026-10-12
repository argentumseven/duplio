"""Housekeeping for ``.duplio-versions``: the old copies kept by mirror, merge and sync.

Every run that replaces a file keeps the previous version in
``<location>/.duplio-versions/<run id>/<path>``. Run ids start with a UTC time stamp
(``20261012T183750699Z-328d`` or ``20261012T183750Z``), which is how age is judged.
Pruning is the only place those old versions are ever destroyed, so it is explicit:
nothing is removed unless asked for, and only whole runs are removed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from duplio.errors import ConfigError
from duplio.util.fmt import parse_duration
from duplio.vfs.base import Backend
from duplio.vfs.location import Location

VERSIONS_DIR = ".duplio-versions"
_STAMP = re.compile(r"^(\d{8}T\d{6})(\d{3})?Z")


@dataclass
class VersionRun:
    name: str
    path: str
    when: float | None  # POSIX time parsed from the name; None if the name is not one of ours
    files: int = 0
    bytes: int = 0


def parse_stamp(name: str) -> float | None:
    match = _STAMP.match(name)
    if not match:
        return None
    moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC).timestamp()
    return moment + (int(match.group(2)) / 1000 if match.group(2) else 0)


def list_versions(location: Location) -> list[VersionRun]:
    """Version runs in a location, oldest first."""
    b = location.backend
    root = b.join(location.path, VERSIONS_DIR)
    if b.stat(root) is None:
        return []
    runs = []
    for name, is_dir in b.listdir(root):
        if not is_dir:
            continue
        folder = b.join(root, name)
        run = VersionRun(name, folder, parse_stamp(name))
        for raw in b.walk(folder, walk_filter=None):
            run.files += 1
            run.bytes += raw.size
        runs.append(run)
    runs.sort(key=lambda r: (r.when is None, r.when or 0.0, r.name))
    return runs


def select_for_pruning(runs: list[VersionRun], *, older_than: str | None = None, keep_last: int | None = None,
                       now: float | None = None) -> list[VersionRun]:
    """Runs to remove: older than the cut-off and not among the newest ``keep_last``. Unrecognised folders stay."""
    if older_than is None and keep_last is None:
        raise ConfigError("say what to prune: --older-than AGE and/or --keep-last N")
    now = time.time() if now is None else now
    try:
        cutoff = now - parse_duration(older_than) if older_than else None
    except ValueError as exc:
        raise ConfigError(f"not a duration: {older_than!r} (examples: 30d, 12w, 1y)") from exc
    dated = [r for r in runs if r.when is not None]
    protected = {r.name for r in (dated[-keep_last:] if keep_last else [])}
    chosen = []
    for run in dated:
        if run.name in protected:
            continue
        if cutoff is not None and (run.when or 0) >= cutoff:
            continue
        chosen.append(run)
    return chosen


def remove_run(backend: Backend, run: VersionRun) -> None:
    for raw in list(backend.walk(run.path, walk_filter=None)):
        backend.remove(raw.path)
    _remove_dirs(backend, run.path)


def _remove_dirs(backend: Backend, folder: str) -> None:
    for name, is_dir in backend.listdir(folder):
        if is_dir:
            _remove_dirs(backend, backend.join(folder, name))
    backend.remove_empty_dirs(folder, backend.parent(folder))


def prune_versions(location: Location, *, older_than: str | None = None, keep_last: int | None = None,
                   dry_run: bool = False) -> list[VersionRun]:
    """Remove (or, with ``dry_run``, just select) old version runs. Returns the runs concerned."""
    chosen = select_for_pruning(list_versions(location), older_than=older_than, keep_last=keep_last)
    if not dry_run:
        for run in chosen:
            remove_run(location.backend, run)
    return chosen
