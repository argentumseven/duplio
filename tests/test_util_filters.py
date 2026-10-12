from __future__ import annotations

import pytest

from duplio.scan.filters import GlobSet, ScanFilter, compile_glob, extension
from duplio.util.fmt import human_duration, human_size, parse_duration, parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("1", 1), ("10KB", 10_000), ("10KiB", 10_240), ("1.5 MiB", 1_572_864), ("2g", 2_000_000_000), ("3 b", 3)],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "10XB", "-5"])
def test_parse_size_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_size(bad)


def test_human_size() -> None:
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(5 * 1024**3) == "5.0 GiB"


def test_durations() -> None:
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("2w3d") == 17 * 86400
    assert parse_duration("12h") == 43200
    with pytest.raises(ValueError):
        parse_duration("7 days please")
    assert human_duration(3.25) == "3.2s" or human_duration(3.25) == "3.3s"
    assert human_duration(125) == "2m 05s"
    assert human_duration(3725) == "1h 02m"


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*.tmp", "a.tmp", True),
        ("*.tmp", "deep/dir/a.tmp", True),
        ("*.tmp", "a.tmpx", False),
        ("Android/data", "Android/data", True),
        ("Android/data", "x/Android/data", False),  # anchored
        ("**/cache/**", "a/cache/b/c.bin", True),
        ("**/cache/**", "cache/x", True),
        ("**/cache/**", "cacheX/x", False),
        ("IMG_????.JPG", "img_1234.jpg", True),  # case-insensitive
        ("[!a]*.txt", "b.txt", True),
        ("[!a]*.txt", "a.txt", False),
        (".Trash-*", "folder/.Trash-1000", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    assert bool(compile_glob(pattern).match(path)) is matches


def test_globset_parent_matching() -> None:
    globs = GlobSet(["**/*.photoslibrary/**"])
    assert globs.matches_path_or_parent("Pictures/Photos Library.photoslibrary/originals/A/x.heic")
    assert not globs.matches_path_or_parent("Pictures/x.heic")


def test_scan_filter_defaults() -> None:
    f = ScanFilter()
    assert f.skip_dir(".git", ".git")
    assert f.skip_dir("x/.duplio-quarantine", ".duplio-quarantine")
    assert f.skip_dir("DCIM/.thumbnails", ".thumbnails")
    assert f.skip_file("x/.DS_Store", ".DS_Store", 10)
    assert f.skip_file("x/.trashed-1700000000-IMG.jpg", ".trashed-1700000000-IMG.jpg", 10)
    assert f.skip_file("empty.txt", "empty.txt", 0)  # min size 1 by default
    assert not f.skip_file("photo.jpg", "photo.jpg", 10)
    assert f.is_protected("Android/data/com.app/files/a.jpg")
    assert f.is_protected("Apps/Thing.app/Contents/Resources/icon.png")
    assert not f.is_protected("DCIM/Camera/a.jpg")


def test_scan_filter_include_and_backup_preset() -> None:
    f = ScanFilter(include=["*.jpg"], skip_hidden=True)
    assert f.skip_file("a.png", "a.png", 5)
    assert not f.skip_file("a.JPG", "a.JPG", 5)
    assert f.skip_dir(".hidden", ".hidden")
    b = ScanFilter.for_backup()
    assert not b.skip_dir(".git", ".git")  # backups keep version control data
    assert not b.skip_file("empty", "empty", 0)
    assert b.skip_file(".DS_Store", ".DS_Store", 3)
    assert not b.is_protected("Android/data/x")


def test_filter_roundtrip() -> None:
    f = ScanFilter(include=["*.a"], exclude=["*.b"], protect=["c/**"], min_size=5, max_size=10, skip_hidden=True)
    g = ScanFilter.from_dict(f.to_dict())
    assert g.to_dict() == f.to_dict()


def test_extension() -> None:
    assert extension("a.JPG") == "jpg"
    assert extension(".bashrc") == ""
    assert extension("noext") == ""
