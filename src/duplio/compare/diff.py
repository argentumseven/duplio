"""Compare two folders without caring what their files are called.

Most "folder compare" tools pair files by path and then look at size and time.
That answers "did this file change?" but not the question people actually have:
*is everything in A also in B, whatever it is called there?* Duplio answers both.

Every file ends up in exactly one category (per side):

``same``       identical content at the same relative path on the other side
``moved``      identical content exists on the other side, but under a different
               path or name (renamed, relocated, or re-sorted into other folders)
``changed``    a file with the same path exists on the other side, with different
               content (the newer side is reported)
``only``       nothing like it on the other side: not at that path, not anywhere
``unreadable`` could not be read, so nothing is claimed about it

Three comparison bases:

``content``    default; the staged engine (size → fingerprint → hash [→ bytes]) decides
               what is identical. Only files whose size matches a file on the other
               side are ever read.
``time-size``  FreeFileSync style: same path, same size, same modification time (within
               what both drives can store; optionally forgiving the exact one-hour
               shift between FAT/NTFS and Unix). Nothing is read unless ``detect_moves``
               is on, which hashes only the files that did *not* match, to find moves
               and to confirm files whose only difference is the timestamp.
``size``       same path and size only.

Paths are matched after Unicode normalisation, and case-insensitively when either
drive is case-insensitive, so "Résumé.doc" on a Mac and the same name on Linux are
the same path.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from duplio.compare.engine import Engine
from duplio.compare.model import DuplicateGroup, EngineStats, Mode, ScanOptions, ScanResult, Verify
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.cache import HashCache
from duplio.scan.inventory import FileEntry, Inventory
from duplio.util.text import name_key
from duplio.util.timecmp import SECOND_NS, mtimes_match, tolerance_for, trusted_newer


class Basis(StrEnum):
    CONTENT = "content"
    TIME_SIZE = "time-size"
    SIZE = "size"


BASIS_HELP = {
    Basis.CONTENT: "Compare what is inside the files (hash). Finds renamed and moved files. The default.",
    Basis.TIME_SIZE: "Same path, size and modification time. Fast; reads no file contents unless looking for moves.",
    Basis.SIZE: "Same path and size only.",
}


class Kind(StrEnum):
    SAME = "same"
    MOVED = "moved"
    CHANGED = "changed"
    ONLY = "only"
    UNREADABLE = "unreadable"


KIND_ORDER = (Kind.ONLY, Kind.CHANGED, Kind.MOVED, Kind.UNREADABLE, Kind.SAME)
KIND_TITLE = {
    Kind.SAME: "Identical (same path)",
    Kind.MOVED: "Moved or renamed",
    Kind.CHANGED: "Different (same path)",
    Kind.ONLY: "Only here",
    Kind.UNREADABLE: "Could not be read",
}


@dataclass
class DiffOptions:
    basis: Basis = Basis.CONTENT
    verify: Verify = Verify.HASH  # hash | bytes (a quick/name-only comparison is not offered here)
    detect_moves: bool = True  # non-content bases: read files that did not match, to find moves
    time_tolerance_ns: int | None = None  # None: derive from the two drives
    ignore_dst: bool = False  # accept an exact one-hour difference as equal
    full_algorithm: str | None = None
    use_sampling: bool = True
    one_file_system: bool = True
    local_workers: int | None = None

    def scan_options(self, mode: Mode) -> ScanOptions:
        opts = ScanOptions(mode=mode, verify=self.verify, use_sampling=self.use_sampling,
                           one_file_system=self.one_file_system, local_workers=self.local_workers)
        if self.full_algorithm:
            opts.full_algorithm = self.full_algorithm
        return opts


@dataclass
class DiffItem:
    side: int  # 0 = A (first location), 1 = B
    kind: Kind
    entry: FileEntry
    others: list[FileEntry] = field(default_factory=list)  # counterparts on the other side
    extra_copies: int = 0  # more identical copies on this same side
    newer: int = 0  # changed: +1 this side is newer, -1 the other is, 0 equal within tolerance
    group: int | None = None  # id of the identical-content group, when one is known
    note: str = ""


@dataclass
class DiffResult:
    inventory: Inventory
    options: DiffOptions
    items: list[DiffItem]
    groups: list[DuplicateGroup]
    stats: EngineStats
    tolerance_ns: int
    case_folded: bool
    elapsed: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def select(self, kind: Kind | None = None, side: int | None = None) -> list[DiffItem]:
        return [i for i in self.items if (kind is None or i.kind is kind) and (side is None or i.side == side)]

    def count(self, kind: Kind, side: int) -> int:
        return sum(1 for i in self.items if i.kind is kind and i.side == side)

    def size(self, kind: Kind, side: int) -> int:
        return sum(i.entry.size for i in self.items if i.kind is kind and i.side == side)

    @property
    def basis_is_exact(self) -> bool:
        return self.options.basis is Basis.CONTENT

    def duplicates_within(self, side: int) -> tuple[int, int]:
        """(redundant files, redundant bytes) among the files of one side that are identical to each other."""
        files = bytes_ = 0
        for g in self.groups:
            on_side = [m for m in _group_entries(g) if m.root == side]
            if len(on_side) > 1:
                files += len(on_side) - 1
                bytes_ += g.size * (len(on_side) - 1)
        return files, bytes_

    @property
    def identical_content(self) -> bool:
        """Every file on both sides has an identical counterpart (layout may differ)."""
        return all(i.kind in (Kind.SAME, Kind.MOVED) for i in self.items)

    @property
    def identical_layout(self) -> bool:
        return all(i.kind is Kind.SAME for i in self.items)


def _group_entries(group: DuplicateGroup) -> list[FileEntry]:
    out: list[FileEntry] = []
    for m in group.members:
        out.append(m)
        out.extend(group.aliases.get(m.id, ()))
    return out


def diff_inventory(
    inventory: Inventory,
    options: DiffOptions | None = None,
    *,
    cache: HashCache | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> DiffResult:
    """Classify every file in a two-root inventory (see the module docstring)."""
    options = options or DiffOptions()
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    if len(inventory.roots) != 2:
        raise ValueError("a comparison needs exactly two locations")
    started = time.monotonic()
    roots = inventory.roots
    fold = any(r.caps.case_insensitive for r in roots)
    tolerance = options.time_tolerance_ns
    if tolerance is None:
        # 1 µs floor: tools that copy times through floating point lose the last digits
        tolerance = tolerance_for(*(r.caps.mtime_resolution_ns for r in roots), floor_ns=1_000)
    tolerance = max(tolerance, 1)

    def key_of(entry: FileEntry) -> str:
        return name_key(entry.rel, fold=fold)

    # Path index: (side, key) -> entries; several only where case/normalisation folds names together.
    by_path: dict[tuple[int, str], list[FileEntry]] = defaultdict(list)
    for e in inventory.entries:
        by_path[(e.root, key_of(e))].append(e)

    def counterparts(entry: FileEntry) -> list[FileEntry]:
        found = by_path.get((1 - entry.root, key_of(entry)), [])
        exact = [c for c in found if c.rel == entry.rel]
        return exact or found

    decided: dict[int, DiffItem] = {}
    stats = EngineStats()
    groups: list[DuplicateGroup] = []
    failed: set[int] = set()
    errors: list[tuple[str, str]] = []

    # ---- 1. the very same file reachable from both sides (hard link): identical without reading it
    linked: dict[tuple[str, tuple[int, int]], list[FileEntry]] = defaultdict(list)
    for e in inventory.entries:
        root = roots[e.root]
        if e.inode is not None and root.caps.stable_inodes:
            linked[(root.backend.id, e.inode)].append(e)
    for members in linked.values():
        if len({m.root for m in members}) < 2:
            continue
        for m in members:
            there = [o for o in members if o.root != m.root]
            same_key = [o for o in there if key_of(o) == key_of(m)]
            decided[m.id] = DiffItem(m.root, Kind.SAME if same_key else Kind.MOVED, m, same_key or there,
                                     note="the same file (hard link)")

    # ---- 2. cheap path-based matching (non-content bases)
    pending = [e for e in inventory.entries if e.id not in decided]
    path_matched: set[int] = set()
    if options.basis is not Basis.CONTENT:
        undecided, pending = pending, []
        progress.stage("Matching paths", total_items=len(undecided))
        for e in undecided:
            cancel.check()
            others = counterparts(e)
            matching = [o for o in others if _equal_by_basis(e, o, options, tolerance)]
            if matching:
                decided[e.id] = DiffItem(e.root, Kind.SAME, e, matching,
                                         note="" if options.basis is Basis.TIME_SIZE else "size only")
                path_matched.add(e.id)
            else:
                pending.append(e)
        progress.advance(items=len(undecided))

    # ---- 3. content comparison (everything undecided, or only the leftovers)
    group_of: dict[int, DuplicateGroup] = {}
    if options.basis is Basis.CONTENT or options.detect_moves:
        # Hard-linked entries (already decided above) still take part: other files with the same content must be
        # able to match them. A link set alone in its size bucket is never read.
        sub = Inventory(roots=roots, entries=[e for e in inventory.entries if e.id not in path_matched])
        mode = Mode.ALL if options.basis is Basis.CONTENT else Mode.CROSS
        if sub.entries:
            result: ScanResult = Engine(sub, options.scan_options(mode), cache=cache, progress=progress,
                                        cancel=cancel).run()
            groups = result.groups
            stats = result.stats
            failed = {e.id for e in result.failed}
            errors = list(result.stats.errors)
            for g in groups:
                for m in _group_entries(g):
                    group_of[m.id] = g
    cancel.check()

    # ---- 4. classify what is left
    failed_sizes: dict[int, set[int]] = {0: set(), 1: set()}
    for e in inventory.entries:
        if e.id in failed:
            failed_sizes[e.root].add(e.size)
    for e in pending:
        item = _classify(e, group_of.get(e.id), e.id in failed, counterparts(e), failed, tolerance, options,
                         failed_sizes)
        decided[e.id] = item
    items = [decided[e.id] for e in inventory.entries if e.id in decided]
    items.sort(key=lambda i: (KIND_ORDER.index(i.kind), i.side, i.entry.rel))
    return DiffResult(inventory=inventory, options=options, items=items, groups=groups, stats=stats,
                      tolerance_ns=tolerance, case_folded=fold, elapsed=time.monotonic() - started, errors=errors)


def _equal_by_basis(a: FileEntry, b: FileEntry, options: DiffOptions, tolerance: int) -> bool:
    if a.size != b.size:
        return False
    if options.basis is Basis.SIZE:
        return True
    return mtimes_match(a.mtime_ns, b.mtime_ns, tolerance_ns=tolerance, dst_shift=options.ignore_dst)


def _classify(
    entry: FileEntry,
    group: DuplicateGroup | None,
    unreadable: bool,
    same_path: list[FileEntry],
    failed: set[int],
    tolerance: int,
    options: DiffOptions,
    failed_sizes: dict[int, set[int]],
) -> DiffItem:
    side = entry.root
    if unreadable:
        return DiffItem(side, Kind.UNREADABLE, entry, same_path, note="could not be read")
    if group is not None:
        members = _group_entries(group)
        there = [m for m in members if m.root != side]
        extra = sum(1 for m in members if m.root == side) - 1
        if there:
            same_key = {m.id for m in same_path}
            at_same_path = [m for m in there if m.id in same_key]
            if at_same_path:
                note = ""
                if options.basis is not Basis.CONTENT:
                    note = "same content, different timestamp"
                return DiffItem(side, Kind.SAME, entry, at_same_path, extra_copies=extra, group=group.id, note=note)
            return DiffItem(side, Kind.MOVED, entry, there, extra_copies=extra, group=group.id)
        extra_only = extra
    else:
        extra_only = 0
    gid = group.id if group is not None else None
    if same_path:
        other = same_path[0]
        if entry.size == other.size and other.id in failed:
            return DiffItem(side, Kind.UNREADABLE, entry, same_path,
                            note="the file at this path on the other side could not be read")
        direction = trusted_newer(entry.mtime_ns, other.mtime_ns, now_ns=time.time_ns(),
                                  tolerance_ns=max(tolerance, SECOND_NS), dst_shift=options.ignore_dst)
        note = ""
        if direction == 0 and abs(entry.mtime_ns - other.mtime_ns) > max(tolerance, SECOND_NS):
            note = "a modification time is in the future, so neither side is called newer"
        return DiffItem(side, Kind.CHANGED, entry, same_path, extra_copies=extra_only, newer=direction, group=gid,
                        note=note)
    if entry.size in failed_sizes[1 - side]:
        # Something of this size on the other side could not be read: it might be this very content.
        return DiffItem(side, Kind.UNREADABLE, entry, [], group=gid,
                        note="a file of the same size on the other side could not be read, so this cannot be ruled out "
                             "as already there")
    return DiffItem(side, Kind.ONLY, entry, [], extra_copies=extra_only, group=gid)
