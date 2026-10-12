"""Checks that run before anything is written to a destination.

The classic network-backup disaster: the share is not mounted, the mount point
is just an empty folder on the local disk, and the "backup" silently fills the
local disk (or the next run sees an empty destination and starts over). These
guards make that impossible to do by accident:

* **is the destination really mounted?** (not just a folder at the mount point)
* **sentinel file**: a marker that only exists on the right drive (the idea behind
  rclone's ``--check-access``); create it once with ``duplio sentinel create``
* **does it answer?** A dead NFS server makes ``stat`` hang forever; the probe
  gives up after a timeout and says so
* **is there room?** estimated bytes plus a safety margin versus free space
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass, field

from duplio.errors import LocationError, SafetyError
from duplio.util.fmt import human_size, iso_now
from duplio.vfs.base import MediaKind
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import Location
from duplio.vfs.mounts import MOUNTS

DEFAULT_SENTINEL = ".duplio-sentinel"
SPACE_MARGIN_BYTES = 64 * 1024 * 1024
SPACE_MARGIN_RATIO = 0.01


class GuardError(SafetyError):
    """The destination failed a safety check; nothing was written."""


class BackendUnresponsive(GuardError):
    """The storage did not answer in time (dead NFS server, sleeping NAS, dropped VPN)."""


@dataclass
class GuardReport:
    warnings: list[str] = field(default_factory=list)
    free_bytes: int | None = None


def probe_responsive(location: Location, timeout: float = 15.0) -> float:
    """Seconds a ``stat`` of the location took; raises :class:`BackendUnresponsive` after ``timeout``.

    A hung network call cannot be interrupted, so it runs in a daemon thread that
    is simply abandoned; the caller gets a clear error instead of a frozen program.
    """
    outcome: list[tuple[float, object] | BaseException] = []

    def work() -> None:
        import time

        start = time.monotonic()
        try:
            # A destination folder that does not exist yet is fine; its nearest existing parent must answer.
            backend, path = location.backend, location.path
            info = backend.stat(path)
            while info is None:
                above = backend.parent(path)
                if above == path:
                    break
                path = above
                info = backend.stat(path)
            outcome.append((time.monotonic() - start, info))
        except BaseException as exc:  # reported to the caller below
            outcome.append(exc)

    thread = threading.Thread(target=work, name="duplio-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    if not outcome:
        raise BackendUnresponsive(
            f"{location.display} did not respond within {timeout:.0f} s. A network drive may be offline or "
            "its mount stale (try unmounting and mounting it again)."
        )
    first = outcome[0]
    if isinstance(first, BaseException):
        raise LocationError(f"cannot reach {location.display}: {first}") from first
    elapsed, info = first
    if info is None:
        raise LocationError(f"nothing exists at or above {location.display}")
    return float(elapsed)


def is_really_mounted(location: Location) -> tuple[bool, str]:
    """Is the path on its own mounted file system rather than the system disk it hangs off?"""
    backend = location.backend
    if not isinstance(backend, LocalBackend):
        return True, "remote backend (connection established)"
    path = os.path.abspath(location.path)
    if sys.platform == "win32":  # pragma: no cover - Windows only
        drive = os.path.splitdrive(path)[0]
        return bool(drive) and os.path.exists(drive + "\\"), f"drive {drive or '?'}"
    entry = MOUNTS.find(path)
    if entry is None or entry.mount_point == os.sep:
        return False, "it is on the system disk (the drive is not mounted there)"
    return True, f"mounted from {entry.source} ({entry.fstype}) at {entry.mount_point}"


_MOUNT_PARENTS = ("/mnt", "/media", "/run/media", "/Volumes", "/net")


def looks_like_mount_point(location: Location) -> bool:
    """Paths where people mount drives (``/mnt/nas``, ``/media/me/DISK``, ``/Volumes/Backup``)."""
    if not isinstance(location.backend, LocalBackend) or sys.platform == "win32":
        return False
    path = os.path.abspath(location.path)
    return any(path == base or path.startswith(base + os.sep) for base in _MOUNT_PARENTS)


def sentinel_path(location: Location, name: str = DEFAULT_SENTINEL) -> str:
    return location.backend.join(location.path, name)


def write_sentinel(location: Location, name: str = DEFAULT_SENTINEL, *, overwrite: bool = False) -> str:
    payload = json.dumps({"duplio_sentinel": secrets.token_hex(8), "created": iso_now()}).encode()
    with location.backend.open_write(sentinel_path(location, name), overwrite=overwrite) as handle:
        handle.write(payload)
    return sentinel_path(location, name)


def check_destination(
    location: Location,
    *,
    expect_mounted: bool | None = None,
    sentinel: str | None = None,
    needed_bytes: int = 0,
    min_free_bytes: int = 0,
    probe_timeout: float | None = 15.0,
) -> GuardReport:
    """Run every requested check; raise :class:`GuardError` on the first failure."""
    report = GuardReport()
    caps = location.caps
    if probe_timeout and caps.kind in (MediaKind.NETWORK, MediaKind.DEVICE):
        probe_responsive(location, probe_timeout)
    # None = automatic: insist on a real mount only where drives are normally mounted.
    if expect_mounted or (expect_mounted is None and looks_like_mount_point(location)):
        ok, why = is_really_mounted(location)
        if not ok:
            raise GuardError(
                f"{location.display} is not a mounted drive: {why}. Mount it first (writing there now would fill "
                "the local disk instead). Pass --no-expect-mounted to skip this check."
            )
    if sentinel:
        if location.backend.stat(sentinel_path(location, sentinel)) is None:
            raise GuardError(
                f"the marker file '{sentinel}' is missing in {location.display}, so this may not be the right "
                f"drive. Create it with: duplio sentinel create {location.uri}"
            )
    try:
        free = location.backend.free_space(location.path)
    except Exception:  # some servers cannot report free space; that is not a reason to stop
        free = None
    report.free_bytes = free
    if free is None:
        if needed_bytes:
            report.warnings.append(f"cannot tell how much space is free on {location.display}")
    elif needed_bytes or min_free_bytes:
        margin = max(SPACE_MARGIN_BYTES, int(needed_bytes * SPACE_MARGIN_RATIO))
        required = needed_bytes + margin + min_free_bytes
        if free < required:
            raise GuardError(
                f"not enough free space on {location.display}: {human_size(needed_bytes)} to write, "
                f"{human_size(free)} free (a {human_size(margin + min_free_bytes)} reserve is kept)"
            )
    return report
