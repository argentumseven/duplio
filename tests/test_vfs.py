from __future__ import annotations

import os
from pathlib import Path

import pytest

from duplio.errors import LocationError, UnsupportedOperation
from duplio.vfs.adb import parse_adb
from duplio.vfs.base import MediaKind, Sampling, WalkStats
from duplio.vfs.local import LocalBackend, rename_noreplace
from duplio.vfs.location import BackendRegistry, location_kind
from duplio.vfs.mounts import MountEntry, MountTable, classify, parse_bsd_mount, parse_mountinfo
from duplio.vfs.smb import parse_smb

from .conftest import write

MOUNTINFO = r"""22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
35 22 0:31 / /home/me/My\040Phone rw,nosuid,nodev,relatime shared:20 - fuse.jmtpfs jmtpfs rw,user_id=1000
36 22 0:32 / /run/user/1000/gvfs rw,nosuid,nodev,relatime shared:21 - fuse.gvfsd-fuse gvfsd-fuse rw,user_id=1000
37 22 0:33 / /mnt/nas rw,relatime shared:22 - cifs //nas/photos rw,vers=3.1.1
38 22 8:17 / /media/me/CARD rw,relatime shared:23 - vfat /dev/sdb1 rw
39 22 0:34 / /mnt/btr rw,relatime shared:24 - btrfs /dev/sdc1 rw
"""


def test_parse_mountinfo_unescapes() -> None:
    entries = parse_mountinfo(MOUNTINFO)
    assert [e.mount_point for e in entries][:2] == ["/", "/home/me/My Phone"]
    assert entries[1].fstype == "fuse.jmtpfs"
    assert entries[3].source == "//nas/photos"


def test_mount_table_longest_prefix() -> None:
    table = MountTable(parse_mountinfo(MOUNTINFO))
    assert table.find("/home/me/My Phone/DCIM").fstype == "fuse.jmtpfs"  # type: ignore[union-attr]
    assert table.find("/home/me/My Phonebook").fstype == "ext4"  # type: ignore[union-attr]
    assert table.find("/mnt/nas/2024/x.jpg").fstype == "cifs"  # type: ignore[union-attr]


def test_classification() -> None:
    table = MountTable(parse_mountinfo(MOUNTINFO))

    def caps(path: str):  # type: ignore[no-untyped-def]
        return classify(table.find(path), path)

    mtp = caps("/run/user/1000/gvfs/mtp:host=Google_Pixel_8_ABC123/Internal shared storage/DCIM")
    assert mtp.kind is MediaKind.DEVICE and mtp.max_parallel_reads == 1
    assert mtp.sampling is Sampling.SEQUENTIAL and not mtp.atomic_rename and not mtp.stable_inodes
    assert caps("/home/me/My Phone/DCIM").kind is MediaKind.DEVICE
    smb_gvfs = caps("/run/user/1000/gvfs/smb-share:server=nas,share=photos/x")
    assert smb_gvfs.kind is MediaKind.NETWORK
    cifs = caps("/mnt/nas/x")
    assert cifs.kind is MediaKind.NETWORK and cifs.case_insensitive and not cifs.can_hardlink
    fat = caps("/media/me/CARD/DCIM")
    assert fat.kind is MediaKind.REMOVABLE and fat.mtime_resolution_ns == 2_000_000_000
    assert caps("/mnt/btr/x").can_reflink
    assert classify(None, "/x").kind is MediaKind.LOCAL


def test_parse_bsd_mount() -> None:
    text = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n//me@nas/photos on /Volumes/photos (smbfs, nodev, nosuid, mounted by me)\n"
    entries = parse_bsd_mount(text)
    assert entries[0] == MountEntry("/", "apfs", "/dev/disk3s1s1", ", sealed, local, read-only, journaled")
    assert entries[1].fstype == "smbfs" and entries[1].mount_point == "/Volumes/photos"


@pytest.mark.parametrize(
    ("text", "server", "share", "path", "user", "domain", "port"),
    [
        ("smb://nas/photos", "nas", "photos", "", None, None, 445),
        ("smb://me@nas/photos/2024/July", "nas", "photos", "2024\\July", "me", None, 445),
        ("smb://WORK;me@files.local:4450/share/x", "files.local", "share", "x", "me", "WORK", 4450),
        ("smb://me%40corp@nas/s", "nas", "s", "", "me@corp", None, 445),
        ("\\\\nas\\photos\\a\\b", "nas", "photos", "a\\b", None, None, 445),
        ("//nas/photos/a", "nas", "photos", "a", None, None, 445),
    ],
)
def test_parse_smb(text, server, share, path, user, domain, port) -> None:  # type: ignore[no-untyped-def]
    t = parse_smb(text)
    assert (t.server, t.share, t.path, t.username, t.domain, t.port) == (server, share, path, user, domain, port)


@pytest.mark.parametrize("bad", ["smb://", "smb://nas", "smb://nas:abc/share", "\\\\nas"])
def test_parse_smb_rejects(bad: str) -> None:
    with pytest.raises(LocationError):
        parse_smb(bad)


