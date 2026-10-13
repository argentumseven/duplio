"""Where a person can start from: their folders, the drives of this computer, and any phone or camera that is plugged in.

Nothing here touches the window. Finding phones can take seconds (the Android bridge may have to start, and a stale phone
mount can stall a directory listing), so it runs on a worker thread: ``PlacesService.refresh()`` starts a scan and
``snapshot()`` hands back whatever was found last without ever waiting for it.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from duplio.vfs.mounts import MOUNTS, MountEntry


@dataclass(frozen=True)
class Place:
    label: str
    target: str


@dataclass(frozen=True)
class Places:
    folders: tuple[Place, ...]
    drives: tuple[Place, ...]
    devices: tuple[Place, ...]
    scanned: bool  # False until the first scan has finished


# Only filesystems that hold a person's files are listed as drives; the many virtual ones (proc, tmpfs, overlay, snaps…) are not.
REAL_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "bcachefs", "vfat", "msdos", "exfat", "ntfs", "ntfs3",
    "fuseblk", "hfs", "hfsplus", "apfs", "nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "fuse.rclone", "fuse.mergerfs",
})
# Mount points that are the system's own business, even when the filesystem underneath is a real one.
SYSTEM_MOUNTS = ("/boot", "/proc", "/sys", "/dev", "/snap", "/var/lib", "/var/snap", "/System", "/private", "/usr", "/etc", "/opt")
MAX_DRIVES = 16


def _is_system_mount(mount_point: str) -> bool:
    if mount_point.startswith("/run/media/"):
        return False
    if mount_point == "/run" or mount_point.startswith("/run/"):
        return True
    return any(mount_point == p or mount_point.startswith(p + "/") for p in SYSTEM_MOUNTS)


def folder_places(home: Path | None = None) -> list[Place]:
    """Home and the folders people keep their pictures and documents in (only those that exist)."""
    home = home or Path.home()
    out = [Place("Home", str(home))]
    for name in ("Pictures", "Downloads", "Documents", "Videos", "Music", "Desktop"):
        path = home / name
        try:
            if path.is_dir():
                out.append(Place(name, str(path)))
        except OSError:
            continue
    return out


def windows_drive_roots() -> list[tuple[str, str]]:  # pragma: no cover - Windows only
    """``(root, kind)`` for every drive letter, such as ``("C:\\", "disk")``."""
    import ctypes
    import string

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel32.GetLogicalDrives()
    kinds = {2: "removable", 3: "disk", 4: "network", 5: "disc", 6: "memory"}
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i):
            root = f"{letter}:\\"
            out.append((root, kinds.get(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)), "drive")))
    return out


def drive_places(*, platform: str | None = None, entries: Sequence[MountEntry] | None = None,
                 windows_drives: Sequence[tuple[str, str]] | None = None) -> list[Place]:
    """The top of every disk this computer has: ``/`` and what is mounted on it, or each drive letter."""
    platform = platform or sys.platform
    if platform == "win32":
        roots = windows_drives if windows_drives is not None else windows_drive_roots()  # pragma: no cover
        return [Place(f"{root}  ({kind})", root) for root, kind in roots]
    out = [Place("Whole computer  (/)", "/")]
    seen = {"/"}
    for entry in (entries if entries is not None else MOUNTS.entries):
        mount_point = entry.mount_point
        if mount_point in seen or _is_system_mount(mount_point) or entry.fstype.lower() not in REAL_FILESYSTEMS:
            continue
        seen.add(mount_point)
        out.append(Place(mount_point, mount_point))
        if len(out) >= MAX_DRIVES:
            break
    return out


def device_places() -> list[Place]:
    """Phones, cameras and removable or network drives that are ready to be read. Slow: call it from a worker thread."""
    from duplio.devices import discover

    return [Place(f"{dev.kind}: {dev.name}", dev.location) for dev in discover() if dev.ready]


def quick_drives() -> list[Place]:
    """One entry that needs no probing, for the moments before the first scan has finished."""
    if sys.platform == "win32":  # pragma: no cover
        anchor = os.path.splitdrive(os.path.expanduser("~"))[0] or "C:"
        return [Place(f"{anchor}\\", anchor + "\\")]
    return [Place("Whole computer  (/)", "/")]


class PlacesService:
    """Scans for places on a worker thread and answers ``snapshot()`` at once from the last scan."""

    def __init__(self, scan: Callable[[bool], tuple[list[Place], list[Place], list[Place] | None]] | None = None) -> None:
        self._scan = scan or self._scan_everything
        self._lock = threading.Lock()
        self._current = Places((Place("Home", os.path.expanduser("~")),), tuple(quick_drives()), (), False)
        self._running = False
        self._version = 0

    @staticmethod
    def _scan_everything(devices: bool) -> tuple[list[Place], list[Place], list[Place] | None]:
        """Folders and drives always; phones only when asked (looking for them may start the Android bridge)."""
        try:
            folders = folder_places()
        except Exception:
            folders = [Place("Home", os.path.expanduser("~"))]
        try:
            MOUNTS.refresh()
            drives = drive_places()
        except Exception:
            drives = quick_drives()
        if not devices:
            return folders, drives, None
        try:
            found = device_places()
        except Exception:
            found = []
        return folders, drives, found

    @property
    def version(self) -> int:
        """Goes up every time a scan finishes, so a window can tell when to redraw its list."""
        with self._lock:
            return self._version

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> Places:
        with self._lock:
            return self._current

    def refresh(self, devices: bool = True) -> bool:
        """Start looking again, unless a scan is already running. Returns whether one was started.

        ``devices=False`` only re-reads the drives of this computer, which is quick and starts nothing; phones are looked for
        when the person asks for them (opening a menu or a dialog that lists them).
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._work, args=(devices,), name="duplio-places", daemon=True).start()
        return True

    def _work(self, devices: bool) -> None:
        try:
            folders, drives, found = self._scan(devices)
        except Exception:
            folders = drives = found = None  # type: ignore[assignment]
        with self._lock:
            now = self._current
            if folders is None or drives is None:
                if devices and not now.scanned:
                    self._current = Places(now.folders, now.drives, (), True)  # stop saying "looking…"; what was known stays
            elif found is None:
                self._current = Places(tuple(folders), tuple(drives), now.devices, now.scanned)
            else:
                self._current = Places(tuple(folders), tuple(drives), tuple(found), True)
            self._version += 1
            self._running = False
