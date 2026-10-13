"""Putting what the window shows in the order a person asks for. Plain logic: nothing here needs a screen.

Names sort the way a person reads them (``IMG_2`` before ``IMG_10``, ``a`` and ``A`` together), and a tie is always settled
by name and then place, so the same click gives the same order every time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from duplio.gui.model import GroupRow, PairRow, Tile

_NUMBER = re.compile(r"(\d+)")

# (key, label) for the "Sort by" boxes. "default" is each screen's own order (what is different first; most space first).
PAIR_FIELDS = (("default", "Differences first"), ("name", "Name"), ("modified", "Date modified"), ("size", "Size"),
               ("type", "Type"), ("folder", "Folder"))
GROUP_FIELDS = (("default", "Most space first"), ("name", "Name"), ("copies", "Number of copies"), ("size", "File size"),
                ("modified", "Date modified"), ("folder", "Folder"))
BROWSE_FIELDS = (("name", "Name"), ("modified", "Date modified"), ("size", "Size"), ("type", "Type"))


def natural_key(text: str) -> tuple[tuple[int, Any], ...]:
    """A key that orders ``file2`` before ``file10`` and ignores upper and lower case."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in _NUMBER.split(text.casefold()) if part)


def type_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].casefold() if 0 < dot < len(name) - 1 else ""


def _tile_key(tile: Tile, field: str) -> Any:
    if field == "name":
        return natural_key(tile.name)
    if field == "modified":
        return tile.entry.mtime_ns
    if field == "size":
        return tile.size
    if field == "type":
        return (type_of(tile.name) or "~", natural_key(tile.name))  # files with no extension last
    if field == "folder":
        return natural_key(tile.folder)
    raise ValueError(f"cannot sort by {field!r}")


def _settle(key: Any, name_key: Any, index: int) -> tuple[Any, Any, int]:
    return (key, name_key, index)  # ties fall back to the name, then to the order given


def sort_pair_rows(rows: Sequence[PairRow], field: str, descending: bool = False) -> list[PairRow]:
    """``rows`` ordered by a property of the file in A (or of the one in B when A has none). ``default`` keeps the order given."""
    if field == "default":
        out = list(rows)
        return out[::-1] if descending else out

    def key(item: tuple[int, PairRow]) -> tuple[Any, Any, int]:
        index, row = item
        tile = row.left or row.right
        assert tile is not None
        return _settle(_tile_key(tile, field), natural_key(tile.name), index)

    ordered = sorted(enumerate(rows), key=key, reverse=descending)
    return [row for _i, row in ordered]


def sort_group_rows(rows: Sequence[GroupRow], field: str, descending: bool = False) -> list[GroupRow]:
    """Duplicate groups ordered by their name, number of copies, size, newest copy or first folder."""
    if field == "default":
        out = list(rows)
        return out[::-1] if descending else out

    def key(item: tuple[int, GroupRow]) -> tuple[Any, Any, int]:
        index, row = item
        first = row.tiles[0]
        if field == "copies":
            value: Any = len(row.tiles)
        elif field == "modified":
            value = max(t.entry.mtime_ns for t in row.tiles)
        else:
            value = _tile_key(first, field)
        return _settle(value, natural_key(first.name), index)

    ordered = sorted(enumerate(rows), key=key, reverse=descending)
    return [row for _i, row in ordered]


@dataclass(frozen=True)
class BrowseItem:
    """One entry of a folder being browsed: a sub-folder (no tile) or a file (with its tile)."""

    name: str
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0
    tile: Tile | None = None
    offline: bool = False

    @property
    def type(self) -> str:
        return "" if self.is_dir else type_of(self.name)


def sort_browse(items: Sequence[BrowseItem], field: str, descending: bool = False, folders_first: bool = True) -> list[BrowseItem]:
    """A folder's entries in the order asked for. Folders may be kept together at the top whatever the order."""

    def key(item: BrowseItem) -> Any:
        if field == "modified":
            return item.mtime_ns
        if field == "size":
            return item.size
        if field == "type":
            return item.type or "~"
        return natural_key(item.name)

    def settle(item: BrowseItem) -> tuple[Any, Any]:
        return (key(item), natural_key(item.name))

    def run(group: list[BrowseItem]) -> list[BrowseItem]:
        ordered = sorted(group, key=settle)
        if descending:
            ordered.reverse()
        return ordered

    if not folders_first:
        return run(list(items))
    folders = [i for i in items if i.is_dir]
    files = [i for i in items if not i.is_dir]
    # folders have no size or type of their own, so they are always ordered by name (or date) among themselves
    folder_field = field if field in ("modified",) else "name"

    def folder_key(item: BrowseItem) -> Any:
        return item.mtime_ns if folder_field == "modified" else natural_key(item.name)

    ordered_folders = sorted(folders, key=lambda i: (folder_key(i), natural_key(i.name)), reverse=descending)
    return ordered_folders + run(files)


def choose(fields: Sequence[tuple[str, str]], label: str) -> str:
    """The key of the field whose label is ``label``."""
    return next(k for k, v in fields if v == label)


def label_of(fields: Sequence[tuple[str, str]], key: str) -> str:
    return next((v for k, v in fields if k == key), fields[0][1])

