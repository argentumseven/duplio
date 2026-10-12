"""Merging one folder into another: what is copied, what is not, and how it is undone."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.merge import MergeOptions, plan_merge, render_layout, run_merge
from duplio.cli.main import cli
from duplio.compare.diff import Basis, DiffOptions, diff_inventory
from duplio.errors import CancelledError, ConfigError, SafetyError
from duplio.events import CancelToken
from duplio.plan.journal import list_journals, summarize
from duplio.plan.undo import undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.appdirs import AppDirs
from duplio.vfs.guards import GuardError
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write
from .fakes import FakeSMB

T0 = 1_600_000_000.0


def make_plan(a: Path | str, b: Path | str, registry: BackendRegistry, *, dest_caps=None, diff_opts=None,  # type: ignore[no-untyped-def]
              **opts):
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    if dest_caps:
        roots[1].caps = roots[1].caps.with_(**dest_caps)
    inventory = scan(roots, ScanFilter(min_size=0))
    diff = diff_inventory(inventory, diff_opts or DiffOptions())
    return plan_merge(diff, MergeOptions(**opts))


def merge(a, b, registry, dirs: AppDirs, **opts):  # type: ignore[no-untyped-def]
    plan = make_plan(a, b, registry, **opts)
    return plan, run_merge(plan, registry, dirs=dirs)


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return a, b


def tree(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not rel.parts[0].startswith(".duplio"):
            out[rel.as_posix()] = p.read_bytes()
    return out


# ---------------------------------------------------------------- what is copied
def test_copies_only_what_is_missing(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    have, renamed = rand_bytes(4000, 1), rand_bytes(5000, 2)
    new1, new2 = rand_bytes(6000, 3), rand_bytes(300, 4)
    write(a / "have.bin", have, mtime=T0)
    write(b / "have.bin", have, mtime=T0)
    write(a / "pics" / "IMG_1.jpg", renamed, mtime=T0)
    write(b / "2019" / "holiday.jpg", renamed, mtime=T0)  # same photo, other name and folder
    write(a / "docs" / "new1.bin", new1, mtime=T0 + 5)
    write(a / "new2.bin", new2, mtime=T0 + 7)
    plan, report = merge(a, b, registry, dirs)
    assert sorted(x.dest_rel for x in plan.actions) == ["docs/new1.bin", "new2.bin"]
    assert report.count("done") == 2 and not report.failures
    assert tree(b) == {"have.bin": have, "2019/holiday.jpg": renamed, "docs/new1.bin": new1, "new2.bin": new2}
    assert (b / "docs" / "new1.bin").stat().st_mtime_ns == int((T0 + 5) * 1e9)  # times are carried over
    assert tree(a)["pics/IMG_1.jpg"] == renamed  # a copy-merge never touches the source
    # a second run finds nothing left to do
    again = make_plan(a, b, registry)
    assert not again.actions and not again.has_work


def test_identical_files_in_source_are_copied_once(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(3000, 5)
    write(a / "one.bin", data, mtime=T0)
    write(a / "x" / "two.bin", data, mtime=T0)
    plan, _ = merge(a, b, registry, dirs)
    assert [x.dest_rel for x in plan.actions] == ["one.bin"]
    assert plan.skipped_count("duplicate-in-source") == 1
    assert list(tree(b)) == ["one.bin"]
    other = b.parent / "B2"
    other.mkdir()
    plan = make_plan(a, other, registry, keep_dupes=True)
    assert sorted(x.dest_rel for x in plan.actions) == ["one.bin", "x/two.bin"]


def test_empty_files_and_unicode_names(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "empty.txt", b"")
    write(a / "Ünï cödé ✓" / "日本語.txt", b"text")
    _, report = merge(a, b, registry, dirs)
    assert not report.failures
    assert tree(b) == {"empty.txt": b"", "Ünï cödé ✓/日本語.txt": b"text"}


def test_non_utf8_names_survive(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    raw = os.fsencode(a) + b"/bad\xffname\n.bin"
    with open(raw, "wb") as handle:
        handle.write(b"payload")
    _, report = merge(a, b, registry, dirs)
    assert not report.failures and report.count("done") == 1
    assert (b / os.fsdecode(b"bad\xffname\n.bin")).read_bytes() == b"payload"


def test_many_files_in_parallel(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    for i in range(60):
        write(a / f"d{i % 5}" / f"f{i}.bin", rand_bytes(2000 + i, i), mtime=T0 + i)
    _, report = merge(a, b, registry, dirs, jobs=4)
    assert report.count("done") == 60 and not report.failures
    assert tree(a) == tree(b)


# ---------------------------------------------------------------- conflicts
@pytest.fixture
def conflict(pair) -> tuple[Path, Path]:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "notes.txt", b"source version", mtime=T0 + 100)
    write(b / "notes.txt", b"destination version", mtime=T0)
    return a, b


def test_conflict_rename_keeps_both(conflict, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = conflict
    plan, _ = merge(a, b, registry, dirs)
    assert [x.kind for x in plan.actions] == ["rename"]
    got = tree(b)
    assert got["notes.txt"] == b"destination version"
    (renamed,) = [k for k in got if k != "notes.txt"]
    assert renamed.startswith("notes (from A ") and renamed.endswith(").txt")
    assert got[renamed] == b"source version"


def test_conflict_skip(conflict, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = conflict
    plan, _ = merge(a, b, registry, dirs, on_conflict="skip")
    assert not plan.actions and plan.skipped_count("conflict") == 1
    assert tree(b) == {"notes.txt": b"destination version"}


def test_conflict_newer(conflict, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = conflict
    plan, report = merge(a, b, registry, dirs, on_conflict="newer")
    assert [x.kind for x in plan.actions] == ["replace"] and report.count("done") == 1
    assert tree(b)["notes.txt"] == b"source version"
    versions = list((b / ".duplio-versions").rglob("notes.txt"))
    assert len(versions) == 1 and versions[0].read_bytes() == b"destination version"
    # destination newer: left alone
    os.utime(a / "notes.txt", (T0 - 500, T0 - 500))
    write(b / "notes.txt", b"destination edited", mtime=T0 + 1000)
    plan = make_plan(a, b, registry, on_conflict="newer")
    assert not plan.actions and plan.skipped_count("dest-newer") == 1


def test_conflict_newer_with_equal_times_keeps_both(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "x.txt", b"aaaa", mtime=T0)
    write(b / "x.txt", b"bbbb", mtime=T0)
    plan = make_plan(a, b, registry, on_conflict="newer")
    assert [x.kind for x in plan.actions] == ["rename"]


def test_conflict_replace_and_undo(conflict, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = conflict
    plan, report = merge(a, b, registry, dirs, on_conflict="replace")
    assert tree(b)["notes.txt"] == b"source version"
    assert plan.disruptive
    result = undo(report.apply_id, registry, dirs=dirs)
    assert result.count("restored") == 1
    assert tree(b) == {"notes.txt": b"destination version"}
    assert tree(a) == {"notes.txt": b"source version"}


# ---------------------------------------------------------------- names
def test_names_are_fixed_for_windows_style_destinations(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "report: final?.txt", b"1")
    write(a / "a:b", b"2")
    write(a / "a?b", b"3")  # fixes to the same name as 'a:b'
    write(a / "CON", b"4")
    write(a / "sub." / "x.txt", b"5")
    plan, report = merge(a, b, registry, dirs, dest_caps={"windows_names": True})
    assert not report.failures
    got = tree(b)
    assert got["report_ final_.txt"] == b"1"
    assert sorted(k for k in got if k.startswith("a_b")) == ["a_b", "a_b (2)"]
    assert {got["a_b"], got["a_b (2)"]} == {b"2", b"3"}
    assert got["_CON"] == b"4" and got["sub/x.txt"] == b"5"
    assert any("renamed for the destination" in x.note for x in plan.actions)


def test_names_policies(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "ok.txt", b"ok")
    write(a / "bad?.txt", b"bad")
    caps = {"windows_names": True}
    plan = make_plan(a, b, registry, dest_caps=caps, names="skip")
    assert [x.dest_rel for x in plan.actions] == ["ok.txt"] and plan.skipped_count("name") == 1
    with pytest.raises(SafetyError, match="cannot be stored"):
        make_plan(a, b, registry, dest_caps=caps, names="abort")
    plan = make_plan(a, b, registry, dest_caps=caps, names="keep")
    assert sorted(x.dest_rel for x in plan.actions) == ["bad?.txt", "ok.txt"] and plan.warnings
    # a normal Linux destination needs no rewriting
    plan = make_plan(a, b, registry)
    assert sorted(x.dest_rel for x in plan.actions) == ["bad?.txt", "ok.txt"] and not plan.warnings


def test_too_long_names_are_shortened(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    long_name = "é" * 100 + ".txt"  # 204 bytes: fits on the source file system
    write(a / long_name, b"x")
    plan = make_plan(a, b, registry, dest_caps={"windows_names": True})
    assert [x.dest_rel for x in plan.actions] == [long_name]  # 104 UTF-16 units: fine for NTFS/SMB too


def test_case_insensitive_destination_never_merges_names(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "Photo.JPG", b"upper")
    write(a / "photo.jpg", b"lower")
    plan = make_plan(a, b, registry, dest_caps={"case_insensitive": True})
    assert sorted(x.dest_rel for x in plan.actions) == ["Photo.JPG", "photo (2).jpg"]
    # and an existing file differing only by case counts as the same path
    write(b / "NOTES.TXT", b"dest")
    write(a / "notes.txt", b"source")
    plan = make_plan(a, b, registry, dest_caps={"case_insensitive": True}, on_conflict="skip")
    assert plan.skipped_count("conflict") == 1


# ---------------------------------------------------------------- layout
def test_layout_tokens(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "vacation" / "beach.jpg", b"sand", mtime=1_700_000_000)  # 2023-11-14 UTC
    write(a / "README", b"r", mtime=1_700_000_000)
    plan, report = merge(a, b, registry, dirs, layout="{year}/{month}/{stem}-{date}.{ext}")
    assert sorted(x.dest_rel for x in plan.actions) == ["2023/11/README-2023-11-14.", "2023/11/beach-2023-11-14.jpg"]
    assert not report.failures
    entry = plan.actions[0].entry
    assert render_layout("{dir}/{name}", entry).count("/") <= 1
    with pytest.raises(ConfigError, match="unknown layout token"):
        make_plan(a, b, registry, layout="{nope}/{name}")
    with pytest.raises(ConfigError, match="--basis content"):
        make_plan(a, b, registry, layout="{name}", diff_opts=DiffOptions(basis=Basis.TIME_SIZE))
    with pytest.raises(ConfigError):
        render_layout("../{name}", entry)


# ---------------------------------------------------------------- safety
def test_source_changed_since_comparison_is_skipped(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "moving.bin", rand_bytes(2000, 8), mtime=T0)
    write(a / "stable.bin", rand_bytes(2000, 9), mtime=T0)
    plan = make_plan(a, b, registry)
    write(a / "moving.bin", rand_bytes(2500, 10), mtime=T0 + 50)  # edited after the comparison
    report = run_merge(plan, registry, dirs=dirs)
    assert report.count("skipped") == 1 and report.count("done") == 1
    assert list(tree(b)) == ["stable.bin"]


def test_never_overwrites_a_file_that_appeared_meanwhile(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.txt", b"from source", mtime=T0)
    plan = make_plan(a, b, registry)
    write(b / "f.txt", b"someone else wrote this", mtime=T0 + 1)
    report = run_merge(plan, registry, dirs=dirs)
    assert report.count("done") == 1
    got = tree(b)
    assert got["f.txt"] == b"someone else wrote this"
    assert got["f (2).txt"] == b"from source"


def test_failed_copy_leaves_no_partial_file(pair, registry, dirs, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "big.bin", rand_bytes(300_000, 11), mtime=T0)
    import duplio.transfer as transfer

    real = transfer.hash_file

    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EIO, "read-back failed")

    monkeypatch.setattr(transfer, "hash_file", broken)
    monkeypatch.setattr("duplio.vfs.retry.time.sleep", lambda s: None)
    _, report = merge(a, b, registry, dirs)
    assert report.count("failed") == 1
    assert [p for p in b.rglob("*") if p.is_file()] == []  # the unverified copy was removed
    monkeypatch.setattr(transfer, "hash_file", real)


def test_not_enough_space_stops_before_copying(pair, registry, dirs, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", rand_bytes(100_000, 12))
    plan = make_plan(a, b, registry)
    monkeypatch.setattr(type(plan.dest.backend), "free_space", lambda self, path: 1000)
    with pytest.raises(GuardError, match="not enough free space"):
        run_merge(plan, registry, dirs=dirs)
    assert tree(b) == {}


def test_cancel_stops_cleanly(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    for i in range(6):
        write(a / f"f{i}.bin", rand_bytes(1000, i))
    plan = make_plan(a, b, registry)
    token = CancelToken()
    token.cancel()
    report = run_merge(plan, registry, dirs=dirs, cancel=token)
    assert report.cancelled and report.count("done") == 0
    assert [p for p in b.rglob("*") if p.is_file()] == []
    with pytest.raises(CancelledError):
        token.check()


def test_dry_run_changes_nothing(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", b"data")
    plan = make_plan(a, b, registry)
    report = run_merge(plan, registry, dirs=dirs, dry_run=True)
    assert not report.results and tree(b) == {} and not list_journals(dirs.journals)


# ---------------------------------------------------------------- journal and undo
def test_undo_removes_copies_and_prunes_folders(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "deep" / "er" / "f.bin", b"one", mtime=T0)
    write(a / "g.bin", b"two", mtime=T0)
    write(b / "keep.bin", b"mine", mtime=T0)
    _, report = merge(a, b, registry, dirs)
    summary = summarize(list_journals(dirs.journals)[0])
    assert summary.header["action"] == "merge" and summary.count("done") == 2
    result = undo(report.apply_id, registry, dirs=dirs)
    assert result.count("restored") == 2
    assert tree(b) == {"keep.bin": b"mine"} and not (b / "deep").exists()


def test_undo_refuses_to_remove_the_only_copy(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "precious.bin", b"only here soon", mtime=T0)
    _, report = merge(a, b, registry, dirs)
    os.remove(a / "precious.bin")  # the source is gone afterwards
    result = undo(report.apply_id, registry, dirs=dirs)
    assert result.count("conflict") == 1 and result.count("restored") == 0
    assert tree(b) == {"precious.bin": b"only here soon"}


def test_undo_keeps_a_copy_that_was_edited(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.txt", b"original", mtime=T0)
    _, report = merge(a, b, registry, dirs)
    write(b / "f.txt", b"edited after the merge", mtime=T0 + 9)
    result = undo(report.apply_id, registry, dirs=dirs)
    assert result.count("conflict") == 1
    assert tree(b) == {"f.txt": b"edited after the merge"}


# ---------------------------------------------------------------- move
def test_move_quarantines_sources_after_verification(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(5000, 13)
    write(a / "docs" / "a.bin", data, mtime=T0)
    write(a / "docs" / "same-as-a.bin", data, mtime=T0)  # duplicate inside the source
    write(a / "b.bin", rand_bytes(100, 14), mtime=T0)
    _, report = merge(a, b, registry, dirs, move=True)
    assert report.count("done") == 2 and report.moved is not None and report.moved.count("done") == 3
    assert sorted(tree(b)) == ["b.bin", "docs/a.bin"]
    assert tree(a) == {}  # every source file left, in the quarantine
    assert len(list((a / ".duplio-quarantine").rglob("*.bin"))) == 3
    assert not (a / "docs").exists()  # emptied folders are pruned
    # one undo brings the sources back and removes the copies
    result = undo(report.apply_id, registry, dirs=dirs)
    assert result.count("restored") == 5 and result.count("conflict") == 0
    assert sorted(tree(a)) == ["b.bin", "docs/a.bin", "docs/same-as-a.bin"] and tree(b) == {}


def test_move_leaves_the_source_alone_if_the_copy_is_damaged(pair, registry, dirs, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", rand_bytes(4000, 15), mtime=T0)
    import duplio.backup.merge as merge_mod

    real = merge_mod.copy_verified

    def copy_then_corrupt(src, dst, **kw):  # type: ignore[no-untyped-def]
        digest = real(src, dst, **kw)
        data = bytearray(Path(dst[1]).read_bytes())
        data[0] ^= 0xFF
        Path(dst[1]).write_bytes(bytes(data))  # damaged after the copy was verified
        return digest

    monkeypatch.setattr(merge_mod, "copy_verified", copy_then_corrupt)
    _, report = merge(a, b, registry, dirs, move=True)
    assert report.moved is not None and report.moved.count("failed") == 1
    assert (a / "f.bin").exists() and not (a / ".duplio-quarantine").exists()


def test_move_protected_source_files_stay(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "Photos.photoslibrary" / "x.bin", b"inside a library", mtime=T0)
    write(a / "plain.bin", b"plain", mtime=T0)
    merge(a, b, registry, dirs, move=True)
    assert (a / "Photos.photoslibrary" / "x.bin").exists() and not (a / "plain.bin").exists()
    assert sorted(tree(b)) == ["Photos.photoslibrary/x.bin", "plain.bin"]


def test_clean_source_removes_what_dest_already_has(pair, registry, dirs) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    data = rand_bytes(3000, 16)
    write(a / "dup.bin", data, mtime=T0)
    write(b / "elsewhere" / "dup-renamed.bin", data, mtime=T0)
    write(a / "new.bin", b"new", mtime=T0)
    merge(a, b, registry, dirs, move=True)
    assert (a / "dup.bin").exists()  # not removed without --clean-source
    write(a / "new2.bin", b"newer", mtime=T0)
    merge(a, b, registry, dirs, move=True, clean_source=True)
    assert not (a / "dup.bin").exists() and not (a / "new2.bin").exists()
    assert tree(b)["elsewhere/dup-renamed.bin"] == data


def test_option_validation() -> None:
    with pytest.raises(SafetyError):
        MergeOptions(move=True, verify=False).validate()
    with pytest.raises(ConfigError):
        MergeOptions(clean_source=True).validate()
    with pytest.raises(ConfigError):
        MergeOptions(on_conflict="explode").validate()


# ---------------------------------------------------------------- network drive
def test_merge_into_and_out_of_an_smb_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirs) -> None:  # type: ignore[no-untyped-def]
    server = tmp_path / "nas"
    (server / "photos").mkdir(parents=True)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: FakeSMB(server))
    monkeypatch.setenv("DUPLIO_SMB_PASSWORD", "pw")
    local = tmp_path / "phone"
    have = rand_bytes(8000, 20)
    write(local / "IMG_1.jpg", have, mtime=T0)
    write(server / "photos" / "2024" / "kept.jpg", have, mtime=T0)
    write(local / "IMG_2: final?.jpg", rand_bytes(9000, 21), mtime=T0 + 3)
    write(local / "clip.mp4", rand_bytes(50_000, 22), mtime=T0 + 4)
    reg = BackendRegistry()
    try:
        plan = make_plan(local, "smb://me@nas/photos", reg)
        assert plan.dest.caps.windows_names and plan.dest.caps.kind.value == "network"
        report = run_merge(plan, reg, dirs=dirs)
        assert report.count("done") == 2 and not report.failures
        got = tree(server / "photos")
        assert sorted(got) == ["2024/kept.jpg", "IMG_2_ final_.jpg", "clip.mp4"]
        assert [p for p in (server / "photos").rglob("*") if ".duplio-tmp" in p.name] == []
        # and back: the NAS copy of the renamed photo is recognised as already present
        back = make_plan("smb://me@nas/photos", local, reg)
        assert [x.dest_rel for x in back.actions] == []
        assert back.skipped_count("present") + back.skipped_count("moved") >= 2
        undone = undo(report.apply_id, reg, dirs=dirs)
        assert undone.count("restored") == 2 and sorted(tree(server / "photos")) == ["2024/kept.jpg"]
    finally:
        reg.close()


# ---------------------------------------------------------------- CLI
def invoke(*args: str, **kw):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "160"}).invoke(cli, ["--no-color", *args], catch_exceptions=False, **kw)


def test_cli_dry_run_then_merge_then_undo(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "x" / "new.bin", b"brand new", mtime=T0)
    write(a / "same.bin", b"same", mtime=T0)
    write(b / "same.bin", b"same", mtime=T0)
    preview = invoke("merge", str(a), str(b), "--dry-run")
    assert preview.exit_code == 0 and "x/new.bin" in preview.output and "Dry run" in preview.output
    assert tree(b) == {"same.bin": b"same"}
    done = invoke("merge", str(a), str(b))
    assert done.exit_code == 0, done.output
    assert "Merge complete" in done.output and tree(b)["x/new.bin"] == b"brand new"
    assert "Nothing to copy" in invoke("merge", str(a), str(b)).output
    assert "merge" in invoke("history").output
    assert invoke("undo", "--yes").exit_code == 0
    assert tree(b) == {"same.bin": b"same"}


def test_cli_confirmation_rules(conflict, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    a, b = conflict
    refused = invoke("merge", str(a), str(b), "--on-conflict", "replace")
    assert refused.exit_code == 2 and "--yes" in refused.output
    assert tree(b) == {"notes.txt": b"destination version"}
    class Terminal:  # pretend stdin is a terminal so the question is asked
        stdin = type("Stdin", (), {"isatty": staticmethod(lambda: True)})()
        exit = staticmethod(sys.exit)

    monkeypatch.setattr("duplio.cli.cmd_merge.sys", Terminal)
    declined = invoke("merge", str(a), str(b), "--on-conflict", "replace", input="n\n")
    assert declined.exit_code == 0 and tree(b) == {"notes.txt": b"destination version"}
    accepted = invoke("merge", str(a), str(b), "--on-conflict", "replace", input="y\n")
    assert accepted.exit_code == 0 and tree(b)["notes.txt"] == b"source version"
    bad = invoke("merge", str(a), str(b), "--move", "--no-verify", "--yes")
    assert bad.exit_code == 2 and "verified" in bad.output


def test_cli_sentinel_guard_and_bwlimit(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "f.bin", rand_bytes(5000, 30))
    refused = invoke("merge", str(a), str(b), "--sentinel")
    assert refused.exit_code == 2 and "marker" in refused.output and tree(b) == {}
    assert invoke("sentinel", "create", str(b)).exit_code == 0
    ok = invoke("merge", str(a), str(b), "--sentinel", "--bwlimit", "100MB/s")
    assert ok.exit_code == 0, ok.output
    assert tree(b) == {"f.bin": (a / "f.bin").read_bytes()}


def test_cli_move_with_layout(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    write(a / "pic.jpg", b"jpeg", mtime=1_700_000_000)
    result = invoke("merge", str(a), str(b), "--layout", "{year}/{month}/{name}", "--move", "--yes")
    assert result.exit_code == 0, result.output
    assert tree(b) == {"2023/11/pic.jpg": b"jpeg"} and tree(a) == {}
    assert "Undo everything with" in result.output


def test_cli_rejects_overlap_and_bad_options(pair) -> None:  # type: ignore[no-untyped-def]
    a, _ = pair
    (a / "sub").mkdir()
    assert invoke("merge", str(a), str(a / "sub")).exit_code == 2
    assert invoke("merge", str(a), str(a.parent / "B"), "--layout", "{bogus}").exit_code == 2
    assert invoke("merge", str(a), str(a.parent / "B"), "--clean-source").exit_code == 2
