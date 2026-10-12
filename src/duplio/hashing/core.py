"""Streaming file hashing and cheap content sampling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from duplio.events import CancelToken
from duplio.hashing.algorithms import get_algorithm
from duplio.vfs.base import Backend, Sampling

READ_BUFFER = 1 << 20  # 1 MiB: large enough to amortise syscalls, small enough to stay cache-friendly
KIB = 1024


class SizeMismatch(OSError):
    """The number of bytes read differs from the size recorded at scan time."""


def hash_file(
    backend: Backend,
    path: str,
    algorithm: str,
    *,
    expected_size: int | None = None,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> str:
    """Hex digest of a whole file, streamed in 1 MiB blocks.

    If ``expected_size`` is given and the stream is shorter or longer, the
    file changed under us (or a transport truncated it) and
    :class:`SizeMismatch` is raised instead of returning a misleading digest.
    """
    hasher = get_algorithm(algorithm).new()
    buf = bytearray(READ_BUFFER)
    view = memoryview(buf)
    total = 0
    with backend.open_read(path) as handle:
        readinto = getattr(handle, "readinto", None)
        while True:
            if cancel is not None:
                cancel.check()
            if readinto is not None:
                n = readinto(view)
                if not n:
                    break
                hasher.update(view[:n])
            else:
                chunk = handle.read(READ_BUFFER)
                if not chunk:
                    break
                n = len(chunk)
                hasher.update(chunk)
            total += n
            if on_bytes is not None:
                on_bytes(n)
    if expected_size is not None and total != expected_size:
        raise SizeMismatch(f"{path}: read {total} bytes, expected {expected_size} (file changed?)")
    return hasher.hexdigest()


@dataclass(frozen=True)
class SampleLayout:
    """Which slices of a file to fingerprint before committing to a full read."""

    name: str
    block: int
    regions: str  # "htm" = head+tail+middle, "h" = head only

    @property
    def sampled_bytes(self) -> int:
        return self.block * len(self.regions)

    @property
    def full_hash_threshold(self) -> int:
        """At or below this size, hashing everything costs about as much as sampling."""
        return max(128 * KIB, 2 * self.sampled_bytes)

    def regions_for(self, size: int) -> list[tuple[int, int]]:
        regions: list[tuple[int, int]] = []
        b = self.block
        if "h" in self.regions:
            regions.append((0, min(b, size)))
        if "t" in self.regions and size > b:
            regions.append((max(size - b, 0), min(b, size)))
        if "m" in self.regions and size > 3 * b:
            middle = (size // 2) // 4096 * 4096  # page aligned: one I/O on most media
            regions.append((middle, b))
        return regions

    @property
    def cache_kind(self) -> str:
        return f"sample:{self.name}"


RANDOM_LAYOUT = SampleLayout("htm16k", 16 * KIB, "htm")
SEQUENTIAL_LAYOUT = SampleLayout("h64k", 64 * KIB, "h")


def layout_for(sampling: Sampling) -> SampleLayout | None:
    if sampling is Sampling.RANDOM:
        return RANDOM_LAYOUT
    if sampling is Sampling.SEQUENTIAL:
        return SEQUENTIAL_LAYOUT
    return None


def sample_file(
    backend: Backend,
    path: str,
    size: int,
    layout: SampleLayout,
    algorithm: str,
    *,
    on_bytes: Callable[[int], None] | None = None,
) -> str:
    """Digest of the sampled regions of a file (plus its size)."""
    hasher = get_algorithm(algorithm).new()
    hasher.update(size.to_bytes(8, "little"))
    regions = layout.regions_for(size)
    if len(regions) == 1 and regions[0][0] == 0:
        data = backend.read_range(path, 0, regions[0][1])
        if len(data) != regions[0][1]:
            raise SizeMismatch(f"{path}: short read while sampling (file changed?)")
        hasher.update(data)
        if on_bytes:
            on_bytes(len(data))
        return hasher.hexdigest()
    with backend.open_read(path) as handle:
        for offset, length in regions:
            handle.seek(offset)
            data = handle.read(length)
            if len(data) != length:
                raise SizeMismatch(f"{path}: short read while sampling (file changed?)")
            hasher.update(offset.to_bytes(8, "little"))
            hasher.update(data)
            if on_bytes:
                on_bytes(length)
    return hasher.hexdigest()


def files_identical(
    a: tuple[Backend, str],
    b: tuple[Backend, str],
    *,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> bool:
    """Byte-for-byte comparison of two files, stopping at the first difference."""
    with a[0].open_read(a[1]) as fa, b[0].open_read(b[1]) as fb:
        while True:
            if cancel is not None:
                cancel.check()
            ca = _read_full(fa, READ_BUFFER)
            cb = _read_full(fb, READ_BUFFER)
            if ca != cb:
                return False
            if on_bytes:
                on_bytes(len(ca) + len(cb))
            if not ca:
                return True


def _read_full(handle, size: int) -> bytes:  # type: ignore[no-untyped-def]
    """Read exactly ``size`` bytes unless EOF; streams may return short reads."""
    parts = []
    remaining = size
    while remaining:
        chunk = handle.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)
