"""What the window shows, as plain data: tiles, side-by-side rows and duplicate groups.

Nothing here knows about Tk, so it is tested without a screen. A *tile* is one file as it appears on the screen (a picture
and a few lines of text). A *pair row* puts a file of folder A next to what folder B has for it; a *group row* lays out
every copy of one file, wherever they are.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from duplio.compare.diff import DiffItem, DiffResult, Kind
from duplio.compare.model import DuplicateGroup
from duplio.gui.palette import PALETTE
from duplio.plan.model import ActionType
from duplio.plan.planner import decide
from duplio.plan.rules import parse_rules
from duplio.preview.service import PICTURE_EXT, VIDEO_EXT, PreviewItem, extension
from duplio.scan.inventory import FileEntry, Root
from duplio.util.fmt import format_ns, human_size, plural

PALETTE_OK, PALETTE_WARN, PALETTE_BAD = PALETTE["ok"], PALETTE["warn"], PALETTE["bad"]

PICTURE, VIDEO, AUDIO, DOCUMENT, ARCHIVE, OTHER = "picture", "video", "audio", "document", "archive", "other"

_VIDEO = VIDEO_EXT
_AUDIO = frozenset({"mp3", "m4a", "wav", "flac", "aac", "ogg", "opus", "wma", "aiff", "amr"})
_DOCUMENT = frozenset({"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt", "ods", "odp", "md", "csv", "epub", "pages", "numbers", "key"})
_ARCHIVE = frozenset({"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "dmg"})


def file_kind(name: str) -> str:
    ext = extension(name)
    if ext in PICTURE_EXT:
        return PICTURE
    if ext in _VIDEO:
        return VIDEO
    if ext in _AUDIO:
        return AUDIO
    if ext in _DOCUMENT:
        return DOCUMENT
    if ext in _ARCHIVE:
        return ARCHIVE
    return OTHER


def side_letter(side: int) -> str:
    return "AB"[side] if side in (0, 1) else str(side + 1)


@dataclass(frozen=True)
class Tile:
    """One file as shown on screen."""

    side: int
    entry: FileEntry
    root: Root

    @property
    def key(self) -> str:
        return f"{self.entry.root}:{self.entry.id}"

    @property
    def picture_key(self) -> str:
        """Names this picture of this version of this file, whichever scan the tile came from (``key`` only means something
        inside one scan: the numbers start again at the next one). Empty for a file that has no picture."""
        item = self.preview_item
        return item.key if item is not None else ""

    @property
    def rel(self) -> str:
        return self.entry.rel

    @property
    def name(self) -> str:
        return posixpath.basename(self.entry.rel) or self.entry.rel

    @property
    def folder(self) -> str:
        return posixpath.dirname(self.entry.rel)

    @property
    def size(self) -> int:
        return self.entry.size

    @property
    def kind(self) -> str:
        return file_kind(self.name)

    @property
    def ext(self) -> str:
        return extension(self.name).upper()

    @property
    def details(self) -> str:
        return f"{human_size(self.entry.size)}  ·  {format_ns(self.entry.mtime_ns)[:10]}"

    @property
    def where(self) -> str:
        return f"{side_letter(self.side)}:  {self.entry.rel}"

    @property
    def location(self) -> str:
        return self.root.backend.display(self.entry.path)

    @property
    def preview_item(self) -> PreviewItem | None:
        if self.kind not in (PICTURE, VIDEO):  # a movie gets a picture too: its cover, or a frame taken by ffmpeg
            return None
        e = self.entry
        return PreviewItem(self.root.backend, e.path, e.size, e.mtime_ns, e.rel, e.offline)

    @property
    def online_only(self) -> bool:
        return self.entry.offline


def tiles_of(entries: Iterable[FileEntry], roots: Sequence[Root]) -> list[Tile]:
    return [Tile(e.root, e, roots[e.root]) for e in entries]


# --------------------------------------------------------------------------- two folders side by side
class RowKind(StrEnum):
    ONLY_A = "only-a"
    ONLY_B = "only-b"
    MOVED = "moved"
    CHANGED = "changed"
    SAME = "same"
    UNREADABLE = "unreadable"


ROW_ORDER = (RowKind.ONLY_A, RowKind.ONLY_B, RowKind.CHANGED, RowKind.MOVED, RowKind.UNREADABLE, RowKind.SAME)


@dataclass
class PairRow:
    kind: RowKind
    left: Tile | None
    right: Tile | None
    extra_left: int = 0  # more identical copies on the left
    extra_right: int = 0
    relation: str = ""  # what to say between the two tiles
    note: str = ""
    index: int = 0
    glyph: str = ""  # replaces the symbol for the kind (the sync preview shows arrows)
    color: str = ""
    left_text: str = ""  # what an empty left (or right) slot says instead of "Not in A"
    right_text: str = ""

    @property
    def tiles(self) -> list[Tile]:
        return [t for t in (self.left, self.right) if t is not None]


def _relation(item: DiffItem, side: int) -> tuple[RowKind, str]:
    mine, theirs = side_letter(side), side_letter(1 - side)
    if item.kind is Kind.SAME:
        return RowKind.SAME, "Identical"
    if item.kind is Kind.MOVED:
        return RowKind.MOVED, "Same content, different name or place"
    if item.kind is Kind.CHANGED:
        if item.newer > 0:
            return RowKind.CHANGED, f"Different content ({mine} is newer)"
        if item.newer < 0:
            return RowKind.CHANGED, f"Different content ({theirs} is newer)"
        return RowKind.CHANGED, "Different content"
    if item.kind is Kind.UNREADABLE:
        return RowKind.UNREADABLE, "Could not be read"
    return (RowKind.ONLY_A, f"Missing from {theirs}") if side == 0 else (RowKind.ONLY_B, f"Not in {theirs}")


def build_pair_rows(diff: DiffResult) -> list[PairRow]:
    """One row per file of A with whatever B has for it, plus the files of B that no row of A already shows."""
    roots = diff.inventory.roots
    rows: list[PairRow] = []
    shown_right: set[int] = set()

    def tile(entry: FileEntry) -> Tile:
        return Tile(entry.root, entry, roots[entry.root])

    for item in diff.items:
        if item.side != 0:
            continue
        kind, relation = _relation(item, 0)
        right = tile(item.others[0]) if item.others else None
        shown_right.update(o.id for o in item.others)
        rows.append(PairRow(kind, tile(item.entry), right, item.extra_copies, max(len(item.others) - 1, 0), relation, item.note))
    for item in diff.items:
        if item.side != 1 or item.entry.id in shown_right:
            continue
        kind, relation = _relation(item, 1)
        left = tile(item.others[0]) if item.others else None
        rows.append(PairRow(kind, left, tile(item.entry), max(len(item.others) - 1, 0), item.extra_copies, relation, item.note))
    rows.sort(key=lambda r: (ROW_ORDER.index(r.kind), (r.left or r.right).rel.casefold()))  # type: ignore[union-attr]
    for n, row in enumerate(rows):
        row.index = n
    return rows


def count_rows(rows: Iterable[PairRow]) -> Counter[RowKind]:
    return Counter(r.kind for r in rows)


ROW_TITLES = {
    RowKind.ONLY_A: "Only in A",
    RowKind.ONLY_B: "Only in B",
    RowKind.CHANGED: "Different",
    RowKind.MOVED: "Renamed or moved",
    RowKind.UNREADABLE: "Could not be read",
    RowKind.SAME: "Identical",
}


@dataclass
class BackupVerdict:
    """"Is everything in A also in B?" in numbers and words."""

    safe: int
    safe_bytes: int
    missing: int
    missing_bytes: int
    different: int
    unreadable: int
    headline: str
    detail: str
    ok: bool
    partial: bool = False  # something was not looked at (sub-folders were left out), so "safe" does not cover everything


def scope_notes(diff: DiffResult, names: tuple[str, str]) -> list[str]:
    """What a check did NOT look at: the sub-folders of a folder that was checked without them."""
    notes = []
    inventory = diff.inventory
    for index, name in enumerate(names):
        left = inventory.unvisited[index] if index < len(inventory.unvisited) else []
        if inventory.roots[index].recursive or not left:
            continue
        shown = ", ".join(sorted(left, key=str.casefold)[:3]) + (f" and {len(left) - 3:,} more" if len(left) > 3 else "")
        notes.append(f"{name}: only the files directly inside it were checked; its {plural(len(left), 'sub-folder')} ({shown}) were not looked "
                     "inside")
    return notes


def backup_verdict(diff: DiffResult, a_name: str = "A", b_name: str = "B") -> BackupVerdict:
    mine = [i for i in diff.items if i.side == 0]
    safe = [i for i in mine if i.kind in (Kind.SAME, Kind.MOVED)]
    missing = [i for i in mine if i.kind is Kind.ONLY]
    different = [i for i in mine if i.kind is Kind.CHANGED]
    unreadable = [i for i in mine if i.kind is Kind.UNREADABLE]
    not_safe = len(missing) + len(different)
    notes = scope_notes(diff, (a_name, b_name))
    a_partial = bool(diff.inventory.unvisited and diff.inventory.unvisited[0] and not diff.inventory.roots[0].recursive)
    b_partial = bool(len(diff.inventory.unvisited) > 1 and diff.inventory.unvisited[1] and not diff.inventory.roots[1].recursive)
    where = f"directly in {a_name}" if a_partial else f"in {a_name}"
    if not mine:
        headline, ok = f"There are no files {where} to check.", True
    elif not not_safe and not unreadable:
        headline, ok = f"Everything {where} is safe in {b_name}.", True
    elif not not_safe:
        headline, ok = f"{len(unreadable):,} files could not be checked.", False
    else:
        headline, ok = f"{not_safe:,} of {len(mine):,} files {where} are NOT in {b_name}.", False
    parts = [f"{len(safe):,} files are safe (an identical copy exists in {b_name}, whatever it is called there)"]
    if missing:
        parts.append(f"{len(missing):,} exist only in {a_name} ({human_size(sum(i.entry.size for i in missing))})")
    if different:
        parts.append(f"{len(different):,} are in {b_name} only as a different version")
    if unreadable:
        parts.append(f"{len(unreadable):,} could not be read, so nothing is claimed about them")
    detail = "; ".join(parts) + "."
    if b_partial and (missing or different):
        detail += (f" {b_name} has sub-folders that were not looked inside, so a copy kept in one of them shows here as missing: "
                   "tick the box to include them and check again.")
    detail = " ".join([detail, *[n + "." for n in notes]]) if notes else detail
    return BackupVerdict(len(safe), sum(i.entry.size for i in safe), len(missing), sum(i.entry.size for i in missing),
                         len(different), len(unreadable), headline, detail, ok, partial=a_partial)


# --------------------------------------------------------------------------- duplicates
class Mark(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"
    LOCKED = "locked"  # may not be touched (reference folder, protected path, read-only drive)


@dataclass
class GroupRow:
    group: DuplicateGroup
    tiles: list[Tile]
    keep: set[int] = field(default_factory=set)  # entry ids that stay
    locked: dict[int, str] = field(default_factory=dict)  # entry id -> why it may not be touched
    skipped: bool = False  # leave this group alone
    index: int = 0

    def mark(self, tile: Tile) -> Mark:
        if tile.entry.id in self.locked:
            return Mark.LOCKED
        return Mark.KEEP if tile.entry.id in self.keep or self.skipped else Mark.REMOVE

    @property
    def removals(self) -> list[Tile]:
        return [t for t in self.tiles if not self.skipped and self.mark(t) is Mark.REMOVE]

    @property
    def reclaimable(self) -> int:
        return sum(t.size for t in self.removals)

    def toggle(self, tile: Tile) -> bool:
        """Switch one copy between keep and remove. At least one copy always stays; returns whether anything changed."""
        eid = tile.entry.id
        if eid in self.locked:
            return False
        if self.skipped:
            self.skipped = False
        if eid in self.keep:
            if len(self.keep) <= 1:
                return False  # the last kept copy cannot be unmarked
            self.keep.discard(eid)
        else:
            self.keep.add(eid)
        return True

    def keep_only(self, tile: Tile) -> None:
        if tile.entry.id in self.locked:
            return
        self.skipped = False
        self.keep = {tile.entry.id}


def build_group_rows(groups: Sequence[DuplicateGroup], roots: Sequence[Root], rules: Sequence[str],
                     action: ActionType = ActionType.QUARANTINE) -> list[GroupRow]:
    parsed = parse_rules(list(rules))
    out: list[GroupRow] = []
    for g in sorted(groups, key=lambda g: (-g.wasted, g.id)):
        d = decide(g, roots, parsed, action)
        row = GroupRow(g, tiles_of(g.members, roots))
        row.keep = {m.id for m in d.keep}
        row.locked = dict(d.reasons)
        out.append(row)
    for n, row in enumerate(out):
        row.index = n
    return out


def apply_rules(rows: Sequence[GroupRow], roots: Sequence[Root], rules: Sequence[str],
                action: ActionType = ActionType.QUARANTINE) -> None:
    """Choose again which copy of each group stays, discarding earlier clicks."""
    parsed = parse_rules(list(rules))
    for row in rows:
        d = decide(row.group, roots, parsed, action)
        row.keep = {m.id for m in d.keep}
        row.locked = dict(d.reasons)
        row.skipped = False


def keep_overrides(rows: Sequence[GroupRow]) -> dict[int, list[int]]:
    """``{group id: entry ids to keep}`` for ``build_plan``, leaving out groups the user skipped."""
    return {r.group.id: sorted(r.keep) for r in rows if not r.skipped}


def chosen_group_ids(rows: Sequence[GroupRow]) -> list[int]:
    return [r.group.id for r in rows if not r.skipped and r.removals]


# --------------------------------------------------------------------------- what a sync or an import would do
def sync_rows(plan) -> list[PairRow]:  # type: ignore[no-untyped-def]
    """The changes a two-way sync would make, as rows: A on the left, B on the right, an arrow between."""
    from duplio.backup.sync import SyncOp

    roots = plan.roots

    def tile(entry: FileEntry | None) -> Tile | None:
        return Tile(entry.root, entry, roots[entry.root]) if entry is not None else None

    out: list[PairRow] = []
    for a in plan.actions:
        if a.op is SyncOp.MARK:
            continue
        left, right = tile(a.a), tile(a.b)
        if a.op is SyncOp.COPY:
            to_b = a.frm == 0
            out.append(PairRow(RowKind.ONLY_A if to_b else RowKind.ONLY_B, left, right, relation="Copied to B" if to_b else "Copied to A",
                               glyph="→" if to_b else "←", color=PALETTE_OK, note=a.reason,
                               right_text="Will be copied here" if to_b else "", left_text="" if to_b else "Will be copied here"))
        elif a.op is SyncOp.UPDATE:
            to_b = a.frm == 0
            out.append(PairRow(RowKind.CHANGED, left, right, relation=("B is updated from A" if to_b else "A is updated from B"),
                               glyph="→" if to_b else "←", color=PALETTE_WARN, note="the replaced version is kept"))
        elif a.op is SyncOp.DELETE:
            from_b = a.side == 1
            out.append(PairRow(RowKind.ONLY_B if from_b else RowKind.ONLY_A, left, right,
                               relation=("Removed from B" if from_b else "Removed from A"), glyph="✗", color=PALETTE_BAD,
                               note=a.reason or "it was deleted on the other side; this one goes to the quarantine",
                               left_text="deleted here" if from_b else "", right_text="" if from_b else "deleted here"))
        else:  # conflict
            out.append(PairRow(RowKind.CHANGED, left, right, relation="Changed on both sides", glyph="⚡", color=PALETTE_WARN,
                               note=a.reason or "both versions are kept"))
    for n, row in enumerate(out):
        row.index = n
    return out


def ingest_rows(report, source) -> list[PairRow]:  # type: ignore[no-untyped-def]
    """What an import would do with each file on the phone or card (``source`` is the phone's :class:`Location`)."""
    from duplio.scan.inventory import RootRole

    root = Root(0, source, RootRole.NORMAL, "", source.caps)
    out: list[PairRow] = []
    for n, item in enumerate(report.items):
        entry = FileEntry(n + 1, 0, source.backend.join(source.path, item.source_rel), item.source_rel, item.size, item.mtime_ns)
        tile = Tile(0, entry, root)
        if item.status in ("would-import", "imported"):
            out.append(PairRow(RowKind.ONLY_A, tile, None, relation="Will be imported" if item.status == "would-import" else "Imported",
                               glyph="→", color=PALETTE_OK, note=item.dest_rel or "", right_text="Will be copied into the library"))
        elif item.status == "maybe-duplicate":
            out.append(PairRow(RowKind.ONLY_A, tile, None, relation="Compared on import", glyph="?", color=PALETTE_WARN,
                               note="same size as a file already in the library", right_text="Maybe already there"))
        elif item.status == "failed":
            out.append(PairRow(RowKind.UNREADABLE, tile, None, relation="Failed", note=item.message, right_text="Not imported"))
        else:
            out.append(PairRow(RowKind.SAME, tile, None, relation="Already in the library", note=item.dest_rel or "",
                               glyph="✓", color=PALETTE_OK, right_text="Already imported" if item.status == "previously-imported" else
                               f"Already there: {item.dest_rel}" if item.dest_rel else "Already there"))
    for n, row in enumerate(out):
        row.index = n
    return out


def build_server_address(scheme: str, host: str, user: str, port: str, folder: str) -> str:
    """The address Duplio uses for a server, from the pieces a person fills in."""
    from urllib.parse import quote

    host = host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    who = f"{quote(user.strip(), safe='')}@" if user.strip() else ""
    where = f":{port.strip()}" if port.strip() else ""
    path = "/" + quote(folder.strip().strip("/"), safe="/") if folder.strip().strip("/") else "/"
    if scheme == "smb":
        return f"smb://{who}{host}{where}{path}"
    return f"{scheme}://{who}{host}{where}{path}"
