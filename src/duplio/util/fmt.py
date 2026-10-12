"""Human-friendly formatting and parsing of sizes, durations and timestamps."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

_SIZE_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[kmgtp]?i?b?)?\s*$", re.IGNORECASE
)
_SIZE_FACTORS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ti": 1024**4,
    "tib": 1024**4,
    "p": 1000**5,
    "pb": 1000**5,
    "pi": 1024**5,
    "pib": 1024**5,
}


def human_size(num_bytes: int | float) -> str:
    """Format a byte count using binary units: ``1536 -> '1.5 KiB'``."""
    value = float(num_bytes)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{sign}{int(value)} B"
            return f"{sign}{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def parse_size(text: str) -> int:
    """Parse ``'10MB'``, ``'1.5 GiB'``, ``'4096'`` into a byte count.

    Decimal suffixes (KB, MB...) are powers of 1000, binary suffixes (KiB,
    MiB...) are powers of 1024, matching common conventions.
    """
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"not a size: {text!r}")
    unit = (match.group("unit") or "").lower()
    if unit not in _SIZE_FACTORS:
        raise ValueError(f"unknown size unit in {text!r}")
    return int(float(match.group("num")) * _SIZE_FACTORS[unit])


def human_duration(seconds: float) -> str:
    """Format seconds as ``'1h 02m'``, ``'3m 05s'`` or ``'4.2s'``."""
    if seconds < 0:
        seconds = 0
    if seconds < 10:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


_DURATION_RE = re.compile(r"(\d+)\s*([smhdwy])", re.IGNORECASE)
_DURATION_FACTORS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "y": 365 * 86400}


def parse_duration(text: str) -> float:
    """Parse ``'7d'``, ``'2w3d'``, ``'12h'`` into seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty duration")
    pos = 0
    total = 0.0
    for match in _DURATION_RE.finditer(text):
        if text[pos : match.start()].strip():
            raise ValueError(f"not a duration: {text!r}")
        total += int(match.group(1)) * _DURATION_FACTORS[match.group(2).lower()]
        pos = match.end()
    if pos == 0 or text[pos:].strip():
        raise ValueError(f"not a duration: {text!r}")
    return total


def format_ts(epoch_seconds: float, *, utc: bool = False) -> str:
    """Format a POSIX timestamp as ``YYYY-MM-DD HH:MM:SS`` (local time by default)."""
    if utc:
        return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M:%S")


def format_ns(mtime_ns: int) -> str:
    return format_ts(mtime_ns / 1e9)


def utc_stamp() -> str:
    """A filesystem-safe UTC timestamp such as ``20260131T120000Z``."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def utc_stamp_ms() -> str:
    """Like :func:`utc_stamp` with milliseconds, so ids sort in creation order."""
    now = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}Z"


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def plural(count: int, word: str, plural_word: str | None = None) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count:,} {plural_word or word + 's'}"
