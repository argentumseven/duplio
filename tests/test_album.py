"""Favorites and albums: the list of starred files, and the folder of verified copies made from it."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from duplio.backup.album import AlbumOptions, album_name, plan_album, run_album
from duplio.errors import ConfigError, SafetyError
from duplio.favorites import Favorite, Favorites
from duplio.plan.undo import undo
from duplio.util.appdirs import AppDirs
from duplio.vfs.location import BackendRegistry

MOMENT = datetime(2026, 10, 14, 15, 30, 22)


def star(favorites: Favorites, path: Path) -> Favorite:
    info = path.stat()
    favorites.add(str(path), path.name, info.st_size, info.st_mtime_ns)
    found = favorites.get(str(path))
    assert found is not None
    return found


# --------------------------------------------------------------------------- the list of favorites
def test_stars_are_kept_in_order_and_survive_a_restart(tmp_path: Path) -> None:
    store = Favorites(tmp_path / "favorites.json")
    assert store.add("/a/one.jpg", "one.jpg", 10, 1, now=100.0) and store.add("/b/two.jpg", "two.jpg", 20, 2, now=200.0)
    assert not store.add("/a/one.jpg", "one.jpg", 10, 1)  # starring twice changes nothing
    assert store.version == 2 and len(store) == 2
    again = Favorites(tmp_path / "favorites.json")
    assert [f.uri for f in again.items()] == ["/a/one.jpg", "/b/two.jpg"] and again.get("/b/two.jpg") == Favorite("/b/two.jpg", "two.jpg", 20, 2, 200.0)
    assert "/a/one.jpg" in again and not again.toggle("/a/one.jpg", "one.jpg", 10, 1) and "/a/one.jpg" not in again
    assert again.toggle("/c/three.jpg", "three.jpg", 30, 3) and again.clear() == 2 and len(Favorites(tmp_path / "favorites.json")) == 0


def test_a_damaged_list_is_set_aside_and_one_bad_entry_does_not_lose_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "favorites.json"
    path.write_text("{ not json")
    store = Favorites(path)
    assert len(store) == 0 and "kept as favorites.json.bad" in store.problem and (tmp_path / "favorites.json.bad").exists()
    good = {"uri": "/x.jpg", "name": "x.jpg", "size": 1, "mtime_ns": 2, "added": 3.0}
    path.write_text(json.dumps({"items": [good, {"uri": "/broken"}, "nonsense"]}))
    assert [f.uri for f in Favorites(path).items()] == ["/x.jpg"]


def test_a_list_that_cannot_be_saved_says_so_and_keeps_working(tmp_path: Path) -> None:
    blocked = tmp_path / "state"
    blocked.write_text("a file where a folder should be")
    store = Favorites(blocked / "favorites.json")
    store.add("/a.jpg", "a.jpg", 1, 1)
    assert "could not be saved" in store.problem and "/a.jpg" in store  # it is still starred for this session


# --------------------------------------------------------------------------- the album's name
@pytest.mark.parametrize(("options", "expected"), [
    (AlbumOptions(name="Summer trip"), "Summer trip"),
    (AlbumOptions(name="Summer trip", stamp="before"), "2026-10-14 Summer trip"),
    (AlbumOptions(name="Summer trip", stamp="after"), "Summer trip 2026-10-14"),
    (AlbumOptions(name="", stamp="before"), "2026-10-14"),
    (AlbumOptions(name="Trip", stamp="after", stamp_format="minutes"), "Trip 2026-10-14 15-30"),
    (AlbumOptions(name="Trip", stamp="before", stamp_format="seconds", separator=" - "), "2026-10-14 15-30-22 - Trip"),
    (AlbumOptions(name="  Lots   of   space  "), "Lots of space"),
    (AlbumOptions(name='Who? What: "this"/that'), "Who_ What_ _this__that"),
    (AlbumOptions(name="Ends with dots..."), "Ends with dots"),
    (AlbumOptions(name="CON"), "_CON"),
])
def test_the_album_is_named_by_the_persons_words_and_or_the_time(options: AlbumOptions, expected: str) -> None:
    assert album_name(options, MOMENT) == expected


def test_an_album_with_nothing_to_name_it_by_is_refused() -> None:
    with pytest.raises(ConfigError):
        album_name(AlbumOptions(name="   "), MOMENT)
    with pytest.raises(ConfigError):
        album_name(AlbumOptions(name="x", stamp="sideways"), MOMENT)
    assert len(album_name(AlbumOptions(name="x" * 500), MOMENT)) <= 120


# --------------------------------------------------------------------------- making an album
def build(tmp_path: Path) -> tuple[Favorites, list[Path], Path]:
    files = []
    for folder, name, body in (("trip", "IMG_1.jpg", b"one" * 400), ("trip", "IMG_2.jpg", b"two" * 500),
                               ("beach", "IMG_1.jpg", b"another one" * 300), ("beach", "clip.mp4", b"film" * 900)):
        path = tmp_path / "pictures" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files.append(path)
    store = Favorites(tmp_path / "state" / "favorites.json")
    for path in files:
        star(store, path)
    destination = tmp_path / "albums"
    destination.mkdir()
    return store, files, destination


def test_an_album_is_verified_copies_in_a_new_folder_and_the_originals_are_untouched(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    store, files, destination = build(tmp_path)
    before = {p: p.read_bytes() for p in files}
    plan = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Holiday", stamp="before"), now=MOMENT)
    assert plan.folder_name == "2026-10-14 Holiday" and len(plan.actions) == 4 and not plan.skipped
    report = run_album(plan, registry, dirs=dirs)
    assert report.count("done") == 4 and not report.failures and report.bytes_copied == sum(len(b) for b in before.values())
    album = destination / "2026-10-14 Holiday"
    assert sorted(p.name for p in album.iterdir()) == ["IMG_1 (2).jpg", "IMG_1.jpg", "IMG_2.jpg", "clip.mp4"]  # the two IMG_1 files both kept
    copied = {p.read_bytes() for p in album.iterdir()}
    assert copied == set(before.values())  # exactly the starred content
    assert {p: p.read_bytes() for p in files} == before  # the originals are as they were
    assert report.journal is not None and report.journal.exists()


def test_the_album_can_be_taken_away_again_from_history(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    store, files, destination = build(tmp_path)
    plan = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Holiday"), now=MOMENT)
    report = run_album(plan, registry, dirs=dirs)
    album = destination / "Holiday"
    assert len(list(album.iterdir())) == 4
    done = undo(report.apply_id, registry, dirs=dirs)
    assert (done.count("restored") == 4 and not album.exists()) or not list(album.iterdir())
    assert all(p.exists() for p in files)  # undoing the album never touches the originals


def test_an_album_never_merges_into_a_folder_that_exists_and_never_overwrites(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    store, _files, destination = build(tmp_path)
    (destination / "Holiday").mkdir()
    (destination / "Holiday" / "precious.txt").write_text("keep me")
    first = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Holiday"), now=MOMENT)
    assert first.folder_name == "Holiday (2)" and "already something called 'Holiday'" in first.warnings[0]
    run_album(first, registry, dirs=dirs)
    again = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Holiday"), now=MOMENT)
    assert again.folder_name == "Holiday (3)"  # a second album from the same stars is a new folder, not a mix
    assert (destination / "Holiday" / "precious.txt").read_text() == "keep me" and len(list((destination / "Holiday").iterdir())) == 1


def test_favorites_that_cannot_be_reached_are_left_out_and_say_why(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    store, files, destination = build(tmp_path)
    files[0].unlink()  # the drive it was on is gone
    store.add("sftp://nobody@127.0.0.1:1/photo.jpg", "photo.jpg", 5, 5)  # a server that is not there
    plan = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Rest"), now=MOMENT)
    assert len(plan.actions) == 3 and len(plan.skipped) == 2
    reasons = " | ".join(why for _fav, why in plan.skipped)
    assert "was not found" in reasons and "could not be reached" in reasons
    report = run_album(plan, registry, dirs=dirs)
    assert report.count("done") == 3


def test_a_file_changed_since_it_was_starred_is_copied_as_it_is_now_and_the_plan_says_so(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    store, files, destination = build(tmp_path)
    files[1].write_bytes(b"edited later" * 50)
    os.utime(files[1], ns=(1, 1))
    plan = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="A"), now=MOMENT)
    changed = next(a for a in plan.actions if a.entry.path == str(files[1]))
    assert "changed since it was starred" in changed.note
    run_album(plan, registry, dirs=dirs)
    assert (destination / "A" / "IMG_2.jpg").read_bytes() == b"edited later" * 50


def test_an_album_cannot_be_made_in_a_place_that_does_not_exist(tmp_path: Path, registry: BackendRegistry) -> None:
    store, _files, _destination = build(tmp_path)
    with pytest.raises(SafetyError, match="not a folder that exists"):
        plan_album(store.items(), registry, registry.open(str(tmp_path / "nowhere")), AlbumOptions(name="A"), now=MOMENT)


def test_stopping_an_album_leaves_only_complete_checked_copies(tmp_path: Path, dirs: AppDirs, registry: BackendRegistry) -> None:
    from duplio.events import CancelToken

    store, _files, destination = build(tmp_path)
    plan = plan_album(store.items(), registry, registry.open(str(destination)), AlbumOptions(name="Stopped"), now=MOMENT)
    token = CancelToken()
    token.cancel("stop")
    report = run_album(plan, registry, dirs=dirs, cancel=token)
    assert report.cancelled and report.count("done") == 0
    album = destination / "Stopped"
    assert not album.exists() or not any(album.iterdir())
