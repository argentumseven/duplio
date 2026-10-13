"""Reading one folder for the folder browser: its sub-folders, and its files with sizes and times, in one trip to the drive.

A remote folder is listed once (a WebDAV PROPFIND, an FTP MLSD, an SFTP readdir) and the answer carries every size and
time, so a folder of thousands of pictures costs one request, not one per file. A short-lived memory of listings lets
Back and Up be instant; it is only for looking, never used for anything that changes a file, and Refresh always asks the
drive again. Nothing here needs Tk.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from duplio.gui.sorting import BrowseItem
from duplio.scan.inventory import FileEntry, Root, RootRole
from duplio.vfs.base import Backend, RawEntry, WalkStats
from duplio.vfs.location import Location

MAX_ENTRIES = 250_000  # beyond this a folder is shown cut short, and says so (the window stays responsive)


@dataclass
class FolderListing:
    backend: Backend
    path: str
    dirs: list[str] = field(default_factory=list)
    files: list[RawEntry] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    read_at: float = field(default_factory=time.monotonic)

    @property
    def total(self) -> int:
        return len(self.dirs) + len(self.files)


class _OneLevel:
    """A walk filter that stops the walk at this folder and notes the sub-folders it passes."""

    def __init__(self) -> None:
        self.dirs: list[str] = []

    def skip_dir(self, rel: str, name: str) -> bool:
        self.dirs.append(name)
        return True

    def skip_file(self, rel: str, name: str, size: int) -> bool:
        return False


def read_folder(backend: Backend, path: str, *, show_hidden: bool = False, should_stop: Callable[[], bool] | None = None,
                limit: int = MAX_ENTRIES) -> FolderListing:
    """The folder's contents. Raises nothing for an unreadable folder: the reason is in ``errors``."""
    one = _OneLevel()
    stats = WalkStats()
    out = FolderListing(backend, path)
    for raw in backend.walk(path, walk_filter=one, stats=stats, one_file_system=False, should_stop=should_stop):
        if len(out.files) + len(one.dirs) >= limit:
            out.truncated = True
            break
        if show_hidden or not raw.rel.startswith("."):
            out.files.append(raw)
    out.dirs = [d for d in one.dirs if show_hidden or not d.startswith(".")][: max(0, limit - len(out.files))]
    out.errors = list(stats.errors)
    out.read_at = time.monotonic()
    return out


def items_of(listing: FolderListing, location: Location) -> list[BrowseItem]:
    """The entries as things the browser can show: folders, and files with a tile (so pictures can have thumbnails)."""
    from duplio.gui.model import Tile

    root = Root(0, location, RootRole.NORMAL, "", location.caps)
    items = [BrowseItem(name, True) for name in listing.dirs]
    for n, raw in enumerate(listing.files):
        entry = FileEntry(n + 1, 0, raw.path, raw.rel, raw.size, raw.mtime_ns, offline=raw.offline)
        items.append(BrowseItem(raw.rel, False, raw.size, raw.mtime_ns, Tile(0, entry, root), raw.offline))
    return items


class BrowseCache:
    """Listings taken a little while ago, so going Back or Up does not ask a slow drive again."""

    def __init__(self, ttl: float = 300.0, max_folders: int = 48, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.max_folders, self._clock = ttl, max_folders, clock
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, str, bool], tuple[float, FolderListing]] = OrderedDict()

    def get(self, backend: Backend, path: str, show_hidden: bool) -> FolderListing | None:
        """A fresh-enough listing of a slow, remote or cloud drive (a fast local disk is never remembered)."""
        if backend.capabilities(path).read_cost == 0:
            return None
        key = (backend.id, backend.normalize(path), show_hidden)
        with self._lock:
            found = self._items.get(key)
            if found is None or self._clock() - found[0] > self.ttl:
                return None
            self._items.move_to_end(key)
            return found[1]

    def put(self, listing: FolderListing, show_hidden: bool) -> None:
        if listing.backend.capabilities(listing.path).read_cost == 0 or listing.errors or listing.truncated:
            return
        key = (listing.backend.id, listing.backend.normalize(listing.path), show_hidden)
        with self._lock:
            self._items[key] = (self._clock(), listing)
            self._items.move_to_end(key)
            while len(self._items) > self.max_folders:
                self._items.popitem(last=False)

    def forget(self, backend: Backend | None = None, path: str | None = None) -> None:
        with self._lock:
            if backend is None:
                self._items.clear()
                return
            for key in [k for k in self._items if k[0] == backend.id and (path is None or k[1] == backend.normalize(path))]:
                del self._items[key]