def test_parse_adb() -> None:
    assert parse_adb("adb:/sdcard/DCIM").serial is None
    assert parse_adb("adb://R58M/sdcard/DCIM").serial == "R58M"
    assert parse_adb("adb://R58M/sdcard/DCIM").path == "/sdcard/DCIM"
    assert parse_adb("adb:").path == "/sdcard"
    with pytest.raises(LocationError):
        parse_adb("adb:sdcard")


def test_location_kind() -> None:
    assert location_kind("smb://x/y") == "smb"
    assert location_kind("adb:/sdcard") == "adb"
    assert location_kind("/home/me") == "local"
    assert location_kind("~/x") == "local"


def test_registry_local_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with BackendRegistry() as reg:
        loc = reg.open("~/folder")
        assert loc.path == str(tmp_path / "folder")
        assert reg.open("/x").backend is loc.backend


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


def test_walk_skips_symlinks_and_filters(tmp_path: Path) -> None:
    from duplio.scan.filters import ScanFilter

    write(tmp_path / "a.txt", "1")
    write(tmp_path / "sub" / "b.txt", "22")
    write(tmp_path / ".git" / "objects" / "x", "3")
    write(tmp_path / "empty", "")
    os.symlink(tmp_path / "a.txt", tmp_path / "link.txt")
    os.symlink(tmp_path / "sub", tmp_path / "linkdir")
    stats = WalkStats()
    entries = list(LocalBackend().walk(str(tmp_path), walk_filter=ScanFilter(), stats=stats))
    assert sorted(e.rel for e in entries) == ["a.txt", "sub/b.txt"]
    assert stats.symlinks_skipped == 2
    assert stats.filtered >= 2  # .git and the empty file


def test_walk_reports_unreadable_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "locked" / "x", "1")
    write(tmp_path / "open" / "y", "2")
    real_scandir = os.scandir

    def scandir(path):  # type: ignore[no-untyped-def]
        if str(path).endswith("locked"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    stats = WalkStats()
    entries = list(LocalBackend().walk(str(tmp_path), stats=stats))
    assert [e.rel for e in entries] == ["open/y"]
    assert stats.errors and stats.errors[0][0].endswith("locked")


def test_rename_noreplace_never_clobbers(tmp_path: Path) -> None:
    a = write(tmp_path / "a", "A")
    b = write(tmp_path / "b", "B")
    with pytest.raises(FileExistsError):
        rename_noreplace(str(a), str(b))
    assert a.read_text() == "A" and b.read_text() == "B"
    rename_noreplace(str(a), str(tmp_path / "c"))
    assert not a.exists() and (tmp_path / "c").read_text() == "A"


def test_open_write_is_atomic_and_no_clobber(tmp_path: Path) -> None:
    backend = LocalBackend()
    target = tmp_path / "out" / "file.bin"
    with backend.open_write(str(target)) as handle:
        handle.write(b"hello")
    assert target.read_bytes() == b"hello"
    with pytest.raises(FileExistsError), backend.open_write(str(target)) as handle:
        handle.write(b"x")
    assert target.read_bytes() == b"hello"
    # A failure while writing leaves nothing behind.
    with pytest.raises(RuntimeError), backend.open_write(str(tmp_path / "out" / "new.bin")) as handle:
        handle.write(b"partial")
        raise RuntimeError("boom")
    assert sorted(os.listdir(tmp_path / "out")) == ["file.bin"]
    with backend.open_write(str(target), overwrite=True) as handle:
        handle.write(b"world")
    assert target.read_bytes() == b"world"


def test_stat_and_same_file(tmp_path: Path) -> None:
    backend = LocalBackend()
    a = write(tmp_path / "a", "A")
    os.link(a, tmp_path / "hard")
    write(tmp_path / "b", "A")
    assert backend.stat(str(tmp_path / "missing")) is None
    info = backend.stat(str(a))
    assert info is not None and info.is_file and info.size == 1 and info.nlink == 2
    assert backend.same_file(str(a), str(tmp_path / "hard")) is True
    assert backend.same_file(str(a), str(tmp_path / "b")) is False


def test_remove_refuses_directories(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError):
        LocalBackend().remove(str(tmp_path / "d"))


def test_reflink_reports_unsupported_cleanly(tmp_path: Path) -> None:
    backend = LocalBackend()
    src = write(tmp_path / "src", b"data" * 100)
    dst = tmp_path / "dst"
    try:
        backend.reflink(str(src), str(dst))
    except UnsupportedOperation:
        assert not dst.exists()
    else:
        assert dst.read_bytes() == src.read_bytes()


def test_read_range(tmp_path: Path) -> None:
    f = write(tmp_path / "f", bytes(range(256)) * 10)
    assert LocalBackend().read_range(str(f), 250, 10) == bytes([250, 251, 252, 253, 254, 255, 0, 1, 2, 3])


def test_remove_empty_dirs_stops_at_root(tmp_path: Path) -> None:
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    write(tmp_path / "a" / "keep.txt", "k")
    LocalBackend().remove_empty_dirs(str(tmp_path / "a" / "b" / "c"), str(tmp_path))
    assert not (tmp_path / "a" / "b").exists()
    assert (tmp_path / "a" / "keep.txt").exists()
