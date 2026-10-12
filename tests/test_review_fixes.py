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


# ---------------------------------------------------------------- encryption downgrade
def test_removing_the_key_block_does_not_turn_backups_into_plaintext(registry: BackendRegistry, tmp_path: Path) -> None:
    import json

    repo = Repository.init(registry.open(str(tmp_path / "repo")), password=PW, scrypt_n=FAST)
    write(tmp_path / "s" / "a.bin", rand_bytes(5000, 1), mtime=T0)
    repo.backup(registry.open(str(tmp_path / "s")))
    config_path = tmp_path / "repo" / "duplio-repo.json"
    original = config_path.read_text()
    config = json.loads(original)
    del config["encryption"]
    config_path.write_text(json.dumps(config))  # the version and hash fields still say "encrypted"
    with pytest.raises(RepositoryError, match="no longer has the key block"):
        Repository.open(registry.open(str(tmp_path / "repo")))
    config.update(version=1, hash="blake2b-256")  # a careful attacker also edits those: the data gives it away
    config_path.write_text(json.dumps(config))
    with pytest.raises(RepositoryError, match="holds encrypted data"):
        Repository.open(registry.open(str(tmp_path / "repo")))
    config_path.write_text(original)
    assert Repository.open(registry.open(str(tmp_path / "repo")), password=PW).encrypted


def test_key_derivation_cost_is_capped_by_memory(registry: BackendRegistry, tmp_path: Path) -> None:
    import json

    Repository.init(registry.open(str(tmp_path / "repo")), password=PW, scrypt_n=FAST)
    config_path = tmp_path / "repo" / "duplio-repo.json"
    config = json.loads(config_path.read_text())
    config["encryption"]["kdf"].update(n=2**22, r=32)  # each field alone is allowed; together they ask for 16 GiB
    config_path.write_text(json.dumps(config))
    with pytest.raises(RepositoryError, match="unreasonable"):
        Repository.open(registry.open(str(tmp_path / "repo")), password=PW)


# ---------------------------------------------------------------- manifest names
def test_manifest_keeps_unicode_variants_apart(registry: BackendRegistry, tmp_path: Path) -> None:
    import unicodedata

    from duplio.backup.manifest import create_manifest, verify_manifest

    root = tmp_path / "archive"
    composed = unicodedata.normalize("NFC", "café.txt")
    decomposed = unicodedata.normalize("NFD", "café.txt")
    write(root / composed, b"one", mtime=T0)
    write(root / decomposed, b"two", mtime=T0)
    location = registry.open(str(root))
    manifest = create_manifest(location)
    assert len(manifest.entries) == 2
    (root / decomposed).unlink()
    report = verify_manifest(location, manifest)
    assert [(i.rel, i.status) for i in report.items if i.status == "missing"] == [(decomposed, "missing")]
    assert not report.clean


# ---------------------------------------------------------------- snapshot permissions
def test_snapshots_keep_permissions_and_notice_chmod(registry: BackendRegistry, tmp_path: Path) -> None:
    import os
    import stat

    src, dest = tmp_path / "src", tmp_path / "snaps"
    key = write(src / "id_rsa", b"PRIVATE" * 100, mtime=T0)
    script = write(src / "run.sh", b"#!/bin/sh\n", mtime=T0)
    os.chmod(key, 0o600)
    os.chmod(script, 0o755)

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    first = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_000)
    assert mode(dest / first.name / "id_rsa") == 0o400  # still private (never world-readable)
    assert mode(dest / first.name / "run.sh") == 0o555  # still executable, no longer writable
    unchanged = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_100)
    assert unchanged.linked == 2
    os.chmod(key, 0o644)  # permission-only change: no new mtime
    again = SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=1_700_000_200)
    assert (again.linked, again.copied) == (1, 1) and mode(dest / again.name / "id_rsa") == 0o444
    assert mode(dest / first.name / "id_rsa") == 0o400  # the older snapshot is untouched


# ---------------------------------------------------------------- sync decisions
def _pair(tmp_path: Path, registry: BackendRegistry, dirs):  # type: ignore[no-untyped-def]
    from .test_sync import Pair

    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return Pair(a, b, registry, dirs)


