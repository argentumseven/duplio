"""Cutting files into chunks: fixed blocks, or boundaries that follow the content.

**Fixed blocks** (the default) are simple and fast, and deduplicate whole files, appended data and edits that keep every
byte where it was. Their weakness is an *insertion*: add one byte near the start of a large file and every following
block shifts, so none of it matches what is already stored.

**Content-defined chunking** (``cdc``) decides where to cut from the bytes themselves, so after an insertion the cut
points re-synchronise and only the chunks around the edit are new. It is the technique behind restic, Borg and Kopia. This
one follows FastCDC (Xia et al., USENIX ATC 2016): a rolling *gear* hash over the last 64 bytes, a minimum chunk size
below which nothing is cut, and "normalised" cutting that uses a stricter test before the average size and a looser one
after it, which keeps chunk sizes close to the average.

Two choices that differ from the paper:

* The gear table is **keyed**: derived from the repository (and, in an encrypted repository, from its secret key). Chunk
  boundaries of one repository therefore cannot be used to recognise a known file in it. Public tables allow exactly
  that attack against encrypted deduplicating backups ("chunking fingerprint" attacks); restic randomises its polynomial
  for the same reason.
* The hash of a position depends only on the 64 bytes ending there, never on where the chunk started, so the cut points are
  a pure function of the content. That is what lets them re-synchronise, and it makes the chunker deterministic.

The hash is evaluated with numpy, 64 positions at a time (log2(64) = 6 vectorised passes instead of a Python loop per
byte). Speed: tens of MB/s, roughly an order of magnitude below fixed blocks (see ``docs/DESIGN-NOTES.md``). numpy is
therefore an optional dependency: ``pip install 'duplio[cdc]'``. Restoring and checking a repository never need it.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from typing import IO, Any, Protocol

from duplio.errors import RepositoryError

WINDOW = 64  # bytes the gear hash looks back over
READ_BLOCK = 4 * 1024 * 1024
SCAN_BLOCK = 256 * 1024  # positions hashed per vectorised step; bounds the work wasted after a cut is found


class Chunker(Protocol):
    name: str

    def describe(self) -> dict[str, Any]: ...

    def split(self, handle: IO[bytes], limit: int | None = None) -> Iterator[bytes]:
        """Yield the chunks of the next ``limit`` bytes of ``handle`` (all of it when ``limit`` is None)."""
        ...


def _read(handle: IO[bytes], size: int) -> bytes:
    parts: list[bytes] = []
    while size > 0:
        block = handle.read(size)
        if not block:
            break
        parts.append(block)
        size -= len(block)
    return b"".join(parts)


class FixedChunker:
    name = "fixed"

    def __init__(self, size: int) -> None:
        self.size = size

    def describe(self) -> dict[str, Any]:
        return {"type": "fixed", "size": self.size}

    def split(self, handle: IO[bytes], limit: int | None = None) -> Iterator[bytes]:
        remaining = limit
        while remaining is None or remaining > 0:
            data = _read(handle, self.size if remaining is None else min(self.size, remaining))
            if not data:
                return
            if remaining is not None:
                remaining -= len(data)
            yield data


def _numpy() -> Any:
    try:
        import numpy
    except ImportError:
        raise RepositoryError(
            "this repository uses content-defined chunking, which needs numpy: pip install 'duplio[cdc]' "
            "(restoring and checking do not need it)"
        ) from None
    return numpy


def gear_table(seed: bytes) -> Any:
    """256 pseudo-random 64-bit values derived from ``seed`` (a stable function of it on every platform)."""
    np = _numpy()
    raw = hashlib.shake_256(b"duplio-gear-v1\0" + seed).digest(256 * 8)
    return np.frombuffer(raw, dtype="<u8").astype(np.uint64)


def _high_mask(bits: int) -> int:
    bits = max(1, min(bits, 63))
    return ((1 << bits) - 1) << (64 - bits)


class GearChunker:
    """FastCDC-style normalised content-defined chunking over a keyed gear hash."""

    name = "cdc"

    def __init__(self, minimum: int, average: int, maximum: int, seed: bytes) -> None:
        if not (WINDOW <= minimum < average < maximum):
            raise RepositoryError("chunk sizes must satisfy window <= minimum < average < maximum")
        self.minimum, self.average, self.maximum = minimum, average, maximum
        self._seed = seed
        np = _numpy()
        self._np = np
        self._gear = gear_table(seed)
        bits = round(math.log2(average))
        self._mask_hard = np.uint64(_high_mask(bits + 2))  # before the average: cuts are rarer
        self._mask_easy = np.uint64(_high_mask(bits - 2))  # after it: cuts are likelier

    def describe(self) -> dict[str, Any]:
        return {"type": "cdc", "min": self.minimum, "avg": self.average, "max": self.maximum}

    # ------------------------------------------------------------------ hashing
    def _hashes(self, view: memoryview, first: int, last: int) -> Any:
        """Gear hash of every position in ``[first, last)``: ``sum(gear[b[i-k]] << k for k in range(64))``."""
        np = self._np
        data = np.frombuffer(view[first - (WINDOW - 1) : last], dtype=np.uint8)
        acc = self._gear[data]
        for shift in (1, 2, 4, 8, 16, 32):  # doubling: window 1 -> 2 -> 4 -> ... -> 64
            acc[shift:] += acc[:-shift] << np.uint64(shift)
        return acc[WINDOW - 1 :]

    def _cut(self, buf: bytearray) -> int:
        """Length of the first chunk of ``buf`` (which holds at least ``maximum`` bytes unless the file ended)."""
        np = self._np
        n = len(buf)
        if n <= self.minimum:
            return n
        end = min(n, self.maximum)
        view = memoryview(buf)
        try:
            start = self.minimum
            while start < end:
                stop = min(start + SCAN_BLOCK, end)
                # positions i (the last byte of a chunk of length i + 1) from start - 1 to stop - 2
                hashes = self._hashes(view, start - 1, stop - 1)
                lengths = np.arange(start, stop)
                masks = np.where(lengths < self.average, self._mask_hard, self._mask_easy)
                hits = np.flatnonzero((hashes & masks) == 0)
                if hits.size:
                    return int(lengths[hits[0]])
                start = stop
            return end
        finally:
            view.release()

    # ------------------------------------------------------------------ splitting
    def split(self, handle: IO[bytes], limit: int | None = None) -> Iterator[bytes]:
        buf = bytearray()
        remaining = limit
        finished = False
        while True:
            while not finished and len(buf) < self.maximum:
                want = READ_BLOCK if remaining is None else min(READ_BLOCK, remaining)
                if want <= 0:
                    finished = True
                    break
                block = handle.read(want)
                if not block:
                    finished = True
                    break
                buf += block
                if remaining is not None:
                    remaining -= len(block)
            if not buf:
                return
            cut = self._cut(buf)
            chunk = bytes(buf[:cut])
            del buf[:cut]
            yield chunk


def build(spec: dict[str, Any], *, seed: bytes) -> Chunker:
    """The chunker a repository's configuration describes."""
    kind = spec.get("type", "fixed")
    if kind == "fixed":
        return FixedChunker(int(spec["size"]))
    if kind == "cdc":
        return GearChunker(int(spec["min"]), int(spec["avg"]), int(spec["max"]), seed)
    raise RepositoryError(f"this repository uses an unknown chunker {kind!r}; please upgrade Duplio")
