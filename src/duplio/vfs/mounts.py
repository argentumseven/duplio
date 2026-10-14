"""Identify what kind of storage a local path really lives on.

A phone mounted through GVFS or jmtpfs, an SMB share mounted with ``cifs`` and
a USB stick all look like ordinary folders. They behave very differently:
MTP is single-session and slow to seek, FAT stores times with two-second
precision, network shares hate hundreds of parallel reads and spinning disks
want sequential access. This module reads the mount table and turns a path
into a :class:`~duplio.vfs.base.Capabilities` profile.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache

from duplio.vfs.base import Capabilities, MediaKind, Sampling

SECOND_NS = 1_000_000_000


@dataclass(frozen=True)
class MountEntry:
    mount_point: str
    fstype: str
    source: str
    options: str = ""


_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[MountEntry]:
    """Parse ``/proc/self/mountinfo`` (see proc(5))."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        parts = line.split(" ")
        try:
            sep = parts.index("-", 6)
        except ValueError:
            continue
        if len(parts) < sep + 3:
            continue
        entries.append(
            MountEntry(
                mount_point=_unescape(parts[4]),
                fstype=parts[sep + 1],
                source=_unescape(parts[sep + 2]),
                options=parts[5],
            )
        )
    return entries


_MAC_MOUNT_RE = re.compile(r"^(?P<src>.+?) on (?P<mp>.+?) \((?P<fs>[^,)]+)(?P<opts>[^)]*)\)\s*$")


def parse_bsd_mount(text: str) -> list[MountEntry]:
    """Parse the output of ``mount`` on macOS/BSD."""
    entries = []
    for line in text.splitlines():
        match = _MAC_MOUNT_RE.match(line)
        if match:
            entries.append(
                MountEntry(match.group("mp"), match.group("fs").strip(), match.group("src"), match.group("opts"))
            )
    return entries


class MountTable:
    """Process-wide, refreshable view of mounted filesystems."""

    def __init__(self, entries: list[MountEntry] | None = None) -> None:
        self._lock = threading.Lock()
        self._entries = entries
        self._fixed = entries is not None

    def refresh(self) -> None:
        if not self._fixed:
            with self._lock:
                self._entries = None
            _rotational_for_source.cache_clear()

    @property
    def entries(self) -> list[MountEntry]:
        with self._lock:
            if self._entries is None:
                self._entries = _load_mounts()
            return self._entries

    def find(self, path: str) -> MountEntry | None:
        """The mount holding ``path`` (longest matching mount point)."""
        path = os.path.abspath(path)
        best: MountEntry | None = None
        for entry in self.entries:
            mp = entry.mount_point
            if path == mp or path.startswith(mp.rstrip(os.sep) + os.sep) or mp == os.sep:
                if best is None or len(mp) > len(best.mount_point):
                    best = entry
        return best


def _load_mounts() -> list[MountEntry]:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as handle:
                return parse_mountinfo(handle.read())
        except OSError:
            return []
    if sys.platform == "darwin" or "bsd" in sys.platform:
        try:
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10, check=False)
            return parse_bsd_mount(out.stdout)
        except (OSError, subprocess.SubprocessError):
            return []
    return []


MOUNTS = MountTable()

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_MTP_FUSE = {"jmtpfs", "simple-mtpfs", "go-mtpfs", "mtpfs", "aft-mtp-mount", "android-file-transfer"}
_NETWORK_FUSE = {"sshfs", "rclone", "s3fs", "davfs", "davfs2", "gcsfuse", "goofys", "curlftpfs", "smbnetfs"}
_NETWORK_FS = {"nfs", "nfs4", "9p", "afs", "ceph", "glusterfs", "lustre", "beegfs", "virtiofs", "afpfs", "webdav"}
_SMB_FS = {"cifs", "smb3", "smbfs"}
_FAT_FS = {"vfat", "msdos", "fat", "fat32", "exfat", "fuseblk-exfat"}
_READONLY_FS = {"iso9660", "udf", "squashfs", "cramfs", "erofs", "cd9660"}
_REFLINK_FS = {"btrfs", "xfs", "bcachefs", "apfs", "ocfs2", "zfs-reflink"}
_CASE_INSENSITIVE_FS = _FAT_FS | {"apfs", "hfs", "ntfs", "smbfs", "cifs", "smb3"}

