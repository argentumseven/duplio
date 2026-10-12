"""Find the small picture a camera already stored inside a photo, reading as little of the file as possible.

Almost every JPEG from a phone or camera carries a thumbnail (about 160 pixels wide) in its first few dozen kilobytes,
inside the EXIF block. RAW files (DNG, CR2, NEF, ARW, ORF, RW2…) are TIFF containers that carry one or more full JPEG
previews. Reading just those bytes from a slow or remote drive costs a few kilobytes instead of the whole photo, which is
the whole point: Duplio can *show* a picture without downloading it.

Everything here is plain Python on bytes, so it works without Pillow. The reader is a function ``read(offset, length)``
(for a drive, a ranged read); a byte budget stops a malformed file from making us read it all.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

Reader = Callable[[int, int], bytes]

JPEG_HEAD = 64 * 1024  # an EXIF block never exceeds one JPEG segment (64 KiB)
DEFAULT_BUDGET = 6 * 1024 * 1024
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_TIFF_MAGICS = {42, 0x4F52, 0x0055}  # TIFF, Olympus ORF, Panasonic RW2
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8}
MAX_IFDS = 12


class BudgetExceeded(Exception):
    """The file asked for more bytes than the budget allows."""


@dataclass(frozen=True)
class Embedded:
    jpeg: bytes
    width: int | None
    height: int | None
    source: str  # "exif-thumbnail" or "embedded-preview"

    @property
    def long_side(self) -> int | None:
        return max(self.width, self.height) if self.width and self.height else None


@dataclass
class Found:
    thumbnail: Embedded | None = None
    orientation: int = 1  # EXIF orientation 1-8 (1 = upright)
    pixels: tuple[int, int] | None = None  # width, height of the picture itself, when the header says
    bytes_read: int = 0


class _Source:
    """A budgeted view of a file. The first bytes, once read, are served from memory without counting again."""

    def __init__(self, read: Reader, size: int | None, budget: int) -> None:
        self._read = read
        self.size = size
        self.budget = budget
        self.used = 0
        self._prefix = b""

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset < 0:
            return b""
        if self.size is not None:
            if offset >= self.size:
                return b""
            length = min(length, self.size - offset)
        end = offset + length
        if end <= len(self._prefix):
            return self._prefix[offset:end]
        if offset < len(self._prefix):  # partly known: fetch only the rest
            tail = self._fetch(len(self._prefix), end - len(self._prefix))
            self._prefix += tail if offset <= len(self._prefix) else b""
            return self._prefix[offset:end]
        data = self._fetch(offset, length)
        if offset == 0:
            self._prefix = data
        return data

    def _fetch(self, offset: int, length: int) -> bytes:
        if self.used + length > self.budget:
            raise BudgetExceeded(f"would read more than {self.budget:,} bytes")
        data = self._read(offset, length)
        self.used += len(data)
        return data


# ---------------------------------------------------------------------------------------------------------------------
# TIFF structure (also the body of a JPEG's EXIF block)
# ---------------------------------------------------------------------------------------------------------------------


class _Ifd:
    def __init__(self) -> None:
        self.entries: dict[int, tuple[int, int, bytes, int]] = {}  # tag -> (type, count, 4-byte field, field offset)
        self.next = 0


class _Tiff:
    def __init__(self, src: _Source, base: int) -> None:
        self.src, self.base = src, base
        header = src.read(base, 8)
        if len(header) < 8 or header[:2] not in (b"II", b"MM"):
            raise ValueError("not a TIFF structure")
        self.bo = "<" if header[:2] == b"II" else ">"
        if struct.unpack(self.bo + "H", header[2:4])[0] not in _TIFF_MAGICS:
            raise ValueError("not a TIFF structure")
        self.first = struct.unpack(self.bo + "I", header[4:8])[0]

    def ifd(self, offset: int) -> _Ifd | None:
        raw = self.src.read(self.base + offset, 2)
        if len(raw) < 2:
            return None
        count = struct.unpack(self.bo + "H", raw)[0]
        if count == 0 or count > 512:
            return None
        body = self.src.read(self.base + offset + 2, count * 12 + 4)
        if len(body) < count * 12 + 4:
            return None
        out = _Ifd()
        for i in range(count):
            tag, typ, num = struct.unpack(self.bo + "HHI", body[i * 12 : i * 12 + 8])
            out.entries[tag] = (typ, num, body[i * 12 + 8 : i * 12 + 12], offset + 2 + i * 12 + 8)
        out.next = struct.unpack(self.bo + "I", body[count * 12 : count * 12 + 4])[0]
        return out

    def values(self, ifd: _Ifd, tag: int, limit: int = 16) -> list[int]:
        entry = ifd.entries.get(tag)
        if entry is None:
            return []
        typ, num, field, _ = entry
        size = _TYPE_SIZE.get(typ)
        if size is None or typ not in (1, 3, 4, 7, 8, 9, 13) or num == 0 or num > limit:
            return []
        total = size * num
        data = field[:total] if total <= 4 else self.src.read(self.base + struct.unpack(self.bo + "I", field)[0], total)
        if len(data) < total:
            return []
        fmt = {1: "B", 3: "H", 4: "I", 7: "B", 8: "h", 9: "i", 13: "I"}[typ]
        return list(struct.unpack(self.bo + fmt * num, data[:total]))

    def first_value(self, ifd: _Ifd, tag: int) -> int | None:
        found = self.values(ifd, tag, limit=1)
        return found[0] if found else None


def _candidates(tiff: _Tiff, *, in_jpeg: bool) -> tuple[list[tuple[int, int, int | None, int | None, str]], int, tuple[int, int] | None]:
    """``(candidates, orientation, pixels)``; a candidate is ``(offset, length, width, height, source)``."""
    found: list[tuple[int, int, int | None, int | None, str]] = []
    orientation = 1
    pixels: tuple[int, int] | None = None
    queue: list[tuple[int, int]] = [(tiff.first, 0)]
    seen: set[int] = set()
    index = 0
    while queue and index < MAX_IFDS:
        offset, depth = queue.pop(0)
        if offset in seen or offset == 0:
            continue
        seen.add(offset)
        ifd = tiff.ifd(offset)
        if ifd is None:
            continue
        index += 1
        if depth == 0 and index == 1:
            value = tiff.first_value(ifd, 0x0112)
            if value in range(1, 9):
                orientation = int(value)  # type: ignore[arg-type]
        width, height = tiff.first_value(ifd, 0x0100), tiff.first_value(ifd, 0x0101)
        jpeg_at, jpeg_len = tiff.first_value(ifd, 0x0201), tiff.first_value(ifd, 0x0202)
        if jpeg_at and jpeg_len:
            found.append((jpeg_at, jpeg_len, width, height, "exif-thumbnail" if in_jpeg else "embedded-preview"))
        compression = tiff.first_value(ifd, 0x0103)
        strips, counts = tiff.values(ifd, 0x0111, 2), tiff.values(ifd, 0x0117, 2)
        if compression in (6, 7) and len(strips) == 1 and len(counts) == 1 and counts[0] > 0:
            found.append((strips[0], counts[0], width, height, "embedded-preview"))
        exif_ifd = tiff.first_value(ifd, 0x8769)
        if exif_ifd:
            sub = tiff.ifd(exif_ifd)
            if sub is not None:
                px, py = tiff.first_value(sub, 0xA002), tiff.first_value(sub, 0xA003)
                if px and py and pixels is None:
                    pixels = (px, py)
        for sub_offset in tiff.values(ifd, 0x014A, 8):
            queue.append((sub_offset, depth + 1))
        if ifd.next:
            queue.append((ifd.next, depth))
    return found, orientation, pixels


def _choose(src: _Source, found: list[tuple[int, int, int | None, int | None, str]], want_px: int) -> Embedded | None:
    sized = []
    for offset, length, width, height, source in found:
        if length <= 0 or (src.size is not None and offset + length > src.size):
            continue
        side = max(width, height) if width and height else None
        sized.append((offset, length, width, height, source, side))
    if not sized:
        return None
    big_enough = sorted((c for c in sized if c[5] is not None and c[5] >= want_px), key=lambda c: c[1])
    unknown = sorted((c for c in sized if c[5] is None), key=lambda c: c[1])
    smaller = sorted((c for c in sized if c[5] is not None and c[5] < want_px), key=lambda c: -c[1])
    for offset, length, width, height, source, _ in [*big_enough, *smaller, *unknown]:
        try:
            data = src.read(offset, length)
        except BudgetExceeded:
            continue
        if data[:2] == b"\xff\xd8":
            return Embedded(data, width, height, source)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the first frame header of a JPEG held in memory."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            return None
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if marker in _SOF and pos + 9 <= len(data):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        pos += 2 + length
    return None


def _from_jpeg(src: _Source, want_px: int) -> Found:
    head = src.read(0, JPEG_HEAD)
    result = Found()
    if head[:2] != b"\xff\xd8":
        return result
    pos = 2
    while pos + 4 <= len(head):
        if head[pos] != 0xFF:
            break
        marker = head[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", head[pos + 2 : pos + 4])[0]
        start, end = pos + 4, pos + 2 + length
        if marker == 0xE1 and head[start : start + 6] == b"Exif\x00\x00":
            if end > len(head):
                head = src.read(0, end)
            try:
                tiff = _Tiff(src, start + 6)
                found, orientation, pixels = _candidates(tiff, in_jpeg=True)
            except (ValueError, struct.error):
                found, orientation, pixels = [], 1, None
            result.orientation = orientation
            result.pixels = pixels
            # Offsets in the EXIF block are relative to its TIFF header.
            absolute = [(start + 6 + o, n, w, h, s) for o, n, w, h, s in found]
            result.thumbnail = _choose(src, absolute, want_px)
        elif marker in _SOF and start + 5 <= len(head):
            height, width = struct.unpack(">HH", head[start + 1 : start + 5])
            result.pixels = result.pixels or (width, height)
            break  # the frame header comes after every APPn segment that matters
        pos = end
    return result


def _from_tiff(src: _Source, want_px: int) -> Found:
    result = Found()
    try:
        tiff = _Tiff(src, 0)
        found, orientation, pixels = _candidates(tiff, in_jpeg=False)
    except (ValueError, struct.error):
        return result
    result.orientation, result.pixels = orientation, pixels
    result.thumbnail = _choose(src, found, want_px)
    return result


def find_embedded(read: Reader, size: int | None, *, want_px: int = 240, budget: int = DEFAULT_BUDGET) -> Found:
    """Look for an embedded picture in a JPEG or TIFF-based file. Never raises for a malformed file."""
    src = _Source(read, size, budget)
    try:
        magic = src.read(0, 4)
        if magic[:2] == b"\xff\xd8":
            result = _from_jpeg(src, want_px)
        elif magic[:2] in (b"II", b"MM"):
            result = _from_tiff(src, want_px)
        else:
            result = Found()
    except (BudgetExceeded, struct.error, ValueError, IndexError):
        result = Found()
    if result.thumbnail is not None and result.thumbnail.width is None:
        size_found = _jpeg_size(result.thumbnail.jpeg)
        if size_found is not None:
            result = Found(
                Embedded(result.thumbnail.jpeg, size_found[0], size_found[1], result.thumbnail.source),
                result.orientation, result.pixels, src.used,
            )
    result.bytes_read = src.used
    return result
