"""The picture buffer behind folder browsing: the on-disk store, one fetch per picture, the background fetcher and its
failsafes, and the memory budget of the window's pictures. Nothing here needs Tk."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from duplio.gui.memory import MIB
from duplio.gui.model import Tile
from duplio.gui.prefetch import FolderPrefetcher
from duplio.gui.thumbs import Thumbnailer
from duplio.preview.service import PreviewItem, PreviewService
from duplio.preview.settings import PreviewSettings
from duplio.preview.store import StoredThumb, ThumbStore
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

OLD = 1_600_000_000


def thumb(n: int, size: int = 1000) -> StoredThumb:
    return StoredThumb(bytes([n % 256]) * size, "jpg", 10, 10, 1, "embedded", None)


def wait_until(condition: Any, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


# --------------------------------------------------------------------------- the store
def test_the_store_trims_least_recently_used_first_and_keeps_exact_totals(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=5500, persistent=True)
    for n in range(5):
        store.put(f"k{n:02d}" + "0" * 29, "d", thumb(n))
    assert (store.count, store.total_bytes) == (5, 5000)
    assert store.get("k00" + "0" * 29) is not None  # looking at the oldest makes it the newest
    for n in range(5, 8):
        store.put(f"k{n:02d}" + "0" * 29, "d", thumb(n))
    assert store.total_bytes <= 5500 and store.count == store.stats().count
    assert store.has("k00" + "0" * 29) and store.has("k07" + "0" * 29)  # the one just looked at, and the newest, stay
    assert not store.has("k01" + "0" * 29)  # the one nobody looked at goes
    assert store.total_bytes == sum(len(store.get(f"k{n:02d}" + "0" * 29).data) for n in range(8) if store.has(f"k{n:02d}" + "0" * 29))  # type: ignore[union-attr]
    store.close()


def test_pictures_of_the_folder_on_screen_are_trimmed_last_but_the_limit_always_holds(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=4500, persistent=True)
    keys = [f"{n:02d}" + "a" * 30 for n in range(4)]
    for n, key in enumerate(keys):
        store.put(key, "d", thumb(n))
    store.pin(set(keys[:2]))  # the old ones are the folder being looked at
    for n in range(4, 8):
        store.put(f"{n:02d}" + "b" * 30, "d", thumb(n))
    assert store.has(keys[0]) and store.has(keys[1])  # kept although they are the least recently used
    assert store.total_bytes <= 4500
    store.pin(set(keys) | {f"{n:02d}" + "b" * 30 for n in range(4, 8)})  # now everything is pinned and there is too much
    for n in range(8, 14):
        store.put(f"{n:02d}" + "c" * 30, "d", thumb(n))
    assert store.total_bytes <= 4500  # the limit holds even when everything is pinned
    store.close()


def test_use_times_are_written_in_batches_not_per_picture(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=10**6, persistent=True)
    key = "ab" + "0" * 30
    store.put(key, "d", thumb(1))
    before = store._db.execute("SELECT used FROM thumbs WHERE key=?", (key,)).fetchone()[0]
    time.sleep(0.02)
    for _ in range(20):
        store.get(key)
    assert store._db.execute("SELECT used FROM thumbs WHERE key=?", (key,)).fetchone()[0] == before  # nothing written yet
    store.stats()
    store.close()
    again = sqlite3.connect(tmp_path / "s" / "index.sqlite3")
    assert again.execute("SELECT used FROM thumbs WHERE key=?", (key,)).fetchone()[0] > before  # written on the way out
    again.close()


def test_totals_survive_a_restart_and_a_damaged_index_is_started_again(tmp_path: Path) -> None:
    root = tmp_path / "s"
    store = ThumbStore(root, max_bytes=10**6, persistent=True)
    for n in range(3):
        store.put(f"{n:02d}" + "d" * 30, "d", thumb(n))
    store.close()
    again = ThumbStore(root, max_bytes=10**6, persistent=True)
    assert (again.count, again.total_bytes) == (3, 3000)
    again.close()
    (root / "index.sqlite3").write_bytes(b"this is not a database" * 50)
    for leftover in root.glob("*-wal"):
        leftover.unlink()
    healed = ThumbStore(root, max_bytes=10**6, persistent=True)  # a cache that cannot be read is only a cache that was lost
    assert (healed.count, healed.total_bytes) == (0, 0)
    healed.put("00" + "e" * 30, "d", thumb(1))
    assert healed.get("00" + "e" * 30) is not None
    healed.close()


def test_a_picture_file_that_vanished_is_forgotten_and_the_totals_follow(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=10**6, persistent=True)
    key = "cd" + "0" * 30
    store.put(key, "d", thumb(1))
    (tmp_path / "s" / "cd" / f"{key}.jpg").unlink()
    assert store.get(key) is None and (store.count, store.total_bytes) == (0, 0)
    store.close()


# --------------------------------------------------------------------------- pictures to show
@pytest.fixture
def folder(tmp_path: Path) -> tuple[Path, list[PreviewItem], BackendRegistry]:
    pytest.importorskip("PIL")
    from .previewkit import photo_with_exif_thumbnail

    root = tmp_path / "pics"
    for n in range(30):
        photo_with_exif_thumbnail(root / f"IMG_{n:03d}.jpg", seed=n, padding=2000)
        import os

        os.utime(root / f"IMG_{n:03d}.jpg", (OLD + n, OLD + n))
    reg = BackendRegistry()
    roots = make_roots([(reg.open(str(root)), RootRole.NORMAL)])
    inventory = scan(roots, ScanFilter(min_size=0))
    items = [Tile(0, e, inventory.roots[0]).preview_item for e in sorted(inventory.entries, key=lambda e: e.rel)]
    assert all(items) and len(items) == 30
    return root, [i for i in items if i is not None], reg


def service(**changes: Any) -> PreviewService:
    return PreviewService(None, PreviewSettings(mode="always", keep="session", **changes))


def test_two_requests_for_one_picture_read_the_drive_once(folder: Any) -> None:
    _root, items, reg = folder
    svc = service()
    calls: list[str] = []
    real = svc._produce

    def slow(item: PreviewItem, ext: str, cancel: Any, allow: bool) -> Any:
        calls.append(item.name)
        time.sleep(0.3)
        return real(item, ext, cancel, allow)

    svc._produce = slow  # type: ignore[method-assign]
    results: list[Any] = []
    threads = [threading.Thread(target=lambda: results.append(svc.get(items[0]))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 5 and calls == [items[0].name]  # one read, five answers
    assert svc.stats.fetched == 1 and svc.stats.hits == 4
    svc.close()
    reg.close()


def test_a_full_disk_does_not_lose_a_picture_that_was_just_fetched(folder: Any) -> None:
    _root, items, reg = folder
    svc = service()
    svc.set_memory_limit(0)  # nothing stays in memory, so the picture goes straight on to the disk buffer
    store = svc.buffer_for(items[0])
    assert store is not None

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    store.warm.put = broken  # type: ignore[method-assign]
    got = svc.get(items[0])
    assert got.thumb.data and svc.stats.not_kept == 1
    svc.close()
    reg.close()


# --------------------------------------------------------------------------- filling the buffer in the background
def finished(pre: FolderPrefetcher) -> bool:
    return pre.status().state in ("finished", "stopped")


def test_a_folder_is_fetched_from_the_viewer_onward_then_back_round_and_then_it_is_all_at_hand(folder: Any) -> None:
    _root, items, reg = folder
    svc = service()
    order: list[str] = []
    real = svc.get

    def logged(item: PreviewItem, **kw: Any) -> Any:
        order.append(item.name)
        return real(item, **kw)

    svc.get = logged  # type: ignore[method-assign]
    pre = FolderPrefetcher(svc, workers=1)
    pre.start(items, first=20)
    assert wait_until(lambda: finished(pre))
    status = pre.status()
    assert status.state == "finished" and status.fetched == 30 and status.ready == 30 and status.failed == 0
    # from where the viewer is, on to the end of the folder, and then back to the part that was skipped
    assert order == [f"IMG_{n:03d}.jpg" for n in (*range(20, 30), *range(0, 20))]
    assert all(svc.cached(i) for i in items)
    again = FolderPrefetcher(svc, workers=2)  # a second visit finds everything already there
    again.start(items)
    assert wait_until(lambda: finished(again))
    assert again.status().had == 30 and again.status().fetched == 0
    pre.close()
    again.close()
    svc.close()
    reg.close()


def test_when_this_folder_alone_fills_the_buffer_the_fetch_stops_instead_of_throwing_its_pictures_away(folder: Any) -> None:
    _root, items, reg = folder
    svc = service(max_bytes=40_000)  # room for only some of the 30 pictures
    svc.set_memory_limit(10_000)  # (a little in memory, the rest of the room on disk)
    svc.pin(items)  # as the Browse page does: these are the pictures on screen
    pre = FolderPrefetcher(svc, workers=1)
    pre.start(items)
    assert wait_until(lambda: finished(pre))
    status = pre.status()
    store = svc.buffer_for(items[0])
    assert store is not None
    assert status.state == "stopped" and "fill the picture buffer" in status.reason and "Settings" in status.reason
    assert 0 < status.fetched < 30
    assert store.count == status.fetched  # nothing was evicted to make room: what was fetched is still there
    pre.close()
    svc.close()
    reg.close()


def test_a_buffer_full_of_other_folders_makes_room_instead_of_stopping(folder: Any) -> None:
    _root, items, reg = folder
    svc = service(max_bytes=400_000)
    svc.set_memory_limit(10_000)
    store = svc.buffer_for(items[0])
    assert store is not None
    old_keys = [f"{n:02d}" + "b" * 30 for n in range(380)]
    for key in old_keys:
        store.warm.put(key, "other", thumb(1, 1000))  # an old, big folder fills the disk buffer
    assert store.fullness > 0.9
    svc.pin(items)
    pre = FolderPrefetcher(svc, workers=1)
    pre.start(items)
    assert wait_until(lambda: finished(pre))
    assert pre.status().state == "finished" and pre.status().fetched == 30
    assert all(svc.cached(i) for i in items)  # every picture of this folder is there
    assert sum(1 for key in old_keys if store.warm.has(key)) < 380  # at the old folder's cost
    pre.close()
    svc.close()
    reg.close()


def test_pictures_nobody_looked_at_for_a_long_while_are_let_go_when_the_store_opens(tmp_path: Path) -> None:
    store = ThumbStore(tmp_path / "s", max_bytes=10**9, persistent=True)
    for n in range(6):
        store.put(f"k{n:02d}" + "0" * 29, "d", thumb(n))
    store.close()
    db = sqlite3.connect(tmp_path / "s" / "index.sqlite3")
    db.execute("UPDATE thumbs SET used = ? WHERE key < 'k03'", (time.time() - 90 * 86400,))  # three not looked at for 90 days
    db.commit()
    db.close()
    again = ThumbStore(tmp_path / "s", max_bytes=10**9, persistent=True)
    assert again.expire(60) == 3 and again.count == 3 and not again.has("k00" + "0" * 29) and again.has("k05" + "0" * 29)
    assert again.expire(0) == 0  # 0 means never
    again.close()


class FakeStore:
    max_bytes = 1000

    def __init__(self, fullness: float = 0.1, free: int | None = 10 * 1024**3) -> None:
        self.fullness, self._free = fullness, free

    count = 10

    @property
    def total_bytes(self) -> int:
        return int(self.fullness * self.max_bytes)

    def pinned_bytes(self) -> int:
        return self.total_bytes  # the whole of it is this folder's

    def free_disk(self) -> int | None:
        return self._free


class FakePreviews:
    def __init__(self, store: Any, behaviour: Any = None) -> None:
        self.store, self.behaviour, self.calls = store, behaviour, 0

    def buffer_for(self, item: Any) -> Any:
        return self.store

    def cached(self, item: Any) -> bool:
        return False

    def get(self, item: Any, **kw: Any) -> Any:
        self.calls += 1
        if self.behaviour:
            return self.behaviour(item)
        return None


def fake_items(n: int = 10) -> list[PreviewItem]:
    reg = BackendRegistry()
    backend = reg.open("/tmp").backend
    return [PreviewItem(backend, f"/tmp/p{i}.jpg", 10, 1, f"p{i}.jpg") for i in range(n)]


def test_an_almost_full_disk_stops_the_fetch() -> None:
    previews = FakePreviews(FakeStore(free=100 * 1024 * 1024))
    pre = FolderPrefetcher(previews, workers=1)
    pre.start(fake_items())
    assert wait_until(lambda: finished(pre))
    assert pre.status().state == "stopped" and "almost full" in pre.status().reason and previews.calls == 0
    pre.close()


def test_a_drive_that_stops_answering_stops_the_fetch_with_a_reason() -> None:
    def fail(item: Any) -> None:
        raise OSError("connection reset")

    previews = FakePreviews(FakeStore(), fail)
    pre = FolderPrefetcher(previews, workers=1, max_failures_in_a_row=5)
    pre.start(fake_items(30))
    assert wait_until(lambda: finished(pre))
    status = pre.status()
    assert status.state == "stopped" and "stopped answering" in status.reason and status.failed == 5
    assert previews.calls == 5  # it did not keep hammering a drive that is gone
    pre.close()


def test_cloud_only_files_are_skipped_not_downloaded(folder: Any) -> None:
    _root, items, reg = folder
    cloud = [PreviewItem(i.backend, i.path, i.size, i.mtime_ns, i.name, offline=True) for i in items[:6]]
    svc = service()
    pre = FolderPrefetcher(svc, workers=1)
    pre.start(cloud)
    assert wait_until(lambda: finished(pre))
    assert pre.status().skipped == 6 and pre.status().fetched == 0 and svc.stats.bytes_read == 0
    pre.start(cloud, allow_online_only=True)  # unless the person asks for them
    assert wait_until(lambda: pre.status().state == "finished" and pre.status().fetched == 6)
    pre.close()
    svc.close()
    reg.close()


def test_pause_resume_stop_and_a_new_folder_replaces_the_old_run() -> None:
    gate = threading.Event()

    def slow(item: Any) -> None:
        gate.wait(5)

    previews = FakePreviews(FakeStore(), slow)
    pre = FolderPrefetcher(previews, workers=1)
    pre.start(fake_items(6))
    assert wait_until(lambda: previews.calls == 1)
    pre.pause()
    gate.set()
    assert wait_until(lambda: pre.status().handled == 1)
    time.sleep(0.1)
    assert pre.status().state == "paused" and previews.calls == 1  # paused: nothing new starts
    pre.resume()
    assert wait_until(lambda: pre.status().state == "finished") and previews.calls == 6
    pre.start(fake_items(4))
    assert wait_until(lambda: pre.status().state == "finished")
    assert pre.status().total == 4 and pre.status().handled == 4  # counts belong to this folder only
    pre.stop("closed the folder")
    assert pre.status().state == "finished"  # nothing was running, so nothing to stop
    pre.close()


def test_moving_the_viewer_reorders_what_is_left() -> None:
    gate = threading.Event()
    seen: list[str] = []

    def hold(item: Any) -> None:
        seen.append(item.name)
        gate.wait(5)

    previews = FakePreviews(FakeStore(), hold)
    pre = FolderPrefetcher(previews, workers=1)
    pre.start(fake_items(20), first=0)
    assert wait_until(lambda: len(seen) == 1)  # busy with p0
    pre.move_to(15)
    gate.set()
    assert wait_until(lambda: pre.status().state == "finished")
    # from the new position on to the end, and then back to what was left behind
    assert seen[1:] == [f"p{n}.jpg" for n in (*range(15, 20), *range(1, 15))]
    pre.close()


# --------------------------------------------------------------------------- the memory the window's pictures may use
class Master:
    def after(self, _ms: int, _fn: object) -> None:
        pass  # the tests call pump() themselves


@pytest.fixture
def pictures(folder: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    root, _items, reg = folder
    monkeypatch.setattr(Thumbnailer, "_make_image", lambda self, ppm: ("image", len(ppm)))
    svc = service()
    tiles = []
    inventory = scan(make_roots([(reg.open(str(root)), RootRole.NORMAL)]), ScanFilter(min_size=0))
    for e in sorted(inventory.entries, key=lambda e: e.rel):
        tiles.append(Tile(0, e, inventory.roots[0]))
    yield svc, tiles
    svc.close()
    reg.close()


def request_all(thumbs: Thumbnailer, tiles: list[Tile], box: int = 60, **kw: Any) -> list[tuple[str, Any, str]]:
    got: list[tuple[str, Any, str]] = []
    for tile in tiles:
        thumbs.request(tile, box, lambda key, image, why: got.append((key, image, why)), **kw)
    assert wait_until(lambda: (thumbs.pump() or True) and len(got) == len(tiles))
    return got


def test_finished_pictures_are_kept_up_to_a_byte_budget_and_come_back_from_the_buffer_not_the_drive(pictures: Any) -> None:
    svc, tiles = pictures
    thumbs = Thumbnailer(Master(), svc, workers=2, image_budget=60 * 1024)  # room for a handful, not thirty
    try:
        request_all(thumbs, tiles)
        stats = thumbs.stats()
        assert 0 < stats["images"] < 30 and stats["bytes"] <= stats["budget"]
        fetched = svc.stats.fetched
        request_all(thumbs, tiles)  # all thirty again: the ones that went are made again from the buffer
        assert svc.stats.fetched == fetched  # ... and the drive was not asked again for any of them
        assert thumbs.stats()["bytes"] <= stats["budget"]
    finally:
        thumbs.close()


def test_the_picture_budget_shrinks_when_the_computer_is_short_of_memory_and_comes_back(pictures: Any) -> None:
    svc, _tiles = pictures
    free = {"now": 4096 * MIB}
    thumbs = Thumbnailer(Master(), svc, workers=1, image_budget=512 * MIB, memory=lambda: free["now"])
    try:
        assert thumbs.check_memory(force=True) is False and thumbs.budget == 512 * MIB
        free["now"] = 100 * MIB
        assert thumbs.check_memory(force=True) is True and thumbs.budget == 128 * MIB and thumbs.stats()["low_memory"]
        free["now"] = 600 * MIB  # better, but not yet comfortable: stays small (no flapping)
        assert thumbs.check_memory(force=True) is True
        free["now"] = 2048 * MIB
        assert thumbs.check_memory(force=True) is False and thumbs.budget == 512 * MIB
        thumbs.set_budget(1)  # a nonsense budget still leaves the floor
        assert thumbs.budget >= 1
    finally:
        thumbs.close()


def test_scrolling_cannot_pile_up_unbounded_work(pictures: Any) -> None:
    svc, tiles = pictures

    class Tight(Thumbnailer):
        STACK_LIMIT = 5

    gate = threading.Event()
    real = svc.get

    def held(item: PreviewItem, **kw: Any) -> Any:
        gate.wait(5)
        return real(item, **kw)

    svc.get = held  # type: ignore[method-assign]
    thumbs = Tight(Master(), svc, workers=1)
    try:
        got: list[Any] = []
        for tile in tiles:
            thumbs.request(tile, 60, lambda key, image, why: got.append(key))
        assert thumbs.dropped >= 30 - 5 - 1  # the one a worker holds, the five waiting; the rest were long out of sight
        gate.set()
        assert wait_until(lambda: (thumbs.pump() or True) and thumbs._outstanding == 0)
        assert len(got) <= 6
    finally:
        gate.set()
        thumbs.close()


def test_read_ahead_waits_for_what_is_on_screen(pictures: Any) -> None:
    svc, tiles = pictures
    order: list[str] = []
    gate = threading.Event()
    real = svc.get

    def logged(item: PreviewItem, **kw: Any) -> Any:
        order.append(item.name)
        if len(order) == 1:
            gate.wait(5)
        return real(item, **kw)

    svc.get = logged  # type: ignore[method-assign]
    thumbs = Thumbnailer(Master(), svc, workers=1)
    try:
        got: list[Any] = []
        ready = lambda key, image, why: got.append(key)  # noqa: E731
        thumbs.request(tiles[0], 60, ready)  # a worker takes it and waits at the gate
        assert wait_until(lambda: len(order) == 1)
        for tile in tiles[10:14]:
            thumbs.request(tile, 60, ready, background=True)  # read-ahead, queued first
        for tile in tiles[1:3]:
            thumbs.request(tile, 60, ready)  # what the person is looking at, queued later
        gate.set()
        assert wait_until(lambda: (thumbs.pump() or True) and len(got) == 7)
        assert order[1:3] == [tiles[2].name, tiles[1].name]  # the visible ones first (newest first), then the read-ahead
        assert set(order[3:]) == {t.name for t in tiles[10:14]}
    finally:
        gate.set()
        thumbs.close()
