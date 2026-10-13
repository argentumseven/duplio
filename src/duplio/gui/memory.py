"""How much memory the computer has, so the picture buffer can be sized to it and shrink when memory runs short.

Everything here answers ``None`` when the system will not say; callers then fall back to a modest fixed size. Nothing
here needs Tk.
"""

from __future__ import annotations

import contextlib
import os
import sys

MIB = 1024 * 1024
GIB = 1024 * MIB
FALLBACK_BUDGET = 256 * MIB  # when the computer's memory is unknown
LOWEST_BUDGET = 48 * MIB  # the buffer never shrinks below this, however short memory is


def total_memory() -> int | None:
    """Installed memory in bytes."""
    if sys.platform == "win32":
        status = _windows_status()
        return status[0] if status else None
    with contextlib.suppress(ValueError, OSError, AttributeError):
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    return None


def available_memory() -> int | None:
    """Memory that can be used right now without pushing other programs to disk, in bytes (``None`` if not known)."""
    if sys.platform == "win32":
        status = _windows_status()
        return status[1] if status else None
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
    return None


def _windows_status() -> tuple[int, int] | None:
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]

        status = Status()
        status.length = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return int(status.total), int(status.avail)
    except Exception:
        return None


def default_image_budget(total: int | None = None) -> int:
    """How much memory the pictures on screen may use when the person has not chosen: an eighth of the computer's memory,
    but at least 192 MiB and at most 1 GiB."""
    total = total_memory() if total is None else total
    if total is None:
        return FALLBACK_BUDGET
    return max(192 * MIB, min(total // 8, GIB))


def describe(nbytes: int) -> str:
    if nbytes >= GIB:
        return f"{nbytes / GIB:.1f} GB"
    if nbytes >= 10 * MIB:
        return f"{nbytes / MIB:.0f} MB"
    if nbytes >= MIB:
        return f"{nbytes / MIB:.1f} MB"
    return f"{nbytes / 1024:.0f} KB"
