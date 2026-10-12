"""The storage backend contract.

Every place Duplio can read or write — a local folder, a phone mounted through
MTP/FUSE, an Android device over ADB, an SMB share — is a :class:`Backend`.
Engines only ever talk to this interface, so new storage types slot in without
touching comparison, planning or backup code.

Paths handed to a backend are *backend-native* strings (POSIX paths for local
and ADB, UNC paths for SMB). Paths relative to a scan root are always POSIX
(``a/b/c.jpg``) so they compare cleanly across backends.
"""

from __future__ import annotations

import contextlib
import posixpath
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import IO, NamedTuple, Protocol

from duplio.errors import UnsupportedOperation


class MediaKind(StrEnum):
    """Broad storage class; drives scheduling and safety defaults."""

    LOCAL = "local"  # internal SSD / NVMe / generic local filesystem
    ROTATIONAL = "rotational"  # spinning disk: avoid parallel random reads
    REMOVABLE = "removable"  # USB stick, SD card, external FAT/exFAT drive
    NETWORK = "network"  # SMB, NFS, SSHFS, cloud FUSE mounts
    DEVICE = "device"  # phone or camera (MTP, PTP, AFC, ADB)
    READONLY = "readonly"  # optical media, squashfs, ISO images


class Sampling(StrEnum):
    """How cheaply a backend can read small slices of a file."""

    RANDOM = "random"  # seek anywhere: sample head, middle and tail
    SEQUENTIAL = "sequential"  # only the head is cheap (MTP, slow links)
    NONE = "none"  # partial reads are not worth it; hash whole files


@dataclass(frozen=True)
class Capabilities:
    """What a backend (at a given path) can do, and how it behaves."""

    kind: MediaKind = MediaKind.LOCAL
    label: str = "Local disk"
    can_write: bool = True
    can_delete: bool = True
    can_rename: bool = True  # move within the same backend/filesystem
    atomic_rename: bool = True  # write-temp-then-rename is safe and supported
    can_hardlink: bool = False
    can_reflink: bool = False
    can_trash: bool = False
    stable_inodes: bool = True  # equal inode numbers really mean "same file"
    case_insensitive: bool = False
    mtime_resolution_ns: int = 1  # granularity of stored modification times
    sampling: Sampling = Sampling.RANDOM
    max_parallel_reads: int = 4
    remote_hash_algorithms: tuple[str, ...] = ()  # hashes computed on the far side
    fstype: str = ""
    windows_names: bool = False  # NTFS/FAT/SMB/Android rules: no ``<>:"|?*``, no trailing dots, reserved names

    def with_(self, **changes: object) -> Capabilities:
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class StatInfo:
    size: int
    mtime_ns: int
    is_dir: bool
    is_file: bool
    is_symlink: bool = False
    inode: tuple[int, int] | None = None  # (device, inode) when meaningful
    nlink: int = 1
    mode: int | None = None
    ctime_ns: int | None = None  # inode change time where it is a real change time (POSIX); it cannot be set by users


def same_state(before: StatInfo, after: StatInfo) -> bool:
    """Did a file keep the same size, times and identity between two looks?

    Times or identities a backend does not provide are not compared. Used to notice a file that was written while
    it was being read or copied.
    """
    if before.size != after.size or before.mtime_ns != after.mtime_ns:
        return False
    if before.ctime_ns is not None and after.ctime_ns is not None and before.ctime_ns != after.ctime_ns:
        return False
    return not (before.inode is not None and after.inode is not None and before.inode != after.inode)


class RemoteDigest(NamedTuple):
    """A digest computed on the storage side, with the size/mtime seen right after."""

    digest: str
    size: int | None = None
    mtime_ns: int | None = None


@dataclass(slots=True)
class RawEntry:
    """A regular file discovered by :meth:`Backend.walk`."""

    path: str  # backend-native absolute path
    rel: str  # POSIX path relative to the walk root
    size: int
    mtime_ns: int
    inode: tuple[int, int] | None = None
    nlink: int = 1
    ctime_ns: int | None = None


class WalkFilter(Protocol):
    def skip_dir(self, rel: str, name: str) -> bool: ...

    def skip_file(self, rel: str, name: str, size: int) -> bool: ...


@dataclass
class WalkStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    symlinks_skipped: int = 0
    special_skipped: int = 0
    filtered: int = 0
    other_fs_skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


ErrorHandler = Callable[[str, BaseException], None]


