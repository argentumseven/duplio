"""Looking inside a folder: reading it (local and remote), putting what is shown in order, and remembering listings briefly."""

from __future__ import annotations

from pathlib import Path

import pytest

from duplio.gui.listing import BrowseCache, items_of, read_folder
from duplio.gui.sorting import BROWSE_FIELDS, BrowseItem, natural_key, sort_browse, type_of
from duplio.vfs.base import MediaKind
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

OLD = 1_600_000_000


@pytest.fixture
def reg():  # type: ignore[no-untyped-def]
    with BackendRegistry() as registry:
        yield registry


def make_folder(root: Path) -> None:
    write(root / "IMG_2.jpg", rand_bytes(300, 1), mtime=OLD + 20)
    write(root / "IMG_10.jpg", rand_bytes(100, 2), mtime=OLD + 10)
    write(root / "IMG_1.JPG", rand_bytes(200, 3), mtime=OLD + 30)
    write(root / "notes.txt", rand_bytes(50, 4), mtime=OLD + 5)
    write(root / "README", rand_bytes(60, 5), mtime=OLD + 1)
    write(root / ".hidden.jpg", rand_bytes(70, 6), mtime=OLD)
    write(root / "sub" / "deep.jpg", rand_bytes(10, 7), mtime=OLD)
    (root / "zebra").mkdir()
    (root / "Alpha").mkdir()
    (root / ".cache").mkdir()


# --------------------------------------------------------------------------- order
def test_names_sort_the_way_a_person_reads_them() -> None:
    names = ["IMG_10.jpg", "img_2.jpg", "IMG_1.jpg", "b.txt", "A.txt", "IMG_2b.jpg"]
    assert sorted(names, key=natural_key) == ["A.txt", "b.txt", "IMG_1.jpg", "img_2.jpg", "IMG_2b.jpg", "IMG_10.jpg"]
    assert type_of("a.JPG") == "jpg" and type_of("README") == "" and type_of(".hidden") == "" and type_of("a.") == ""


def listing_items(root: Path, reg: BackendRegistry, **kw: object) -> list[BrowseItem]:
    loc = reg.open(str(root))
    return items_of(read_folder(loc.backend, loc.path, **kw), loc)  # type: ignore[arg-type]


def test_a_folder_is_read_with_sizes_and_times_and_hidden_things_stay_hidden(tmp_path: Path, reg: BackendRegistry) -> None:
    make_folder(tmp_path / "f")
    items = listing_items(tmp_path / "f", reg)
    assert {i.name for i in items if i.is_dir} == {"sub", "zebra", "Alpha"}  # .cache is hidden
    files = {i.name: i for i in items if not i.is_dir}
    assert set(files) == {"IMG_2.jpg", "IMG_10.jpg", "IMG_1.JPG", "notes.txt", "README"}  # nothing from inside sub/
    assert files["IMG_2.jpg"].size == 300 and files["IMG_2.jpg"].mtime_ns == (OLD + 20) * 10**9
    assert files["IMG_2.jpg"].tile is not None and files["IMG_2.jpg"].tile.picture_key
    assert files["notes.txt"].tile is not None and files["notes.txt"].tile.picture_key == ""  # no picture for a text file
    shown = {i.name for i in listing_items(tmp_path / "f", reg, show_hidden=True)}
    assert {".hidden.jpg", ".cache"} <= shown


def test_the_order_asked_for_is_the_order_shown(tmp_path: Path, reg: BackendRegistry) -> None:
    make_folder(tmp_path / "f")
    items = listing_items(tmp_path / "f", reg)

    def names(field: str, descending: bool = False, folders_first: bool = True) -> list[str]:
        return [i.name for i in sort_browse(items, field, descending, folders_first)]

    assert names("name") == ["Alpha", "sub", "zebra", "IMG_1.JPG", "IMG_2.jpg", "IMG_10.jpg", "notes.txt", "README"]
    assert names("name", True) == ["zebra", "sub", "Alpha", "README", "notes.txt", "IMG_10.jpg", "IMG_2.jpg", "IMG_1.JPG"]
    assert names("size")[3:] == ["notes.txt", "README", "IMG_10.jpg", "IMG_1.JPG", "IMG_2.jpg"]  # 50, 60, 100, 200, 300 bytes
    assert names("modified")[3:] == ["README", "notes.txt", "IMG_10.jpg", "IMG_2.jpg", "IMG_1.JPG"]
    assert names("type")[3:] == ["IMG_1.JPG", "IMG_2.jpg", "IMG_10.jpg", "notes.txt", "README"]  # jpg (case ignored), txt, none last
    assert names("name", folders_first=False)[:3] == ["Alpha", "IMG_1.JPG", "IMG_2.jpg"]  # folders mixed in with the files
    assert names("name") == names("name")  # the same click, the same order
    assert {f for f, _l in BROWSE_FIELDS} == {"name", "modified", "size", "type"}


