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
            caps = caps.with_(kind=MediaKind.REMOVABLE, label=f"Removable drive ({fstype})", max_parallel_reads=2)
        if rotational:
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
    kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, None, None, None, fs_buf, 64)
    fstype = fs_buf.value.lower()
    caps = Capabilities(
        label=f"Local disk ({fstype or 'unknown'})",
        windows_names=True,
        case_insensitive=True,
        can_hardlink=fstype == "ntfs",
        can_reflink=fstype == "refs",
        mtime_resolution_ns=2 * SECOND_NS if "fat" in fstype else 100,
        fstype=fstype,
    )
    if drive_type == 2:  # DRIVE_REMOVABLE
        return caps.with_(kind=MediaKind.REMOVABLE, label=f"Removable drive ({fstype})", max_parallel_reads=2)
    if drive_type == 4:  # DRIVE_REMOTE
        return caps.with_(
            kind=MediaKind.NETWORK,
            label="Network drive",
            can_hardlink=False,
            stable_inodes=False,
            mtime_resolution_ns=SECOND_NS,
        )
    if drive_type == 5:  # DRIVE_CDROM
        return caps.with_(kind=MediaKind.READONLY, label="Optical media", can_write=False, can_delete=False)
    return caps


def capabilities_for_local_path(path: str, table: MountTable | None = None) -> tuple[Capabilities, str]:
    """``(capabilities, mount_point)`` for a local path."""
    if sys.platform == "win32":  # pragma: no cover
        drive = os.path.splitdrive(os.path.abspath(path))[0] or path[:2]
        return _windows_caps(path), drive
    table = table or MOUNTS
    entry = table.find(path)
    caps = classify(entry, os.path.abspath(path))
    return caps, entry.mount_point if entry else os.sep