class Backend(ABC):
    """Abstract storage backend. Implementations must be thread-safe for reads."""

    #: Short machine name, e.g. ``"local"``, ``"smb"``, ``"adb"``.
    scheme: str = ""

    # ---- identity -------------------------------------------------------
    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identity used for caches, e.g. ``local`` or ``adb:R58M``."""

    @abstractmethod
    def capabilities(self, path: str) -> Capabilities:
        """Capabilities for the filesystem holding ``path``."""

    def device_key(self, path: str) -> str:
        """Identifies the physical device for I/O scheduling."""
        return self.id

    @abstractmethod
    def uri(self, path: str) -> str:
        """A location string that :func:`duplio.vfs.location.open_location` accepts."""

    def display(self, path: str) -> str:
        return self.uri(path)

    # ---- path algebra ---------------------------------------------------
    def join(self, base: str, rel: str) -> str:
        """Join a backend path with a POSIX relative path."""
        if not rel:
            return base
        return posixpath.join(base, rel)

    def parent(self, path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    def basename(self, path: str) -> str:
        return posixpath.basename(path.rstrip("/"))

    def normalize(self, path: str) -> str:
        """Canonical form used for overlap checks and identity."""
        return posixpath.normpath(path)

    def is_within(self, path: str, root: str) -> bool:
        """True if ``path`` equals ``root`` or lies beneath it."""
        p, r = self.normalize(path), self.normalize(root)
        if self.capabilities(root).case_insensitive:
            p, r = p.casefold(), r.casefold()
        if p == r:
            return True
        sep = self.sep
        return p.startswith(r.rstrip(sep) + sep)

    sep: str = "/"

    # ---- reading --------------------------------------------------------
    @abstractmethod
    def walk(
        self,
        root: str,
        *,
        walk_filter: WalkFilter | None = None,
        stats: WalkStats | None = None,
        one_file_system: bool = True,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[RawEntry]:
        """Yield regular files below ``root``. Symlinks are never followed."""

    @abstractmethod
    def stat(self, path: str) -> StatInfo | None:
        """Metadata for ``path`` without following symlinks; ``None`` if absent."""

    @abstractmethod
    def open_read(self, path: str) -> IO[bytes]:
        """Open a file for binary reading."""

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        with self.open_read(path) as handle:
            if offset:
                handle.seek(offset)
            return _read_exact(handle, length)

    def remote_hash(self, paths: Sequence[str], algorithm: str) -> dict[str, RemoteDigest]:
        """Hash files on the storage side (no data transfer), keyed by path."""
        raise UnsupportedOperation(f"{self.scheme} cannot hash remotely")

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        """``(name, is_dir)`` pairs for a directory."""
        raise UnsupportedOperation(f"{self.scheme} cannot list directories")

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def free_space(self, path: str) -> int | None:
        return None

    # ---- writing --------------------------------------------------------
    def open_write(
        self, path: str, *, overwrite: bool = False
    ) -> contextlib.AbstractContextManager[IO[bytes]]:
        """Write a file as a unit: on error nothing is left at ``path``.

        Without ``overwrite`` an existing file is never replaced and
        :class:`FileExistsError` is raised instead.
        """
        raise UnsupportedOperation(f"{self.scheme} is read-only")

    def makedirs(self, path: str) -> None:
        raise UnsupportedOperation(f"{self.scheme} cannot create directories")

    def remove(self, path: str) -> None:
        raise UnsupportedOperation(f"{self.scheme} cannot delete files")

    def rename(self, src: str, dst: str) -> None:
        """Move within this backend. Must never overwrite an existing ``dst``."""
        raise UnsupportedOperation(f"{self.scheme} cannot rename files")

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        """Best-effort removal of now-empty parents up to (not including) ``stop_at``."""

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        """Best effort; backends that cannot set times silently ignore it."""

    def hardlink(self, src: str, dst: str) -> None:
        raise UnsupportedOperation(f"{self.scheme} cannot create hard links")

    def reflink(self, src: str, dst: str) -> None:
        raise UnsupportedOperation(f"{self.scheme} cannot clone files")

    def trash(self, path: str) -> None:
        raise UnsupportedOperation(f"{self.scheme} has no trash")

    def same_file(self, a: str, b: str) -> bool | None:
        """Whether two paths are provably the same file; ``None`` if unknown."""
        if self.normalize(a) == self.normalize(b):
            return True
        caps = self.capabilities(a)
        if not caps.stable_inodes:
            return None
        sa, sb = self.stat(a), self.stat(b)
        if sa is None or sb is None or sa.inode is None or sb.inode is None:
            return None
        return sa.inode == sb.inode

    def recover(self) -> None:
        """Re-establish a dropped connection after a transient failure (no-op for local storage)."""

    def close(self) -> None:
        """Release connections and sessions."""


def _read_exact(handle: IO[bytes], length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = handle.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def rel_join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))
