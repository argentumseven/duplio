"""Remembering folder listings for the folder browser."""

from __future__ import annotations

from pathlib import Path

from duplio.vfs.base import Capabilities, MediaKind
from duplio.vfs.listing import ListingCache
from duplio.vfs.local import LocalBackend


class SlowDrive(LocalBackend):
    def __init__(self, kind: MediaKind = MediaKind.NETWORK) -> None:
        super().__init__()
        self.kind = kind
        self.calls = 0

    @property
    def id(self) -> str:
        return f"test-{self.kind.value}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(kind=self.kind, label="Test drive")

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        self.calls += 1
        return super().listdir(path)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_slow_drive_is_asked_once_within_the_time_limit(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_text("x")
    clock = Clock()
    cache = ListingCache(ttl=60, clock=clock)
    drive = SlowDrive()
    first = cache.list(drive, str(tmp_path))
    clock.now += 30
    assert cache.list(drive, str(tmp_path)) == first and drive.calls == 1 and cache.hits == 1
    clock.now += 31  # now older than the limit
    cache.list(drive, str(tmp_path))
    assert drive.calls == 2


def test_refresh_and_invalidate_ask_the_drive_again(tmp_path: Path) -> None:
    cache = ListingCache(ttl=60)
    drive = SlowDrive()
    cache.list(drive, str(tmp_path))
    (tmp_path / "new").mkdir()
    assert ("new", True) not in cache.list(drive, str(tmp_path))  # still the remembered listing
    assert ("new", True) in cache.list(drive, str(tmp_path), refresh=True)
    (tmp_path / "newer").mkdir()
    cache.invalidate(drive, str(tmp_path))
    assert ("newer", True) in cache.list(drive, str(tmp_path))
    cache.invalidate()
    assert len(cache) == 0


def test_fast_local_disks_are_never_remembered(tmp_path: Path) -> None:
    cache = ListingCache(ttl=60)
    drive = SlowDrive(MediaKind.LOCAL)
    cache.list(drive, str(tmp_path))
    (tmp_path / "x").mkdir()
    assert ("x", True) in cache.list(drive, str(tmp_path)) and drive.calls == 2 and len(cache) == 0


def test_a_returned_listing_cannot_change_what_is_remembered(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    cache = ListingCache(ttl=60)
    drive = SlowDrive()
    cache.list(drive, str(tmp_path)).append(("junk", True))
    assert ("junk", True) not in cache.list(drive, str(tmp_path))


def test_the_cache_stays_small(tmp_path: Path) -> None:
    cache = ListingCache(ttl=60, max_folders=3)
    drive = SlowDrive()
    for n in range(6):
        (tmp_path / str(n)).mkdir()
        cache.list(drive, str(tmp_path / str(n)))
    assert len(cache) == 3
