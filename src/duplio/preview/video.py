"""The picture some movie files carry inside them.

MP4, M4V and MOV files made by iTunes, many video editors and some cameras hold a cover picture (the ``covr`` item of the
metadata). Reading it costs a few small ranged reads, however big the movie is, so it is the cheapest picture of a movie
there is; most phone videos have none, and then a frame has to be taken (see :mod:`duplio.preview.ffmpeg`).
"""

from __future__ import annotations

import struct
from collections.abc import Callable

ReadRange = Callable[[int, int], bytes]
MAX_READS = 300  # the boxes are walked with small ranged reads; a file that needs more than this is not worth it
MAX_COVER = 8 * 1024 * 1024
_CONTAINERS = {b"moov", b"udta", b"meta", b"ilst", b"covr"}


class _Walker:
    def __init__(self, read: ReadRange, size: int) -> None:
        self.read, self.size, self.reads = read, size, 0

    def boxes(self, start: int, end: int):  # type: ignore[no-untyped-def]
        """``(type, body_start, body_end)`` for each box between ``start`` and ``end``."""
        pos = start
        while pos + 8 <= end and self.reads < MAX_READS:
            head = self.read(pos, 16)
            self.reads += 1
            if len(head) < 8:
                return
            length, kind = struct.unpack(">I4s", head[:8])
            header = 8
            if length == 1 and len(head) >= 16:
                length, header = struct.unpack(">Q", head[8:16])[0], 16
            elif length == 0:
                length = end - pos
            if length < header or pos + length > end + 0:
                length = min(max(length, header), end - pos) if length >= header else end - pos
            yield kind, pos + header, pos + length
            pos += length

    def find(self, start: int, end: int, path: list[bytes]) -> tuple[int, int] | None:
        for kind, body, stop in self.boxes(start, end):
            if kind != path[0]:
                continue
            if len(path) == 1:
                return body, stop
            inner = body + 4 if kind == b"meta" else body  # a "meta" box has four bytes of version and flags first
            found = self.find(inner, stop, path[1:])
            if found is not None:
                return found
        return None


def cover_art(read: ReadRange, size: int) -> bytes | None:
    """The cover picture (JPEG or PNG bytes) of an MP4/MOV-family file, or ``None`` when it has none."""
    if size < 16 or read(4, 4) not in (b"ftyp", b"moov", b"free", b"wide", b"mdat", b"skip"):
        return None
    walker = _Walker(read, size)
    for path in ([b"moov", b"udta", b"meta", b"ilst", b"covr"], [b"moov", b"meta", b"ilst", b"covr"]):
        found = walker.find(0, size, path)
        if found is None:
            continue
        for kind, body, stop in walker.boxes(*found):
            if kind != b"data" or stop - body <= 8 or stop - body > MAX_COVER:
                continue
            picture = read(body + 8, stop - body - 8)  # four bytes of type, four of locale, then the picture
            if picture[:2] == b"\xff\xd8" or picture[:8] == b"\x89PNG\r\n\x1a\n":
                return picture
    return None