def test_a_file_outside_the_filters_on_one_side_is_not_a_deletion(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    from .test_sync import ops, tree

    pair = _pair(tmp_path, registry, dirs)
    for i in range(6):
        write(pair.a / f"f{i}.bin", rand_bytes(5000, 10 + i), mtime=T0)
    write(pair.a / "report.bin", rand_bytes(5000, 1), mtime=T0)
    pair.sync()
    write(pair.b / "report.bin", b"x" * 10, mtime=T0 + 100)  # truncated on B: now below --min-size
    plan, state = pair.plan(scan_filter=ScanFilter(min_size=1000))
    assert "report.bin" not in ops(plan)  # was: delete
    assert any("outside the filters" in s.reason for s in plan.skipped)
    from duplio.backup.sync import run_sync

    run_sync(plan, pair.registry, state, dirs=pair.dirs)
    state.close()
    assert "report.bin" in tree(pair.a) and len(tree(pair.b)["report.bin"]) == 10


def test_deletions_wait_when_a_copy_failed(tmp_path: Path, registry: BackendRegistry, dirs, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import duplio.backup.sync as sync_mod

    from .test_sync import tree

    pair = _pair(tmp_path, registry, dirs)
    write(pair.a / "x.bin", rand_bytes(5000, 1), mtime=T0)
    write(pair.a / "other.bin", rand_bytes(5000, 2), mtime=T0)
    pair.sync()
    (pair.a / "x.bin").rename(pair.a / "y.bin")  # renamed on A: B should get y.bin, then lose x.bin
    real = sync_mod.execute_action

    def disk_full(action, env):  # type: ignore[no-untyped-def]
        if action.entry.rel == "y.bin":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(action, env)

    monkeypatch.setattr(sync_mod, "execute_action", disk_full)
    _, report = pair.sync()
    assert report.count("failed") == 1
    assert "x.bin" in tree(pair.b)  # still live: the only copy of that content on B was not moved aside
    assert any("postponed" in r.message for r in report.results if r.status == "skipped")
    monkeypatch.setattr(sync_mod, "execute_action", real)
    pair.sync()
    assert "y.bin" in tree(pair.b) and "x.bin" not in tree(pair.b)


def test_ignore_dst_also_applies_to_each_sides_own_history(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    import os

    pair = _pair(tmp_path, registry, dirs)
    for i in range(3):
        write(pair.a / f"f{i}.bin", rand_bytes(3000, i), mtime=T0)
    pair.sync()
    for p in pair.b.iterdir():  # B's clock/time zone moved by exactly one hour
        os.utime(p, (T0 + 3600, T0 + 3600))
    assert pair.plan()[0].has_work
    plan, _ = pair.plan(ignore_dst=True)
    assert not plan.has_work


def test_sync_never_deletes_or_replaces_protected_paths(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    import os

    from .test_sync import ops, tree

    pair = _pair(tmp_path, registry, dirs)
    write(pair.a / "Library.photoslibrary" / "master.bin", rand_bytes(3000, 1), mtime=T0)
    write(pair.a / "plain.bin", rand_bytes(3000, 2), mtime=T0)
    for i in range(4):
        write(pair.a / f"pad{i}.bin", rand_bytes(3000, 20 + i), mtime=T0)
    pair.sync()
    os.remove(pair.b / "Library.photoslibrary" / "master.bin")
    os.remove(pair.b / "plain.bin")
    plan, _ = pair.sync()
    assert ops(plan) == {"plain.bin": "delete"}
    assert "Library.photoslibrary/master.bin" in tree(pair.a)  # protected: left alone
    assert any("protected" in s.reason for s in plan.skipped)


def test_reset_with_dry_run_keeps_the_history(tmp_path: Path) -> None:
    import os

    from click.testing import CliRunner

    from duplio.cli.main import cli

    def run(*args: str):  # type: ignore[no-untyped-def]
        return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)

    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    for i in range(5):
        write(a / f"f{i}.bin", rand_bytes(2000, i), mtime=T0)
    assert run("sync", str(a), str(b)).exit_code == 0
    preview = run("sync", str(a), str(b), "--reset", "--dry-run")
    assert preview.exit_code == 0 and "First sync" in preview.output
    os.remove(b / "f0.bin")
    result = run("sync", str(a), str(b), "--yes")  # the history survived: this is a deletion, not a "new" file
    assert result.exit_code == 0 and not (a / "f0.bin").exists()


# ---------------------------------------------------------------- diff and merge decisions
def _diff(a: Path, b: Path, registry: BackendRegistry):  # type: ignore[no-untyped-def]
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    return diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())


def test_a_hard_linked_pair_does_not_hide_identical_copies(tmp_path: Path, registry: BackendRegistry) -> None:
    import os

    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    data = rand_bytes(4000, 1)
    write(a / "x.bin", data, mtime=T0)
    os.link(a / "x.bin", b / "x.bin")  # one file reachable from both sides (a snapshot, a link-dest backup)
    write(a / "y.bin", data, mtime=T0)  # an independent copy of the same content
    kinds = {i.entry.rel: i.kind.value for i in _diff(a, b, registry).items if i.side == 0}
    assert kinds == {"x.bin": "same", "y.bin": "moved"}  # was: y is "only here" and would be copied again
    # and a same-path file identical to a hard-linked one is not "changed"
    c, d = tmp_path / "C", tmp_path / "D"
    d.mkdir()
    write(c / "other.bin", data, mtime=T0)
    os.link(c / "other.bin", d / "z.bin")
    write(c / "z.bin", data, mtime=T0)
    kinds = {i.entry.rel: i.kind.value for i in _diff(c, d, registry).items if i.side == 0}
    assert kinds["z.bin"] == "same"


def test_an_unreadable_file_of_the_same_size_blocks_only_here(tmp_path: Path, registry: BackendRegistry,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    data = rand_bytes(4000, 7)
    write(a / "photo.bin", data, mtime=T0)
    write(b / "elsewhere" / "p.bin", data, mtime=T0)  # might be the very same photo, but cannot be read
    real = LocalBackend.open_read

    def deny(self, path):  # type: ignore[no-untyped-def]
        if path.endswith("p.bin"):
            raise PermissionError(13, "denied", path)
        return real(self, path)

    monkeypatch.setattr(LocalBackend, "open_read", deny)
    diff = _diff(a, b, registry)
    mine = {i.entry.rel: i for i in diff.items if i.side == 0}
    assert mine["photo.bin"].kind.value == "unreadable"  # was: "only here", copied again
    plan = plan_merge(diff, MergeOptions())
    assert not plan.actions and plan.skipped_count("unreadable") == 1


def test_merge_never_replaces_a_protected_destination_file(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = tmp_path / "A", tmp_path / "B"
    write(a / "App.app" / "Contents" / "main.bin", b"new build", mtime=T0 + 100)
    write(b / "App.app" / "Contents" / "main.bin", b"installed build", mtime=T0)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    diff = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    plan = plan_merge(diff, MergeOptions(on_conflict="replace"))
    assert [x.kind for x in plan.actions] == ["rename"]
    run_merge(plan, registry, dirs=dirs)
    assert (b / "App.app" / "Contents" / "main.bin").read_bytes() == b"installed build"


# ---------------------------------------------------------------- crash diagnosis and stray copies
def test_recover_explains_a_crash_between_archiving_and_writing(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    from duplio.plan.journal import Journal, list_journals, summarize
    from duplio.plan.undo import inspect_incomplete

    a, b = tmp_path / "A", tmp_path / "B"
    write(a / "n.txt", b"new version", mtime=T0 + 50)
    write(b / "n.txt", b"old version", mtime=T0)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    plan = plan_merge(diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions()),
                      MergeOptions(on_conflict="replace"))
    report = run_merge(plan, registry, dirs=dirs)
    intent = summarize(list_journals(dirs.journals)[0]).ops[0].intent
    assert intent["archived"].endswith(f"{report.apply_id}/n.txt")  # the journal says where the old version goes
    # Now fake the moment of the crash: old version set aside, nothing written yet, no "done" record.
    archived = Path(intent["archived"])
    (b / "n.txt").rename(tmp_path / "gone-for-now")
    archived.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "gone-for-now").rename(archived)
    journal = Journal(dirs.journals / "99999999T000000000Z-crash.jsonl", header={"apply_id": "crash", "action": "merge"})
    journal.intent(op="merge-replace", src_root=intent["src_root"], src=intent["src"], dst_root=intent["dst_root"],
                   dst=intent["dst"], archived=str(archived), size=1, mtime_ns=1)
    journal.close()
    states = [state for _, op, state in inspect_incomplete(registry, dirs) if op.intent.get("archived")]
    assert states and "previous version was set aside" in states[0] and str(archived) in states[0]


def test_a_copy_whose_readback_failed_and_whose_cleanup_failed_is_not_left_behind(
        tmp_path: Path, registry: BackendRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    import duplio.transfer as transfer
    from duplio.transfer import copy_verified

    backend = registry.local()
    source = write(tmp_path / "src.bin", rand_bytes(5000, 4), mtime=T0)
    target = tmp_path / "dst.bin"
    real_hash, real_remove = transfer.hash_file, LocalBackend.remove
    calls = {"hash": 0, "remove": 0}

    def flaky_hash(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["hash"] += 1
        if calls["hash"] == 1:
            raise OSError(errno.ESTALE, "Stale file handle")  # the read-back drops
        return real_hash(*args, **kwargs)

    def flaky_remove(self, path):  # type: ignore[no-untyped-def]
        calls["remove"] += 1
        if calls["remove"] == 1:
            raise OSError(errno.EIO, "I/O error")  # and so does the cleanup that follows it
        return real_remove(self, path)

    monkeypatch.setattr(transfer, "hash_file", flaky_hash)
    monkeypatch.setattr(LocalBackend, "remove", flaky_remove)
    monkeypatch.setattr("duplio.vfs.retry.time.sleep", lambda s: None)
    digest = copy_verified((backend, str(source)), (backend, str(target)), algorithm="blake2b")
    assert digest and target.read_bytes() == source.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dst.bin", "src.bin"]  # no "dst (2).bin", no stray file


# ---------------------------------------------------------------- names
def test_a_backslash_in_a_linux_file_name_is_part_of_the_name(tmp_path: Path, registry: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    write(a / "report\\final.txt", b"hello", mtime=T0)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    diff = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    plan = plan_merge(diff, MergeOptions())
    assert [x.dest_rel for x in plan.actions] == ["report\\final.txt"]  # was: report/final.txt (a new folder)
    run_merge(plan, registry, dirs=dirs)
    assert (b / "report\\final.txt").read_bytes() == b"hello"
    # a layout typed with Windows separators still works
    from duplio.backup.merge import render_layout

    assert render_layout("{year}\\{name}", plan.actions[0].entry) == "2020/report\\final.txt"


def test_keep_both_names_respect_the_destination_rules(tmp_path: Path, registry: BackendRegistry) -> None:
    a, b = tmp_path / "phone: DCIM", tmp_path / "B"
    write(a / "IMG_1.jpg", b"from phone", mtime=T0 + 5)
    write(b / "IMG_1.jpg", b"already here", mtime=T0)
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    roots[1].caps = roots[1].caps.with_(windows_names=True)  # an SMB/exFAT destination
    plan = plan_merge(diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions()), MergeOptions())
    (action,) = plan.actions
    assert action.kind == "rename" and ":" not in action.dest_rel and action.dest_rel.startswith("IMG_1 (from phone_ DCIM ")


# ---------------------------------------------------------------- retention
def test_retention_keeps_the_newest_snapshot_without_errors() -> None:
    from duplio.backup.repository import SnapshotMeta
    from duplio.backup.retention import RetentionPolicy, apply_policy

    def snap(n: int, errors: int) -> SnapshotMeta:
        return SnapshotMeta(id=f"s{n}", time=f"2026-01-0{n}T00:00:00+00:00", host="h", source="/src", label="",
                            tags=[], parent=None, files=1, bytes=1, errors=errors)

    snaps = [snap(1, 0), snap(2, 0), snap(3, 4)]  # the newest is incomplete
    decision = apply_policy(snaps, RetentionPolicy(keep_last=1))
    assert {m.id for m, _ in decision.keep} == {"s3", "s2"}
    assert [m.id for m in decision.remove] == ["s1"]
