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
