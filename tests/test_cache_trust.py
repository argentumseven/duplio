"""The digest cache must never be what decides that a file is safe somewhere else.

Remembered digests are keyed by size and modification time, so content that changed *without* either
changing (bit rot, a failing disk, a tool that restores timestamps) is invisible to the cache. Commands
that answer "is this already in the destination / backup?" therefore read fresh unless --use-cache is given.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def rot(path: Path) -> None:
    """Change one byte without changing the size or the modification time: silent corruption."""
    stat = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "A", tmp_path / "B"
    data = rand_bytes(50_000, 1)
    write(a / "x.bin", data, mtime=T0)
    write(b / "x.bin", data, mtime=T0)
    # prime the cache with the true digests (a normal scan caches them)
    assert invoke("scan", str(a), str(b)).exit_code == 0
    assert invoke("cache", "stats").output.startswith("2 cached")
    return a, b


def test_compare_notices_rot_in_the_backup(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    rot(b / "x.bin")
    fresh = invoke("compare", str(a), "--against", str(b), "--list")
    assert fresh.exit_code == 0 and "x.bin" in fresh.stdout  # NOT safely backed up any more
    trusting = invoke("compare", str(a), "--against", str(b), "--list", "--use-cache")
    assert "x.bin" not in trusting.stdout  # the stale digest still says "backed up": why it is opt-in


def test_diff_notices_rot(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    rot(b / "x.bin")
    assert invoke("diff", str(a), str(b), "--list", "changed").stdout.split() == ["x.bin"]
    assert invoke("diff", str(a), str(b), "--list", "changed", "--use-cache").stdout.split() == []


def test_merge_does_not_skip_a_file_whose_copy_rotted(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    rot(b / "x.bin")
    preview = invoke("merge", str(a), str(b), "--dry-run")
    assert "different file(s) at the same path" in preview.output  # kept both instead of assuming it is there
    assert "Nothing to copy" in invoke("merge", str(a), str(b), "--dry-run", "--use-cache").output


def test_mirror_checksum_repairs_a_rotted_copy(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    good = (a / "x.bin").read_bytes()
    rot(b / "x.bin")
    result = invoke("mirror", str(a), str(b), "--checksum", "--yes")
    assert result.exit_code == 0, result.output
    assert (b / "x.bin").read_bytes() == good
    kept = [p.read_bytes() for p in (b / ".duplio-versions").rglob("x.bin")]
    assert len(kept) == 1 and kept[0] != good  # the damaged copy is kept as a version, not destroyed


def test_ingest_does_not_trust_a_rotted_library_copy(tmp_path: Path) -> None:
    phone = tmp_path / "phone"
    data = rand_bytes(40_000, 3)
    write(phone / "IMG_20240101_101010.jpg", data, mtime=T0)
    libraries = {name: tmp_path / name for name in ("fresh", "trusting")}
    held = {}
    for name, library in libraries.items():
        held[name] = library / "2024" / "2024-01" / "IMG_20240101_101010.jpg"
        write(held[name], data, mtime=T0)  # already in the library (put there by some other means)
    assert invoke("scan", str(phone), *map(str, libraries.values())).exit_code == 0  # remembers every digest
    for path in held.values():
        rot(path)
    fresh = invoke("ingest", str(phone), str(libraries["fresh"]))
    assert fresh.exit_code == 0 and "1 imported" in fresh.output and "duplicate" not in fresh.output
    assert len(list(libraries["fresh"].rglob("*.jpg"))) == 2  # the rotted file was not mistaken for the photo
    trusting = invoke("ingest", str(phone), str(libraries["trusting"]), "--use-cache")
    assert "1 duplicate" in trusting.output  # the stale digest is believed: why trusting the cache is opt-in
    assert len(list(libraries["trusting"].rglob("*.jpg"))) == 1


def test_scan_and_dedupe_still_use_the_cache(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    result = invoke("scan", str(a), str(b))
    assert "2 cache hits" in result.output or ("cache hits" in result.output and " 0 cache hits" not in result.output)
