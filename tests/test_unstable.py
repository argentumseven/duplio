"""Files that change while they are backed up, and kept copies that change while a plan is applied."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.repository import Repository
from duplio.backup.retention import RetentionPolicy, apply_policy
from duplio.backup.snapshots import SnapshotSet
from duplio.cli.main import cli
from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.plan.executor import ApplyOptions, Executor, apply_plan
from duplio.plan.model import ActionType
from duplio.plan.planner import build_plan
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def make(registry: BackendRegistry, tmp_path: Path) -> Repository:
    return Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)


def append_while_read(monkeypatch: pytest.MonkeyPatch, victim: Path, *, times: int | None) -> list[int]:
    """Make ``victim`` grow each time the repository stores a chunk (``times`` times, or for ever)."""
    calls: list[int] = []
    real = Repository.put

    def put(self, data, *, compress):  # type: ignore[no-untyped-def]
        if times is None or len(calls) < times:
            calls.append(1)
            with victim.open("ab") as handle:
                handle.write(b"more data arriving")
        return real(self, data, compress=compress)

    monkeypatch.setattr(Repository, "put", put)
    return calls


# --------------------------------------------------------------------------- backups of a moving target
def test_a_file_that_settles_is_read_again_and_stored_cleanly(registry: BackendRegistry, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    log = write(tmp_path / "src" / "app.log", rand_bytes(200_000, 1), mtime=T0)
    append_while_read(monkeypatch, log, times=1)  # changes during the first read only
    report = repo.backup(registry.open(str(tmp_path / "src")))
    assert report.changed_during_backup == [] and report.snapshot.unstable == 0 and not report.snapshot.problems
    monkeypatch.undo()
    repo.restore("latest", registry.open(str(tmp_path / "out")))
    assert (tmp_path / "out" / "app.log").read_bytes() == log.read_bytes()  # the final content, not a torn mix


def test_a_file_that_never_holds_still_is_flagged_everywhere(registry: BackendRegistry, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    write(tmp_path / "src" / "stable.txt", b"quiet", mtime=T0)
    busy = write(tmp_path / "src" / "busy.db", rand_bytes(200_000, 2), mtime=T0)
    append_while_read(monkeypatch, busy, times=None)
    report = repo.backup(registry.open(str(tmp_path / "src")))
    monkeypatch.undo()
    assert report.changed_during_backup == ["busy.db"]
    assert report.snapshot.unstable == 1 and report.snapshot.problems == 1
    flagged = {e.path: e.unstable for e in repo.tree(report.snapshot.id)}
    assert flagged == {"busy.db": True, "stable.txt": False}
    # The summary on disk says so too, so every later reader sees it.
    assert {s.id: s.unstable for s in Repository.open(repo.loc).snapshots()}[report.snapshot.id] == 1
    # Restoring works, and says which files to distrust.
    restored = repo.restore("latest", registry.open(str(tmp_path / "out")))
    assert restored.restored == 2 and restored.unstable == ["busy.db"]


def test_the_command_exits_non_zero_and_names_the_files(registry: BackendRegistry, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    make(registry, tmp_path)
    busy = write(tmp_path / "src" / "busy.db", rand_bytes(200_000, 2), mtime=T0)
    append_while_read(monkeypatch, busy, times=None)
    result = invoke("backup", "run", str(tmp_path / "src"), str(tmp_path / "repo"))
    monkeypatch.undo()
    assert result.exit_code == 1
    assert "kept changing" in result.output and "busy.db" in result.output and "finished with problems" in result.output
    listing = invoke("backup", "snapshots", str(tmp_path / "repo"))
    assert "1 unstable" in listing.output
    restore = invoke("backup", "restore", str(tmp_path / "repo"), "latest", str(tmp_path / "out"))
    assert "internally inconsistent" in restore.output and "busy.db" in restore.output


def test_an_unstable_file_is_never_trusted_as_unchanged_next_time(registry: BackendRegistry, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    busy = write(tmp_path / "src" / "busy.db", rand_bytes(200_000, 2), mtime=T0)
    append_while_read(monkeypatch, busy, times=None)
    repo.backup(registry.open(str(tmp_path / "src")))
    monkeypatch.undo()
    # The file is quiet now. Size and time look just like the stored entry's only if it was not re-read; it must be.
    again = repo.backup(registry.open(str(tmp_path / "src")))
    assert again.snapshot.reused_files == 0 and again.snapshot.unstable == 0
    assert not {e.path: e.unstable for e in repo.tree(again.snapshot.id)}["busy.db"]
    restored = repo.restore("latest", registry.open(str(tmp_path / "out")))
    assert restored.unstable == [] and (tmp_path / "out" / "busy.db").read_bytes() == busy.read_bytes()


def test_retention_does_not_count_an_unstable_snapshot_as_the_good_one(registry: BackendRegistry,
                                                                       tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    busy = write(tmp_path / "src" / "busy.db", rand_bytes(100_000, 2), mtime=T0)
    clean = repo.backup(registry.open(str(tmp_path / "src"))).snapshot
    time.sleep(0.01)
    write(busy, rand_bytes(100_000, 3), mtime=T0 + 10)  # edited since, so it is read again...
    append_while_read(monkeypatch, busy, times=None)  # ...and keeps changing while it is read
    moving = repo.backup(registry.open(str(tmp_path / "src"))).snapshot
    monkeypatch.undo()
    decision = apply_policy(Repository.open(repo.loc).snapshots(), RetentionPolicy(keep_last=1))
    kept = {s.id: reasons for s, reasons in decision.keep}
    assert moving.id in kept and clean.id in kept  # the older clean one survives thinning
    assert any("without problems" in r for r in kept[clean.id])


def test_older_snapshots_without_the_new_fields_still_load(registry: BackendRegistry, tmp_path: Path) -> None:
    import gzip
    import json

    repo = make(registry, tmp_path)
    write(tmp_path / "src" / "a.txt", b"x" * 1000, mtime=T0)
    snap = repo.backup(registry.open(str(tmp_path / "src"))).snapshot
    path = tmp_path / "repo" / "trees" / f"{snap.id}.json.gz"
    data = json.loads(gzip.decompress(path.read_bytes()))
    data["entries"] = [row[:5] for row in data["entries"]]  # as written before change times were recorded
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    entry = Repository.open(repo.loc).tree(snap.id)[0]
    assert entry.ctime_ns is None and entry.ino is None and entry.unstable is False
    assert Repository.open(repo.loc).backup(registry.open(str(tmp_path / "src"))).snapshot.reused_files == 1


def test_hard_link_snapshot_flags_a_file_that_changed_while_copied(registry: BackendRegistry, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio import transfer
    from duplio.backup import snapshots as snapshots_module

    busy = write(tmp_path / "src" / "busy.db", rand_bytes(100_000, 2), mtime=T0)
    real = transfer.copy_verified

    def copy_then_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        out = real(*args, **kwargs)
        with busy.open("ab") as handle:
            handle.write(b"x")
        return out

    monkeypatch.setattr(snapshots_module, "copy_verified", copy_then_write)
    snaps = SnapshotSet(registry.open(str(tmp_path / "snaps")))
    report = snaps.create(registry.open(str(tmp_path / "src")))
    assert report.changed_during == ["busy.db"] and report.meta.unstable == 1
    assert snaps.snapshots()[0][1].problems == 1
    monkeypatch.undo()
    result = invoke("snapshot", "create", str(tmp_path / "src"), str(tmp_path / "snaps2"))
    assert result.exit_code == 0  # a quiet run is clean


# --------------------------------------------------------------------------- a kept copy that changes mid-run
def triple(registry: BackendRegistry, tmp_path: Path):  # type: ignore[no-untyped-def]
    data = rand_bytes(250_000, 11)
    for rel in ("main/IMG.jpg", "main/dl/IMG (1).jpg", "phone/IMG.jpg"):
        write(tmp_path / rel, data, mtime=T0)
    locs = [(registry.open(str(tmp_path / "main")), RootRole.NORMAL), (registry.open(str(tmp_path / "phone")), RootRole.NORMAL)]
    return compare(scan(make_roots(locs), ScanFilter()), ScanOptions(mode=Mode.ALL))


def silently_rewrite(path: Path) -> None:
    st = path.stat()
    path.write_bytes(rand_bytes(st.st_size, 99))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


@pytest.mark.parametrize("action", [ActionType.DELETE, ActionType.QUARANTINE])
def test_a_kept_copy_that_changes_during_the_run_stops_later_removals(registry: BackendRegistry, tmp_path: Path,
                                                                      dirs, monkeypatch: pytest.MonkeyPatch,
                                                                      action: ActionType) -> None:  # type: ignore[no-untyped-def]
    plan = build_plan(triple(registry, tmp_path), action=action)
    keeper = tmp_path / "main" / "IMG.jpg"
    assert {i.keeper.path for i in plan.items} == {str(keeper)} and len(plan.items) == 2
    acted: list[int] = []
    real = Executor._act

    def act_then_damage_keeper(self, *args):  # type: ignore[no-untyped-def]
        out = real(self, *args)
        if not acted:
            silently_rewrite(keeper)  # same size, same modification time; only the change time moves
        acted.append(1)
        return out

    monkeypatch.setattr(Executor, "_act", act_then_damage_keeper)
    report = apply_plan(plan, registry, dirs=dirs, options=ApplyOptions(confirm_delete=True))
    assert sorted(o.status for o in report.outcomes) == ["done", "failed"]
    survivors = [p for p in (tmp_path / "main" / "dl" / "IMG (1).jpg", tmp_path / "phone" / "IMG.jpg") if p.exists()]
    assert len(survivors) == 1 and survivors[0].read_bytes() == rand_bytes(250_000, 11)


def test_an_unchanged_kept_copy_is_hashed_once_for_reversible_steps_and_every_time_for_deletes(
        registry: BackendRegistry, tmp_path: Path, dirs, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    real = Executor._fresh_digest

    def count(action: ActionType) -> int:
        tmp = tmp_path / action.value
        tmp.mkdir()
        keeper_hashes: list[str] = []

        def spy(self, backend, path, algorithm, size):  # type: ignore[no-untyped-def]
            if path.endswith("main/IMG.jpg"):
                keeper_hashes.append(path)
            return real(self, backend, path, algorithm, size)

        monkeypatch.setattr(Executor, "_fresh_digest", spy)
        apply_plan(build_plan(triple(registry, tmp), action=action), registry, dirs=dirs,
                   options=ApplyOptions(confirm_delete=True))
        monkeypatch.undo()
        return len(keeper_hashes)

    assert count(ActionType.QUARANTINE) == 1  # unchanged, old enough to trust, undoable: reuse the verdict
    assert count(ActionType.DELETE) == 2  # irreversible: look again each time


# --------------------------------------------------------------------------- freeing a phone: the copy is read again
def test_ingest_rechecks_the_library_copy_right_before_deleting_the_original(registry: BackendRegistry, tmp_path: Path,
                                                                             dirs, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from duplio.backup import ingest as ingest_module
    from duplio.backup.ingest import IngestOptions, ingest

    data = rand_bytes(40_000, 71)
    write(tmp_path / "phone" / "DCIM" / "IMG_1.jpg", data, mtime=T0)
    real = ingest_module._free_source

    def damage_the_copy_first(source, library, verified, *args):  # type: ignore[no-untyped-def]
        # Hours later in a big run: the verified copy in the library has been damaged (same size, other content).
        for _raw, dest_rel, _digest in verified:
            target = tmp_path / "library" / dest_rel
            damaged = bytearray(target.read_bytes())
            damaged[100] ^= 0xFF
            target.write_bytes(bytes(damaged))
        return real(source, library, verified, *args)

    monkeypatch.setattr(ingest_module, "_free_source", damage_the_copy_first)
    report = ingest(registry.open(str(tmp_path / "phone")), registry.open(str(tmp_path / "library")), dirs=dirs,
                    options=IngestOptions(free_source=True), confirm_free=True)
    assert report.freed == 0 and (tmp_path / "phone" / "DCIM" / "IMG_1.jpg").read_bytes() == data
    assert any("no longer matches" in message for _, message in report.errors)
