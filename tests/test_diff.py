"""Folder comparison: classification, bases, edge cases, CLI and exports."""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli
from duplio.compare.diff import Basis, DiffOptions, DiffResult, Kind, diff_inventory
from duplio.errors import OverlappingRootsError
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write
from .fakes import FakeSMB

T0 = 1_600_000_000.0
HOUR = 3600.0


def run_diff(a: Path, b: Path, registry: BackendRegistry, *, caps_b=None, **opts) -> DiffResult:  # type: ignore[no-untyped-def]
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    if caps_b is not None:
        roots[1].caps = roots[1].caps.with_(**caps_b)
    inventory = scan(roots, ScanFilter(min_size=0))
    return diff_inventory(inventory, DiffOptions(**opts))


def kinds(result: DiffResult, side: int) -> dict[str, str]:
    return {i.entry.rel: i.kind.value for i in result.items if i.side == side}


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return a, b


def test_every_category(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    same, moved = rand_bytes(5000, 1), rand_bytes(7000, 2)
    write(a / "same.txt", same, mtime=T0)
    write(b / "same.txt", same, mtime=T0)
    write(a / "photos" / "a.jpg", moved, mtime=T0)
    write(b / "2024" / "renamed.jpg", moved, mtime=T0)
    write(a / "chg.txt", b"1" * 100, mtime=T0)
    write(b / "chg.txt", b"2" * 100, mtime=T0 + 1000)
    write(a / "onlya.bin", rand_bytes(900, 3), mtime=T0)
    write(b / "onlyb.bin", rand_bytes(123, 4), mtime=T0)
    write(a / "empty", b"")
    write(b / "empty", b"")
    r = run_diff(a, b, registry)
    assert kinds(r, 0) == {"same.txt": "same", "photos/a.jpg": "moved", "chg.txt": "changed", "onlya.bin": "only",
                           "empty": "same"}
    assert kinds(r, 1) == {"same.txt": "same", "2024/renamed.jpg": "moved", "chg.txt": "changed", "onlyb.bin": "only",
                           "empty": "same"}
    moved_a = next(i for i in r.items if i.side == 0 and i.kind is Kind.MOVED)
    assert [o.rel for o in moved_a.others] == ["2024/renamed.jpg"]
    changed_a = next(i for i in r.items if i.side == 0 and i.kind is Kind.CHANGED)
    assert changed_a.newer == -1  # B's copy is newer
    assert not r.identical_content and not r.identical_layout
    assert (r.count(Kind.ONLY, 0), r.size(Kind.ONLY, 0)) == (1, 900)


def test_same_size_different_content_is_changed_not_same(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", b"A" * 4096, mtime=T0)
    write(b / "f.bin", b"A" * 4095 + b"B", mtime=T0)  # same size, same time, differs in the last byte
    assert kinds(run_diff(a, b, registry), 0) == {"f.bin": "changed"}


def test_identical_layout_and_content(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    for root in (a, b):
        write(root / "x" / "one.bin", rand_bytes(3000, 1), mtime=T0)
        write(root / "two.bin", rand_bytes(2000, 2), mtime=T0)
    r = run_diff(a, b, registry)
    assert r.identical_layout and r.identical_content
    write(b / "x" / "one.bin", rand_bytes(3000, 1), mtime=T0)
    os.rename(b / "two.bin", b / "other-name.bin")
    r = run_diff(a, b, registry)
    assert r.identical_content and not r.identical_layout


def test_duplicates_inside_one_side_are_counted_not_confused(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(4000, 7)
    write(a / "one.bin", data, mtime=T0)
    write(a / "copy" / "two.bin", data, mtime=T0)
    write(b / "one.bin", data, mtime=T0)
    r = run_diff(a, b, registry)
    assert kinds(r, 0) == {"one.bin": "same", "copy/two.bin": "moved"}  # B lacks the second path
    assert r.duplicates_within(0) == (1, 4000)
    assert r.duplicates_within(1) == (0, 0)
    one = next(i for i in r.items if i.side == 0 and i.entry.rel == "one.bin")
    assert one.extra_copies == 1


def test_unicode_and_case_aware_paths(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(2500, 3)
    name = "Résumé.doc"
    write(a / unicodedata.normalize("NFC", name), data, mtime=T0)
    write(b / unicodedata.normalize("NFD", name), data, mtime=T0)
    assert kinds(run_diff(a, b, registry), 0) == {unicodedata.normalize("NFC", name): "same"}
    # a case-insensitive drive on one side: README.TXT and readme.txt are the same path
    write(a / "README.TXT", b"x" * 300, mtime=T0)
    write(b / "readme.txt", b"y" * 300, mtime=T0)
    assert kinds(run_diff(a, b, registry), 0)["README.TXT"] == "only"
    r = run_diff(a, b, registry, caps_b={"case_insensitive": True})
    assert kinds(r, 0)["README.TXT"] == "changed" and r.case_folded


def test_hard_link_between_sides_is_identical_without_reading(pair, registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "snap" / "f.bin", rand_bytes(6000, 5), mtime=T0)
    os.makedirs(b / "snap")
    os.link(a / "snap" / "f.bin", b / "snap" / "f.bin")
    os.link(a / "snap" / "f.bin", b / "elsewhere.bin")

    def no_reading(self, path):  # type: ignore[no-untyped-def]
        raise AssertionError("hard-linked files must not be read")

    monkeypatch.setattr(type(registry.local()), "open_read", no_reading)
    r = run_diff(a, b, registry)
    assert kinds(r, 0) == {"snap/f.bin": "same"}
    assert kinds(r, 1) == {"snap/f.bin": "same", "elsewhere.bin": "moved"}


def test_unreadable_file_is_reported_not_guessed(pair, registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(5000, 9)
    write(a / "locked.bin", data, mtime=T0)
    write(b / "locked.bin", data, mtime=T0)
    write(a / "fine.bin", rand_bytes(5000, 10), mtime=T0)
    write(b / "fine.bin", rand_bytes(5000, 10), mtime=T0)
    backend = registry.local()
    real = type(backend).open_read

    def flaky(self, path):  # type: ignore[no-untyped-def]
        if str(path).endswith(str(Path("A") / "locked.bin")):
            raise PermissionError(13, "denied")
        return real(self, path)

    monkeypatch.setattr(type(backend), "open_read", flaky)
    r = run_diff(a, b, registry, use_sampling=False)
    got = kinds(r, 0)
    assert got["locked.bin"] == "unreadable" and got["fine.bin"] == "same"
    assert kinds(r, 1)["locked.bin"] == "unreadable"  # the twin on the other side cannot be judged either
    assert r.errors


# ---------------------------------------------------------------- time / size bases
def test_time_size_basis(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(3000, 11)
    write(a / "same.bin", data, mtime=T0)
    write(b / "same.bin", data, mtime=T0 + 1.5)  # inside a 2 s tolerance
    write(a / "stamp.bin", data, mtime=T0)
    write(b / "stamp.bin", data, mtime=T0 + 60)  # identical content, newer timestamp
    write(a / "edit.bin", b"a" * 3000, mtime=T0)
    write(b / "edit.bin", b"b" * 3000, mtime=T0 + 60)
    write(a / "dst.bin", rand_bytes(100, 12), mtime=T0)
    write(b / "dst.bin", (a / "dst.bin").read_bytes(), mtime=T0 + HOUR)
    base = {"basis": Basis.TIME_SIZE}
    r = run_diff(a, b, registry, time_tolerance_ns=2_000_000_000, detect_moves=False, **base)
    assert kinds(r, 0) == {"same.bin": "same", "stamp.bin": "changed", "edit.bin": "changed", "dst.bin": "changed"}
    r = run_diff(a, b, registry, time_tolerance_ns=2_000_000_000, detect_moves=False, ignore_dst=True, **base)
    assert kinds(r, 0)["dst.bin"] == "same"
    # with move detection, files that differ only by timestamp are confirmed by content
    r = run_diff(a, b, registry, time_tolerance_ns=2_000_000_000, **base)
    got = kinds(r, 0)
    assert got["stamp.bin"] == "same" and got["edit.bin"] == "changed"
    stamp = next(i for i in r.items if i.side == 0 and i.entry.rel == "stamp.bin")
    assert "timestamp" in stamp.note


def test_time_size_never_reads_matching_files(pair, registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(3000, 13)
    write(a / "f.bin", data, mtime=T0)
    write(b / "f.bin", data, mtime=T0)

    def no_reading(self, path):  # type: ignore[no-untyped-def]
        raise AssertionError("no file should be opened")

    monkeypatch.setattr(type(registry.local()), "open_read", no_reading)
    r = run_diff(a, b, registry, basis=Basis.TIME_SIZE)
    assert kinds(r, 0) == {"f.bin": "same"}


def test_size_basis_ignores_time_and_content(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", b"1" * 500, mtime=T0)
    write(b / "f.bin", b"2" * 500, mtime=T0 + 99999)
    r = run_diff(a, b, registry, basis=Basis.SIZE)
    assert kinds(r, 0) == {"f.bin": "same"}
    assert next(iter(r.items)).note == "size only"


def test_moves_found_by_time_size_basis(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(8000, 14)
    write(a / "old" / "movie.mkv", data, mtime=T0)
    write(b / "new" / "movie.mkv", data, mtime=T0)
    write(a / "lonely.bin", rand_bytes(777, 15), mtime=T0)
    r = run_diff(a, b, registry, basis=Basis.TIME_SIZE)
    assert kinds(r, 0) == {"old/movie.mkv": "moved", "lonely.bin": "only"}
    r = run_diff(a, b, registry, basis=Basis.TIME_SIZE, detect_moves=False)
    assert kinds(r, 0) == {"old/movie.mkv": "only", "lonely.bin": "only"}


def test_overlapping_locations_are_refused(pair, registry) -> None:  # type: ignore[no-untyped-def]
    a, _ = pair
    (a / "sub").mkdir()
    with pytest.raises(OverlappingRootsError):
        make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(a / "sub")), RootRole.NORMAL)])


# ---------------------------------------------------------------- network drive
def test_diff_local_against_smb_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = tmp_path / "nas"
    (server / "photos").mkdir(parents=True)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: FakeSMB(server))
    monkeypatch.setenv("DUPLIO_SMB_PASSWORD", "pw")
    local = tmp_path / "local"
    data = rand_bytes(9000, 21)
    write(local / "IMG_1.jpg", data, mtime=T0)
    write(server / "photos" / "2024" / "holiday.jpg", data, mtime=T0 + 1)  # renamed + 1 s off (SMB whole seconds)
    write(local / "only-local.txt", b"x" * 50, mtime=T0)
    reg = BackendRegistry()
    try:
        roots = make_roots([(reg.open(str(local)), RootRole.NORMAL), (reg.open("smb://me@nas/photos"), RootRole.NORMAL)])
        res = diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions())
    finally:
        reg.close()
    assert kinds(res, 0) == {"IMG_1.jpg": "moved", "only-local.txt": "only"}
    assert kinds(res, 1) == {"2024/holiday.jpg": "moved"}


# ---------------------------------------------------------------- CLI
def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "160"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


@pytest.fixture
def populated(pair) -> tuple[Path, Path]:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(4000, 31)
    write(a / "keep.bin", data, mtime=T0)
    write(b / "keep.bin", data, mtime=T0)
    write(a / "moved" / "x.bin", rand_bytes(3000, 32), mtime=T0)
    write(b / "elsewhere.bin", rand_bytes(3000, 32), mtime=T0)
    write(a / "only a.txt", b"hello", mtime=T0)
    write(b / "only b.txt", b"world!", mtime=T0)
    return a, b


def test_cli_diff_report(populated) -> None:  # type: ignore[no-untyped-def]
    a, b = populated
    out = invoke("diff", str(a), str(b)).output
    for word in ("Identical", "Moved or renamed", "Only here", "only a.txt", "only b.txt", "elsewhere.bin", "duplio merge"):
        assert word in out, word


def test_cli_diff_check_exit_codes(populated, pair) -> None:  # type: ignore[no-untyped-def]
    a, b = populated
    assert invoke("diff", str(a), str(b), "--check", "--summary").exit_code == 1
    os.remove(a / "only a.txt")
    os.remove(b / "only b.txt")
    assert invoke("diff", str(a), str(b), "--check", "--summary").exit_code == 0  # moved file: same content
    assert invoke("diff", str(a), str(b), "--check", "--strict", "--summary").exit_code == 1
    os.rename(b / "elsewhere.bin", b / "x.bin")
    os.makedirs(b / "moved")
    os.rename(b / "x.bin", b / "moved" / "x.bin")
    assert invoke("diff", str(a), str(b), "--check", "--strict", "--summary").exit_code == 0


def test_cli_list_is_raw_and_nul_safe(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    raw = os.fsencode(a) + b"/odd\xff\nname.txt"
    with open(raw, "wb") as handle:
        handle.write(b"data")
    write(a / "plain.txt", b"data2")
    runner = CliRunner(env={"COLUMNS": "160"})
    result = runner.invoke(cli, ["--no-color", "diff", str(a), str(b), "--list", "only-a", "--print0"], catch_exceptions=False)
    assert result.exit_code == 0
    assert sorted(result.stdout_bytes.split(b"\0")[:-1]) == sorted([b"odd\xff\nname.txt", b"plain.txt"])
    assert result.stdout_bytes.endswith(b"\0")


def test_cli_exports(populated, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    a, b = populated
    for name in ("r.json", "r.csv", "r.html"):
        result = invoke("diff", str(a), str(b), "--summary", "--export", str(tmp_path / name))
        assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["format"] == "duplio-diff" and data["summary"]["only"]["A"]["files"] == 1
    assert {i["path"] for i in data["items"] if i["kind"] == "only"} == {"only a.txt", "only b.txt"}
    assert "only a.txt" in (tmp_path / "r.csv").read_text()
    html = (tmp_path / "r.html").read_text()
    assert "<script" not in html and "only a.txt" in html
    assert invoke("diff", str(a), str(b), "--summary", "--export", str(tmp_path / "r.txt")).exit_code == 2


def test_cli_json_and_errors(populated, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    a, b = populated
    data = json.loads(invoke("diff", str(a), str(b), "--json").output)
    assert data["identical_content"] is False
    bad = invoke("diff", str(a), str(b), "--time-tolerance", "soon")
    assert bad.exit_code == 2 and "not a duration" in bad.output
    missing = invoke("diff", str(a), str(tmp_path / "nope"))
    assert missing.exit_code == 2 and "does not exist" in missing.output
    nested = invoke("diff", str(a), str(a / "moved"))
    assert nested.exit_code == 2 and "overlap" in nested.output


def test_cli_diff_hostile_names_render(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    with open(os.fsencode(a) + b"/[red]bold\xff\x1b[31m.txt", "wb") as handle:
        handle.write(b"x" * 20)
    result = invoke("diff", str(a), str(b))
    assert result.exit_code == 0
    assert "\x1b" not in result.output and "[red]bold" in result.output
