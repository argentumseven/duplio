"""Discover files under one or more roots, possibly on different backends.

Roots are walked concurrently (a phone and a NAS should not wait for each
other). Before walking, roots are checked for overlap: scanning ``~/Photos``
and ``~/Photos/2024`` together — or the same folder through a bind mount or
symlink — would make every file look like its own duplicate, and removing
"one copy" would remove the only copy.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from duplio.errors import LocationError, OverlappingRootsError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.scan.filters import ScanFilter
from duplio.vfs.base import Capabilities, WalkStats
from duplio.vfs.local import LocalBackend
from duplio.vfs.location import Location


class RootRole(StrEnum):
    NORMAL = "normal"
    REFERENCE = "reference"  # read-only: used for matching, never modified


@dataclass
class Root:
    index: int
    location: Location
    role: RootRole = RootRole.NORMAL
    label: str = ""
    caps: Capabilities = field(default_factory=Capabilities)
    recursive: bool = True  # False: only the files directly in this folder are looked at, not those in its sub-folders

    @property
    def backend(self):  # type: ignore[no-untyped-def]
        return self.location.backend

    @property
    def path(self) -> str:
        return self.location.path

    @property
    def is_reference(self) -> bool:
        return self.role is RootRole.REFERENCE

    @property
    def writable(self) -> bool:
        return not self.is_reference and self.caps.can_delete

    @property
    def name(self) -> str:
        return self.label or self.location.display


@dataclass(slots=True)
class FileEntry:
    """A regular file in the inventory."""

    id: int
    root: int
    path: str
    rel: str
    size: int
    mtime_ns: int
    inode: tuple[int, int] | None = None
    nlink: int = 1
    protected: bool = False
    ctime_ns: int | None = None  # inode change time where the storage reports a real one
    offline: bool = False  # a cloud-only placeholder: reading it downloads it


@dataclass
class Inventory:
    roots: list[Root]
    entries: list[FileEntry] = field(default_factory=list)
    walk_stats: list[WalkStats] = field(default_factory=list)
    unvisited: list[list[str]] = field(default_factory=list)  # per root: sub-folders that were not looked inside (non-recursive roots)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def errors(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for st in self.walk_stats:
            out.extend(st.errors)
        return out

    def root_of(self, entry: FileEntry) -> Root:
        return self.roots[entry.root]


def check_overlaps(roots: Sequence[Root]) -> None:
    """Raise :class:`OverlappingRootsError` if any two roots overlap."""
    for i, a in enumerate(roots):
        for b in roots[i + 1 :]:
            if a.backend.id != b.backend.id:
                if _same_phone_twice(a, b) or _same_phone_twice(b, a):
                    raise OverlappingRootsError(
                        f"'{a.location.display}' and '{b.location.display}' look like the same phone reached "
                        "through ADB and MTP at once. Use one access method per scan."
                    )
                continue
            if a.backend.is_within(a.path, b.path) or b.backend.is_within(b.path, a.path):
                raise OverlappingRootsError(
                    f"'{a.location.display}' and '{b.location.display}' overlap. "
                    "Scan the parent folder once instead (files would otherwise be matched with themselves)."
                )
            if isinstance(a.backend, LocalBackend) and (_local_alias(a.path, b.path) or _local_alias(b.path, a.path)):
                raise OverlappingRootsError(
                    f"'{a.location.display}' and '{b.location.display}' refer to the same folder tree "
                    "(through a symlink or bind mount)."
                )


def _same_phone_twice(adb_root: Root, other: Root) -> bool:
    serial = getattr(adb_root.backend, "serial", None)
    if adb_root.backend.scheme != "adb" or not serial or not isinstance(other.backend, LocalBackend):
        return False
    path = other.path
    return "mtp:host=" in path and serial.lower() in path.lower()


def _local_alias(inner: str, outer: str) -> bool:
    """Is ``inner`` (or any of its ancestors) the same directory as ``outer``?"""
    try:
        outer_st = os.stat(outer)
    except OSError:
        return False
    probe = os.path.realpath(inner)
    seen = 0
    while True:
        try:
            st = os.stat(probe)
            if (st.st_dev, st.st_ino) == (outer_st.st_dev, outer_st.st_ino):
                return True
        except OSError:
            return False
        parent = os.path.dirname(probe)
        if parent == probe or seen > 256:
            return False
        probe = parent
        seen += 1


def make_roots(locations: Sequence[tuple[Location, RootRole]], labels: Sequence[str] | None = None) -> list[Root]:
    roots: list[Root] = []
    for idx, (loc, role) in enumerate(locations):
        info = loc.backend.stat(loc.path)
        if info is not None and info.is_symlink and isinstance(loc.backend, LocalBackend):
            # A root given as a symlink (e.g. ~/Pictures -> /data/Pictures) is scanned at its
            # real location, which also lets the overlap check see through the link.
            loc = Location(loc.backend, os.path.realpath(loc.path))
            info = loc.backend.stat(loc.path)
        if info is None:
            raise LocationError(f"location does not exist: {loc.display}")
        if not info.is_dir:
            raise LocationError(f"location is not a folder: {loc.display}")
        caps = loc.backend.capabilities(loc.path)
        label = labels[idx] if labels and idx < len(labels) else ""
        roots.append(Root(index=idx, location=loc, role=role, label=label, caps=caps))
    check_overlaps(roots)
    return roots


class _TopFolderOnly:
    """A walk filter that keeps the walk in the top folder, and notes the sub-folders it did not go into."""

    def __init__(self, inner: ScanFilter, unvisited: list[str]) -> None:
        self._inner = inner
        self._unvisited = unvisited

    def skip_dir(self, rel: str, name: str) -> bool:
        if not self._inner.skip_dir(rel, name):  # only folders that would have been looked into count as left out
            self._unvisited.append(rel or name)
        return True

    def skip_file(self, rel: str, name: str, size: int) -> bool:
        return self._inner.skip_file(rel, name, size)


def scan(
    roots: Sequence[Root],
    scan_filter: ScanFilter,
    *,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    one_file_system: bool = True,
) -> Inventory:
    """Walk all roots concurrently and build an :class:`Inventory`."""
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    progress.stage("Discovering files")
    inventory = Inventory(roots=list(roots))
    per_root: list[list[FileEntry]] = [[] for _ in roots]
    stats = [WalkStats() for _ in roots]
    unvisited: list[list[str]] = [[] for _ in roots]
    lock = threading.Lock()

    def walk_one(root: Root) -> None:
        bucket = per_root[root.index]
        pending = 0
        walk_filter = scan_filter if root.recursive else _TopFolderOnly(scan_filter, unvisited[root.index])
        for raw in root.backend.walk(
            root.path,
            walk_filter=walk_filter,
            stats=stats[root.index],
            one_file_system=one_file_system,
            should_stop=lambda: cancel.cancelled,
        ):
            if scan_filter.uses_time and scan_filter.skip_by_time(raw.mtime_ns):
                continue
            bucket.append(
                FileEntry(
                    id=-1,
                    root=root.index,
                    path=raw.path,
                    rel=raw.rel,
                    size=raw.size,
                    mtime_ns=raw.mtime_ns,
                    inode=raw.inode,
                    nlink=raw.nlink,
                    protected=scan_filter.is_protected(raw.rel),
                    ctime_ns=raw.ctime_ns,
                    offline=raw.offline,
                )
            )
            pending += 1
            if pending >= 256:
                with lock:
                    progress.advance(items=pending, detail=raw.rel)
                pending = 0
        if pending:
            with lock:
                progress.advance(items=pending)

    with ThreadPoolExecutor(max_workers=max(1, len(roots)), thread_name_prefix="duplio-walk") as pool:
        futures = [pool.submit(walk_one, r) for r in roots]
        for fut in futures:
            fut.result()
    cancel.check()

    next_id = 0
    for bucket in per_root:
        for entry in bucket:
            entry.id = next_id
            next_id += 1
            inventory.entries.append(entry)
    inventory.walk_stats = stats
    inventory.unvisited = unvisited
    for root, st in zip(roots, stats, strict=True):
        if st.errors:
            progress.message(
                f"{len(st.errors)} item(s) under {root.location.display} could not be read", level="warning"
            )
    return inventory
