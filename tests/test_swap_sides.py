"""A comparison seen from the other side must say exactly what a fresh comparison of the swapped folders says."""

from __future__ import annotations

from pathlib import Path

from duplio.backup.merge import MergeOptions, plan_merge
from duplio.compare.diff import DiffOptions, diff_inventory, swap_sides
from duplio.hashing.cache import NullCache
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

OLD = 1_600_000_000


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "same.jpg", rand_bytes(5000, 1), mtime=OLD)
    write(b / "same.jpg", rand_bytes(5000, 1), mtime=OLD)
    write(a / "renamed-in-a.jpg", rand_bytes(4000, 2), mtime=OLD)
    write(b / "sub" / "renamed-in-b.jpg", rand_bytes(4000, 2), mtime=OLD)
    write(a / "only-a.jpg", rand_bytes(3000, 3), mtime=OLD)
    write(b / "only-b.jpg", rand_bytes(3500, 4), mtime=OLD)
    write(a / "changed.txt", rand_bytes(2000, 5), mtime=OLD)
    write(b / "changed.txt", rand_bytes(2000, 6), mtime=OLD + 100)
    write(a / "copy1.bin", rand_bytes(1500, 7), mtime=OLD)
    write(a / "copy2.bin", rand_bytes(1500, 7), mtime=OLD)
    write(b / "copy1.bin", rand_bytes(1500, 7), mtime=OLD)
    return a, b


def diff_of(first: Path, second: Path):  # type: ignore[no-untyped-def]
    with BackendRegistry() as reg:
        roots = make_roots([(reg.open(str(first)), RootRole.NORMAL), (reg.open(str(second)), RootRole.NORMAL)])
        return diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions(), cache=NullCache())


def summary(diff):  # type: ignore[no-untyped-def]
    return sorted((i.side, i.kind.value, i.entry.rel, tuple(sorted(o.rel for o in i.others)), i.newer) for i in diff.items)


def test_swapping_gives_the_same_answer_as_comparing_the_other_way_round(tmp_path: Path) -> None:
    a, b = make_tree(tmp_path)
    forward = diff_of(a, b)
    backward = diff_of(b, a)
    assert summary(swap_sides(forward)) == summary(backward)


def test_swapping_twice_gets_back_to_the_start(tmp_path: Path) -> None:
    a, b = make_tree(tmp_path)
    forward = diff_of(a, b)
    again = swap_sides(swap_sides(forward))
    assert summary(again) == summary(forward)
    assert [r.location.path for r in again.inventory.roots] == [str(a), str(b)]
    assert [r.index for r in again.inventory.roots] == [0, 1]


def test_the_merge_plan_from_a_swapped_comparison_matches_a_fresh_one(tmp_path: Path) -> None:
    a, b = make_tree(tmp_path)
    swapped_plan = plan_merge(swap_sides(diff_of(a, b)), MergeOptions())
    fresh_plan = plan_merge(diff_of(b, a), MergeOptions())

    def describe(plan):  # type: ignore[no-untyped-def]
        actions = sorted((x.kind, x.entry.rel, x.dest_rel) for x in plan.actions)
        skipped = sorted((s.reason, s.entry.rel) for s in plan.skipped)
        return actions, skipped, plan.src.location.path, plan.dest.location.path

    assert describe(swapped_plan) == describe(fresh_plan)
    assert swapped_plan.src.location.path == str(b) and swapped_plan.dest.location.path == str(a)
    assert {x.entry.rel for x in swapped_plan.actions} == {"only-b.jpg", "changed.txt"}


def test_the_original_comparison_is_not_changed_by_swapping(tmp_path: Path) -> None:
    a, b = make_tree(tmp_path)
    forward = diff_of(a, b)
    before = summary(forward)
    swap_sides(forward)
    assert summary(forward) == before
    assert all(e.root in (0, 1) for e in forward.inventory.entries)
