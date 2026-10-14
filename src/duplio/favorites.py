"""The pictures and movies a person has starred.

A favorite is only a reference: where the file is, and what it looked like (its size and time) when it was starred. The file
itself is never touched by starring. The list lives in one small JSON file in Duplio's state folder, written whole and swapped
into place, so a crash never leaves half a list.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Favorite:
    uri: str  # the file's address, as the rest of Duplio takes it (a path, or smb://, sftp://, adb:// …)
    name: str
    size: int
    mtime_ns: int
    added: float  # when it was starred (seconds since 1970)


class Favorites:
    """The starred files, in the order they were starred. Window thread only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, Favorite] = {}
        self.version = 0  # goes up with every change, so a page knows when to redraw
        self.problem = ""  # set when the saved list could not be read or written
        self._load()

    # ---- reading ---------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.problem = f"The list of favorites could not be read ({exc}); it was kept as favorites.json.bad and a new one started."
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            return
        entries = raw.get("items", []) if isinstance(raw, dict) else []
        for entry in entries:
            try:
                fav = Favorite(str(entry["uri"]), str(entry["name"]), int(entry["size"]), int(entry["mtime_ns"]), float(entry["added"]))
            except (KeyError, TypeError, ValueError):
                continue  # one damaged entry does not lose the rest
            self._items[fav.uri] = fav

    def items(self) -> list[Favorite]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, uri: object) -> bool:
        return uri in self._items

    def get(self, uri: str) -> Favorite | None:
        return self._items.get(uri)

    # ---- changing --------------------------------------------------------------
    def add(self, uri: str, name: str, size: int, mtime_ns: int, *, now: float | None = None) -> bool:
        if uri in self._items:
            return False
        self._items[uri] = Favorite(uri, name, size, mtime_ns, now if now is not None else time.time())
        self._changed()
        return True

    def remove(self, uri: str) -> bool:
        if self._items.pop(uri, None) is None:
            return False
        self._changed()
        return True

    def toggle(self, uri: str, name: str, size: int, mtime_ns: int) -> bool:
        """Star it if it is not starred, unstar it if it is. Returns whether it is starred now."""
        if uri in self._items:
            self.remove(uri)
            return False
        self.add(uri, name, size, mtime_ns)
        return True

    def clear(self) -> int:
        count = len(self._items)
        if count:
            self._items.clear()
            self._changed()
        return count

    def _changed(self) -> None:
        self.version += 1
        self.save()

    def save(self) -> None:
        data: dict[str, Any] = {"format": "duplio-favorites", "version": 1, "items": [asdict(f) for f in self._items.values()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self.problem = ""
        except OSError as exc:
            self.problem = f"The list of favorites could not be saved ({exc}). Starred pictures will be forgotten when Duplio closes."
            with contextlib.suppress(OSError):
                tmp.unlink()
