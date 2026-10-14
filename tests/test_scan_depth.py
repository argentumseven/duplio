"""A root can be scanned for its top folder only; the sub-folders left out are remembered so the answer can say so."""

from __future__ import annotations

from pathlib import Path

from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import Location


def build(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "Day 1" / "inner").mkdir(parents=True)
    (root / "Day 2").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "a.txt").write_text("a")
    (root / "Day 1" / "b.txt").write_text("b")
    (root / "Day 1" / "inner" / "c.txt").write_text("c")
    (root / "node_modules" / "pkg" / "d.txt").write_text("d")
    return root


def scanned(root: Path, *, recursive: bool, **filter_args: object):  # type: ignore[no-untyped-def]
    roots = make_roots([(Location(LocalBackend(), str(root)), RootRole.NORMAL)])
    roots[0].recursive = recursive
    return scan(roots, ScanFilter(min_size=0, exclude=["node_modules", ".git"], **filter_args))  # type: ignore[arg-type]


def test_a_recursive_root_finds_everything_below_it(tmp_path: Path) -> None:
    inventory = scanned(build(tmp_path), recursive=True)
    assert {e.rel for e in inventory.entries} == {"a.txt", "Day 1/b.txt", "Day 1/inner/c.txt"}  # (node_modules is excluded here)
    assert inventory.unvisited == [[]]


def test_a_top_folder_only_root_finds_just_the_files_in_it_and_remembers_the_folders_it_did_not_enter(tmp_path: Path) -> None:
    inventory = scanned(build(tmp_path), recursive=False)
    assert {e.rel for e in inventory.entries} == {"a.txt"}
    assert sorted(inventory.unvisited[0]) == ["Day 1", "Day 2"]  # folders that are excluded anyway (node_modules, .git) are not "left out"


def test_hidden_folders_that_would_have_been_skipped_anyway_are_not_reported(tmp_path: Path) -> None:
    root = build(tmp_path)
    (root / ".hidden").mkdir()
    inventory = scanned(root, recursive=False, skip_hidden=True)
    assert ".hidden" not in inventory.unvisited[0]


def test_two_roots_can_differ(tmp_path: Path) -> None:
    one, two = build(tmp_path / "one"), build(tmp_path / "two")
    roots = make_roots([(Location(LocalBackend(), str(one)), RootRole.NORMAL), (Location(LocalBackend(), str(two)), RootRole.NORMAL)])
    roots[0].recursive = False
    inventory = scan(roots, ScanFilter(min_size=0))
    by_root = {r: {e.rel for e in inventory.entries if e.root == r} for r in (0, 1)}
    assert by_root[0] == {"a.txt"} and "Day 1/inner/c.txt" in by_root[1]
    assert inventory.unvisited[0] and not inventory.unvisited[1]