_MTP_CAPS = Capabilities(
    kind=MediaKind.DEVICE,
    label="Phone (MTP)",
    can_rename=True,
    atomic_rename=False,
    can_hardlink=False,
    stable_inodes=False,
    case_insensitive=True,
    mtime_resolution_ns=SECOND_NS,
    sampling=Sampling.SEQUENTIAL,
    max_parallel_reads=1,  # MTP serialises all requests on a single session
)


_WINDOWS_NAME_FS = {"cifs", "smb3", "smbfs", "vfat", "msdos", "fat", "fat32", "exfat", "ntfs", "ntfs3", "fuseblk", "fuse.exfat"}


def classify(entry: MountEntry | None, path: str) -> Capabilities:
    """Turn a mount entry plus a path into a capabilities profile."""
    caps = _classify(entry, path)
    fstype = (entry.fstype.lower() if entry else "")
    if fstype in _WINDOWS_NAME_FS or "exfat" in fstype or "ntfs" in fstype or caps.kind is MediaKind.DEVICE or "smb-share" in path:
        caps = caps.with_(windows_names=True)
    return caps


def _classify(entry: MountEntry | None, path: str) -> Capabilities:
    if entry is None:
        return Capabilities(label="Local disk")

    fstype = entry.fstype.lower()
    base = Capabilities(label="Local disk", fstype=fstype)
    sub = fstype[5:] if fstype.startswith("fuse.") else ""

    if sub == "gvfsd-fuse":
        return _classify_gvfs(path, fstype)
    if sub in _MTP_FUSE:
        return _MTP_CAPS.with_(label=f"Phone (MTP via {sub})", fstype=fstype)
    if sub == "ifuse":
        return _MTP_CAPS.with_(
            label="iPhone/iPad (AFC via ifuse)", fstype=fstype, atomic_rename=True, max_parallel_reads=2
        )
    if sub in _NETWORK_FUSE or fstype in _NETWORK_FS:
        return base.with_(
            kind=MediaKind.NETWORK,
            label=f"Network share ({sub or fstype})",
            can_hardlink=False,
            stable_inodes=fstype.startswith("nfs"),
            mtime_resolution_ns=SECOND_NS,
            max_parallel_reads=4,
        )
    if fstype in _SMB_FS:
        return base.with_(
            kind=MediaKind.NETWORK,
            label="SMB share (mounted)",
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=True,
            mtime_resolution_ns=SECOND_NS,
            max_parallel_reads=4,
        )
    if fstype in _READONLY_FS:
        return base.with_(
            kind=MediaKind.READONLY,
            label=f"Read-only media ({fstype})",
            can_write=False,
            can_delete=False,
            can_rename=False,
        )
    if fstype in _FAT_FS or "exfat" in fstype:
        return base.with_(
            kind=MediaKind.REMOVABLE,
            label=f"Removable drive ({fstype})",
            can_hardlink=False,
            case_insensitive=True,
            mtime_resolution_ns=2 * SECOND_NS,  # FAT stores times in 2-second steps
            max_parallel_reads=2,
        )

    caps = base.with_(
        can_hardlink=fstype not in {"fuseblk"},
        can_reflink=fstype in _REFLINK_FS,
        case_insensitive=fstype in _CASE_INSENSITIVE_FS and sys.platform != "linux",
    )
    if fstype in {"ntfs", "ntfs3", "fuseblk"}:
        caps = caps.with_(label=f"NTFS drive ({fstype})", mtime_resolution_ns=100)
    if sys.platform.startswith("linux") and entry.source.startswith("/dev/"):
        rotational, removable = _rotational_for_source(entry.source)
        if removable or entry.mount_point.startswith(("/media/", "/run/media/")):
            # A drive that is plugged in stays "removable" even when it spins: one read at a time, but it is not the internal disk
            caps = caps.with_(kind=MediaKind.REMOVABLE, label=f"Removable drive ({fstype})", max_parallel_reads=1 if rotational else 2)
        elif rotational:
            caps = caps.with_(
                kind=MediaKind.ROTATIONAL, label=f"Hard disk ({fstype})", max_parallel_reads=1
            )
    if sys.platform == "darwin" and entry.mount_point.startswith("/Volumes/") and fstype not in {"apfs", "hfs"}:
        caps = caps.with_(kind=MediaKind.REMOVABLE, label=f"External volume ({fstype})")
    return caps


