from __future__ import annotations

import os
from pathlib import Path

import pytest

from duplio.compare.engine import compare
from duplio.compare.model import Confidence, Mode, ScanOptions, Verify
from duplio.errors import ConfigError, OverlappingRootsError
from duplio.events import RecordingProgress
from duplio.hashing.cache import HashCache
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write


def run(registry: BackendRegistry, roots, mode=Mode.ALL, verify=Verify.HASH, **kw):  # type: ignore[no-untyped-def]
    locs = [(registry.open(str(p)), role) for p, role in roots]
    inv = scan(make_roots(locs), ScanFilter(**kw.pop("filter_kw", {})))
    opts = ScanOptions(mode=mode, verify=verify)
    for k, v in kw.items():
        setattr(opts, k, v)
    return compare(inv, opts, cache=kw.get("cache"))


def rels(group) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(f"{m.root}:{m.rel}" for m in group.members)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    big = rand_bytes(600_000, 1)
    write(tmp_path / "a" / "photo.jpg", big)
    write(tmp_path / "a" / "sub" / "photo copy.jpg", big)
    write(tmp_path / "b" / "photo.jpg", big)
    # Same size and identical in every sampled region (head, middle, tail): must NOT match.
    tricky = bytearray(big)
    tricky[200_000] ^= 0xFF
    write(tmp_path / "b" / "tricky.jpg", bytes(tricky))
    # Same size, different head.
    other = bytearray(big)
    other[0] ^= 0xFF
    write(tmp_path / "b" / "other.jpg", bytes(other))
    write(tmp_path / "a" / "small.txt", "hello world")
    write(tmp_path / "b" / "small.txt", "hello world")
    write(tmp_path / "b" / "unique.bin", rand_bytes(1234, 2))
    return tmp_path


def test_all_mode_finds_exact_groups_only(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)])
    groups = {g.size: rels(g) for g in res.groups}
    assert groups[600_000] == ["0:photo.jpg", "0:sub/photo copy.jpg", "1:photo.jpg"]
    assert groups[11] == ["0:small.txt", "1:small.txt"]
    assert len(res.groups) == 2
    assert all(g.confidence is Confidence.CERTAIN for g in res.groups)
    assert res.wasted == 2 * 600_000 + 11
    assert res.groups[0].size == 600_000  # sorted by wasted space


def test_sampling_prunes_before_full_hash(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)])
    # 5 files of 600 kB were sampled; the one with a different head was pruned there,
    # the one that differs only outside the samples needed a full hash to be told apart.
    assert res.stats.sampled == 5
    assert res.stats.full_hashed == 4 + 2  # 4 big survivors + 2 small files


def test_sampling_catches_middle_difference(registry: BackendRegistry, tmp_path: Path) -> None:
    big = rand_bytes(600_000, 1)
    write(tmp_path / "m" / "a.bin", big)
    changed = bytearray(big)
    changed[300_000] ^= 0xFF  # inside the middle sample
    write(tmp_path / "m" / "b.bin", bytes(changed))
    res = run(registry, [(tmp_path / "m", RootRole.NORMAL)])
    assert res.groups == [] and res.stats.full_hashed == 0


def test_byte_verification(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)], verify=Verify.BYTES)
    assert len(res.groups) == 2
    assert res.stats.byte_compared == 3
    assert all("byte-for-byte" in g.method for g in res.groups)


def test_non_cryptographic_hash_forces_byte_compare(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "a", RootRole.NORMAL)], full_algorithm="md5")
    assert res.stats.byte_compared >= 1
    assert all("byte-for-byte" in g.method for g in res.groups)


def test_cross_mode(registry: BackendRegistry, tree: Path) -> None:
    write(tree / "a" / "dup1.bin", rand_bytes(5000, 9))
    write(tree / "a" / "dup2.bin", rand_bytes(5000, 9))
    res = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)], mode=Mode.CROSS)
    sizes = sorted(g.size for g in res.groups)
    assert sizes == [11, 600_000]  # the a-only pair is not reported


def test_within_mode(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)], mode=Mode.WITHIN)
    assert [rels(g) for g in res.groups] == [["0:photo.jpg", "0:sub/photo copy.jpg"]]


def test_reference_mode(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "b", RootRole.NORMAL), (tree / "a", RootRole.REFERENCE)], mode=Mode.REFERENCE)
    assert sorted(g.size for g in res.groups) == [11, 600_000]
    with pytest.raises(ConfigError):
        run(registry, [(tree / "b", RootRole.NORMAL)], mode=Mode.REFERENCE)


