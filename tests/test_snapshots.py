"""Hard-link snapshots."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.snapshots import INCOMPLETE, META_DIR, SnapshotSet
from duplio.cli.main import cli
from duplio.errors import CancelledError, RepositoryError, SafetyError
from duplio.events import CancelToken
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0


def take(registry: BackendRegistry, src: Path, dest: Path, *, now: float, **kw):  # type: ignore[no-untyped-def]
    return SnapshotSet(registry.open(str(dest))).create(registry.open(str(src)), now=now, **kw)


def names(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    write(src / "keep.bin", rand_bytes(5000, 1), mtime=T0)
    write(src / "docs" / "edit.txt", b"version 1", mtime=T0)
    write(src / "docs" / "gone.txt", b"will be deleted", mtime=T0)
    return src


def test_first_snapshot_is_a_complete_read_only_copy(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    report = take(registry, source, dest, now=1_700_000_000)
    assert names(dest) == ["2023-11-14T221320Z"] and report.name == "2023-11-14T221320Z"
    snap = dest / report.name
    assert (snap / "keep.bin").read_bytes() == (source / "keep.bin").read_bytes()
    assert (snap / "docs" / "edit.txt").read_bytes() == b"version 1"
    assert (snap / "keep.bin").stat().st_mtime_ns == (source / "keep.bin").stat().st_mtime_ns
    assert stat.S_IMODE((snap / "keep.bin").stat().st_mode) & 0o222 == 0  # read-only: links share data
    assert (report.linked, report.copied) == (0, 3) and report.meta.files == 3
    assert (dest / META_DIR / f"{report.name}.json").exists()
    assert not [p for p in dest.iterdir() if p.name.startswith(INCOMPLETE)]
    assert os.readlink(dest / "latest") == report.name


def test_unchanged_files_are_hard_linked(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    first = take(registry, source, dest, now=1_700_000_000)
    write(source / "docs" / "edit.txt", b"version 2 is longer", mtime=T0 + 100)
    os.remove(source / "docs" / "gone.txt")
    write(source / "new.txt", b"brand new", mtime=T0 + 100)
    second = take(registry, source, dest, now=1_700_003_600)
    a, b = dest / first.name, dest / second.name
    assert (second.linked, second.copied) == (1, 2)
    assert (a / "keep.bin").stat().st_ino == (b / "keep.bin").stat().st_ino and (b / "keep.bin").stat().st_nlink == 2
    assert (a / "docs" / "edit.txt").stat().st_ino != (b / "docs" / "edit.txt").stat().st_ino
    assert (a / "docs" / "edit.txt").read_bytes() == b"version 1"  # the old snapshot is untouched
    assert (b / "docs" / "edit.txt").read_bytes() == b"version 2 is longer"
    assert (a / "docs" / "gone.txt").exists() and not (b / "docs" / "gone.txt").exists()
    assert second.meta.parent == first.name and second.meta.new_bytes == len(b"version 2 is longer") + len(b"brand new")
    assert os.readlink(dest / "latest") == second.name


def test_forgetting_one_snapshot_never_harms_another(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    first = take(registry, source, dest, now=1_700_000_000)
    write(source / "docs" / "edit.txt", b"v2", mtime=T0 + 50)
    second = take(registry, source, dest, now=1_700_003_600)
    snaps = SnapshotSet(registry.open(str(dest)))
    freed = snaps.forget(first.name)
    assert not (dest / first.name).exists() and not (dest / META_DIR / f"{first.name}.json").exists()
    assert freed == len(b"version 1")  # shared files stay: only data no other snapshot links to is freed
    assert (dest / second.name / "keep.bin").read_bytes() == (source / "keep.bin").read_bytes()
    assert [n for n, _ in snaps.snapshots()] == [second.name]
    snaps.forget(second.name)
    assert not (dest / "latest").exists() and names(dest) == []


def test_separate_series_do_not_link_to_each_other(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    other = tmp_path / "other"
    write(other / "keep.bin", (source / "keep.bin").read_bytes(), mtime=T0)
    dest = tmp_path / "snaps"
    take(registry, source, dest, now=1_700_000_000, label="docs")
    second = take(registry, other, dest, now=1_700_000_100, label="other")
    assert second.linked == 0 and second.meta.parent is None
    third = take(registry, source, dest, now=1_700_000_200, label="docs")
    assert third.linked == 3 and third.name.endswith("-docs")


def test_two_snapshots_in_one_second_get_distinct_names(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    a = take(registry, source, dest, now=1_700_000_000)
    b = take(registry, source, dest, now=1_700_000_000)
    assert a.name != b.name and len(names(dest)) == 2


def test_destination_without_hard_links_is_refused(tmp_path: Path, registry: BackendRegistry, source: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "snaps"
    loc = registry.open(str(dest))
    real = type(loc.backend).capabilities
    monkeypatch.setattr(type(loc.backend), "capabilities",
                        lambda self, path: real(self, path).with_(can_hardlink=False, label="exFAT"))
    with pytest.raises(SafetyError, match="does not support hard links"):
        SnapshotSet(loc).create(registry.open(str(source)))
    monkeypatch.undo()

    def refuse(self, src, dst):  # type: ignore[no-untyped-def]
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(type(loc.backend), "hardlink", refuse)
    with pytest.raises(SafetyError, match="hard links do not work"):
        SnapshotSet(loc).create(registry.open(str(source)))
    assert not [p for p in dest.rglob("*") if p.is_file()]  # the probe cleaned up after itself


def test_interrupted_run_leaves_no_snapshot(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        take(registry, source, dest, now=1_700_000_000, cancel=token)
    assert names(dest) == [] and not [p for p in dest.iterdir() if p.name.startswith(INCOMPLETE)]
    # a stale half-built folder from a crash is cleaned up by the next run, and nothing else is touched
    stale = dest / f"{INCOMPLETE}dead"
    write(stale / "partial.bin", b"half")
    keep = dest / "my-own-folder"
    write(keep / "file.txt", b"not ours")
    take(registry, source, dest, now=1_700_000_100)
    assert not stale.exists() and (keep / "file.txt").exists()


def test_a_file_that_cannot_be_copied_is_reported_not_fatal(tmp_path: Path, registry: BackendRegistry, source: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import duplio.backup.snapshots as mod

    real = mod.copy_verified

    def flaky(src, dst, **kw):  # type: ignore[no-untyped-def]
        if str(src[1]).endswith("edit.txt"):
            raise PermissionError(13, "denied")
        return real(src, dst, **kw)

    monkeypatch.setattr(mod, "copy_verified", flaky)
    report = take(registry, source, tmp_path / "snaps", now=1_700_000_000)
    assert len(report.errors) == 1 and report.meta.errors == 1 and report.meta.files == 2
    assert not (tmp_path / "snaps" / report.name / "docs" / "edit.txt").exists()


def test_snapshot_inside_source_is_refused(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    with pytest.raises(RepositoryError, match="cannot be inside"):
        take(registry, source, source / "snaps", now=1_700_000_000)
    with pytest.raises(RepositoryError, match="source folder not found"):
        take(registry, tmp_path / "nope", tmp_path / "snaps", now=1_700_000_000)


def test_odd_file_names(tmp_path: Path, registry: BackendRegistry) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with open(os.fsencode(src) + b"/bad\xffname\n.bin", "wb") as handle:
        handle.write(b"payload")
    write(src / "Ünï ✓" / "日本語.txt", b"text")
    report = take(registry, src, tmp_path / "snaps", now=1_700_000_000)
    again = take(registry, src, tmp_path / "snaps", now=1_700_000_500)
    assert report.copied == 2 and again.linked == 2 and not again.errors


def test_restoring_is_just_copying_a_folder(tmp_path: Path, registry: BackendRegistry, source: Path) -> None:
    dest = tmp_path / "snaps"
    report = take(registry, source, dest, now=1_700_000_000)
    restored = tmp_path / "restored"
    restored.mkdir()
    result = CliRunner().invoke(cli, ["--no-color", "merge", str(dest / report.name), str(restored)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert (restored / "docs" / "edit.txt").read_bytes() == b"version 1"


# ---------------------------------------------------------------- CLI
def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def test_cli_create_list_forget(tmp_path: Path, source: Path) -> None:
    dest = tmp_path / "snaps"
    first = invoke("snapshot", "create", str(source), str(dest), "--label", "docs")
    assert first.exit_code == 0 and "Snapshot complete" in first.output and "3 new or changed" in first.output
    write(source / "new.txt", b"x", mtime=T0 + 5)
    second = invoke("snapshot", "create", str(source), str(dest), "--label", "docs")
    assert "3 unchanged" in second.output
    listing = invoke("snapshot", "list", str(dest))
    assert listing.output.count("docs") >= 2
    preview = invoke("snapshot", "forget", str(dest), "--keep-last", "1")
    assert preview.exit_code == 0 and "Dry run" in preview.output and len(names(dest)) == 2
    refused = invoke("snapshot", "forget", str(dest), "--keep-last", "1", "--apply")
    assert refused.exit_code == 2 and "--yes" in refused.output
    done = invoke("snapshot", "forget", str(dest), "--keep-last", "1", "--apply", "--yes")
    assert done.exit_code == 0 and len(names(dest)) == 1
    assert invoke("snapshot", "forget", str(dest)).exit_code == 2  # no policy: refuses instead of deleting everything


def test_cli_exit_status_with_errors(tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import duplio.backup.snapshots as mod

    real = mod.copy_verified

    def flaky(src, dst, **kw):  # type: ignore[no-untyped-def]
        if str(src[1]).endswith("keep.bin"):
            raise OSError(5, "I/O error")
        return real(src, dst, **kw)

    monkeypatch.setattr(mod, "copy_verified", flaky)
    monkeypatch.setattr("duplio.vfs.retry.time.sleep", lambda s: None)
    result = invoke("snapshot", "create", str(source), str(tmp_path / "snaps"))
    assert result.exit_code == 1 and "finished with errors" in result.output