def test_files_with_no_extension_sort_last_by_type_and_ties_fall_back_to_the_name() -> None:
    items = [BrowseItem(n, False, 10, 5) for n in ("b", "a.png", "a.jpg", "c.jpg", "A")]
    assert [i.name for i in sort_browse(items, "type", False, False)] == ["a.jpg", "c.jpg", "a.png", "A", "b"]
    assert [i.name for i in sort_browse(items, "size", False, False)] == ["A", "a.jpg", "a.png", "b", "c.jpg"]  # all equal: by name


# --------------------------------------------------------------------------- remote and remembered listings
def test_a_remote_folder_is_listed_with_one_request_and_every_size_and_time(tmp_path: Path, reg: BackendRegistry) -> None:
    pytest.importorskip("wsgidav")
    pytest.importorskip("cheroot")
    from .remotekit import DavServer

    with DavServer(tmp_path / "server") as dav:
        make_folder(tmp_path / "server" / "f")
        before = len(dav.app.requests)
        loc = reg.open(dav.url("f"))
        listing = read_folder(loc.backend, loc.path)
        propfinds = [r for r in dav.app.requests[before:] if r[0] == "PROPFIND"]
        assert len(propfinds) == 1  # the whole folder, sizes and times included, in one trip
        items = items_of(listing, loc)
        files = {i.name: i for i in items if not i.is_dir}
        assert files["IMG_2.jpg"].size == 300 and files["IMG_2.jpg"].mtime_ns > 0
        assert {i.name for i in items if i.is_dir} == {"sub", "zebra", "Alpha"}
        cache = BrowseCache(ttl=300)
        assert cache.get(loc.backend, loc.path, False) is None
        cache.put(listing, False)
        assert cache.get(loc.backend, loc.path, False) is listing  # Back and Up need no new trip
        assert cache.get(loc.backend, loc.path, True) is None  # a different view of the folder is a different listing
        cache.forget(loc.backend, loc.path)
        assert cache.get(loc.backend, loc.path, False) is None


def test_a_fast_local_folder_is_never_remembered_and_old_listings_expire(tmp_path: Path, reg: BackendRegistry) -> None:
    make_folder(tmp_path / "f")
    loc = reg.open(str(tmp_path / "f"))
    listing = read_folder(loc.backend, loc.path)
    real = loc.backend.capabilities

    def pretend(**changes: object):  # type: ignore[no-untyped-def]
        loc.backend.capabilities = lambda path: real(path).with_(**changes)  # type: ignore[method-assign,assignment]

    pretend(kind=MediaKind.LOCAL, virtual=False)
    cache = BrowseCache()
    cache.put(listing, False)
    assert cache.get(loc.backend, loc.path, False) is None  # an internal disk is always read fresh
    pretend(virtual=True)  # a cloud drive
    now = [0.0]
    slow = BrowseCache(ttl=10, clock=lambda: now[0])
    slow.put(listing, False)
    assert slow.get(loc.backend, loc.path, False) is listing  # kept for a while ...
    now[0] = 11
    assert slow.get(loc.backend, loc.path, False) is None  # ... then asked again
    bad = read_folder(loc.backend, str(tmp_path / "nowhere"))
    slow.put(bad, False)
    assert slow.get(loc.backend, str(tmp_path / "nowhere"), False) is None  # a failed listing is never remembered


def test_a_huge_folder_is_cut_short_and_says_so(tmp_path: Path, reg: BackendRegistry) -> None:
    for n in range(40):
        write(tmp_path / "big" / f"f{n:02d}.txt", b"x")
    loc = reg.open(str(tmp_path / "big"))
    cut = read_folder(loc.backend, loc.path, limit=25)
    assert cut.truncated and cut.total == 25
    assert not read_folder(loc.backend, loc.path).truncated


def test_a_folder_that_cannot_be_read_reports_why(tmp_path: Path, reg: BackendRegistry) -> None:
    loc = reg.open(str(tmp_path / "nowhere"))
    listing = read_folder(loc.backend, loc.path)
    assert listing.total == 0 and listing.errors
