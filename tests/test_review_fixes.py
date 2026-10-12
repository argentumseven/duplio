"""Regression tests for problems found by an independent review of merge, sync, snapshots and repositories."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from duplio.backup.merge import MergeOptions, plan_merge, run_merge
from duplio.backup.repository import Repository
from duplio.backup.snapshots import SnapshotSet
from duplio.compare.diff import DiffOptions, diff_inventory
from duplio.errors import LockError, RepositoryError
from duplio.events import NullProgress
from duplio.hashing.core import READ_BUFFER
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0
PW = "correct horse battery"
FAST = 2**10


# ---------------------------------------------------------------- unreadable snapshot summaries
@pytest.mark.parametrize("password", [PW, None], ids=["encrypted", "plain"])
def test_prune_refuses_when_a_snapshot_summary_cannot_be_read(registry: BackendRegistry, tmp_path: Path,
                                                              password: str | None) -> None:
    repo = Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024, password=password, scrypt_n=FAST)
    write(tmp_path / "s1" / "a.bin", rand_bytes(200_000, 1), mtime=T0)
    write(tmp_path / "s2" / "b.bin", rand_bytes(200_000, 2), mtime=T0)
    first = repo.backup(registry.open(str(tmp_path / "s1")), label="one")
    repo.backup(registry.open(str(tmp_path / "s2")), label="two")
    summary = tmp_path / "repo" / "snapshots" / f"{first.snapshot.id}.json"
    damaged = bytearray(summary.read_bytes())
    damaged[-1] ^= 0xFF  # one flipped bit in the small summary file
    summary.write_bytes(bytes(damaged))
    repo = Repository.open(registry.open(str(tmp_path / "repo")), password=password)
    report = repo.check()
    assert not report.ok and len(report.unreadable_summaries) == 1  # was: "healthy"
    objects = sorted((tmp_path / "repo" / "objects").rglob("*"))
    with pytest.raises(RepositoryError, match="refusing to prune"):
        repo.prune()
    assert sorted((tmp_path / "repo" / "objects").rglob("*")) == objects  # nothing was deleted
    assert (tmp_path / "repo" / "trees" / f"{first.snapshot.id}.json.gz").exists()


def test_a_flaky_read_of_one_summary_cannot_make_prune_destroy_its_data(registry: BackendRegistry, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)
    write(tmp_path / "s1" / "a.bin", rand_bytes(200_000, 1), mtime=T0)
    write(tmp_path / "s2" / "b.bin", rand_bytes(200_000, 2), mtime=T0)
    first = repo.backup(registry.open(str(tmp_path / "s1")), label="one")
    repo.backup(registry.open(str(tmp_path / "s2")), label="two")
    real = LocalBackend.open_read

    def hiccup(self, path):  # type: ignore[no-untyped-def]
        if path.endswith(f"snapshots/{first.snapshot.id}.json"):
            raise OSError(errno.EIO, "Input/output error", path)
        return real(self, path)

    monkeypatch.setattr(LocalBackend, "open_read", hiccup)
    with pytest.raises(RepositoryError, match="refusing to prune"):
        repo.prune()
    monkeypatch.undo()
    restored = Repository.open(repo.loc).restore(first.snapshot.id, registry.open(str(tmp_path / "out")))
    assert restored.restored == 1 and not restored.errors


def test_a_summary_cannot_name_another_snapshot(registry: BackendRegistry, tmp_path: Path) -> None:
    import json

    repo = Repository.init(registry.open(str(tmp_path / "repo")))
    write(tmp_path / "s" / "a.bin", b"x" * 100, mtime=T0)
    done = repo.backup(registry.open(str(tmp_path / "s")))
    path = tmp_path / "repo" / "snapshots" / f"{done.snapshot.id}.json"
    data = json.loads(path.read_text())
    data["id"] = "../../elsewhere"
    path.write_text(json.dumps(data))
    assert [s.id for s in repo.snapshots()] == [done.snapshot.id]


# ---------------------------------------------------------------- overlapping snapshot runs
def test_a_second_snapshot_run_cannot_wipe_the_first_ones_work(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "snaps"
    for i in range(10):
        write(src / f"f{i:02d}.bin", rand_bytes(2000, i), mtime=T0)
    outcome: dict[str, object] = {"items": 0}

    class StartsAnotherRun(NullProgress):
        def advance(self, **kw):  # type: ignore[no-untyped-def]
            if kw.get("items"):
                outcome["items"] = int(outcome["items"]) + 1  # type: ignore[call-overload]
                if outcome["items"] == 5 and "second" not in outcome:
                    outcome["second"] = "started"
                    try:
                        SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_500)
                    except LockError as exc:
                        outcome["second"] = exc

    first = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_000,
                                                         progress=StartsAnotherRun())
    assert isinstance(outcome["second"], LockError)
    assert first.meta.files == 10 and len([p for p in (dest / first.name).rglob("*") if p.is_file()]) == 10
    assert not (dest / ".duplio-snapshots" / ".lock").exists()  # released afterwards


def test_forget_is_locked_too_and_checks_names(registry: BackendRegistry, tmp_path: Path) -> None:
    from duplio.errors import SafetyError

    write(tmp_path / "src" / "a.bin", b"x" * 100, mtime=T0)
    snaps = SnapshotSet(registry.open(str(tmp_path / "snaps")))
    done = snaps.create(registry.open(str(tmp_path / "src")), now=1_700_000_000)
    for bad in ("../src", "a/b", ".duplio-snapshots", "", ".."):
        with pytest.raises(SafetyError):
            snaps.forget(bad)
    assert (tmp_path / "src" / "a.bin").exists()
    snaps.forget(done.name)


def test_forget_uses_the_folder_name_not_the_id_inside_the_record(registry: BackendRegistry, tmp_path: Path) -> None:
    import json

    src, dest = tmp_path / "src", tmp_path / "snaps"
    write(src / "a.bin", rand_bytes(100, 1), mtime=T0)
    first = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_000)
    write(src / "a.bin", rand_bytes(100, 2), mtime=T0 + 10)
    SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_100)
    record = dest / ".duplio-snapshots" / f"{first.name}.json"
    data = json.loads(record.read_text())
    data["id"] = "../precious"
    record.write_text(json.dumps(data))
    write(tmp_path / "precious" / "thesis.doc", b"irreplaceable")
    from click.testing import CliRunner

    from duplio.cli.main import cli

    result = CliRunner(env={"COLUMNS": "170"}).invoke(
        cli, ["--no-color", "snapshot", "forget", str(dest), "--keep-last", "1", "--apply", "-y"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "precious" / "thesis.doc").exists()
    assert not (dest / first.name).exists()


# ---------------------------------------------------------------- source changing during a copy
def test_a_source_written_to_during_the_copy_is_not_reported_as_copied(registry: BackendRegistry, tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    size = READ_BUFFER * 3
    original = rand_bytes(size, 9)
    source = write(a / "db.sqlite", original, mtime=T0)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    diff = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    plan = plan_merge(diff, MergeOptions())
    fired = []

    class WriterAppears(NullProgress):
        def advance(self, **kw):  # type: ignore[no-untyped-def]
            if kw.get("nbytes") and not fired:
                fired.append(True)
                with open(source, "r+b") as handle:  # the application keeps writing to its file mid-copy
                    handle.seek(0)
                    handle.write(b"\xff" * 100)
                    handle.seek(size - 100)
                    handle.write(b"\xee" * 100)

    from duplio.util.appdirs import get_app_dirs

    report = run_merge(plan, registry, dirs=get_app_dirs().ensure(), progress=WriterAppears())
    assert [r.status for r in report.results] == ["skipped"] and "changed while" in report.results[0].message
    assert not (b / "db.sqlite").exists()  # no torn copy left behind under the real name
