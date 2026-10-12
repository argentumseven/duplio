"""Garbage collection of a repository: references come from file lists, margins and a trash protect the rest."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import pytest

from duplio.backup.repository import Repository
from duplio.errors import RepositoryError
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0
PW = "correct horse battery"
FAST = 2**10
IMMEDIATE = {"grace_s": 0, "min_age_s": 0}


def make(registry: BackendRegistry, tmp_path: Path, password: str | None = None) -> Repository:
    return Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024, password=password,
                           scrypt_n=FAST)


def two_snapshots(registry: BackendRegistry, tmp_path: Path, repo: Repository):  # type: ignore[no-untyped-def]
    write(tmp_path / "s1" / "a.bin", rand_bytes(200_000, 1), mtime=T0)
    write(tmp_path / "s2" / "b.bin", rand_bytes(200_000, 2), mtime=T0)
    first = repo.backup(registry.open(str(tmp_path / "s1")), label="one").snapshot
    second = repo.backup(registry.open(str(tmp_path / "s2")), label="two").snapshot
    return first, second


def object_names(root: Path) -> set[str]:
    return {p.name for p in (root / "objects").rglob("*") if p.is_file()}


def trashed_names(root: Path) -> set[str]:
    return {p.name for p in (root / "trash").rglob("objects/*/*") if p.is_file()}


def age_everything(root: Path, seconds: float = 3 * 86400) -> None:
    old = time.time() - seconds
    for p in (root / "objects").rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))


def restore_ok(registry: BackendRegistry, repo: Repository, snap_id: str, tmp_path: Path, name: str) -> bool:
    report = Repository.open(repo.loc, password=PW if repo.encrypted else None).restore(
        snap_id, registry.open(str(tmp_path / name)))
    return report.restored >= 1 and not report.errors


# --------------------------------------------------------------------------- references come from file lists
@pytest.mark.parametrize("password", [PW, None], ids=["encrypted", "plain"])
def test_a_deleted_summary_does_not_make_its_data_look_unused(registry: BackendRegistry, tmp_path: Path,
                                                              password: str | None) -> None:
    repo = make(registry, tmp_path, password)
    first, _ = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    before = object_names(root)
    (root / "snapshots" / f"{first.id}.json").unlink()  # the summary vanishes; the file list stays
    age_everything(root)
    repo = Repository.open(repo.loc, password=password)
    report = repo.prune(grace_s=0, min_age_s=0)
    assert report.objects == 0 and report.trees_protected == [first.id]
    assert object_names(root) == before
    assert repo.check().orphan_trees == [first.id]
    # ...and `repair` brings the snapshot back with its original details.
    assert repo.repair() == [first.id]
    brought_back = {s.id: s for s in repo.snapshots()}[first.id]
    assert (brought_back.label, brought_back.source, brought_back.files) == ("one", first.source, 1)
    assert restore_ok(registry, repo, first.id, tmp_path, "out1")


def test_an_old_tree_without_a_summary_is_set_aside_not_deleted(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    (root / "snapshots" / f"{first.id}.json").unlink()
    age_everything(root)
    report = repo.prune(grace_s=86400, min_age_s=0, orphan_grace_s=0)
    assert report.trees_set_aside == [first.id] and report.objects > 0
    assert not (root / "trees" / f"{first.id}.json.gz").exists()
    # Nothing is gone for good: the trash brings the snapshot back, whole.
    chunks, snaps = repo.trash_restore()
    assert chunks == report.objects and snaps == 1
    assert restore_ok(registry, repo, first.id, tmp_path, "out1")
    assert repo.check(read_data=True).ok


def test_a_young_tree_without_a_summary_is_protected(registry: BackendRegistry, tmp_path: Path) -> None:
    # A backup that wrote its file list and has not published the summary yet (or lost its lock meanwhile).
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    (tmp_path / "repo" / "snapshots" / f"{first.id}.json").unlink()
    age_everything(tmp_path / "repo")
    report = repo.prune(**IMMEDIATE)
    assert report.objects == 0 and report.trees_protected == [first.id]


@pytest.mark.parametrize("password", [PW, None], ids=["encrypted", "plain"])
@pytest.mark.parametrize("target", ["tree", "summary"])
@pytest.mark.parametrize("damage", ["flip", "truncate", "empty", "delete"])
def test_damage_to_metadata_never_costs_data(registry: BackendRegistry, tmp_path: Path, password: str | None,
                                             target: str, damage: str) -> None:
    """Corruption-injection: whatever happens to one snapshot's small files, prune must either refuse or keep every
    chunk that any snapshot's file list still names."""
    repo = make(registry, tmp_path, password)
    first, second = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    victim = root / ("trees" if target == "tree" else "snapshots") / (
        f"{first.id}.json.gz" if target == "tree" else f"{first.id}.json")
    raw = victim.read_bytes()
    if damage == "flip":
        victim.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))
    elif damage == "truncate":
        victim.write_bytes(raw[: len(raw) // 2])
    elif damage == "empty":
        victim.write_bytes(b"")
    else:
        victim.unlink()
    age_everything(root)
    objects_before = object_names(root)
    repo = Repository.open(repo.loc, password=password)
    try:
        repo.prune(grace_s=0, min_age_s=0)
    except RepositoryError:
        pass  # refusing is the expected outcome; the point is that nothing was lost either way
    # Whatever happened to the small files, no chunk that any snapshot names may be gone.
    assert object_names(root) == objects_before
    assert restore_ok(registry, repo, second.id, tmp_path, "out2")


# --------------------------------------------------------------------------- unreadable inputs stop the run
def test_a_flaky_read_of_a_tree_cannot_make_prune_destroy_its_data(registry: BackendRegistry, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    age_everything(tmp_path / "repo")
    before = object_names(tmp_path / "repo")
    real = LocalBackend.open_read

    def hiccup(self, path):  # type: ignore[no-untyped-def]
        if path.endswith(f"trees/{first.id}.json.gz"):
            raise OSError(errno.EIO, "Input/output error", path)
        return real(self, path)

    monkeypatch.setattr(LocalBackend, "open_read", hiccup)
    with pytest.raises(RepositoryError, match="refusing to prune"):
        repo.prune(**IMMEDIATE)
    monkeypatch.undo()
    assert object_names(tmp_path / "repo") == before


# --------------------------------------------------------------------------- safety margin and deferred deletion
def test_prune_sets_data_aside_and_a_later_run_deletes_it(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    first, second = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    age_everything(root)
    freed_candidates = object_names(root) - {c for e in repo.tree(second.id) for c in e.chunks}
    report = repo.prune()
    assert report.objects == len(freed_candidates) > 0
    assert trashed_names(root) == freed_candidates
    assert not (freed_candidates & object_names(root))
    # A run straight afterwards does not delete what was just set aside.
    assert repo.prune().purged_objects == 0 and trashed_names(root) == freed_candidates
    # Once the batch is older than the grace period, the next run deletes it for good.
    (batch,) = (root / "trash").iterdir()
    batch.rename(root / "trash" / "20200101T000000000Z")
    final = repo.prune()
    assert final.purged_objects == len(freed_candidates)
    assert not (root / "trash" / "20200101T000000000Z").exists()


def test_recent_data_is_never_touched(registry: BackendRegistry, tmp_path: Path) -> None:
    # Models another machine's backup that is still writing (its lock may even have been lost).
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    before = object_names(tmp_path / "repo")
    report = repo.prune(grace_s=0)  # margin left at its default
    assert report.objects == 0 and report.too_young > 0
    assert object_names(tmp_path / "repo") == before


def test_dry_run_changes_nothing(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    age_everything(tmp_path / "repo")
    before = sorted(p.relative_to(tmp_path) for p in (tmp_path / "repo").rglob("*"))
    report = repo.prune(dry_run=True)
    assert report.objects > 0
    assert sorted(p.relative_to(tmp_path) for p in (tmp_path / "repo").rglob("*")) == before


def test_check_points_at_the_trash_when_chunks_are_missing(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    _, second = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    needed = sorted({c for e in repo.tree(second.id) for c in e.chunks})
    age_everything(root)
    # Simulate the worst case: a publish that raced a prune, leaving a snapshot whose chunk was set aside.
    victim = root / "objects" / needed[0][:2] / needed[0]
    dest = root / "trash" / "20260101T000000000Z" / "objects" / needed[0][:2]
    dest.mkdir(parents=True)
    victim.rename(dest / needed[0])
    report = Repository.open(repo.loc).check()
    assert not report.ok and report.in_trash[second.id] == 1
    repo.trash_restore()
    assert Repository.open(repo.loc).check(read_data=True).ok


def test_trash_purge_deletes_for_good(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    age_everything(tmp_path / "repo")
    repo.prune(min_age_s=0)
    assert repo.trash() and trashed_names(tmp_path / "repo")
    files, _ = repo.trash_purge(dry_run=True)
    assert files > 0 and trashed_names(tmp_path / "repo")
    repo.trash_purge()
    assert not repo.trash()


def test_a_file_list_made_before_summaries_were_embedded_can_still_be_adopted(registry: BackendRegistry,
                                                                              tmp_path: Path) -> None:
    import gzip
    import json

    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    root = tmp_path / "repo"
    tree_path = root / "trees" / f"{first.id}.json.gz"
    data = json.loads(gzip.decompress(tree_path.read_bytes()))
    del data["meta"]
    tree_path.write_bytes(gzip.compress(json.dumps(data).encode()))
    (root / "snapshots" / f"{first.id}.json").unlink()
    assert repo.repair() == [first.id]
    recovered = {s.id: s for s in repo.snapshots()}[first.id]
    assert "recovered" in recovered.tags and recovered.files == 1


def test_stats_show_what_is_waiting_in_the_trash(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    first, _ = two_snapshots(registry, tmp_path, repo)
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    age_everything(tmp_path / "repo")
    before = repo.stats()
    assert before["trash_bytes"] == 0
    report = repo.prune(min_age_s=0)
    after = repo.stats()
    assert after["trash_bytes"] == report.bytes > 0
    assert after["stored_bytes"] == before["stored_bytes"] - report.bytes
