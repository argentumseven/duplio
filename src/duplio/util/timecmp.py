"""Comparing modification times across file systems that store them differently.

* FAT stores two-second steps, SMB/NFS often whole seconds, NTFS 100 ns, ext4 1 ns.
  Two copies of one file therefore rarely carry *identical* times. Times are equal
  if they differ by no more than the coarser side's resolution.
* FAT keeps local time and NTFS keeps UTC, so after a daylight-saving change the
  same file can differ by exactly one hour. ``dst_shift`` accepts that single,
  exact offset (still within the resolution) as equal. It is an offset, not a
  tolerance window: a 59-minute difference is still a difference.
"""

from __future__ import annotations

SECOND_NS = 1_000_000_000
HOUR_NS = 3600 * SECOND_NS


def tolerance_for(*resolutions_ns: int, floor_ns: int = 0) -> int:
    """The widest storage resolution among the given sides (at least ``floor_ns``)."""
    return max([floor_ns, *resolutions_ns, 1])


def mtimes_match(a_ns: int, b_ns: int, *, tolerance_ns: int = SECOND_NS, dst_shift: bool = False) -> bool:
    diff = abs(a_ns - b_ns)
    if diff <= tolerance_ns:
        return True
    return dst_shift and abs(diff - HOUR_NS) <= tolerance_ns


def newer(a_ns: int, b_ns: int, *, tolerance_ns: int = SECOND_NS, dst_shift: bool = False) -> int:
    """``1`` if A is newer, ``-1`` if B is newer, ``0`` if they match (see :func:`mtimes_match`)."""
    if mtimes_match(a_ns, b_ns, tolerance_ns=tolerance_ns, dst_shift=dst_shift):
        return 0
    return 1 if a_ns > b_ns else -1


# ---------------------------------------------------------------------------
# Timestamps that cannot prove anything
# ---------------------------------------------------------------------------

RACY_WINDOW_NS = 3 * SECOND_NS
FUTURE_SLACK_NS = 24 * HOUR_NS


def racy_window(*resolutions_ns: int) -> int:
    """How close to a reference moment a modification time is too close to prove "unchanged"."""
    return max([RACY_WINDOW_NS, *(2 * r for r in resolutions_ns)])


def is_racy(mtime_ns: int, reference_ns: int, *resolutions_ns: int) -> bool:
    """Was the file modified so near the moment it was recorded that an edit could have kept the same time?

    A drive stores times in steps (1-2 s on FAT, SMB and many phones), so a file written, recorded and then
    written again within one step shows the same size and time both times. Size and time are therefore trusted
    only for files that were already older than the window when they were recorded (the "racy timestamp"
    problem every incremental tool has to handle).
    """
    return reference_ns - mtime_ns < racy_window(*resolutions_ns)


def in_the_future(mtime_ns: int, now_ns: int) -> bool:
    """A modification time more than a day ahead of the clock: a wrong clock or a tool that sets it deliberately."""
    return mtime_ns > now_ns + FUTURE_SLACK_NS


def trusted_newer(a_ns: int, b_ns: int, *, now_ns: int, tolerance_ns: int = SECOND_NS, dst_shift: bool = False) -> int:
    """Like :func:`newer`, but ``0`` (undecided) when either time is in the future: neither can be believed."""
    if in_the_future(a_ns, now_ns) or in_the_future(b_ns, now_ns):
        return 0
    return newer(a_ns, b_ns, tolerance_ns=tolerance_ns, dst_shift=dst_shift)