def _classify_gvfs(path: str, fstype: str) -> Capabilities:
    """GVFS exposes every backend under ``/run/user/UID/gvfs/<scheme>:<args>/``."""
    segment = ""
    parts = path.split("/")
    if "gvfs" in parts:
        idx = parts.index("gvfs")
        if idx + 1 < len(parts):
            segment = parts[idx + 1]
    scheme = segment.split(":", 1)[0].lower()
    if scheme == "mtp":
        return _MTP_CAPS.with_(label="Phone (MTP via GVFS)", fstype=fstype)
    if scheme == "gphoto2":
        return _MTP_CAPS.with_(label="Camera / phone (PTP via GVFS)", fstype=fstype, can_rename=False)
    if scheme == "afc":
        return _MTP_CAPS.with_(label="iPhone/iPad (AFC via GVFS)", fstype=fstype, max_parallel_reads=2)
    if scheme in {"smb-share", "smb-server"}:
        return Capabilities(
            kind=MediaKind.NETWORK,
            label="SMB share (GVFS)",
            atomic_rename=False,
            stable_inodes=False,
            case_insensitive=True,
            mtime_resolution_ns=SECOND_NS,
            max_parallel_reads=2,
            fstype=fstype,
        )
    return Capabilities(
        kind=MediaKind.NETWORK,
        label=f"GVFS ({scheme or 'unknown'})",
        atomic_rename=False,
        stable_inodes=False,
        mtime_resolution_ns=SECOND_NS,
        max_parallel_reads=2,
        fstype=fstype,
    )


@lru_cache(maxsize=256)
def _rotational_for_source(source: str) -> tuple[bool, bool]:
    """``(rotational, removable)`` for a block device such as ``/dev/sda1``."""
    try:
        name = os.path.basename(os.path.realpath(source))
        sys_path = os.path.realpath(f"/sys/class/block/{name}")
    except OSError:
        return False, False
    rotational = removable = False
    for candidate in (sys_path, os.path.dirname(sys_path)):
        rot_file = os.path.join(candidate, "queue", "rotational")
        rem_file = os.path.join(candidate, "removable")
        try:
            with open(rot_file, encoding="ascii") as handle:
                rotational = handle.read().strip() == "1"
            try:
                with open(rem_file, encoding="ascii") as handle:
                    removable = handle.read().strip() == "1"
            except OSError:
                pass
            break
        except OSError:
            continue
    return rotational, removable


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _windows_caps(path: str) -> Capabilities:  # pragma: no cover - exercised on Windows only
    import ctypes

    if path.startswith(("\\\\", "//")):
        return Capabilities(
            kind=MediaKind.NETWORK,
            label="Network share (UNC)",
            windows_names=True,
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=True,
            mtime_resolution_ns=SECOND_NS,
            max_parallel_reads=4,
        )
    drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
    fs_buf = ctypes.create_unicode_buffer(64)
    volume_buf = ctypes.create_unicode_buffer(256)
    kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), volume_buf, 256, None, None, None, fs_buf, 64)
    fstype = fs_buf.value.lower()
    volume = volume_buf.value
    remote = _windows_remote_name(os.path.splitdrive(os.path.abspath(path))[0])
    virtual = looks_virtual(volume, fstype, remote)
    caps = Capabilities(
        label=f"Local disk ({fstype or 'unknown'})",
        windows_names=True,
        case_insensitive=True,
        can_hardlink=fstype == "ntfs",
        can_reflink=fstype == "refs",
        mtime_resolution_ns=2 * SECOND_NS if "fat" in fstype else 100,
        fstype=fstype,
        volume=volume,
        virtual=virtual,
    )
    if virtual and drive_type not in (2, 4, 5):
        caps = caps.with_(
            label=f"Virtual/cloud drive ({volume or fstype or 'unknown'})",
            can_hardlink=False,
            can_reflink=False,
            stable_inodes=False,
            mtime_resolution_ns=SECOND_NS,
            max_parallel_reads=2,
        )
    if drive_type == 2:  # DRIVE_REMOVABLE
        return caps.with_(kind=MediaKind.REMOVABLE, label=f"Removable drive ({fstype})", max_parallel_reads=2)
    if drive_type == 3 and not virtual and _windows_plugged_in(drive[:2]):
        # Windows calls a USB hard disk "fixed"; the bus it hangs on says it is not the computer's own disk
        return caps.with_(kind=MediaKind.REMOVABLE, label=f"External drive ({fstype})", max_parallel_reads=1)
    if drive_type == 4:  # DRIVE_REMOTE
        return caps.with_(
            kind=MediaKind.NETWORK,
            label="WebDAV drive" if "davwwwroot" in remote.lower() else "Network drive",
            can_hardlink=False,
            stable_inodes=False,
            mtime_resolution_ns=SECOND_NS,
            virtual=virtual,
        )
    if drive_type == 5:  # DRIVE_CDROM
        return caps.with_(kind=MediaKind.READONLY, label="Optical media", can_write=False, can_delete=False)
    return caps


