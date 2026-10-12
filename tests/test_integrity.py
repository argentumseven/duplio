"""Filters by age and type, checksum manifests, and version housekeeping."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup.manifest import Manifest, create_manifest, verify_manifest
from duplio.backup.versions import list_versions, parse_stamp, prune_versions, select_for_pruning
from duplio.cli.main import cli
from duplio.errors import ConfigError
from duplio.scan.filters import ScanFilter, resolve_types
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0
DAY = 86400.0


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "160"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


# ---------------------------------------------------------------- filters
def test_type_groups_and_extensions() -> None:
    assert "jpg" in resolve_types(["images"]) and "mp4" in resolve_types(["video"])
    assert resolve_types([".PDF", "docx"]) == {"pdf", "docx"}
    with pytest.raises(ValueError, match="unknown file type"):
        resolve_types(["not a type!"])


def test_type_filter_limits_the_scan(tmp_path: Path, registry: BackendRegistry) -> None:
    write(tmp_path / "a.jpg", b"1")
    write(tmp_path / "b.PNG", b"2")
    write(tmp_path / "c.txt", b"3")
    write(tmp_path / "sub" / "d.mp4", b"4")
    roots = make_roots([(registry.open(str(tmp_path)), RootRole.NORMAL)])
    inv = scan(roots, ScanFilter(types=["images"], min_size=0))
    assert sorted(e.rel for e in inv.entries) == ["a.jpg", "b.PNG"]
    inv = scan(roots, ScanFilter(types=["images", ".txt"], min_size=0))
    assert sorted(e.rel for e in inv.entries) == ["a.jpg", "b.PNG", "c.txt"]


def test_age_filter_limits_the_scan(tmp_path: Path, registry: BackendRegistry) -> None:
    now = time.time()
    write(tmp_path / "fresh.txt", b"1", mtime=now - 2 * DAY)
    write(tmp_path / "middle.txt", b"2", mtime=now - 40 * DAY)
    write(tmp_path / "ancient.txt", b"3", mtime=now - 400 * DAY)
    roots = make_roots([(registry.open(str(tmp_path)), RootRole.NORMAL)])
    recent = ScanFilter(min_size=0, modified_after_ns=int((now - 30 * DAY) * 1e9))
    assert [e.rel for e in scan(roots, recent).entries] == ["fresh.txt"]
    old = ScanFilter(min_size=0, modified_before_ns=int((now - 365 * DAY) * 1e9))
    assert [e.rel for e in scan(roots, old).entries] == ["ancient.txt"]
    window = ScanFilter(min_size=0, modified_after_ns=int((now - 100 * DAY) * 1e9),
                        modified_before_ns=int((now - 10 * DAY) * 1e9))
    assert [e.rel for e in scan(roots, window).entries] == ["middle.txt"]


def test_filter_roundtrip() -> None:
    f = ScanFilter(types=["images"], modified_after_ns=5, modified_before_ns=9)
    g = ScanFilter.from_dict(f.to_dict())
    assert (g.types, g.modified_after_ns, g.modified_before_ns) == (["images"], 5, 9)


def test_cli_filters_in_diff_and_scan(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    now = time.time()
    write(a / "new.jpg", b"jpeg", mtime=now - DAY)
    write(a / "old.jpg", b"old jpeg", mtime=now - 500 * DAY)
    write(a / "doc.txt", b"text", mtime=now - DAY)
    out = invoke("diff", str(a), str(b), "--list", "only-a", "--type", "images", "--newer-than", "30d").output
    assert out.split() == ["new.jpg"]
    assert invoke("diff", str(a), str(b), "--newer-than", "soon").exit_code == 2
    assert invoke("diff", str(a), str(b), "--type", "weird type").exit_code == 2
    assert invoke("scan", str(a), "--type", "images", "--newer-than", "2d").exit_code == 0
    assert "--newer-than" not in invoke("sync", "--help").output  # deliberately absent from sync
    assert "--type" in invoke("sync", "--help").output


# ---------------------------------------------------------------- manifest
@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    write(root / "a.bin", rand_bytes(5000, 1), mtime=T0)
    write(root / "sub" / "b.bin", rand_bytes(7000, 2), mtime=T0)
    write(root / "empty", b"", mtime=T0)
    return root


def test_manifest_roundtrip_and_clean_verify(archive: Path, registry: BackendRegistry, tmp_path: Path) -> None:
    loc = registry.open(str(archive))
    m = create_manifest(loc)
    assert [e.rel for e in m.entries] == ["a.bin", "empty", "sub/b.bin"] and not m.errors
    saved = m.save(tmp_path / "m.json")
    loaded = Manifest.load(saved)
    assert loaded.entries == m.entries and loaded.algorithm == m.algorithm
    report = verify_manifest(loc, loaded)
    assert report.clean and report.count("ok") == 3


def test_manifest_finds_every_kind_of_problem(archive: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(archive))
    m = create_manifest(loc)
    # bit rot: flip a byte but keep the size and the modification time
    path = archive / "a.bin"
    data = bytearray(path.read_bytes())
    data[100] ^= 0x01
    path.write_bytes(bytes(data))
    os.utime(path, (T0, T0))
    write(archive / "sub" / "b.bin", b"edited by someone", mtime=T0 + 99)  # a real edit
    os.remove(archive / "empty")  # lost
    write(archive / "extra.txt", b"new", mtime=T0)  # added
    os.utime(archive / "sub", None)
    report = verify_manifest(loc, m)
    got = {i.rel: i.status for i in report.items}
    assert got == {"a.bin": "corrupt", "sub/b.bin": "modified", "empty": "missing", "extra.txt": "new"}
    assert not report.clean


def test_touch_is_not_corruption(archive: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(archive))
    m = create_manifest(loc)
    os.utime(archive / "a.bin", (T0 + 5000, T0 + 5000))
    report = verify_manifest(loc, m)
    assert report.clean and report.count("touched") == 1
    quick = verify_manifest(loc, m, quick=True)
    assert quick.count("modified") == 1  # quick mode cannot tell touch from edit


def test_manifest_matches_unicode_forms(tmp_path: Path, registry: BackendRegistry) -> None:
    import unicodedata

    root = tmp_path / "u"
    name = "Résumé.txt"
    write(root / unicodedata.normalize("NFD", name), b"cv", mtime=T0)
    loc = registry.open(str(root))
    m = create_manifest(loc)
    os.rename(root / unicodedata.normalize("NFD", name), root / unicodedata.normalize("NFC", name))
    assert verify_manifest(loc, m).clean


def test_manifest_file_is_validated(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError):
        Manifest.load(bad)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"format": "something-else"}))
    with pytest.raises(ConfigError, match="not a Duplio manifest"):
        Manifest.load(other)


def test_cli_manifest_workflow(archive: Path, tmp_path: Path) -> None:
    out = tmp_path / "m.json"
    created = invoke("manifest", "create", str(archive), "-o", str(out))
    assert created.exit_code == 0 and "3 files" in created.output
    ok = invoke("manifest", "verify", str(archive), str(out))
    assert ok.exit_code == 0 and "intact" in ok.output
    data = bytearray((archive / "a.bin").read_bytes())
    data[0] ^= 0xFF
    (archive / "a.bin").write_bytes(bytes(data))
    os.utime(archive / "a.bin", (T0, T0))
    bad = invoke("manifest", "verify", str(archive), str(out))
    assert bad.exit_code == 1 and "corrupt" in bad.output and "a.bin" in bad.output


# ---------------------------------------------------------------- versions
def make_runs(root: Path) -> None:
    for name in ("20250101T000000123Z-aaaa", "20250601T120000Z", "20260301T000000000Z-bbbb"):
        write(root / ".duplio-versions" / name / "docs" / "f.txt", b"old " + name.encode())
    write(root / ".duplio-versions" / "not-a-run" / "keep.txt", b"unrecognised folder")


def test_stamp_parsing() -> None:
    assert parse_stamp("20250601T120000Z") is not None
    assert parse_stamp("20250101T000000123Z-aaaa") == pytest.approx(parse_stamp("20250101T000000Z") + 0.123)
    assert parse_stamp("whatever") is None


def test_list_and_prune_versions(tmp_path: Path, registry: BackendRegistry) -> None:
    make_runs(tmp_path)
    loc = registry.open(str(tmp_path))
    runs = list_versions(loc)
    assert [r.name for r in runs if r.when] == ["20250101T000000123Z-aaaa", "20250601T120000Z", "20260301T000000000Z-bbbb"]
    assert all(r.files == 1 for r in runs)
    now = parse_stamp("20260401T000000Z")
    chosen = select_for_pruning(runs, older_than="90d", now=now)
    assert [r.name for r in chosen] == ["20250101T000000123Z-aaaa", "20250601T120000Z"]
    chosen = select_for_pruning(runs, older_than="1d", keep_last=2, now=now)
    assert [r.name for r in chosen] == ["20250101T000000123Z-aaaa"]  # the two newest are protected
    with pytest.raises(ConfigError):
        select_for_pruning(runs)
    with pytest.raises(ConfigError):
        select_for_pruning(runs, older_than="a while")
    assert prune_versions(loc, older_than="1d", keep_last=1, dry_run=True)
    assert len(list_versions(loc)) == 4  # a dry run removes nothing
    removed = prune_versions(loc, older_than="1d", keep_last=1)
    assert len(removed) == 2
    remaining = {r.name for r in list_versions(loc)}
    assert remaining == {"20260301T000000000Z-bbbb", "not-a-run"}  # unrecognised folders are never touched
    assert (tmp_path / ".duplio-versions" / "not-a-run" / "keep.txt").exists()


def test_cli_versions(tmp_path: Path) -> None:
    make_runs(tmp_path)
    assert "20250601T120000Z" in invoke("versions", "list", str(tmp_path)).output
    preview = invoke("versions", "prune", str(tmp_path), "--older-than", "1d", "--keep-last", "1")
    assert preview.exit_code == 0 and "preview" in preview.output
    assert (tmp_path / ".duplio-versions" / "20250601T120000Z").exists()
    refused = invoke("versions", "prune", str(tmp_path), "--older-than", "1d", "--keep-last", "1", "--apply")
    assert refused.exit_code == 2 and "--yes" in refused.output
    done = invoke("versions", "prune", str(tmp_path), "--older-than", "1d", "--keep-last", "1", "--apply", "--yes")
    assert done.exit_code == 0 and not (tmp_path / ".duplio-versions" / "20250601T120000Z").exists()
    assert invoke("versions", "prune", str(tmp_path)).exit_code == 2


def test_mirror_keep_versions_still_prunes(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "f.txt", b"v1", mtime=T0)
    old = dst / ".duplio-versions" / "20200101T000000Z" / "x.txt"
    write(old, b"ancient")
    write(dst / "f.txt", b"v0", mtime=T0 - 50)
    result = invoke("mirror", str(src), str(dst), "--keep-versions", "30d", "--yes")
    assert result.exit_code == 0, result.output
    assert not old.exists()
