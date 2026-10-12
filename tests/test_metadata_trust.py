"""Size and modification time are hints, not proof.

Content decides identity wherever a file could be removed, replaced or called "safe elsewhere". The tools that must
skip unchanged files cheaply (incremental backup, mirror, snapshots, sync) do use size and time, so they apply the
"racy timestamp" rule: a file changed around the moment it was recorded is compared by content, because an edit
within the same timestamp step keeps the same size and time. Times in the future are never believed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.repository import Repository
from duplio.backup.snapshots import SnapshotSet
from duplio.cli.main import cli
from duplio.util.timecmp import in_the_future, is_racy, racy_window, trusted_newer
from duplio.vfs.location import BackendRegistry

from .conftest import write

T0 = 1_600_000_000.0
SEC = 1_000_000_000


def silent_edit(path: Path, content: bytes) -> None:
    """Change the content but keep the size and the exact modification time."""
    stat = path.stat()
    assert len(content) == stat.st_size
    path.write_bytes(content)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == stat.st_mtime_ns


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def recent() -> float:
    return time.time() - 1.0  # modified a second ago: inside every racy window


# ---------------------------------------------------------------- the rule itself
def test_racy_and_future_rules() -> None:
    now = 1_800_000_000 * SEC
    assert is_racy(now - SEC, now) and is_racy(now, now) and is_racy(now + SEC, now)
    assert not is_racy(now - 10 * SEC, now)
    assert is_racy(now - 10 * SEC, now, 10 * SEC)  # a drive with 10 s steps widens the window
    assert racy_window(1) == 3 * SEC
    assert in_the_future(now + 2 * 86400 * SEC, now) and not in_the_future(now + 3600 * SEC, now)
    assert trusted_newer(now - 10 * SEC, now - 100 * SEC, now_ns=now) == 1
    assert trusted_newer(now + 400 * 86400 * SEC, now - 100 * SEC, now_ns=now) == 0  # never wins on a doubtful clock


# ---------------------------------------------------------------- incremental backup
def test_backup_reads_again_a_file_edited_in_the_same_timestamp_step(registry: BackendRegistry, tmp_path: Path) -> None:
    src = tmp_path / "src"
    f = write(src / "notes.txt", b"version ONE", mtime=recent())
    repo = Repository.init(registry.open(str(tmp_path / "repo")))
    repo.backup(registry.open(str(src)))
    silent_edit(f, b"version TWO")  # same size, same mtime, other content
    repo.backup(registry.open(str(src)))
    repo.restore("latest", registry.open(str(tmp_path / "out")))
    assert (tmp_path / "out" / "notes.txt").read_bytes() == b"version TWO"  # was: the stale ONE


def test_backup_trusts_old_files_and_rehash_distrusts_them(registry: BackendRegistry, tmp_path: Path) -> None:
    src = tmp_path / "src"
    f = write(src / "archive.txt", b"version ONE", mtime=T0)  # long settled
    repo = Repository.init(registry.open(str(tmp_path / "repo")))
    repo.backup(registry.open(str(src)))
    silent_edit(f, b"version TWO")
    repo.backup(registry.open(str(src)))  # size and time are all it has: the documented limit of incremental backup
    repo.restore("latest", registry.open(str(tmp_path / "out1")))
    assert (tmp_path / "out1" / "archive.txt").read_bytes() == b"version ONE"
    repo.backup(registry.open(str(src)), rehash=True)  # --rehash reads everything
    repo.restore("latest", registry.open(str(tmp_path / "out2")))
    assert (tmp_path / "out2" / "archive.txt").read_bytes() == b"version TWO"


# ---------------------------------------------------------------- hard-link snapshots
def test_snapshot_copies_a_file_edited_in_the_same_timestamp_step(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "snaps"
    f = write(src / "notes.txt", b"version ONE", mtime=recent())
    SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=time.time())
    silent_edit(f, b"version TWO")
    second = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=time.time() + 5)
    assert second.linked == 0 and (dest / second.name / "notes.txt").read_bytes() == b"version TWO"


def test_snapshot_verify_links_catches_edits_that_kept_size_and_time(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "snaps"
    f = write(src / "archive.txt", b"version ONE", mtime=T0)
    snaps = SnapshotSet(registry.open(str(dest)))
    snaps.create(registry.open(str(src)), now=1_700_000_000)
    silent_edit(f, b"version TWO")
    trusting = snaps.create(registry.open(str(src)), now=1_700_000_100)
    assert trusting.linked == 1 and (dest / trusting.name / "archive.txt").read_bytes() == b"version ONE"  # the limit
    checked = snaps.create(registry.open(str(src)), now=1_700_000_200, verify_links=True)
    assert checked.linked == 0 and (dest / checked.name / "archive.txt").read_bytes() == b"version TWO"


# ---------------------------------------------------------------- mirror
def test_mirror_compares_recent_files_by_content(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    f = write(src / "notes.txt", b"version ONE", mtime=recent())
    dst.mkdir()
    assert invoke("mirror", str(src), str(dst), "--yes").exit_code == 0
    silent_edit(f, b"version TWO")
    assert invoke("mirror", str(src), str(dst), "--yes").exit_code == 0
    assert (dst / "notes.txt").read_bytes() == b"version TWO"  # was: skipped, same size and time


def test_mirror_checksum_is_the_answer_for_old_files(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    f = write(src / "archive.txt", b"version ONE", mtime=T0)
    dst.mkdir()
    invoke("mirror", str(src), str(dst), "--yes")
    silent_edit(f, b"version TWO")
    invoke("mirror", str(src), str(dst), "--yes")
    assert (dst / "archive.txt").read_bytes() == b"version ONE"  # size and time say "unchanged"
    invoke("mirror", str(src), str(dst), "--checksum", "--yes")
    assert (dst / "archive.txt").read_bytes() == b"version TWO"


# ---------------------------------------------------------------- sync
def _sync_pair(tmp_path: Path, registry: BackendRegistry, dirs):  # type: ignore[no-untyped-def]
    from .test_sync import Pair

    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    return Pair(a, b, registry, dirs)


def test_sync_compares_by_content_a_file_edited_in_the_same_timestamp_step(tmp_path: Path, registry: BackendRegistry,
                                                                           dirs) -> None:  # type: ignore[no-untyped-def]
    from .test_sync import ops, tree

    pair = _sync_pair(tmp_path, registry, dirs)
    f = write(pair.a / "notes.txt", b"version ONE", mtime=recent())
    pair.sync()
    assert tree(pair.b) == {"notes.txt": b"version ONE"}
    silent_edit(f, b"version TWO")  # A changed, but its size and time still match the history
    plan, report = pair.sync()
    assert ops(plan) == {"notes.txt": "conflict"}  # which side is right cannot be known: both are kept, nothing is lost
    assert not report.failures
    contents = sorted(tree(pair.a).values()) + sorted(tree(pair.b).values())
    assert contents.count(b"version ONE") == 2 and contents.count(b"version TWO") == 2


def test_sync_cannot_see_a_silent_edit_of_a_long_settled_file(tmp_path: Path, registry: BackendRegistry,
                                                              dirs) -> None:  # type: ignore[no-untyped-def]
    pair = _sync_pair(tmp_path, registry, dirs)
    f = write(pair.a / "archive.txt", b"version ONE", mtime=T0)
    pair.sync()
    silent_edit(f, b"version TWO")
    plan, _ = pair.plan()
    assert not plan.has_work  # the documented limit of any size-and-time sync; `manifest verify` finds this


def test_a_time_in_the_future_never_wins_a_newer_contest(tmp_path: Path, registry: BackendRegistry,
                                                         dirs) -> None:  # type: ignore[no-untyped-def]
    from duplio.backup.merge import MergeOptions, plan_merge
    from duplio.compare.diff import DiffOptions, diff_inventory
    from duplio.scan.filters import ScanFilter
    from duplio.scan.inventory import RootRole, make_roots, scan

    from .test_sync import ops

    a, b = tmp_path / "A", tmp_path / "B"
    future = time.time() + 5 * 365 * 86400  # a camera with a wrong clock
    write(a / "x.txt", b"written on the camera", mtime=future)
    write(b / "x.txt", b"edited yesterday on the laptop", mtime=time.time() - 86400)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    diff = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    changed = next(i for i in diff.items if i.side == 0)
    assert changed.newer == 0 and "future" in changed.note
    plan = plan_merge(diff, MergeOptions(on_conflict="newer"))
    assert [x.kind for x in plan.actions] == ["rename"]  # keep both instead of trusting the clock
    # sync: both changed, the camera's time is in the future
    pair = _sync_pair(tmp_path / "s", registry, dirs)
    write(pair.a / "x.txt", b"v1", mtime=T0)
    write(pair.b / "x.txt", b"v1", mtime=T0)
    pair.sync()
    write(pair.a / "x.txt", b"written on the camera", mtime=future)
    write(pair.b / "x.txt", b"edited yesterday on the laptop", mtime=time.time() - 86400)
    plan2, _ = pair.plan(on_conflict="newer")
    assert ops(plan2) == {"x.txt": "conflict"}


# ---------------------------------------------------------------- the history format
def test_an_older_sync_history_is_dropped_not_misread(tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    from duplio.backup.syncstate import SyncState, pair_id

    path = dirs.state / "sync" / f"{pair_id('/a', '/b')}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE files (key BLOB PRIMARY KEY, rel_l BLOB, size_l INTEGER, mtime_l INTEGER,
                            rel_r BLOB, size_r INTEGER, mtime_r INTEGER) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema', '1');
        INSERT INTO files VALUES (x'6b', x'6b', 1, 1, x'6b', 1, 1);""")
    conn.commit()
    conn.close()
    state = SyncState.open(dirs, "/a", "/b")
    assert state.load() == {}  # unknown format: behave as a first run (which never deletes)
    state.close()


@pytest.mark.parametrize("dst_shift", [False, True])
def test_trusted_newer_still_orders_ordinary_times(dst_shift: bool) -> None:
    now = int(time.time() * 1e9)
    assert trusted_newer(now - SEC * 100, now - SEC * 200, now_ns=now, dst_shift=dst_shift) == 1
    assert trusted_newer(now - SEC * 200, now - SEC * 100, now_ns=now, dst_shift=dst_shift) == -1