_STANDARD_WINDOWS_FS = {"ntfs", "refs", "fat", "fat32", "exfat", "udf", "cdfs", "ext2", "ext3", "ext4", "hfs", "apfs"}
_VIRTUAL_WORDS = (
    "tresorit", "dropbox", "onedrive", "google drive", "googledrive", "drivefs", "pcloud", "cryptomator", "webdav",
    "dokan", "winfsp", "fuse", "rclone", "nextcloud", "owncloud", "mega", "sync.com", "icedrive", "proton", "filen",
    "box drive", "cbfs", "cloud", "davwwwroot",
)  # fmt: skip


def looks_virtual(volume: str, fstype: str, remote: str = "") -> bool:
    """Is this volume a virtual or cloud drive rather than a real disk?

    Cloud clients (Tresorit, Dropbox, OneDrive, pCloud…) and tools such as Cryptomator present a drive letter that is
    really a program: the folder listing may come from a local cache and *reading a file can download it*. Windows gives
    away what it is through the volume's name or through a file system name that no real disk uses.
    """
    words = f"{volume} {fstype} {remote}".lower()
    if any(word in words for word in _VIRTUAL_WORDS):
        return True
    return bool(fstype) and fstype.lower() not in _STANDARD_WINDOWS_FS and not fstype.lower().startswith(("ntfs", "fat"))


def is_cloud_storage_path(path: str) -> bool:
    """macOS File Provider locations (iCloud Drive, OneDrive, Google Drive, Dropbox, Tresorit…)."""
    normalized = path.replace("\\", "/")
    return "/Library/CloudStorage/" in normalized or "/Library/Mobile Documents/" in normalized


@dataclass(frozen=True)
class DriveInfo:
    path: str
    caps: Capabilities
    mount_point: str
    remote_name: str = ""
    notes: tuple[str, ...] = ()


def describe_location(path: str, table: MountTable | None = None) -> DriveInfo:
    """What Duplio makes of the storage a path lives on, and why (for ``duplio drives PATH``)."""
    caps, mount_point = capabilities_for_local_path(path, table)
    notes: list[str] = []
    remote = ""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        remote = _windows_remote_name(os.path.splitdrive(os.path.abspath(path))[0])
        if remote:
            notes.append(f"Windows says this drive is connected to {remote}.")
        if "davwwwroot" in remote.lower():
            notes.append("That is the Windows WebDAV redirector: the drive is a WebDAV server.")
        elif remote.startswith("\\\\") and caps.kind is MediaKind.NETWORK:
            notes.append("That is a normal Windows network share (SMB).")
    if caps.virtual:
        notes.append(
            "This looks like a virtual or cloud drive: the program that provides it may keep the folder listing locally, "
            "but reading a file can download it. Duplio treats reading here as costly and avoids reading more than needed."
        )
    if is_cloud_storage_path(path):
        notes.append("This is a macOS cloud-storage location (File Provider); files marked 'dataless' download when read.")
    if caps.read_cost == 2 and not caps.virtual:
        notes.append("Reading is slow or costs bandwidth here; pictures are cached so they are fetched only once.")
    if caps.read_cost == 1 and caps.internal:
        notes.append("This computer's own hard disk: reading is slower than an SSD, but no pictures are kept for it.")
    elif caps.read_cost == 1:
        notes.append("Reading is slower than an internal disk (USB drive or card); pictures are kept on disk so they are read once.")
    if caps.read_cost == 0:
        notes.append("Fast local storage: nothing special is done.")
    return DriveInfo(path, caps, mount_point, remote, tuple(notes))


