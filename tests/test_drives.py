"""Recognising what a drive is: slow, virtual or cloud, and whether a file is only a placeholder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli
from duplio.compare.session import _entry, _unentry
from duplio.scan.inventory import FileEntry
from duplio.vfs.base import Capabilities, MediaKind
from duplio.vfs.local import _is_offline
from duplio.vfs.mounts import (
    MountEntry,
    MountTable,
    classify,
    describe_location,
    is_cloud_storage_path,
    list_drives,
    looks_virtual,
)


@pytest.mark.parametrize(
    ("volume", "fstype", "virtual"),
    [
        ("", "NTFS", False), ("Backup", "NTFS", False), ("Data", "exFAT", False), ("", "FAT32", False), ("", "", False),
        ("Tresorit Drive", "NTFS", True), ("", "Tresorit", True), ("OneDrive", "NTFS", True), ("", "WinFsp", True),
        ("", "Dokan", True), ("Cryptomator vault", "NTFS", True), ("", "SomeNewFs", True),
    ],
)
def test_virtual_drives_are_recognised_by_their_names(volume: str, fstype: str, virtual: bool) -> None:
    assert looks_virtual(volume, fstype) is virtual


def test_a_webdav_redirector_connection_counts_as_virtual() -> None:
    assert looks_virtual("", "NTFS", r"\\localhost@8080\DavWWWRoot\vault")


def test_macos_cloud_storage_paths() -> None:
    assert is_cloud_storage_path("/Users/me/Library/CloudStorage/OneDrive-Personal/Photos")
    assert is_cloud_storage_path("/Users/me/Library/Mobile Documents/com~apple~CloudDocs/x")
    assert not is_cloud_storage_path("/Users/me/Pictures")


def test_read_cost_ranks_storage_by_how_costly_reading_is() -> None:
    assert Capabilities(kind=MediaKind.LOCAL).read_cost == 0
    assert Capabilities(kind=MediaKind.REMOVABLE).read_cost == 1
    assert Capabilities(kind=MediaKind.ROTATIONAL).read_cost == 1
    for kind in (MediaKind.NETWORK, MediaKind.DEVICE):
        assert Capabilities(kind=kind).read_cost == 2
    assert Capabilities(kind=MediaKind.LOCAL, virtual=True).read_cost == 2  # a cloud drive that calls itself local


def test_only_the_computers_own_disks_are_internal() -> None:
    assert Capabilities(kind=MediaKind.LOCAL).internal and Capabilities(kind=MediaKind.ROTATIONAL).internal
    for kind in (MediaKind.REMOVABLE, MediaKind.NETWORK, MediaKind.DEVICE, MediaKind.READONLY):
        assert not Capabilities(kind=kind).internal
    assert not Capabilities(kind=MediaKind.LOCAL, virtual=True).internal  # a cloud drive that calls itself local


def test_a_usb_hard_disk_is_removable_not_the_internal_hard_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.vfs import mounts

    monkeypatch.setattr(mounts.sys, "platform", "linux")
    monkeypatch.setattr(mounts, "_rotational_for_source", lambda _source: (True, False))
    spinning_usb = mounts.classify(MountEntry("/run/media/me/BACKUP", "ext4", "/dev/sdb1"), "/run/media/me/BACKUP/x")
    internal = mounts.classify(MountEntry("/data", "ext4", "/dev/sda2"), "/data/x")
    assert spinning_usb.kind is MediaKind.REMOVABLE and not spinning_usb.internal and spinning_usb.max_parallel_reads == 1
    assert internal.kind is MediaKind.ROTATIONAL and internal.internal and internal.max_parallel_reads == 1
    monkeypatch.setattr(mounts, "_rotational_for_source", lambda _source: (False, True))
    card = mounts.classify(MountEntry("/media/me/CARD", "ext4", "/dev/mmcblk0p1"), "/media/me/CARD/x")
    assert card.kind is MediaKind.REMOVABLE and card.max_parallel_reads == 2


def test_the_bus_a_windows_disk_hangs_on_is_read_from_its_descriptor() -> None:
    import struct

    from duplio.vfs.mounts import bus_type_of

    def descriptor(bus: int) -> bytes:  # STORAGE_DEVICE_DESCRIPTOR: BusType is the 4 bytes at offset 28
        return struct.pack("<IIBBBBIIIIII", 1, 40, 0, 0, 0, 0, 0, 0, 0, 0, bus, 0) + b"\0" * 8

    assert bus_type_of(descriptor(7)) == 7  # USB
    assert bus_type_of(descriptor(17)) == 17  # NVMe
    assert bus_type_of(b"\0" * 20) is None


@pytest.mark.parametrize(
    ("fstype", "source", "cost"),
    [("ext4", "/dev/nvme0n1p2", 0), ("vfat", "/dev/sdb1", 1), ("cifs", "//nas/share", 2), ("fuse.sshfs", "me@host:/", 2),
     ("nfs4", "nas:/export", 2), ("fuse.rclone", "gdrive:", 2)],
)
def test_common_mounts_get_the_right_cost(fstype: str, source: str, cost: int) -> None:
    entry = MountEntry("/mnt/x", fstype, source)
    assert classify(entry, "/mnt/x/file").read_cost == cost


def test_describing_a_location_explains_itself() -> None:
    table = MountTable([MountEntry("/", "ext4", "/dev/nvme0n1p1"), MountEntry("/mnt/nas", "cifs", "//nas/share")])
    nas = describe_location("/mnt/nas/photos", table)
    assert nas.caps.kind is MediaKind.NETWORK and any("slow or costs bandwidth" in n for n in nas.notes)
    mac = describe_location("/Users/me/Library/CloudStorage/Tresorit/Photos", table)
    assert mac.caps.virtual and any("cloud" in n.lower() for n in mac.notes)


def test_listing_drives_skips_pseudo_file_systems() -> None:
    table = MountTable([
        MountEntry("/", "ext4", "/dev/sda1"), MountEntry("/proc", "proc", "proc"), MountEntry("/sys", "sysfs", "sysfs"),
        MountEntry("/run/user/1000", "tmpfs", "tmpfs"), MountEntry("/mnt/usb", "vfat", "/dev/sdb1"),
    ])
    assert [d.mount_point for d in list_drives(table)] == ["/", "/mnt/usb"]


def test_placeholder_files_are_recognised() -> None:
    assert _is_offline(SimpleNamespace(st_file_attributes=0x400000))  # RECALL_ON_DATA_ACCESS (OneDrive, Dropbox…)
    assert _is_offline(SimpleNamespace(st_file_attributes=0x1000))  # OFFLINE
    assert _is_offline(SimpleNamespace(st_file_attributes=0x40000))  # RECALL_ON_OPEN
    assert _is_offline(SimpleNamespace(st_flags=0x40000000))  # macOS dataless
    assert not _is_offline(SimpleNamespace(st_file_attributes=0x20))  # ARCHIVE only
    assert not _is_offline(SimpleNamespace())  # Linux has neither


def test_the_online_only_flag_survives_a_saved_session() -> None:
    entry = FileEntry(1, 0, "/a/b.jpg", "b.jpg", 10, 20, offline=True)
    assert _unentry(_entry(entry)).offline is True
    old_row = _entry(FileEntry(1, 0, "/a/b.jpg", "b.jpg", 10, 20))[:9]  # a session saved before the flag existed
    assert _unentry(old_row).offline is False


def test_the_drives_command_runs() -> None:
    runner = CliRunner(env={"COLUMNS": "150"})
    listing = runner.invoke(cli, ["--no-color", "drives"], catch_exceptions=False)
    assert listing.exit_code == 0 and "Reading" in listing.output
    detail = runner.invoke(cli, ["--no-color", "drives", "."], catch_exceptions=False)
    assert detail.exit_code == 0 and "Virtual / cloud drive" in detail.output and "Reading files" in detail.output
