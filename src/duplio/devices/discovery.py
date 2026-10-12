"""Find phones, cameras, network shares and removable drives that are connected.

Each detected device comes with a ready-to-use location string, so the UI can
offer "Pixel 8 (MTP) — /run/user/1000/gvfs/mtp:host=…/Internal shared storage"
as a one-click source.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import unquote

from duplio.vfs.adb import find_adb, list_devices
from duplio.vfs.mounts import MOUNTS, MountTable, classify


@dataclass(frozen=True)
class Device:
    kind: str  # phone | camera | ios | network | removable | adb
    name: str
    location: str  # string accepted by any Duplio command
    detail: str = ""
    ready: bool = True


_GVFS_KINDS = {"mtp": "phone", "gphoto2": "camera", "afc": "ios", "smb-share": "network", "sftp": "network", "dav": "network"}
_MTP_FUSE = {"jmtpfs", "simple-mtpfs", "go-mtpfs", "mtpfs", "aft-mtp-mount"}


def _gvfs_name(segment: str) -> str:
    scheme, _, args = segment.partition(":")
    params = dict(p.split("=", 1) for p in args.split(",") if "=" in p)
    if scheme == "smb-share":
        return f"\\\\{params.get('server', '?')}\\{params.get('share', '?')}"
    host = unquote(params.get("host", segment)).replace("_", " ")
    return host.strip("[]") or segment


def _gvfs_devices() -> list[Device]:
    if not sys.platform.startswith("linux"):
        return []
    roots = [f"/run/user/{os.getuid()}/gvfs", os.path.expanduser("~/.gvfs")]
    found: list[Device] = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for segment in names:
            scheme = segment.split(":", 1)[0]
            kind = _GVFS_KINDS.get(scheme)
            if kind is None:
                continue
            base = os.path.join(root, segment)
            storages: list[str] = []
            if kind in ("phone", "camera", "ios"):
                try:
                    storages = sorted(e for e in os.listdir(base) if os.path.isdir(os.path.join(base, e)))
                except OSError:
                    storages = []
            if kind == "phone" and not storages:
                found.append(
                    Device(kind, _gvfs_name(segment), base, "MTP — unlock the phone and choose 'File transfer'", False)
                )
                continue
            if storages:
                for storage in storages:
                    found.append(Device(kind, f"{_gvfs_name(segment)} — {storage}", os.path.join(base, storage), f"{scheme.upper()} via GVFS"))
            else:
                found.append(Device(kind, _gvfs_name(segment), base, f"{scheme} via GVFS"))
    return found


def _mount_devices(table: MountTable) -> list[Device]:
    found = []
    home = os.path.expanduser("~")
    for entry in table.entries:
        fstype = entry.fstype.lower()
        sub = fstype[5:] if fstype.startswith("fuse.") else ""
        mp = entry.mount_point
        if sub in _MTP_FUSE:
            found.append(Device("phone", os.path.basename(mp) or mp, mp, f"MTP via {sub}"))
        elif sub == "ifuse":
            found.append(Device("ios", os.path.basename(mp) or mp, mp, "iPhone/iPad via ifuse"))
        elif fstype in ("cifs", "smb3", "smbfs"):
            found.append(Device("network", entry.source, mp, "SMB share (mounted)"))
        elif fstype in ("nfs", "nfs4") or sub in ("sshfs", "rclone"):
            found.append(Device("network", entry.source, mp, f"{sub or fstype} share"))
        elif mp.startswith(("/media/", "/run/media/", "/Volumes/")) or (
            mp.startswith(home + os.sep) and fstype in ("vfat", "exfat", "ntfs", "ntfs3", "fuseblk")
        ):
            caps = classify(entry, mp)
            found.append(Device("removable", os.path.basename(mp), mp, caps.label))
    return found


def _windows_drives() -> list[Device]:  # pragma: no cover - Windows only
    import ctypes
    import string

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel32.GetLogicalDrives()
    found = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        kind = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if kind == 2:
            found.append(Device("removable", root, root, "Removable drive"))
        elif kind == 4:
            found.append(Device("network", root, root, "Network drive"))
    return found


def _adb_devices() -> list[Device]:
    if not find_adb():
        return []
    out = []
    for serial, state, desc in list_devices():
        ready = state == "device"
        detail = "ADB (USB debugging)" if ready else f"ADB: {state} — accept the prompt on the phone"
        out.append(Device("adb", desc.strip() or serial, f"adb://{serial}/sdcard", detail, ready))
    return out


def discover(table: MountTable | None = None) -> list[Device]:
    """All detected devices, de-duplicated by location."""
    devices: list[Device] = []
    if sys.platform == "win32":  # pragma: no cover
        devices.extend(_windows_drives())
    else:
        table = table or MOUNTS
        table.refresh()
        devices.extend(_gvfs_devices())
        devices.extend(_mount_devices(table))
    devices.extend(_adb_devices())
    seen: set[str] = set()
    unique = []
    for dev in devices:
        if dev.location in seen:
            continue
        seen.add(dev.location)
        unique.append(dev)
    return unique


PLATFORM_TIPS = {
    "linux": "Phones appear here when unlocked and set to 'File transfer' (GVFS/jmtpfs), or via ADB with USB debugging.",
    "darwin": "macOS does not mount Android phones; use ADB (adb:/sdcard/DCIM) or a FUSE MTP tool. iPhones: use ifuse or import via Photos first.",
    "win32": "Windows exposes phones only through Explorer, not as drive letters; use ADB (adb:/sdcard/DCIM) for direct access.",
}


def platform_tip() -> str:
    key = "linux" if sys.platform.startswith("linux") else sys.platform
    return PLATFORM_TIPS.get(key, PLATFORM_TIPS["linux"])