def test_unique_mode(registry: BackendRegistry, tree: Path) -> None:
    res = run(registry, [(tree / "b", RootRole.NORMAL), (tree / "a", RootRole.REFERENCE)], mode=Mode.UNIQUE)
    assert sorted(e.rel for e in res.unique) == ["other.jpg", "tricky.jpg", "unique.bin"]
    present = sorted(m.rel for g in res.groups for m in g.members if m.root == 0)
    assert present == ["photo.jpg", "small.txt"]


def test_unique_mode_marks_unreadable_as_unverified(registry: BackendRegistry, tree: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.vfs.local import LocalBackend

    real_open, real_range = LocalBackend.open_read, LocalBackend.read_range
    blocked = str(tree / "b" / "photo.jpg")

    def deny_open(self, path):  # type: ignore[no-untyped-def]
        if path == blocked:
            raise PermissionError(13, "Permission denied", path)
        return real_open(self, path)

    def deny_range(self, path, offset, length):  # type: ignore[no-untyped-def]
        if path == blocked:
            raise PermissionError(13, "Permission denied", path)
        return real_range(self, path, offset, length)

    monkeypatch.setattr(LocalBackend, "open_read", deny_open)
    monkeypatch.setattr(LocalBackend, "read_range", deny_range)
    res = run(registry, [(tree / "b", RootRole.NORMAL), (tree / "a", RootRole.REFERENCE)], mode=Mode.UNIQUE)
    assert "photo.jpg" in [e.rel for e in res.unverified]
    assert "photo.jpg" not in [e.rel for e in res.unique]


def test_unreadable_reference_never_yields_false_unique(registry: BackendRegistry, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """If the only reference copy cannot be read, its match must be 'unverified', not 'unique'."""
    from duplio.vfs.local import LocalBackend

    data = rand_bytes(400_000, 8)
    write(tmp_path / "src" / "x.bin", data)
    write(tmp_path / "ref" / "x.bin", data)
    real_open, real_range = LocalBackend.open_read, LocalBackend.read_range
    blocked = str(tmp_path / "ref" / "x.bin")

    def deny_open(self, path):  # type: ignore[no-untyped-def]
        if path == blocked:
            raise PermissionError(13, "Permission denied", path)
        return real_open(self, path)

    def deny_range(self, path, offset, length):  # type: ignore[no-untyped-def]
        if path == blocked:
            raise PermissionError(13, "Permission denied", path)
        return real_range(self, path, offset, length)

    monkeypatch.setattr(LocalBackend, "open_read", deny_open)
    monkeypatch.setattr(LocalBackend, "read_range", deny_range)
    res = run(registry, [(tmp_path / "src", RootRole.NORMAL), (tmp_path / "ref", RootRole.REFERENCE)], mode=Mode.UNIQUE)
    assert res.unique == []
    assert [e.rel for e in res.unverified] == ["x.bin"]


def test_hardlinks_are_aliases_not_duplicates(registry: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(200_000, 3)
    write(tmp_path / "r" / "a.bin", data)
    os.link(tmp_path / "r" / "a.bin", tmp_path / "r" / "link.bin")
    res = run(registry, [(tmp_path / "r", RootRole.NORMAL)])
    assert res.groups == []
    assert res.stats.hardlink_aliases == 1
    write(tmp_path / "r" / "copy.bin", data)
    res = run(registry, [(tmp_path / "r", RootRole.NORMAL)])
    assert len(res.groups) == 1 and res.groups[0].count == 2
    assert sum(len(v) for v in res.groups[0].aliases.values()) == 1


def test_quick_mode_is_probable(registry: BackendRegistry, tmp_path: Path) -> None:
    write(tmp_path / "x" / "same.txt", "aaaa")
    write(tmp_path / "y" / "same.txt", "bbbb")  # same name and size, different content
    res = run(registry, [(tmp_path / "x", RootRole.NORMAL), (tmp_path / "y", RootRole.NORMAL)], verify=Verify.QUICK)
    assert len(res.groups) == 1 and res.groups[0].confidence is Confidence.PROBABLE
    res = run(registry, [(tmp_path / "x", RootRole.NORMAL), (tmp_path / "y", RootRole.NORMAL)])
    assert res.groups == []


def test_overlapping_roots_refused(registry: BackendRegistry, tree: Path) -> None:
    with pytest.raises(OverlappingRootsError):
        run(registry, [(tree / "a", RootRole.NORMAL), (tree / "a" / "sub", RootRole.NORMAL)])
    os.symlink(tree / "a", tree / "alias")
    with pytest.raises(OverlappingRootsError):
        run(registry, [(tree / "a", RootRole.NORMAL), (tree / "alias", RootRole.NORMAL)])


def test_cache_is_used_on_rescan(registry: BackendRegistry, tree: Path, dirs, old_mtime: float) -> None:  # type: ignore[no-untyped-def]
    for p in tree.rglob("*"):
        if p.is_file():
            os.utime(p, (old_mtime, old_mtime))
    with HashCache(dirs.hash_cache) as cache:
        first = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)], cache=cache)
        assert first.stats.cache_hits == 0
    with HashCache(dirs.hash_cache) as cache:
        second = run(registry, [(tree / "a", RootRole.NORMAL), (tree / "b", RootRole.NORMAL)], cache=cache)
        assert second.stats.bytes_read == 0
        assert second.stats.cache_hits > 0
    assert [rels(g) for g in first.groups] == [rels(g) for g in second.groups]


