"""Every place a run can be killed or fail: no data may be lost, and a rerun (or undo) must put things right.

Each test runs one operation, finds all of its durable steps (journal records, renames, writes, removals, state
commits), and replays the operation once per step with the process killed there (or an I/O error raised there).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.plan.executor import ApplyOptions, apply_plan
from duplio.plan.model import ActionType
from duplio.plan.planner import build_plan
from duplio.plan.undo import inspect_incomplete, undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write
from .crashsim import (  # noqa: F401 (pytestmark applies the fork skip)
    Outcome,
    World,
    files_under,
    pytestmark,
    sweep,
)

T0 = 1_600_000_000.0
HIDDEN = (".duplio-quarantine", ".duplio-versions", ".duplio-snapshots")


# --------------------------------------------------------------------------- dedupe (quarantine / delete / link)
DEDUPE_FILES = {
    "main/a.jpg": rand_bytes(70_000, 1),
    "main/dl/a (1).jpg": rand_bytes(70_000, 1),
    "main/b.txt": b"same text " * 40,
    "main/c.bin": rand_bytes(5_000, 3),
    "phone/a.jpg": rand_bytes(70_000, 1),
    "phone/b - Copy.txt": b"same text " * 40,
    "phone/d.bin": rand_bytes(6_000, 4),
}


def make_dedupe_world(root: Path) -> World:
    for rel, data in DEDUPE_FILES.items():
        write(root / rel, data, mtime=T0)
    return World(root)


def dedupe_run(world: World, action: ActionType) -> None:
    registry = BackendRegistry()
    roots = make_roots([(registry.open(str(world.root / "main")), RootRole.NORMAL),
                        (registry.open(str(world.root / "phone")), RootRole.NORMAL)])
    result = compare(scan(roots, ScanFilter(min_size=0)), ScanOptions(mode=Mode.ALL))
    plan = build_plan(result, action=action)
    apply_plan(plan, registry, dirs=world.dirs, options=ApplyOptions(confirm_delete=True))


def everything_held(world: World) -> set[str]:
    """Hashes of all content still on disk anywhere Duplio can bring it back from (folders and its quarantine)."""
    held = set(files_under(world.root / "main").values()) | set(files_under(world.root / "phone").values())
    held |= set(files_under(world.dirs.quarantine).values())
    return held


@pytest.mark.parametrize("action", [ActionType.QUARANTINE, ActionType.DELETE, ActionType.HARDLINK],
                         ids=["quarantine", "delete", "hardlink"])
@pytest.mark.parametrize("mode", ["crash", "error"])
def test_dedupe_never_loses_content_and_a_rerun_finishes(tmp_path: Path, action: ActionType, mode: str) -> None:
    original = {h for h in (__import__("hashlib").sha256(d).hexdigest() for d in DEDUPE_FILES.values())}

    def check(world: World, outcome: Outcome) -> None:
        assert everything_held(world) == original, "content went missing"
        live = {*files_under(world.root / "main").values(), *files_under(world.root / "phone").values()}
        # Anything under a final name is a complete original file, never a partial one.
        assert live <= original
        inspect_incomplete(BackendRegistry(), world.dirs)  # the journal can always be read, even with a torn last line

    def recover(world: World, outcome: Outcome) -> None:
        dedupe_run(world, action)
        assert everything_held(world) == original
        live = list(files_under(world.root / "main", skip=HIDDEN).values()) + list(
            files_under(world.root / "phone", skip=HIDDEN).values())
        if action is not ActionType.HARDLINK:
            assert sorted(live) == sorted(original), "after the rerun each content has exactly one live copy"
        else:
            assert set(live) == original

    sweep(tmp_path, make_dedupe_world, lambda w: dedupe_run(w, action), check, recover, mode=mode)


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_undo_after_an_interruption_restores_every_file(tmp_path: Path, mode: str) -> None:
    initial = {rel: __import__("hashlib").sha256(d).hexdigest() for rel, d in DEDUPE_FILES.items()}

    def check(world: World, outcome: Outcome) -> None:
        pass

    def recover(world: World, outcome: Outcome) -> None:
        try:
            undo(None, BackendRegistry(), dirs=world.dirs)
        except Exception as exc:
            if "no journal" in str(exc).lower() or "nothing" in str(exc).lower():
                pass  # killed before the first journal record: nothing was started
            else:
                raise
        now = {f"{side}/{rel}": h for side in ("main", "phone") for rel, h in files_under(world.root / side, skip=HIDDEN).items()}
        assert now == initial, f"differs from the start: {sorted(set(now.items()) ^ set(initial.items()))}"

    sweep(tmp_path, make_dedupe_world, lambda w: dedupe_run(w, ActionType.QUARANTINE), check, recover, mode=mode)


# --------------------------------------------------------------------------- merge (copy, replace, move)
MERGE_A = {
    "new1.bin": rand_bytes(60_000, 21),
    "docs/new2.bin": rand_bytes(9_000, 22),
    "pics/IMG_1.jpg": rand_bytes(30_000, 23),
    "conf.txt": b"conf from A " * 20,
}
MERGE_B = {
    "2019/holiday.jpg": rand_bytes(30_000, 23),
    "conf.txt": b"conf from B " * 25,
    "keep.txt": b"keep me " * 10,
}


def make_merge_world(root: Path) -> World:
    for rel, data in MERGE_A.items():
        write(root / "A" / rel, data, mtime=T0)
    for rel, data in MERGE_B.items():
        write(root / "B" / rel, data, mtime=T0)
    return World(root)


def merge_run(world: World, **opts: object) -> None:
    from duplio.backup.merge import MergeOptions, plan_merge, run_merge
    from duplio.compare.diff import DiffOptions, diff_inventory

    registry = BackendRegistry()
    roots = make_roots([(registry.open(str(world.root / "A")), RootRole.NORMAL),
                        (registry.open(str(world.root / "B")), RootRole.NORMAL)])
    diff = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    plan = plan_merge(diff, MergeOptions(jobs=1, **opts))  # type: ignore[arg-type]
    run_merge(plan, registry, dirs=world.dirs)


def hashes(data: dict[str, bytes]) -> set[str]:
    import hashlib

    return {hashlib.sha256(d).hexdigest() for d in data.values()}


def merge_held(world: World) -> set[str]:
    """All content on disk in either folder or in the places Duplio keeps replaced and removed files."""
    return set(files_under(world.root / "A").values()) | set(files_under(world.root / "B").values()) | set(
        files_under(world.root / "A", debris=False).values()) | set(files_under(world.dirs.quarantine).values())


def visible(world: World, side: str) -> dict[str, str]:
    return files_under(world.root / side, skip=HIDDEN)


MERGE_VARIANTS = {
    "copy": {},
    "replace": {"on_conflict": "replace"},
    "move": {"move": True},
}


@pytest.mark.parametrize("variant", list(MERGE_VARIANTS))
@pytest.mark.parametrize("mode", ["crash", "error"])
def test_merge_never_loses_content_and_a_rerun_finishes(tmp_path: Path, variant: str, mode: str) -> None:
    opts = MERGE_VARIANTS[variant]
    original = hashes(MERGE_A) | hashes(MERGE_B)

    def check(world: World, outcome: Outcome) -> None:
        assert merge_held(world) >= original, "content went missing"
        for side in ("A", "B"):
            assert set(visible(world, side).values()) <= original, f"a torn or foreign file under a final name in {side}"
        inspect_incomplete(BackendRegistry(), world.dirs)

    def recover(world: World, outcome: Outcome) -> None:
        merge_run(world, **opts)
        assert merge_held(world) >= original
        assert hashes(MERGE_A) <= set(visible(world, "B").values()) | set(
            files_under(world.root / "B", skip=("x",)).values()), "everything from A must now be in B"

    sweep(tmp_path, make_merge_world, lambda w: merge_run(w, **opts), check, recover, mode=mode)


@pytest.mark.parametrize("variant", list(MERGE_VARIANTS))
@pytest.mark.parametrize("mode", ["crash", "error"])
def test_undo_after_an_interrupted_merge_restores_both_folders(tmp_path: Path, variant: str, mode: str) -> None:
    opts = MERGE_VARIANTS[variant]
    start = {side: {rel: h for rel, h in visible(make_merge_world(tmp_path / f"ref-{side}"), side).items()}
             for side in ("A", "B")}

    def check(world: World, outcome: Outcome) -> None:
        pass

    def recover(world: World, outcome: Outcome) -> None:
        try:
            undo(None, BackendRegistry(), dirs=world.dirs)
        except Exception as exc:
            if "no applied plans" not in str(exc):
                raise
        for side in ("A", "B"):
            assert visible(world, side) == start[side], f"{side} differs from the start after undo"

    sweep(tmp_path, make_merge_world, lambda w: merge_run(w, **opts), check, recover, mode=mode)


# --------------------------------------------------------------------------- sync
SYNC_START = {
    "shared.txt": b"shared " * 30,
    "edit.bin": rand_bytes(20_000, 31),
    "delme.txt": b"to be deleted " * 12,
    "conf.txt": b"conf start " * 20,
    "docs/keep.txt": b"keep " * 50,
}


def sync_run(world: World) -> None:
    from duplio.backup.sync import SyncOptions, plan_sync, run_sync
    from duplio.backup.syncstate import SyncState

    registry = BackendRegistry()
    roots = make_roots([(registry.open(str(world.root / "A")), RootRole.NORMAL),
                        (registry.open(str(world.root / "B")), RootRole.NORMAL)])
    inventory = scan(roots, ScanFilter(min_size=0))
    state = SyncState.open(world.dirs, roots[0].location.uri, roots[1].location.uri)
    try:
        plan = plan_sync(inventory, state.load(), SyncOptions(jobs=1))
        run_sync(plan, registry, state, dirs=world.dirs)
    finally:
        state.close()


def make_sync_world(root: Path) -> World:
    for rel, data in SYNC_START.items():
        write(root / "A" / rel, data, mtime=T0)
        write(root / "B" / rel, data, mtime=T0)
    world = World(root)
    sync_run(world)  # the pair has a history, so deletions and edits mean something
    write(root / "A" / "edit.bin", rand_bytes(20_000, 32), mtime=T0 + 500)  # edited on A
    write(root / "A" / "a_only.txt", b"new on A " * 9, mtime=T0 + 600)
    (root / "B" / "delme.txt").unlink()  # deleted on B
    write(root / "B" / "b_only.bin", rand_bytes(7_000, 33), mtime=T0 + 700)
    write(root / "A" / "conf.txt", b"conf edited on A " * 11, mtime=T0 + 800)  # edited on both sides
    write(root / "B" / "conf.txt", b"conf edited on B " * 13, mtime=T0 + 900)
    return world


def sync_held(world: World) -> set[str]:
    return (set(files_under(world.root / "A").values()) | set(files_under(world.root / "B").values())
            | set(files_under(world.dirs.quarantine).values()))


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_sync_never_loses_content_and_a_rerun_converges(tmp_path: Path, mode: str) -> None:
    original = sync_held(make_sync_world(tmp_path / "ref"))  # everything that exists just before the sync starts

    def check(world: World, outcome: Outcome) -> None:
        assert sync_held(world) >= original, "content went missing"
        for side in ("A", "B"):
            assert set(visible(world, side).values()) <= original, f"a torn or foreign file under a final name in {side}"

    def recover(world: World, outcome: Outcome) -> None:
        sync_run(world)
        assert sync_held(world) >= original
        assert visible(world, "A") == visible(world, "B"), "the two sides still differ after the rerun"
        sync_run(world)  # and a further run has nothing left to do
        assert visible(world, "A") == visible(world, "B")

    sweep(tmp_path, make_sync_world, sync_run, check, recover, mode=mode)


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_undo_after_an_interrupted_sync_restores_both_folders(tmp_path: Path, mode: str) -> None:
    ref = make_sync_world(tmp_path / "ref")
    start = {side: visible(ref, side) for side in ("A", "B")}

    def check(world: World, outcome: Outcome) -> None:
        pass

    def recover(world: World, outcome: Outcome) -> None:
        try:
            undo(None, BackendRegistry(), dirs=world.dirs)
        except Exception as exc:
            if "no applied plans" not in str(exc):
                raise
        # (the journal of the setup sync is older; undo takes the newest one, i.e. the interrupted sync)
        for side in ("A", "B"):
            assert visible(world, side) == start[side], f"{side} differs from the start after undo"

    sweep(tmp_path, make_sync_world, sync_run, check, recover, mode=mode)


# --------------------------------------------------------------------------- repository: backup, forget, prune
from duplio.backup.repository import Repository  # noqa: E402
from duplio.backup.snapshots import SnapshotSet  # noqa: E402

REPO_V0 = {
    "big.bin": rand_bytes(150_000, 41),  # several chunks
    "small.txt": b"small " * 40,
    "docs/edit.txt": b"edit v0 " * 30,
    "docs/gone.txt": b"will be removed " * 20,
}


def make_repo_world(root: Path, *, snapshots: int = 1) -> World:
    world = World(root)
    for rel, data in REPO_V0.items():
        write(root / "src" / rel, data, mtime=T0)
    repo = Repository.init(BackendRegistry().open(str(root / "repo")), chunk_size=64 * 1024)
    repo.backup(BackendRegistry().open(str(root / "src")))
    for n in range(1, snapshots):
        write(root / "src" / f"extra{n}.bin", rand_bytes(90_000, 50 + n), mtime=T0 + n)
        repo.backup(BackendRegistry().open(str(root / "src")))
    return world


def mutate_source(root: Path) -> None:
    write(root / "src" / "docs" / "edit.txt", b"edit v1 " * 33, mtime=T0 + 1000)
    write(root / "src" / "new.bin", rand_bytes(100_000, 42), mtime=T0 + 1100)
    (root / "src" / "docs" / "gone.txt").unlink()


def open_repo(world: World) -> Repository:
    return Repository.open(BackendRegistry().open(str(world.root / "repo")))


def restores_to(repo: Repository, snapshot_id: str, expected: dict[str, str], where: Path) -> bool:
    report = repo.restore(snapshot_id, BackendRegistry().open(str(where)))
    return not report.errors and files_under(where) == expected


def repo_backup(world: World) -> None:
    open_repo(world).backup(BackendRegistry().open(str(world.root / "src")))


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_backup_interrupted_anywhere_leaves_a_sound_repository(tmp_path: Path, mode: str) -> None:
    def make(root: Path) -> World:
        world = make_repo_world(root)
        mutate_source(root)
        return world

    before = files_under(make_repo_world(tmp_path / "ref0").root / "src")
    ref = make(tmp_path / "ref1")
    after = files_under(ref.root / "src")

    def check(world: World, outcome: Outcome) -> None:
        repo = open_repo(world)
        report = repo.check(read_data=True)
        assert report.ok, f"the repository is damaged: {report}"
        snaps = repo.snapshots()
        assert snaps, "the earlier snapshot disappeared"
        assert restores_to(repo, snaps[0].id, before, world.root / "out0"), "the earlier snapshot no longer restores"
        for extra in snaps[1:]:
            # A visible snapshot is complete, or says that it is not: files that could not be stored are left out and
            # counted in its error count, and everything it does hold is correct.
            restored = world.root / f"out-{extra.id}"
            ok = repo.restore(extra.id, BackendRegistry().open(str(restored)))
            assert not ok.errors, "a published snapshot cannot be restored"
            got = files_under(restored)
            if extra.errors == 0:
                assert got == after, "a snapshot without recorded problems is incomplete"
            else:
                assert got.items() <= after.items(), "a damaged snapshot holds wrong data"

    def recover(world: World, outcome: Outcome) -> None:
        repo_backup(world)  # a lock left by the killed process must not get in the way
        repo = open_repo(world)
        latest = repo.snapshots()[-1]
        assert restores_to(repo, latest.id, after, world.root / "out-latest")
        repo.prune(grace_s=0, min_age_s=0)  # collecting garbage after a crash must not touch live data
        report = open_repo(world).check(read_data=True)
        assert report.ok, f"prune after the crash broke the repository: {report}"
        assert restores_to(open_repo(world), latest.id, after, world.root / "out-latest2")

    sweep(tmp_path, make, repo_backup, check, recover, mode=mode)


def forget_and_prune(world: World) -> None:
    repo = open_repo(world)
    first = repo.snapshots()[0]
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    repo.prune(grace_s=3600, min_age_s=0)


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_forget_and_prune_interrupted_anywhere_keeps_the_other_snapshots(tmp_path: Path, mode: str) -> None:
    ref = make_repo_world(tmp_path / "ref", snapshots=3)
    latest_files = files_under(ref.root / "src")
    second_files = {rel: h for rel, h in latest_files.items() if not rel.startswith("extra2")}

    def check(world: World, outcome: Outcome) -> None:
        repo = open_repo(world)
        ids = [s.id for s in repo.snapshots()]
        assert len(ids) in (2, 3), f"snapshots left: {len(ids)}"  # the forgotten one is there, or entirely gone
        second, latest = ids[-2], ids[-1]
        assert restores_to(repo, latest, latest_files, world.root / "o1"), "the latest snapshot no longer restores"
        assert restores_to(repo, second, second_files, world.root / "o2"), "the second snapshot no longer restores"
        assert repo.check().ok

    def recover(world: World, outcome: Outcome) -> None:
        repo = open_repo(world)
        if len(repo.snapshots()) == 3:
            with repo.lock("forget"):
                repo.delete_snapshot(repo.snapshots()[0].id)
        repo.prune(grace_s=3600, min_age_s=0)
        repo = open_repo(world)
        assert repo.check(read_data=True).ok
        assert len(repo.snapshots()) == 2
        assert restores_to(repo, repo.snapshots()[-1].id, latest_files, world.root / "o3")

    sweep(tmp_path, lambda r: make_repo_world(r, snapshots=3), forget_and_prune, check, recover, mode=mode)


# --------------------------------------------------------------------------- hard-link snapshots
def snapshot_run(world: World) -> None:
    SnapshotSet(BackendRegistry().open(str(world.root / "snaps"))).create(
        BackendRegistry().open(str(world.root / "src")), now=T0 + 2000)


def make_snapshot_world(root: Path) -> World:
    world = World(root)
    for rel, data in REPO_V0.items():
        write(root / "src" / rel, data, mtime=T0)
    SnapshotSet(BackendRegistry().open(str(root / "snaps"))).create(BackendRegistry().open(str(root / "src")), now=T0 + 1000)
    mutate_source(root)
    return world


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_hard_link_snapshots_interrupted_anywhere_never_show_a_partial_snapshot(tmp_path: Path, mode: str) -> None:
    ref = make_snapshot_world(tmp_path / "ref")
    after = files_under(ref.root / "src")
    first_name, first_files = next((n, files_under(ref.root / "snaps" / n)) for n, _ in
                                   SnapshotSet(BackendRegistry().open(str(ref.root / "snaps"))).snapshots())

    def check(world: World, outcome: Outcome) -> None:
        sets = SnapshotSet(BackendRegistry().open(str(world.root / "snaps")))
        listed = sets.snapshots()
        assert listed and listed[0][0] == first_name
        assert files_under(world.root / "snaps" / first_name) == first_files, "the first snapshot changed"
        for name, meta in listed[1:]:
            got = files_under(world.root / "snaps" / name)
            if meta.errors == 0:
                assert got == after, f"snapshot {name} is listed without problems but is incomplete"
            else:
                assert got.items() <= after.items(), f"snapshot {name} holds wrong data"

    def recover(world: World, outcome: Outcome) -> None:
        sets = SnapshotSet(BackendRegistry().open(str(world.root / "snaps")))
        report = sets.create(BackendRegistry().open(str(world.root / "src")), now=T0 + 3000)
        assert files_under(world.root / "snaps" / report.name) == after
        assert files_under(world.root / "snaps" / first_name) == first_files
        assert not [p for p in (world.root / "snaps").iterdir() if p.name.startswith(".incomplete-")]
        sets.forget(first_name)
        assert files_under(world.root / "snaps" / report.name) == after  # forgetting one never touches another

    sweep(tmp_path, make_snapshot_world, snapshot_run, check, recover, mode=mode)


# --------------------------------------------------------------------------- ingest with "free the phone"
INGEST_SRC = {
    "DCIM/IMG_1.jpg": rand_bytes(50_000, 61),
    "DCIM/IMG_2.jpg": rand_bytes(12_000, 62),
    "DCIM/IMG_3.jpg": rand_bytes(30_000, 63),  # already in the library under another name
    "DCIM/clip.mp4": rand_bytes(80_000, 64),
}


def make_ingest_world(root: Path) -> World:
    for rel, data in INGEST_SRC.items():
        write(root / "phone" / rel, data, mtime=T0)
    write(root / "library" / "2019" / "older.jpg", INGEST_SRC["DCIM/IMG_3.jpg"], mtime=T0)
    return World(root)


def ingest_run(world: World) -> None:
    from duplio.backup.ingest import IngestOptions, ingest

    registry = BackendRegistry()
    ingest(registry.open(str(world.root / "phone")), registry.open(str(world.root / "library")), dirs=world.dirs,
           options=IngestOptions(free_source=True), confirm_free=True)


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_ingest_that_frees_the_source_never_loses_a_photo(tmp_path: Path, mode: str) -> None:
    original = hashes({k: v for k, v in INGEST_SRC.items()})

    def held(world: World) -> set[str]:
        return set(files_under(world.root / "phone").values()) | set(files_under(world.root / "library").values())

    def check(world: World, outcome: Outcome) -> None:
        assert held(world) >= original, "a photo exists neither on the phone nor in the library"
        assert set(files_under(world.root / "library", skip=(".duplio",)).values()) <= original | hashes(
            {"x": INGEST_SRC["DCIM/IMG_3.jpg"]}), "a torn or foreign file under a final name in the library"

    def recover(world: World, outcome: Outcome) -> None:
        ingest_run(world)
        library = set(files_under(world.root / "library", skip=(".duplio",)).values())
        assert original <= library, "after the rerun every photo must be in the library"
        assert not files_under(world.root / "phone"), "and freed from the phone"

    sweep(tmp_path, make_ingest_world, ingest_run, check, recover, mode=mode)


# --------------------------------------------------------------------------- mirror (copy, update, move, retire)
MIRROR_SRC = {
    "keep.txt": b"same on both " * 20,
    "changed.bin": rand_bytes(25_000, 82),
    "new.bin": rand_bytes(15_000, 83),
    "renamed/now-here.dat": rand_bytes(9_000, 84),
}
MIRROR_DEST = {
    "keep.txt": b"same on both " * 20,
    "changed.bin": rand_bytes(25_000, 81),
    "old-place.dat": rand_bytes(9_000, 84),
    "retired.txt": b"only in the mirror " * 9,
}


def make_mirror_world(root: Path) -> World:
    for rel, data in MIRROR_SRC.items():
        write(root / "src" / rel, data, mtime=T0 + 5)
    for rel, data in MIRROR_DEST.items():
        write(root / "dest" / rel, data, mtime=T0)
    return World(root)


def mirror_run(world: World) -> None:
    from duplio.backup.mirror import MirrorOptions, plan_mirror, run_mirror

    registry = BackendRegistry()
    opts = MirrorOptions(delete_extraneous=True)
    plan = plan_mirror(registry.open(str(world.root / "src")), registry.open(str(world.root / "dest")), options=opts)
    run_mirror(plan, options=opts)


@pytest.mark.parametrize("mode", ["crash", "error"])
def test_mirror_interrupted_anywhere_loses_nothing_and_a_rerun_converges(tmp_path: Path, mode: str) -> None:
    original = hashes(MIRROR_SRC) | hashes(MIRROR_DEST)

    def held(world: World) -> set[str]:
        return set(files_under(world.root / "src").values()) | set(files_under(world.root / "dest").values())

    def check(world: World, outcome: Outcome) -> None:
        assert held(world) >= original, "content went missing"
        assert set(files_under(world.root / "dest", skip=HIDDEN).values()) <= original, "a torn file under a final name"

    def recover(world: World, outcome: Outcome) -> None:
        mirror_run(world)
        assert held(world) >= original
        assert files_under(world.root / "dest", skip=HIDDEN) == files_under(world.root / "src"), "not a mirror after the rerun"

    sweep(tmp_path, make_mirror_world, mirror_run, check, recover, mode=mode)


# --------------------------------------------------------------------------- the recovery tools themselves
def crash_at_first(name: str, tmp: Path, scenario, make_world) -> World:  # type: ignore[no-untyped-def]
    """Run ``scenario`` in a world, killed at the first crash point called ``name``."""
    import json

    from .crashsim import run_forked

    counting = make_world(tmp / "count")
    record = tmp / "points.json"
    assert run_forked(lambda: scenario(counting), tmp / "count.log", record=record) == 0
    point = json.loads(record.read_text()).index(name) + 1
    world = make_world(tmp / "crashed")
    from duplio.util.faults import CRASH_EXIT

    assert run_forked(lambda: scenario(world), tmp / "crash.log", crash_at=point) == CRASH_EXIT
    return world


def invoke_recovery(world: World, monkeypatch: pytest.MonkeyPatch, *args: str):  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from duplio.cli.main import cli

    monkeypatch.setenv("DUPLIO_HOME", str(world.root / "home"))
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def test_recover_settles_a_quarantine_that_happened_but_was_never_recorded(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    world = crash_at_first("fs.rename.after", tmp_path, lambda w: dedupe_run(w, ActionType.QUARANTINE),
                           make_dedupe_world)
    before = files_under(world.root / "main", skip=HIDDEN) | {
        "phone/" + k: v for k, v in files_under(world.root / "phone", skip=HIDDEN).items()}
    first = invoke_recovery(world, monkeypatch, "recover")
    assert first.exit_code == 0 and "completed" in first.output, first.output
    assert "No interrupted operations" in invoke_recovery(world, monkeypatch, "recover").output
    undone = invoke_recovery(world, monkeypatch, "undo", "--yes")
    assert undone.exit_code == 0
    restored = files_under(world.root / "main", skip=HIDDEN) | {
        "phone/" + k: v for k, v in files_under(world.root / "phone", skip=HIDDEN).items()}
    start = make_dedupe_world(tmp_path / "pristine")
    assert restored == files_under(start.root / "main") | {
        "phone/" + k: v for k, v in files_under(start.root / "phone").items()}, "undo did not bring everything back"
    assert len(restored) > len(before)


def test_recover_puts_back_a_file_that_a_delete_had_set_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = crash_at_first("fs.rename.after", tmp_path, lambda w: dedupe_run(w, ActionType.DELETE), make_dedupe_world)
    parked = [p for p in world.root.rglob(".duplio-del-*")]
    assert parked, "the crash should have left the file under its temporary name"
    result = invoke_recovery(world, monkeypatch, "recover")
    assert result.exit_code == 0 and "put back" in result.output, result.output
    assert not list(world.root.rglob(".duplio-del-*"))
    pristine = make_dedupe_world(tmp_path / "pristine")
    assert files_under(world.root / "main", skip=HIDDEN) == files_under(pristine.root / "main")
    assert files_under(world.root / "phone", skip=HIDDEN) == files_under(pristine.root / "phone")


def test_a_torn_last_line_does_not_swallow_the_records_written_after_it(tmp_path: Path) -> None:
    from duplio.plan.journal import Journal, read_journal

    path = tmp_path / "j.jsonl"
    with Journal(path, header={"apply_id": "x"}) as journal:
        seq = journal.intent(op="quarantine", src="/a")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "op", "event": "do')  # the process died part-way through this record
    with Journal(path) as journal:
        journal.done(seq, dst="/q/a")
    events = [r["event"] for r in read_journal(path) if r["type"] == "op"]
    assert events == ["intent", "done"]
