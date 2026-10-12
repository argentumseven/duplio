"""Run a scenario once for every place it can be interrupted, and check what is left.

``sweep`` first runs the scenario to count its crash points (see ``duplio.util.faults``), then runs it again for each
point in a forked child that is killed there (``os._exit``, no cleanup of any kind) or made to fail with an I/O error.
After each interruption the parent checks the invariants on whatever the child left on disk and then lets the program
recover. A failure names the point, so the step that needs fixing is known.
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from duplio.util.appdirs import AppDirs
from duplio.util.faults import CRASH_EXIT, configure, reset

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="crash sweeps fork the process")


@dataclass
class World:
    """One self-contained copy of the situation under test: data folders plus Duplio's own state folder."""

    root: Path

    @property
    def dirs(self) -> AppDirs:
        home = self.root / "home"
        return AppDirs(home / "config", home / "state", home / "cache").ensure()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


DEBRIS = (".duplio-tmp-", ".duplio-del-", ".duplio-link-", ".duplio-probe-")


def is_debris(name: str) -> bool:
    """Leftovers of an interrupted write: named so they can never be mistaken for a user's file."""
    return any(marker in name for marker in DEBRIS) or name == "MANIFEST.json"


def files_under(root: Path, *, skip: tuple[str, ...] = (), debris: bool = False) -> dict[str, str]:
    """Relative path -> content hash for every file below ``root`` (folders named in ``skip`` are left out).

    Debris (temporary files, manifests) is left out unless asked for.
    """
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or p.is_symlink() or any(part in skip for part in rel.parts):
            continue
        if not debris and is_debris(p.name):
            continue
        out[rel.as_posix()] = sha(p)
    return out


def _run_child(fn: Callable[[], object], crash_at: int | None, error_at: int | None, record: Path | None,
               log: Path) -> None:
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.dup2(fd, 2)
    names: list[str] | None = [] if record is not None else None
    configure(crash_at=crash_at, error_at=error_at, record=names)
    code = 0
    try:
        fn()
    except BaseException:
        traceback.print_exc()
        code = 3
    reset()
    if record is not None and names is not None:
        record.write_text(json.dumps(names))
    os._exit(code)


def run_forked(fn: Callable[[], object], log: Path, *, crash_at: int | None = None, error_at: int | None = None,
               record: Path | None = None) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            _run_child(fn, crash_at, error_at, record, log)
        finally:
            os._exit(70)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


@dataclass
class Outcome:
    point: int
    name: str
    status: int  # 0 finished, CRASH_EXIT killed at the point, 3 raised an exception


def sweep(
    tmp: Path,
    make_world: Callable[[Path], World],
    scenario: Callable[[World], object],
    check: Callable[[World, Outcome], None],
    recover: Callable[[World, Outcome], None],
    *,
    mode: str = "crash",
    stride: int | None = None,
) -> int:
    """Interrupt ``scenario`` at points; returns how many points there are. Raises with every failing point.

    By default every third point is tried (plus the last), which keeps the test run short; set ``DUPLIO_SWEEP=full``
    to try every one, as a release gate should.
    """
    if stride is None:
        stride = 1 if os.environ.get("DUPLIO_SWEEP") == "full" else 3
    # Killing a process does not lose what the operating system already holds, so the (slow) fsync calls that make
    # real power cuts survivable are not needed to test what a kill leaves behind.
    with mock.patch("os.fsync", lambda fd: None):
        return _sweep(tmp, make_world, scenario, check, recover, mode=mode, stride=stride)


def _sweep(
    tmp: Path,
    make_world: Callable[[Path], World],
    scenario: Callable[[World], object],
    check: Callable[[World, Outcome], None],
    recover: Callable[[World, Outcome], None],
    *,
    mode: str,
    stride: int,
) -> int:
    counting = make_world(tmp / "count")
    record = tmp / "points.json"
    status = run_forked(lambda: scenario(counting), tmp / "count.log", record=record)
    assert status == 0, f"the scenario must succeed when nothing goes wrong:\n{(tmp / 'count.log').read_text()}"
    names: list[str] = json.loads(record.read_text())
    assert names, "the scenario reached no crash point; it changes nothing durable"
    failures: list[str] = []
    points = sorted({*range(1, len(names) + 1, stride), len(names)})
    for point in points:
        world = make_world(tmp / f"p{point}")
        if mode == "crash":
            status = run_forked(lambda w=world: scenario(w), tmp / f"p{point}.log", crash_at=point)
            expected = {CRASH_EXIT}
        else:
            status = run_forked(lambda w=world: scenario(w), tmp / f"p{point}.log", error_at=point)
            expected = {0, 3}
        outcome = Outcome(point, names[point - 1], status)
        try:
            assert status in expected, f"unexpected exit status {status}: {(tmp / f'p{point}.log').read_text()[-2000:]}"
            check(world, outcome)
            recover(world, outcome)
        except Exception as exc:  # every failing point is reported, not just the first
            failures.append(f"#{point} {outcome.name} (status {status}): {type(exc).__name__}: {str(exc)[:600]}")
    assert not failures, f"{len(failures)} of {len(names)} points broke an invariant:\n" + "\n".join(failures[:25])
    return len(names)