def test_cache_ignores_fresh_files(dirs) -> None:  # type: ignore[no-untyped-def]
    import time

    with HashCache(dirs.hash_cache) as cache:
        cache.put("local", "/x", "full:blake2b", 1, time.time_ns(), "abc")
        assert cache.get("local", "/x", "full:blake2b", 1, time.time_ns()) is None
        cache.put("local", "/y", "full:blake2b", 1, 1_000, "abc")
        assert cache.get("local", "/y", "full:blake2b", 1, 1_000) == "abc"
        assert cache.get("local", "/y", "full:blake2b", 2, 1_000) is None  # size changed
        assert cache.get("local", "/y", "full:blake2b", 1, 2_000) is None  # mtime changed


def test_file_changing_size_mid_scan_is_reported(registry: BackendRegistry, tmp_path: Path) -> None:
    write(tmp_path / "s" / "a.bin", rand_bytes(300_000, 5))
    write(tmp_path / "s" / "b.bin", rand_bytes(300_000, 5))
    locs = [(registry.open(str(tmp_path / "s")), RootRole.NORMAL)]
    inv = scan(make_roots(locs), ScanFilter())
    # Grow one file after it was listed.
    with open(tmp_path / "s" / "b.bin", "ab") as fh:
        fh.write(b"more")
    res = compare(inv, ScanOptions(use_sampling=False))
    assert res.groups == []
    assert any("expected" in err for _, err in res.stats.errors)


def test_progress_reports_stages(registry: BackendRegistry, tree: Path) -> None:
    progress = RecordingProgress()
    locs = [(registry.open(str(tree / "a")), RootRole.NORMAL), (registry.open(str(tree / "b")), RootRole.NORMAL)]
    inv = scan(make_roots(locs), ScanFilter(), progress=progress)
    compare(inv, ScanOptions(), progress=progress)
    assert progress.stages[0] == "Discovering files"
    assert "Hashing contents" in progress.stages


def test_similar_images(registry: BackendRegistry, tmp_path: Path) -> None:
    PIL = pytest.importorskip("PIL.Image")
    import random

    rng = random.Random(4)
    img = PIL.new("RGB", (640, 480))
    pixels = img.load()
    for x in range(0, 640, 16):
        for y in range(0, 480, 16):
            colour = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for dx in range(16):
                for dy in range(16):
                    pixels[x + dx, y + dy] = colour  # type: ignore[index]
    (tmp_path / "p").mkdir()
    img.save(tmp_path / "p" / "orig.png")
    img.resize((320, 240)).save(tmp_path / "p" / "small.jpg", quality=80)
    other = PIL.new("RGB", (640, 480), (10, 200, 30))
    for x in range(640):
        other.putpixel((x, x % 480), (255, 255, 255))
    other.save(tmp_path / "p" / "other.png")
    res = run(registry, [(tmp_path / "p", RootRole.NORMAL)], similar_images=True)
    assert len(res.similar) == 1
    names = sorted(m.rel for m in res.similar[0].members)
    assert names == ["orig.png", "small.jpg"]
    assert res.similar[0].members[0].rel == "orig.png"  # largest image represents the group
