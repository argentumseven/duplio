"""Two-way sync: first run, change propagation, deletions, conflicts, brakes, undo."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.sync import SyncOp, SyncOptions, SyncPlan, SyncReport, plan_sync, run_sync
from duplio.backup.syncstate import SyncState, pair_id
from duplio.cli.main import cli
from duplio.compare.diff import Basis
from duplio.errors import ConfigError, SafetyError
from duplio.plan.undo import undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.appdirs import AppDirs
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write
from .fakes import FakeSMB

T0 = 1_600_000_000.0


class Pair:
    """A tiny harness: two folders plus the state folder, with ``plan``/``sync`` helpers."""

    def __init__(self, a: Path | str, b: Path | str, registry: BackendRegistry, dirs: AppDirs) -> None:
        self.a, self.b, self.registry, self.dirs = a, b, registry, dirs

    def plan(self, *, caps_b=None, mutate=None, reset: bool = False, scan_filter=None, **opts) -> tuple[SyncPlan, SyncState]:  # type: ignore[no-untyped-def]
        roots = make_roots([(self.registry.open(str(self.a)), RootRole.NORMAL),
                            (self.registry.open(str(self.b)), RootRole.NORMAL)])
        if caps_b:
            roots[1].caps = roots[1].caps.with_(**caps_b)
        inventory = scan(roots, scan_filter or ScanFilter(min_size=0))
        if mutate:
            mutate(inventory)
        state = SyncState.open(self.dirs, roots[0].location.uri, roots[1].location.uri)
        if reset:
            state.reset()
        return plan_sync(inventory, state.load(), SyncOptions(**opts)), state

    def sync(self, **opts) -> tuple[SyncPlan, SyncReport]:  # type: ignore[no-untyped-def]
        plan, state = self.plan(**opts)
        try:
            return plan, run_sync(plan, self.registry, state, dirs=self.dirs)
        finally:
            state.close()


@pytest.fixture
def pair(tmp_path: Path, registry: BackendRegistry, dirs: AppDirs) -> Pair:
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return Pair(a, b, registry, dirs)


def tree(root: Path | str) -> dict[str, bytes]:
    root = Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not rel.parts[0].startswith(".duplio"):
            out[rel.as_posix()] = p.read_bytes()
    return out


def ops(plan: SyncPlan) -> dict[str, str]:
    return {a.rel: a.op.value for a in plan.actions if a.op is not SyncOp.MARK}


# ---------------------------------------------------------------- first run and steady state
def test_first_run_unions_the_folders_and_never_deletes(pair: Pair) -> None:
    write(pair.a / "only-a.txt", b"a", mtime=T0)
    write(pair.b / "only-b.txt", b"b", mtime=T0)
    write(pair.a / "shared.txt", b"same", mtime=T0)
    write(pair.b / "shared.txt", b"same", mtime=T0)
    plan, report = pair.sync()
    assert plan.first_run and ops(plan) == {"only-a.txt": "copy", "only-b.txt": "copy"}
    assert report.count("done") == 2 and not report.failures
    assert tree(pair.a) == tree(pair.b) == {"only-a.txt": b"a", "only-b.txt": b"b", "shared.txt": b"same"}
    plan, report = pair.sync()
    assert not plan.first_run and not plan.has_work and plan.unchanged == 3


def test_steady_state_reads_nothing(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    write(pair.a / "f.bin", rand_bytes(5000, 1), mtime=T0)
    pair.sync()

    def no_reading(self, path):  # type: ignore[no-untyped-def]
        raise AssertionError("an unchanged tree must not be read")

    monkeypatch.setattr(type(pair.registry.local()), "open_read", no_reading)
    plan, _ = pair.plan()
    assert not plan.has_work


def test_argument_order_does_not_matter(pair: Pair, tmp_path: Path) -> None:
    write(pair.a / "x.txt", b"x", mtime=T0)
    pair.sync()
    swapped = Pair(pair.b, pair.a, pair.registry, pair.dirs)
    plan, _ = swapped.plan()
    assert not plan.first_run and not plan.has_work
    write(pair.a / "x.txt", b"edited on what is now side B", mtime=T0 + 99)
    plan, _ = swapped.sync()
    assert ops(plan) == {"x.txt": "update"} and tree(pair.b)["x.txt"] == b"edited on what is now side B"


def test_first_run_with_different_files_at_one_path_is_a_conflict(pair: Pair) -> None:
    write(pair.a / "n.txt", b"from A", mtime=T0 + 10)
    write(pair.b / "n.txt", b"from B!", mtime=T0)
    plan, report = pair.sync()
    assert ops(plan) == {"n.txt": "conflict"} and not report.failures
    got = tree(pair.a)
    assert got == tree(pair.b)
    assert got["n.txt"] == b"from A"  # newer keeps the name
    (aside,) = [k for k in got if k != "n.txt"]
    assert aside.startswith("n (conflict B ") and got[aside] == b"from B!"


# ---------------------------------------------------------------- propagation
def test_changes_flow_in_both_directions(pair: Pair) -> None:
    for name in ("a-edit.txt", "b-edit.txt", "same.txt"):
        write(pair.a / name, b"v1", mtime=T0)
        write(pair.b / name, b"v1", mtime=T0)
    pair.sync()
    write(pair.a / "a-edit.txt", b"A changed this", mtime=T0 + 100)
    write(pair.b / "b-edit.txt", b"B changed this one", mtime=T0 + 200)
    write(pair.a / "new" / "deep.txt", b"fresh", mtime=T0 + 300)
    plan, report = pair.sync()
    assert ops(plan) == {"a-edit.txt": "update", "b-edit.txt": "update", "new/deep.txt": "copy"}
    assert report.count("done") == 3
    assert tree(pair.a) == tree(pair.b)
    assert tree(pair.b)["a-edit.txt"] == b"A changed this" and tree(pair.a)["b-edit.txt"] == b"B changed this one"
    old = {p.read_bytes() for p in (pair.b / ".duplio-versions").rglob("a-edit.txt")}
    assert old == {b"v1"}  # the replaced version is kept


def test_modification_times_are_carried_over_so_the_next_run_is_quiet(pair: Pair) -> None:
    write(pair.a / "f.txt", b"data", mtime=T0)
    pair.sync()
    assert (pair.b / "f.txt").stat().st_mtime_ns == (pair.a / "f.txt").stat().st_mtime_ns
    assert not pair.plan()[0].has_work


# ---------------------------------------------------------------- deletions
def test_deletion_is_moved_aside_not_destroyed(pair: Pair) -> None:
    write(pair.a / "gone.txt", b"precious", mtime=T0)
    write(pair.b / "gone.txt", b"precious", mtime=T0)
    write(pair.a / "stays.txt", b"s", mtime=T0)
    write(pair.b / "stays.txt", b"s", mtime=T0)
    pair.sync()
    os.remove(pair.b / "gone.txt")
    plan, report = pair.sync()
    assert ops(plan) == {"gone.txt": "delete"} and report.count("done") == 1
    assert not (pair.a / "gone.txt").exists()
    kept = list((pair.a / ".duplio-quarantine").rglob("gone.txt"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"precious"
    assert not pair.plan()[0].has_work  # and it does not come back


def test_delete_on_one_side_but_edit_on_the_other_keeps_the_edit(pair: Pair) -> None:
    write(pair.a / "f.txt", b"v1", mtime=T0)
    write(pair.b / "f.txt", b"v1", mtime=T0)
    pair.sync()
    os.remove(pair.b / "f.txt")
    write(pair.a / "f.txt", b"edited meanwhile", mtime=T0 + 50)
    plan, _ = pair.sync()
    assert ops(plan) == {"f.txt": "copy"}
    assert tree(pair.b) == {"f.txt": b"edited meanwhile"}


def test_deletion_policies(pair: Pair) -> None:
    write(pair.a / "f.txt", b"v1", mtime=T0)
    write(pair.b / "f.txt", b"v1", mtime=T0)
    pair.sync()
    os.remove(pair.b / "f.txt")
    plan, _ = pair.plan(deletions="ignore")
    assert not plan.has_work and any("left alone" in s.reason for s in plan.skipped)
    plan, _ = pair.sync(deletions="restore")
    assert ops(plan) == {"f.txt": "copy"} and tree(pair.b) == {"f.txt": b"v1"}


def test_gone_from_both_sides_is_forgotten(pair: Pair) -> None:
    write(pair.a / "f.txt", b"v1", mtime=T0)
    pair.sync()
    os.remove(pair.a / "f.txt")
    os.remove(pair.b / "f.txt")
    plan, _ = pair.sync()
    assert not plan.has_work and plan.gone
    write(pair.a / "f.txt", b"back again", mtime=T0 + 5)
    plan, _ = pair.plan()
    assert ops(plan) == {"f.txt": "copy"}  # new, not 'deleted on B'


# ---------------------------------------------------------------- conflicts
@pytest.fixture
def conflicted(pair: Pair) -> Pair:
    write(pair.a / "c.txt", b"v1", mtime=T0)
    write(pair.b / "c.txt", b"v1", mtime=T0)
    pair.sync()
    write(pair.a / "c.txt", b"A's version", mtime=T0 + 100)
    write(pair.b / "c.txt", b"B's version is newer", mtime=T0 + 200)
    return pair


def test_conflict_keep_both_newer_wins_the_name(conflicted: Pair) -> None:
    plan, report = conflicted.sync()
    assert ops(plan) == {"c.txt": "conflict"} and not report.failures
    for side in (conflicted.a, conflicted.b):
        got = tree(side)
        assert got["c.txt"] == b"B's version is newer"
        (aside,) = [k for k in got if k != "c.txt"]
        assert aside.startswith("c (conflict A ") and got[aside] == b"A's version"
    assert not conflicted.plan()[0].has_work  # both sides identical, history up to date


@pytest.mark.parametrize(("policy", "winner"), [("newer", b"B's version is newer"), ("a", b"A's version"),
                                                 ("b", b"B's version is newer")])
def test_conflict_winner_policies_archive_the_loser(conflicted: Pair, policy: str, winner: bytes) -> None:
    plan, _ = conflicted.sync(on_conflict=policy)
    assert ops(plan) == {"c.txt": "update"}
    assert tree(conflicted.a) == tree(conflicted.b) == {"c.txt": winner}
    versions = {p.read_bytes() for d in (conflicted.a, conflicted.b) for p in d.rglob(".duplio-versions/*/c.txt")}
    assert len(versions) == 1 and winner not in versions  # the losing version is recoverable


def test_conflict_skip_leaves_both_alone(conflicted: Pair) -> None:
    plan, _ = conflicted.sync(on_conflict="skip")
    assert not plan.has_work and any("both sides" in s.reason for s in plan.skipped)
    assert tree(conflicted.a)["c.txt"] == b"A's version" and tree(conflicted.b)["c.txt"] == b"B's version is newer"


def test_both_changed_to_the_same_content_is_not_a_conflict(pair: Pair) -> None:
    write(pair.a / "c.txt", b"v1", mtime=T0)
    write(pair.b / "c.txt", b"v1", mtime=T0)
    pair.sync()
    write(pair.a / "c.txt", b"same edit", mtime=T0 + 50)
    write(pair.b / "c.txt", b"same edit", mtime=T0 + 60)
    plan, _ = pair.sync()
    assert not plan.has_work and tree(pair.a) == tree(pair.b) == {"c.txt": b"same edit"}


def test_conflict_names_never_collide(conflicted: Pair) -> None:
    import datetime

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    write(conflicted.a / f"c (conflict A {stamp}).txt", b"already here", mtime=T0)
    write(conflicted.b / f"c (conflict A {stamp}).txt", b"already here", mtime=T0)
    _, report = conflicted.sync()
    assert not report.failures
    names = sorted(tree(conflicted.a))
    assert len(names) == 3 and tree(conflicted.a) == tree(conflicted.b)


# ---------------------------------------------------------------- content basis
def test_touch_without_change_is_not_copied_with_content_basis(pair: Pair) -> None:
    write(pair.a / "f.bin", rand_bytes(4000, 3), mtime=T0)
    pair.sync()
    os.utime(pair.a / "f.bin", (T0 + 500, T0 + 500))
    assert ops(pair.plan(basis=Basis.TIME_SIZE)[0]) == {"f.bin": "update"}
    plan, report = pair.sync(basis=Basis.CONTENT)
    assert not plan.has_work and not report.results
    assert (pair.b / ".duplio-versions").exists() is False


# ---------------------------------------------------------------- brakes
def test_empty_side_stops_the_run(pair: Pair) -> None:
    for i in range(5):
        write(pair.a / f"f{i}.txt", b"data %d" % i, mtime=T0)
    pair.sync()
    for p in pair.b.iterdir():
        if p.is_file():
            p.unlink()
    with pytest.raises(SafetyError, match="is empty"):
        pair.plan()
    plan, _ = pair.plan(force=True)  # deliberate: the deletions are propagated
    assert plan.count(SyncOp.DELETE) == 5


def test_mass_deletion_stops_the_run(pair: Pair) -> None:
    for i in range(20):
        write(pair.a / f"f{i}.txt", b"data %d" % i, mtime=T0)
    pair.sync()
    for i in range(12):
        os.remove(pair.b / f"f{i}.txt")
    with pytest.raises(SafetyError, match="would remove 12 of 20"):
        pair.plan()
    plan, report = pair.sync(max_delete=0.9)
    assert plan.count(SyncOp.DELETE) == 12 and report.count("done") == 12


def test_unreadable_side_blocks_all_deletions(pair: Pair) -> None:
    for i in range(6):
        write(pair.a / f"f{i}.txt", b"data %d" % i, mtime=T0)
    pair.sync()
    os.remove(pair.b / "f0.txt")

    def pretend_unreadable(inventory):  # type: ignore[no-untyped-def]
        inventory.walk_stats[1].errors.append(("somewhere", "permission denied"))

    plan, _ = pair.plan(mutate=pretend_unreadable)
    assert plan.count(SyncOp.DELETE) == 0 and plan.warnings
    assert any("not fully readable" in s.reason for s in plan.skipped)


# ---------------------------------------------------------------- safety during the run
def test_file_changed_after_planning_is_left_for_next_time(pair: Pair) -> None:
    write(pair.a / "f.txt", b"v1", mtime=T0)
    pair.sync()
    write(pair.a / "f.txt", b"v2", mtime=T0 + 10)
    plan, state = pair.plan()
    write(pair.a / "f.txt", b"v3 - edited again while syncing", mtime=T0 + 20)
    report = run_sync(plan, pair.registry, state, dirs=pair.dirs)
    state.close()
    assert report.count("skipped") == 1 and tree(pair.b)["f.txt"] == b"v1"
    plan, _ = pair.sync()
    assert ops(plan) == {"f.txt": "update"} and tree(pair.b)["f.txt"] == b"v3 - edited again while syncing"


def test_failure_is_retried_next_time_and_history_stays_honest(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    write(pair.a / "ok.txt", b"fine", mtime=T0)
    write(pair.a / "bad.txt", b"will fail", mtime=T0)
    import duplio.backup.sync as sync_mod

    real = sync_mod.execute_action

    def flaky(action, env):  # type: ignore[no-untyped-def]
        if action.entry.rel == "bad.txt":
            raise OSError(28, "No space left on device")
        return real(action, env)

    monkeypatch.setattr(sync_mod, "execute_action", flaky)
    plan, report = pair.sync()
    assert report.count("done") == 1 and report.count("failed") == 1
    assert tree(pair.b) == {"ok.txt": b"fine"}
    monkeypatch.setattr(sync_mod, "execute_action", real)
    plan, report = pair.sync()
    assert ops(plan) == {"bad.txt": "copy"} and tree(pair.b) == tree(pair.a)


def test_never_overwrites_a_file_that_appears_meanwhile(pair: Pair) -> None:
    write(pair.a / "n.txt", b"from A", mtime=T0)
    plan, state = pair.plan()
    write(pair.b / "n.txt", b"someone else made this", mtime=T0 + 1)
    report = run_sync(plan, pair.registry, state, dirs=pair.dirs)
    state.close()
    assert report.count("failed") == 1
    assert tree(pair.b) == {"n.txt": b"someone else made this"}


def test_names_the_other_side_cannot_hold_are_left_alone(pair: Pair) -> None:
    write(pair.a / "ok.txt", b"ok", mtime=T0)
    write(pair.a / "bad: name?.txt", b"bad", mtime=T0)
    plan, _ = pair.sync(caps_b={"windows_names": True})
    assert ops(plan) == {"ok.txt": "copy"} and any("cannot store" in s.reason for s in plan.skipped)
    assert tree(pair.b) == {"ok.txt": b"ok"}


def test_names_differing_only_by_case_are_not_merged(pair: Pair) -> None:
    write(pair.a / "Photo.jpg", b"1", mtime=T0)
    write(pair.a / "photo.jpg", b"2", mtime=T0)
    plan, report = pair.sync(caps_b={"case_insensitive": True})
    assert any("collapse" in s.reason for s in plan.skipped) and not report.results
    assert tree(pair.b) == {}


def test_dst_shift_is_forgiven_without_reading_files(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    write(pair.a / "f.bin", b"x" * 100, mtime=T0)
    write(pair.b / "f.bin", b"x" * 100, mtime=T0 + 3600)  # same file, an hour off (FAT/NTFS vs Unix)
    reads: list[str] = []
    backend_type = type(pair.registry.local())
    real = backend_type.open_read

    def counting(self, path):  # type: ignore[no-untyped-def]
        reads.append(str(path))
        return real(self, path)

    monkeypatch.setattr(backend_type, "open_read", counting)
    plan, _ = pair.plan(ignore_dst=True)
    assert not plan.has_work and reads == []
    plan, _ = pair.plan(reset=True)  # without the option the content has to settle it
    assert not plan.has_work and reads  # identical content: still nothing to do, but the files were read


# ---------------------------------------------------------------- undo
def test_undo_reverses_the_whole_sync_and_resets_history(pair: Pair) -> None:
    write(pair.a / "keep.txt", b"k", mtime=T0)
    write(pair.b / "keep.txt", b"k", mtime=T0)
    write(pair.a / "gone.txt", b"g", mtime=T0)
    write(pair.b / "gone.txt", b"g", mtime=T0)
    write(pair.a / "edit.txt", b"v1", mtime=T0)
    write(pair.b / "edit.txt", b"v1", mtime=T0)
    pair.sync()
    os.remove(pair.b / "gone.txt")
    write(pair.a / "edit.txt", b"v2", mtime=T0 + 30)
    write(pair.a / "new.txt", b"n", mtime=T0 + 30)
    _, report = pair.sync()
    assert report.count("done") == 3
    result = undo(report.apply_id, pair.registry, dirs=pair.dirs)
    assert result.count("conflict") == 0 and result.count("failed") == 0
    assert tree(pair.a) == {"keep.txt": b"k", "gone.txt": b"g", "edit.txt": b"v2", "new.txt": b"n"}
    assert tree(pair.b) == {"keep.txt": b"k", "edit.txt": b"v1"}
    # history was reset: the next run is a first sync, so it re-copies instead of deleting
    assert not (pair.dirs.state / "sync" / f"{pair_id(str(pair.a), str(pair.b))}.sqlite3").exists()
    plan, _ = pair.plan()
    assert plan.first_run and plan.count(SyncOp.DELETE) == 0


def test_undo_of_a_conflict_restores_the_original_names(conflicted: Pair) -> None:
    _, report = conflicted.sync()
    result = undo(report.apply_id, conflicted.registry, dirs=conflicted.dirs)
    assert result.count("failed") == 0 and result.count("conflict") == 0
    assert tree(conflicted.a) == {"c.txt": b"A's version"}
    assert tree(conflicted.b) == {"c.txt": b"B's version is newer"}


# ---------------------------------------------------------------- network
def test_sync_with_an_smb_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirs: AppDirs) -> None:
    server = tmp_path / "nas"
    (server / "docs").mkdir(parents=True)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: FakeSMB(server))
    monkeypatch.setenv("DUPLIO_SMB_PASSWORD", "pw")
    local = tmp_path / "laptop"
    local.mkdir()
    write(local / "report.docx", rand_bytes(20_000, 1), mtime=T0)
    write(server / "docs" / "from-nas.txt", b"hello from the nas", mtime=T0 + 1)
    reg = BackendRegistry()
    try:
        pair = Pair(local, "smb://me@nas/docs", reg, dirs)
        plan, report = pair.sync()
        assert plan.roots[1].caps.windows_names and not report.failures
        assert tree(local) == tree(server / "docs")
        write(server / "docs" / "report.docx", rand_bytes(21_000, 2), mtime=T0 + 500)
        os.remove(local / "from-nas.txt")
        plan, report = pair.sync()
        assert ops(plan) == {"report.docx": "update", "from-nas.txt": "delete"} and not report.failures
        assert tree(local) == tree(server / "docs") and "from-nas.txt" not in tree(local)
        assert [p for p in (server / "docs").rglob("*") if ".duplio-tmp" in p.name] == []
        assert not pair.plan()[0].has_work
    finally:
        reg.close()


# ---------------------------------------------------------------- CLI
def invoke(*args: str, **kw):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False, **kw)


def test_cli_sync_flow(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    write(a / "one.txt", b"1", mtime=T0)
    write(b / "two.txt", b"2", mtime=T0)
    preview = invoke("sync", str(a), str(b), "--dry-run")
    assert preview.exit_code == 0 and "First sync" in preview.output and "Dry run" in preview.output
    assert tree(b) == {"two.txt": b"2"}
    assert invoke("sync", str(a), str(b)).exit_code == 0  # first run only adds: no confirmation needed
    assert tree(a) == tree(b) == {"one.txt": b"1", "two.txt": b"2"}
    os.remove(b / "one.txt")
    refused = invoke("sync", str(a), str(b))
    assert refused.exit_code == 2 and "--yes" in refused.output and (a / "one.txt").exists()
    done = invoke("sync", str(a), str(b), "--yes")
    assert done.exit_code == 0 and "Sync complete" in done.output and not (a / "one.txt").exists()
    assert "in sync" in invoke("sync", str(a), str(b)).output
    assert invoke("undo", "--yes").exit_code == 0 and (a / "one.txt").read_bytes() == b"1"


def test_cli_reset_forgets_history(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    write(a / "x.txt", b"x", mtime=T0)
    b.mkdir()
    invoke("sync", str(a), str(b))
    os.remove(b / "x.txt")
    result = invoke("sync", str(a), str(b), "--reset")
    assert result.exit_code == 0 and (b / "x.txt").read_bytes() == b"x"  # a first run adds, never deletes


def test_cli_validation(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    assert invoke("sync", str(a), str(b), "--basis", "size").exit_code == 2
    assert invoke("sync", str(a), str(b), "--time-tolerance", "soon").exit_code == 2
    assert invoke("sync", str(a), str(a / "x")).exit_code == 2
    with pytest.raises(ConfigError):
        SyncOptions(on_conflict="nope").validate()
