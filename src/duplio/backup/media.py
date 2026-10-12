"""When was this photo or video taken?

Sources, most trustworthy first:

1. EXIF ``DateTimeOriginal`` / ``DateTimeDigitized`` / ``DateTime`` (photos, via Pillow)
2. QuickTime/MP4 ``mvhd`` creation time (videos; parsed directly, no ffmpeg)
3. Date stamps in camera and messenger file names
   (``IMG_20240131_235959.jpg``, ``PXL_20240131_…``, ``Screenshot_2024-01-31-…``,
   ``IMG-20240131-WA0007.jpg``, ``signal-2024-01-31-235959.jpg``…)
4. The file's modification time
"""

from __future__ import annotations

import re
import struct
from datetime import UTC, datetime
from typing import IO

from duplio.scan.filters import extension

_EXIF_DATE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime
_MP4_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01
_VIDEO_EXT = {"mp4", "m4v", "mov", "3gp", "3g2"}

_NAME_PATTERNS = [
    # 20240131_235959, 20240131-235959, 20240131T235959, PXL_20240131_235959123
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})[_\-T ]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})"),
    # 2024-01-31-23-59-59, 2024-01-31 23.59.59, 2024_01_31_235959
    re.compile(
        r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})(?:[-_ T.]+(?P<H>\d{2})[-_.:]?(?P<M>\d{2})[-_.:]?(?P<S>\d{2}))?"
    ),
    # IMG-20240131-WA0007 (date only)
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),
]


def date_from_name(name: str) -> datetime | None:
    for pattern in _NAME_PATTERNS:
        for match in pattern.finditer(name):
            parts = match.groupdict()
            try:
                dt = datetime(
                    int(parts["y"]),
                    int(parts["m"]),
                    int(parts["d"]),
                    int(parts.get("H") or 0),
                    int(parts.get("M") or 0),
                    int(parts.get("S") or 0),
                )
            except (TypeError, ValueError):
                continue
            if 1990 <= dt.year <= datetime.now().year + 1:
                return dt
    return None


def date_from_exif(handle: IO[bytes]) -> datetime | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(handle) as img:
            exif = img.getexif()
            values: list[object] = []
            try:
                sub = exif.get_ifd(0x8769)
                values.extend(sub.get(tag) for tag in _EXIF_DATE_TAGS[:2])
            except Exception:
                pass
            values.append(exif.get(306))
    except Exception:
        return None
    for value in values:
        if not value or not isinstance(value, str):
            continue
        text = value.strip().replace("\x00", "")
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
            try:
                dt = datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
            except ValueError:
                continue
            if dt.year >= 1990:
                return dt
    return None


def date_from_mp4(handle: IO[bytes]) -> datetime | None:
    """Creation time from the ``moov/mvhd`` box of MP4/MOV/3GP files."""
    try:
        handle.seek(0, 2)
        end = handle.tell()
        handle.seek(0)
        moov = _find_box(handle, 0, end, b"moov")
        if moov is None:
            return None
        mvhd = _find_box(handle, moov[0], moov[1], b"mvhd")
        if mvhd is None:
            return None
        handle.seek(mvhd[0])
        version = handle.read(4)[0]
        raw = handle.read(8 if version == 1 else 4)
        seconds = struct.unpack(">Q" if version == 1 else ">I", raw)[0]
    except (OSError, struct.error, IndexError):
        return None
    if seconds <= _MP4_EPOCH_OFFSET:
        return None
    epoch = seconds - _MP4_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(epoch, UTC).astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _find_box(handle: IO[bytes], start: int, end: int, wanted: bytes) -> tuple[int, int] | None:
    """``(payload_start, box_end)`` of the first box named ``wanted`` in [start, end)."""
    pos = start
    while pos + 8 <= end:
        handle.seek(pos)
        header = handle.read(8)
        if len(header) < 8:
            return None
        size, kind = struct.unpack(">I4s", header)
        header_len = 8
        if size == 1:
            size = struct.unpack(">Q", handle.read(8))[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            return None
        if kind == wanted:
            return pos + header_len, min(pos + size, end)
        pos += size
    return None


def capture_date(name: str, handle: IO[bytes] | None, fallback_mtime_ns: int) -> tuple[datetime, str]:
    """Best capture date for a media file, with the source it came from."""
    ext = extension(name)
    if handle is not None:
        if ext in _VIDEO_EXT:
            dt = date_from_mp4(handle)
            if dt is not None:
                return dt, "video metadata"
        else:
            dt = date_from_exif(handle)
            if dt is not None:
                return dt, "EXIF"
    dt = date_from_name(name)
    if dt is not None:
        return dt, "file name"
    return datetime.fromtimestamp(fallback_mtime_ns / 1e9), "modification time"


def media_kind(name: str) -> str:
    from duplio.scan.filters import IMAGE_EXTENSIONS, MEDIA_EXTENSIONS

    ext = extension(name)
    if ext in IMAGE_EXTENSIONS or ext in {"dng", "raw", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef"}:
        return "Photos"
    if ext in MEDIA_EXTENSIONS:
        return "Videos"
    return "Other"