_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "pstore", "bpf",
    "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "overlay", "tmpfs", "ramfs", "nsfs", "efivarfs",
    "squashfs", "devfs", "fdescfs", "rpc_pipefs", "selinuxfs",
}  # fmt: skip


def list_drives(table: MountTable | None = None) -> list[DriveInfo]:
    """The drives and mounts worth knowing about, with what Duplio makes of each."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        return [describe_location(f"{chr(65 + i)}:\\") for i in range(26) if mask & (1 << i)]
    table = table or MOUNTS
    out = []
    for entry in table.entries:
        fstype = entry.fstype.lower()
        if fstype in _PSEUDO_FS or entry.mount_point.startswith(("/proc", "/sys", "/dev", "/run/lock", "/snap")):
            continue
        out.append(describe_location(entry.mount_point, table))
    return out


_PLUGGED_IN_BUSES = frozenset({4, 7, 12, 13})  # STORAGE_BUS_TYPE: IEEE 1394, USB, SD card, MMC
_BUS_TYPE_OFFSET = 28  # where STORAGE_DEVICE_DESCRIPTOR keeps its BusType


def bus_type_of(descriptor: bytes) -> int | None:
    """The bus a disk hangs on, from the bytes Windows returns for a storage device (``None`` when they are too short)."""
    if len(descriptor) < _BUS_TYPE_OFFSET + 4:
        return None
    return int(struct.unpack_from("<I", descriptor, _BUS_TYPE_OFFSET)[0])


def _windows_plugged_in(drive: str) -> bool:  # pragma: no cover - Windows only
    """Is the disk behind ``drive`` ("D:") attached by USB, FireWire or a card slot? Best effort: any doubt answers no."""
    import ctypes
    from ctypes import wintypes

    handle = None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    try:
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         wintypes.DWORD, ctypes.c_void_p]
        kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)  # no access needed to ask; share read+write; open existing
        if handle in (None, ctypes.c_void_p(-1).value):
            return False
        query = struct.pack("<II4s", 0, 0, b"\0\0\0\0")  # STORAGE_PROPERTY_QUERY: StorageDeviceProperty, PropertyStandardQuery
        out = ctypes.create_string_buffer(1024)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(handle, 0x2D1400, query, len(query), out, len(out), ctypes.byref(returned), None)
        if not ok:
            return False
        return bus_type_of(out.raw[: returned.value]) in _PLUGGED_IN_BUSES
    except Exception:
        return False
    finally:
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


def _windows_remote_name(drive: str) -> str:  # pragma: no cover - Windows only
    import ctypes

    if not drive:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_ulong(1024)
    try:
        result = ctypes.windll.mpr.WNetGetConnectionW(ctypes.c_wchar_p(drive), buf, ctypes.byref(size))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return ""
    return buf.value if result == 0 else ""


def capabilities_for_local_path(path: str, table: MountTable | None = None) -> tuple[Capabilities, str]:
    """``(capabilities, mount_point)`` for a local path."""
    if sys.platform == "win32":  # pragma: no cover
        drive = os.path.splitdrive(os.path.abspath(path))[0] or path[:2]
        return _windows_caps(path), drive
    table = table or MOUNTS
    entry = table.find(path)
    caps = classify(entry, os.path.abspath(path))
    if is_cloud_storage_path(os.path.abspath(path)):
        caps = caps.with_(virtual=True, label=f"Cloud storage ({caps.fstype or 'File Provider'})", can_hardlink=False,
                          stable_inodes=False, mtime_resolution_ns=SECOND_NS, max_parallel_reads=2)
    return caps, entry.mount_point if entry else os.sep
