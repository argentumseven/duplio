"""Where a person can start from (drives, folders, phones) and the browse orders; none of this needs a screen."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from duplio.gui.places import Place, PlacesService, drive_places, folder_places
from duplio.gui.sorting import BROWSE_ORDERS, order_label, order_of
from duplio.vfs.mounts import MountEntry


def entry(mount_point: str, fstype: str = "ext4", source: str = "/dev/sda1") -> MountEntry:
    return MountEntry(mount_point, fstype, source)


def test_linux_drives_start_at_the_top_and_list_only_real_disks() -> None:
    entries = [entry("/"), entry("/proc", "proc"), entry("/sys", "sysfs"), entry("/dev", "devtmpfs"), entry("/run", "tmpfs"),
               entry("/boot", "ext4"), entry("/snap/core/1", "squashfs"), entry("/home", "ext4"), entry("/mnt/backup", "xfs"),
               entry("/media/me/STICK", "vfat"), entry("/run/media/me/CARD", "exfat"), entry("/mnt/nas", "cifs"),
               entry("/var/lib/docker/overlay2/x", "overlay"), entry("/mnt/backup", "xfs")]
    places = drive_places(platform="linux", entries=entries)
    assert places[0] == Place("Whole computer  (/)", "/")
    assert [p.target for p in places] == ["/", "/home", "/mnt/backup", "/media/me/STICK", "/run/media/me/CARD", "/mnt/nas"]


def test_windows_drives_are_every_letter() -> None:
    places = drive_places(platform="win32", windows_drives=[("C:\\", "disk"), ("D:\\", "removable"), ("Z:\\", "network")])
    assert [p.target for p in places] == ["C:\\", "D:\\", "Z:\\"]
    assert places[1].label == "D:\\  (removable)"


def test_the_drive_list_is_bounded() -> None:
    entries = [entry(f"/mnt/d{n}") for n in range(100)]
    assert len(drive_places(platform="linux", entries=entries)) <= 16


def test_folder_places_are_home_and_the_folders_that_exist(tmp_path: Path) -> None:
    (tmp_path / "Pictures").mkdir()
    (tmp_path / "Music").mkdir()
    (tmp_path / "Pictures2").mkdir()
    assert [p.label for p in folder_places(tmp_path)] == ["Home", "Pictures", "Music"]


def test_the_places_service_answers_at_once_and_scans_in_the_background() -> None:
    release, started = threading.Event(), threading.Event()
    calls: list[int] = []

    def scan(devices: bool) -> tuple[list[Place], list[Place], list[Place] | None]:
        calls.append(1)
        started.set()
        release.wait(5)
        return [Place("Home", "/h")], [Place("Whole computer  (/)", "/"), Place("/mnt/x", "/mnt/x")], [Place("phone: Pixel", "adb://1/sdcard")]

    service = PlacesService(scan)
    first = service.snapshot()
    assert not first.scanned and first.drives  # something to show before the scan is done
    assert service.refresh() and started.wait(2)
    assert service.busy and not service.refresh()  # one scan at a time
    assert not service.snapshot().scanned  # and asking never waits for it
    release.set()
    for _ in range(100):
        if not service.busy:
            break
        threading.Event().wait(0.02)
    done = service.snapshot()
    assert done.scanned and [p.target for p in done.drives] == ["/", "/mnt/x"] and done.devices[0].label == "phone: Pixel"
    assert service.version == 1 and len(calls) == 1


def test_a_scan_that_fails_leaves_what_was_known_and_never_breaks_the_caller() -> None:
    def broken(devices: bool) -> tuple[list[Place], list[Place], list[Place] | None]:
        raise OSError("the phone mount is stale")

    service = PlacesService(broken)
    service.refresh()
    for _ in range(100):
        if service.version:
            break
        threading.Event().wait(0.02)
    after = service.snapshot()
    assert after.scanned and after.devices == () and after.drives  # the menu stops saying "looking…" and still has somewhere to go
    assert not service.busy and service.refresh()  # and it can try again


def test_every_browse_order_round_trips_and_an_unknown_one_falls_back() -> None:
    for field, descending, label in BROWSE_ORDERS:
        assert order_label(field, descending) == label and order_of(label) == (field, descending)
    assert order_of("nonsense") == ("name", False) and order_label("nonsense", True) == "Name, A to Z"
    assert order_of("Newest first") == ("modified", True) and order_of("Smallest first") == ("size", False)


def test_looking_only_at_the_drives_keeps_what_was_known_about_phones() -> None:
    def scan(devices: bool) -> tuple[list[Place], list[Place], list[Place] | None]:
        return [Place("Home", "/h")], [Place("Whole computer  (/)", "/"), Place("/mnt/new", "/mnt/new")], ([Place("phone: Pixel", "adb://1")] if devices else None)

    service = PlacesService(scan)
    service.refresh()
    wait_for(lambda: service.version == 1)
    service.refresh(devices=False)  # for example on opening the Browse page
    wait_for(lambda: service.version == 2)
    now = service.snapshot()
    assert now.scanned and [p.label for p in now.devices] == ["phone: Pixel"] and len(now.drives) == 2


def wait_for(done: Any) -> None:
    for _ in range(200):
        if done():
            return
        threading.Event().wait(0.02)
    raise AssertionError("timed out")
