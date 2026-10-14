"""The picture buffer as two tiers: pictures are held in memory first, go on to the disk buffer only when memory is full, and the
disk buffer lets its oldest go when it is full. A picture is in one tier, not both. Nothing here needs Tk."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from duplio.gui.memory import MIB
from duplio.gui.thumbs import Thumbnailer
from duplio.preview.memory import MemoryTier
from duplio.preview.service import PreviewItem, PreviewService
from duplio.preview.settings import PreviewSettings
from duplio.preview.store import StoredThumb
from duplio.util.appdirs import AppDirs
from duplio.vfs.base import MediaKind

from .previewkit import photo_with_exif_thumbnail
from .test_preview import CountingBackend, item_for


def thumb(n: int, size: int = 1000) -> StoredThumb:
    return StoredThumb(bytes([n % 256]) * size, "jpg", 10, 10, 1, "embedded", None)


def key(n: int) -> str:
    return f"{n:02d}" + "a" * 30


# --------------------------------------------------------------------------- the memory tier
def test_memory_holds_pictures_and_lets_the_least_recently_used_go_first() -> None:
    tier = MemoryTier(max_bytes=5000)
    for n in range(4):
        assert tier.put(key(n), "d", thumb(n), demote=True) == []
    assert tier.get(key(0)) is not None  # looking at the oldest makes it the newest
    out = tier.put(key(4), "d", thumb(4), demote=True)
    assert [o.key for o in out] == [key(1), key(2)]  # the two nobody looked at, down to a little under the limit
    assert all(o.demote for o in out)
    assert tier.has(key(0)) and tier.has(key(4)) and not tier.has(key(1))
    assert tier.total_bytes <= 5000 and tier.count == 3


def test_a_picture_that_is_not_to_be_kept_on_disk_is_marked_so_when_it_leaves() -> None:
    tier = MemoryTier(max_bytes=1500)
    tier.put(key(0), "d", thumb(0), demote=False)
    out = tier.put(key(1), "d", thumb(1), demote=False)
    assert [o.demote for o in out] == [False]


def test_the_folder_on_screen_stays_and_the_newcomer_moves_on_instead() -> None:
    tier = MemoryTier(max_bytes=3700)  # room for three of these (1000 bytes and a little for the bookkeeping)
    tier.pin({key(n) for n in range(10)})
    held = [tier.put(key(n), "d", thumb(n), demote=True) for n in range(3)]
    assert held == [[], [], []]
    out = tier.put(key(3), "d", thumb(3), demote=True)  # memory is full of the folder on screen
    assert [o.key for o in out] == [key(3)]  # so this one goes on to the disk, and the nearer ones stay in memory
    assert all(tier.has(key(n)) for n in range(3)) and not tier.has(key(3))


def test_a_picture_of_another_folder_is_let_go_before_any_of_the_folder_on_screen() -> None:
    tier = MemoryTier(max_bytes=3700)
    tier.put("zz" + "b" * 30, "other", thumb(9), demote=True)  # looked at before, not on screen now
    tier.pin({key(n) for n in range(10)})
    tier.put(key(0), "d", thumb(0), demote=True)
    tier.put(key(1), "d", thumb(1), demote=True)
    out = tier.put(key(2), "d", thumb(2), demote=True)
    assert [o.key for o in out] == ["zz" + "b" * 30]  # the old one made room; none of the folder on screen went
    assert all(tier.has(key(n)) for n in range(3))


def test_lowering_the_limit_pushes_out_what_no_longer_fits_even_from_the_folder_on_screen() -> None:
    tier = MemoryTier(max_bytes=10_000)
    tier.pin({key(n) for n in range(6)})
    for n in range(6):
        tier.put(key(n), "d", thumb(n), demote=True)
    out = tier.set_limit(2500)
    assert len(out) >= 3 and tier.total_bytes <= 2500
    assert tier.set_limit(10_000) == []  # raising it again pushes out nothing


def test_taking_everything_hands_back_the_newest_first_and_clear_forgets() -> None:
    tier = MemoryTier(max_bytes=10_000)
    for n in range(3):
        tier.put(key(n), "d", thumb(n), demote=True)
    assert [o.key for o in tier.take_all()] == [key(2), key(1), key(0)] and tier.count == 0 and tier.total_bytes == 0
    tier.put(key(5), "d", thumb(5), demote=True)
    assert tier.clear() == 1 and tier.count == 0


# --------------------------------------------------------------------------- memory, then disk, then trimming
@pytest.fixture
def dirs(tmp_path: Path) -> AppDirs:
    return AppDirs(tmp_path / "cfg", tmp_path / "state", tmp_path / "cache").ensure()


def usb_items(tmp_path: Path, count: int, kind: MediaKind = MediaKind.REMOVABLE) -> list[PreviewItem]:
    pytest.importorskip("PIL")
    backend = CountingBackend(kind)
    for n in range(count):
        photo_with_exif_thumbnail(tmp_path / "d" / f"IMG_{n:03d}.jpg", seed=n, padding=2000)
    return [item_for(backend, tmp_path / "d" / f"IMG_{n:03d}.jpg") for n in range(count)]


def tiers(service: PreviewService, item: PreviewItem) -> tuple[bool, bool]:
    store = service.buffer_for(item)
    assert store is not None
    return store.hot.has(item.key), store.warm.has(item.key)


def test_pictures_fill_memory_first_then_the_disk_and_the_oldest_are_trimmed_when_that_is_full(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 30)
    service = PreviewService(dirs, PreviewSettings(max_bytes=12_000))
    sizes = [len(service.get(i).thumb.data) for i in items[:1]]
    each = sizes[0] + 200
    service.set_memory_limit(each * 4 + each // 2)  # memory has room for four pictures
    service.clear_memory()
    service.clear_disk()
    # while there is room, everything stays in memory and the disk is not touched
    for item in items[:4]:
        service.get(item)
    assert service.held()[0] == 4 and service.stored() == (0, 0)
    # the next ones do not fit in memory: the ones looked at longest ago move on to the disk, nothing is in two places
    for item in items[4:12]:
        service.get(item)
    held, _ = service.held()
    stored, size = service.stored()
    assert held <= 4 and stored >= 4 and size <= 12_000
    for item in items[:12]:
        in_memory, on_disk = tiers(service, item)
        assert not (in_memory and on_disk), "a picture must be in one tier, not both"
    # a lot more: the disk overruns and its oldest are trimmed to make room
    for item in items[12:]:
        service.get(item)
    assert service.stored()[1] <= 12_000
    assert service.cached(items[-1]) and not service.cached(items[0])  # the newest are at hand, the oldest were let go
    service.close()


def test_the_pictures_nearest_the_viewer_stay_in_memory_while_later_ones_go_to_disk(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 20)
    service = PreviewService(dirs)
    first = len(service.get(items[0]).thumb.data) + 200
    service.clear_memory()
    service.set_memory_limit(first * 5 + first // 2)
    service.pin(items)  # the folder on screen
    for item in items:  # fetched from the viewer onward, as the background fetch does
        service.get(item)
    in_memory = [tiers(service, i)[0] for i in items]
    assert in_memory[:5] == [True] * 5 and not any(in_memory[5:])  # the first five stay; the rest are on disk
    assert all(tiers(service, i)[1] for i in items[5:])
    service.close()


def test_what_is_only_in_memory_is_moved_to_disk_when_the_program_closes_and_found_next_time(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 6)
    service = PreviewService(dirs)
    for item in items:
        service.get(item)
    assert service.held()[0] == 6 and service.stored() == (0, 0)
    service.close()
    backend = items[0].backend
    reads = backend.bytes_read  # type: ignore[attr-defined]
    again = PreviewService(dirs)
    assert all(again.get(i).from_cache for i in items) and backend.bytes_read == reads  # type: ignore[attr-defined]
    again.close()


def test_a_temporary_buffer_is_not_filled_on_close(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 3)
    service = PreviewService(dirs, PreviewSettings(keep="session"))
    for item in items:
        service.get(item)
    service.close()
    assert not (dirs.cache / "previews").exists()


def test_with_keeping_off_nothing_is_held_anywhere(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 2)
    service = PreviewService(dirs, PreviewSettings(mode="off"))
    service.get(items[0])
    service.get(items[0])
    assert service.stats.fetched == 2 and service.stats.hits == 0 and service.held() == (0, 0) and not service.cached(items[0])
    service.close()


def test_the_memory_buffer_and_the_disk_buffer_are_emptied_independently(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 10)
    service = PreviewService(dirs)
    one = len(service.get(items[0]).thumb.data) + 200
    service.clear_memory()
    service.set_memory_limit(one * 3 + one // 2)
    for item in items:
        service.get(item)
    held, _ = service.held()
    stored, _ = service.stored()
    assert held > 0 and stored > 0 and held + stored == 10
    assert service.clear_memory() == held and service.held() == (0, 0) and service.stored()[0] == stored  # the disk was left alone
    for item in items:
        service.get(item)  # (what was in memory is fetched again)
    held, _ = service.held()
    stored = service.stored()[0]
    assert service.clear_disk() == stored and service.stored() == (0, 0) and service.held()[0] == held  # memory was left alone
    service.close()


def test_a_full_disk_buffer_while_memory_overflows_loses_no_picture_that_was_just_fetched(tmp_path: Path, dirs: AppDirs) -> None:
    items = usb_items(tmp_path, 3)
    service = PreviewService(dirs)
    service.set_memory_limit(0)
    store = service.buffer_for(items[0])
    assert store is not None

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    store.warm.put = broken  # type: ignore[method-assign]
    assert all(service.get(i).thumb.data for i in items) and service.stats.not_kept == 3
    service.close()


# --------------------------------------------------------------------------- how the window shares its memory
class Master:
    def after(self, _ms: int, _fn: object) -> None:
        pass


class FakeService:
    def __init__(self) -> None:
        self.limit: int | None = None

    def set_memory_limit(self, nbytes: int) -> None:
        self.limit = nbytes

    def held(self) -> tuple[int, int]:
        return 7, 12345


def test_the_memory_budget_is_shared_between_the_images_drawn_and_the_pictures_held() -> None:
    service = FakeService()
    thumbs = Thumbnailer(Master(), service, workers=1, image_budget=1024 * MIB)  # type: ignore[arg-type]
    try:
        assert thumbs.budget == 1024 * MIB and service.limit == 768 * MIB  # a quarter for images, the rest for pictures held
        thumbs.set_budget(4096 * MIB)
        assert service.limit == 4096 * MIB - 512 * MIB  # images never need more than 512 MB
        stats = thumbs.stats()
        assert stats["budget"] == 4096 * MIB and stats["held"] == 7 and stats["bytes"] == stats["image_bytes"] + 12345
        free = {"now": 100 * MIB}
        thumbs._memory = lambda: free["now"]
        assert thumbs.check_memory(force=True) is True and thumbs.budget == 1024 * MIB  # a quarter of what was chosen
        assert service.limit == 1024 * MIB - 256 * MIB  # and the pictures held shrink with it (they move on to the disk)
    finally:
        thumbs.close()


def test_emptying_the_memory_buffer_empties_images_and_held_pictures() -> None:
    class Service(FakeService):
        cleared = 0

        def clear_memory(self) -> int:
            self.cleared += 1
            return 7

    service = Service()
    thumbs = Thumbnailer(Master(), service, workers=1)  # type: ignore[arg-type]
    try:
        thumbs._images[("k", 60)] = ("image", 100)
        thumbs._image_bytes = 100
        assert thumbs.clear_memory() == 7 and service.cleared == 1 and thumbs.stats()["images"] == 0
    finally:
        thumbs.close()


def test_time_is_not_what_decides_which_tier(tmp_path: Path, dirs: AppDirs) -> None:
    """Pictures made long ago in memory are still in memory: only room decides, not age."""
    items = usb_items(tmp_path, 3)
    service = PreviewService(dirs)
    service.get(items[0])
    time.sleep(0.05)
    service.get(items[1])
    assert tiers(service, items[0])[0] and tiers(service, items[1])[0]
    service.close()
