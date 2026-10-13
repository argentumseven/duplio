"""The folder tree: your folders, every drive down to its top, phones and recent places, each opening into its sub-folders.

It sits to the left of the Browse page, always on screen, so moving through a folder structure is a matter of clicking, never of
typing an address. A folder's sub-folders are read only when it is opened, on a worker thread, so a slow drive or a stale phone
mount never freezes the window; the folder being viewed is opened in the tree and highlighted by itself.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.gui.jobs import explain
from duplio.gui.places import Place
from duplio.gui.sorting import natural_key
from duplio.gui.style import PALETTE, shorten
from duplio.gui.widgets import Btn
from duplio.vfs.location import Location

if TYPE_CHECKING:
    from duplio.gui.app import Context

Key = tuple[str, str]  # (which drive, the folder on it): the same folder gets the same key wherever it appears in the tree

SECTIONS = (("folders", "Folders"), ("drives", "Drives"), ("devices", "Phones and devices"), ("recent", "Recent"))
MAX_CHILDREN = 3000  # a folder with more sub-folders than this shows the first ones and says so
MAX_DEPTH = 64


def key_of(location: Location) -> Key:
    return (location.backend.id, location.backend.normalize(location.path))


def lineage(location: Location) -> list[Location]:
    """The folder, then the one above it, and so on up to the top of its drive."""
    out = [location]
    seen = {key_of(location)}
    backend, path = location.backend, location.path
    while len(out) < MAX_DEPTH:
        above = backend.parent(path)
        here = Location(backend, above)
        if not above or above == path or key_of(here) in seen:
            break
        seen.add(key_of(here))
        out.append(here)
        path = above
    return out


class FolderTree(tk.Frame):
    """A tree of folders that opens lazily. ``on_pick(address)`` is called when the person clicks a folder in it."""

    def __init__(self, master: tk.Misc, ctx: Context, on_pick: Callable[[str], None], on_connect: Callable[[], None],
                 show_hidden: Callable[[], bool]) -> None:
        super().__init__(master, bg=PALETTE["bg"])
        self.ctx, self._on_pick, self._show_hidden = ctx, on_pick, show_hidden
        f = ctx.fonts
        self._loc: dict[str, Location] = {}
        self._by_key: dict[Key, list[str]] = {}
        self._state: dict[str, str] = {}  # folder node -> unloaded | loading | loaded
        self._waiting: dict[str, list[Callable[[], None]]] = {}
        self._sections: dict[str, str] = {}
        self._shown_places: dict[str, tuple[Any, ...]] = {}
        self._places_version = -1
        self._inbox: queue.SimpleQueue[tuple[str, int, str, Any]] = queue.SimpleQueue()
        self._outstanding = 0
        self._counter = 0
        self._token = 0
        self._current: Key | None = None
        self._reveal = 0
        self._polling = False
        self._again: str | None = None  # the "look again" row under Phones and devices
        self._known: dict[Key, list[str]] = {}  # sub-folders the page has already read, for nodes that have not been made yet
        self._programmatic: str | None = None  # a node the tree itself selected, which must not be taken for a click
        self._last: Location | None = None  # the folder the page is showing

        # The button is packed first so it stays on screen however short the panel is made.
        Btn(self, "+ Phone or server…", on_connect, fonts=f, padx=10, pady=5).pack(side="bottom", anchor="w", pady=(8, 0))
        holder = tk.Frame(self, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, show="tree", selectmode="browse")
        bar_y = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        bar_x = ttk.Scrollbar(holder, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=bar_y.set, xscrollcommand=bar_x.set)
        self.tree.column("#0", width=260, minwidth=260, stretch=True)
        bar_y.pack(side="right", fill="y")
        bar_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("section", foreground=PALETTE["heading"], font=f.bold)
        self.tree.tag_configure("note", foreground=PALETTE["muted"], font=f.small)
        self.tree.bind("<<TreeviewOpen>>", self._opened)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        for kind, title in SECTIONS:
            self._sections[kind] = self.tree.insert("", "end", text=title, open=True, tags=("section",))
        self.sync_places(force=True)

    # ---- the roots ---------------------------------------------------------------------------------------------
    def sync_places(self, force: bool = False) -> None:
        """Show the folders, drives and phones the places service knows of (cheap to call often: it compares first)."""
        service = self.ctx.places
        if not force and service.version == self._places_version:
            return
        self._places_version = service.version
        found = service.snapshot()
        recent = [Place(self._short(r), r) for r in self.ctx.state.recent[:8]]
        devices: list[Place] = list(found.devices)
        refilled = False
        for kind, places in (("folders", list(found.folders)), ("drives", list(found.drives)), ("devices", devices), ("recent", recent)):
            note = ""
            if kind == "devices":
                note = "looking…" if not found.scanned else ("none found" if not devices else "")
            wanted = (tuple((p.label, p.target) for p in places), note, found.scanned if kind == "devices" else True)
            if self._shown_places.get(kind) == wanted and not force:
                continue
            if kind == "recent" and self._shown_places.get(kind) is not None and not force:
                continue  # what was recent when the page opened stays put, so the tree does not shuffle under the pointer
            self._shown_places[kind] = wanted
            self._fill_section(kind, places, note)
            refilled = refilled or kind != "devices"
        if refilled and self._last is not None and not force:
            self._reveal_again()

    def _short(self, address: str) -> str:
        return shorten(address, 230, self.ctx.fonts.base, middle=True)

    def _fill_section(self, kind: str, places: Sequence[Place], note: str = "") -> None:
        section = self._sections[kind]
        for child in self.tree.get_children(section):
            self._forget(child)
        for place in places:
            self._add_root(section, place)
        if note:
            self.tree.insert(section, "end", text="  " + note, tags=("note",))
        if kind == "devices":
            self._again = self.tree.insert(section, "end", text="  ↻ look again", tags=("note",))
        if kind == "recent":
            if places:
                self.tree.move(section, "", "end")
            else:
                self.tree.detach(section)  # nothing recent yet: no heading either

    def _add_root(self, parent: str, place: Place, *, first: bool = False) -> str | None:
        try:
            location = self.ctx.registry.open(place.target)
        except Exception:
            return None
        return self._add_folder(parent, location, place.label, first=first)

    def _add_folder(self, parent: str, location: Location, label: str, *, first: bool = False) -> str:
        self._counter += 1
        iid = f"f{self._counter}"
        self.tree.insert(parent, 0 if first else "end", iid=iid, text=label, open=False)
        self.tree.insert(iid, "end", iid=iid + "~", text="  …", tags=("note",))  # makes the open/close arrow appear
        self._loc[iid] = location
        self._by_key.setdefault(key_of(location), []).append(iid)
        self._state[iid] = "unloaded"
        known = self._known.get(key_of(location))
        if known is not None:
            self._fill_children(iid, known)
        return iid

    def _forget(self, iid: str) -> None:
        """Remove a node and everything below it, and what is remembered about them."""
        for child in self.tree.get_children(iid):
            self._forget(child)
        location = self._loc.pop(iid, None)
        if location is not None:
            nodes = self._by_key.get(key_of(location), [])
            if iid in nodes:
                nodes.remove(iid)
        self._state.pop(iid, None)
        self._waiting.pop(iid, None)
        self.tree.delete(iid)

    # ---- opening a folder: its sub-folders are read in the background -------------------------------------------------
    def _opened(self, _event: tk.Event) -> None:
        iid = self.tree.focus()
        if iid in self._loc:
            self._load(iid)

    def _load(self, iid: str, then: Callable[[], None] | None = None, refresh: bool = False) -> None:
        state = self._state.get(iid)
        if state == "loaded" and not refresh:
            if then:
                then()
            return
        if then:
            self._waiting.setdefault(iid, []).append(then)
        if state == "loading":
            return
        self._state[iid] = "loading"
        placeholder = iid + "~"
        if self.tree.exists(placeholder):
            self.tree.item(placeholder, text="  looking…")
        location, hidden = self._loc[iid], self._show_hidden()
        self._token += 1
        token = self._token
        self._outstanding += 1
        if not self._polling:
            self._polling = True
            self.after(30, self._poll)

        def work() -> None:
            try:
                cached = None if refresh else self.ctx.browse_cache.get(location.backend, location.path, hidden)
                if cached is not None:
                    names = list(cached.dirs)
                else:
                    entries = self.ctx.listings.list(location.backend, location.path, refresh=refresh)
                    names = [n for n, is_dir in entries if is_dir and (hidden or not n.startswith("."))]
                self._inbox.put((iid, token, "ok", names))
            except Exception as exc:
                self._inbox.put((iid, token, "error", explain(exc)))

        threading.Thread(target=work, name="duplio-tree", daemon=True).start()

    def _poll(self) -> None:
        self._polling = False
        if not self.winfo_exists():
            return
        try:
            while True:
                iid, _token, status, payload = self._inbox.get_nowait()
                self._outstanding -= 1
                if iid not in self._loc or not self.tree.exists(iid):
                    continue  # the node was removed while it was being read
                if status == "ok":
                    self._fill_children(iid, payload)
                else:
                    self._failed(iid, str(payload))
        except queue.Empty:
            pass
        if self._outstanding > 0:
            self._polling = True
            self.after(60, self._poll)

    def _fill_children(self, iid: str, names: Sequence[str]) -> None:
        for child in self.tree.get_children(iid):
            self._forget(child)
        location = self._loc[iid]
        ordered = sorted(names, key=natural_key)
        for name in ordered[:MAX_CHILDREN]:
            self._add_folder(iid, location.child(name), name)
        if len(ordered) > MAX_CHILDREN:
            self.tree.insert(iid, "end", text=f"  … {len(ordered) - MAX_CHILDREN:,} more not shown", tags=("note",))
        if not ordered:
            self.tree.item(iid, open=False)  # nothing inside: no arrow
        self._state[iid] = "loaded"
        for callback in self._waiting.pop(iid, []):
            callback()

    def _failed(self, iid: str, reason: str) -> None:
        for child in self.tree.get_children(iid):
            self._forget(child)
        self.tree.insert(iid, "end", iid=iid + "~", text=f"  cannot read: {shorten(reason, 200, self.ctx.fonts.small)}", tags=("note",))
        self._state[iid] = "unloaded"  # closing and opening it again tries again
        for callback in self._waiting.pop(iid, []):
            callback()

    def learn(self, location: Location, folders: Sequence[str]) -> None:
        """The page has just read this folder: use what it found instead of asking the drive again."""
        key = key_of(location)
        self._known[key] = list(folders)
        while len(self._known) > 8:
            del self._known[next(iter(self._known))]
        for iid in list(self._by_key.get(key, [])):
            if self._state.get(iid) == "unloaded":
                self._fill_children(iid, folders)

    def clear_known(self) -> None:
        """Forget what the page handed over (the view of hidden files changed, or the person pressed Reload)."""
        self._known.clear()

    def reload(self, location: Location) -> None:
        """Read the sub-folders of this folder again (the person pressed Reload)."""
        self.clear_known()
        for iid in list(self._by_key.get(key_of(location), [])):
            if self._state.get(iid) == "loaded":
                self._load(iid, refresh=True)

    # ---- clicking ---------------------------------------------------------------------------------------------------
    def _selected(self, _event: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == self._programmatic:
            self._programmatic = None
            return
        self._programmatic = None
        if iid == self._again:
            self.ctx.places.refresh()
            self._restore_selection()
            return
        location = self._loc.get(iid)
        if location is None:
            return
        if key_of(location) == self._current:
            return  # it is already showing: this is the tree following the page, not the person clicking
        self._on_pick(location.uri)

    def _restore_selection(self) -> None:
        for iid in self._by_key.get(self._current or ("", ""), []):
            self._highlight(iid)
            return
        self._programmatic = None
        self.tree.selection_remove(*self.tree.selection())

    # ---- following the page: open the path down to the folder it shows and highlight it ------------------------------
    def _reveal_again(self) -> None:
        if self._last is not None:
            self.reveal(self._last)

    def reveal(self, location: Location) -> None:
        self._last = location
        self._current = key_of(location)
        self._reveal += 1
        generation = self._reveal
        sel = self.tree.selection()
        if sel and sel[0] in self._loc and key_of(self._loc[sel[0]]) == self._current:
            return  # the person clicked it here; nothing to move
        chain = lineage(location)
        keys = [key_of(loc) for loc in chain]
        best: tuple[int, str] | None = None
        for kind in ("folders", "drives", "devices", "recent"):
            for iid in self.tree.get_children(self._sections[kind]):
                loc = self._loc.get(iid)
                if loc is None or key_of(loc) not in keys:
                    continue
                depth = keys.index(key_of(loc))
                if best is None or depth < best[0]:
                    best = (depth, iid)
            if best is not None:
                break  # a folder, drive or phone that holds it wins over a recent place that is the very same folder
        if best is None:
            self._add_recent(location)
            return self.reveal(location)
        depth, root = best
        below = list(reversed(chain[:depth]))
        self._walk(generation, root, below)

    def _add_recent(self, location: Location) -> None:
        section = self._sections["recent"]
        self.tree.move(section, "", "end")
        self._add_folder(section, location, self._short(location.display), first=True)

    def _walk(self, generation: int, iid: str, remaining: list[Location]) -> None:
        if generation != self._reveal or not self.tree.exists(iid):
            return
        if not remaining:
            self._highlight(iid)
            return
        self.tree.item(iid, open=True)
        self._load(iid, then=lambda: self._step(generation, iid, remaining))

    def _step(self, generation: int, iid: str, remaining: list[Location]) -> None:
        if generation != self._reveal or not self.tree.exists(iid):
            return
        wanted = key_of(remaining[0])
        for child in self.tree.get_children(iid):
            loc = self._loc.get(child)
            if loc is not None and key_of(loc) == wanted:
                self._walk(generation, child, remaining[1:])
                return
        self._highlight(iid)  # the next folder is not in the list (hidden, or it could not be read): stop at this one

    def _highlight(self, iid: str) -> None:
        """Select and show a node because the page moved, which is not the same as the person clicking it."""
        if self.tree.selection() != (iid,):
            self._programmatic = iid
            self.tree.selection_set(iid)
        self.tree.see(iid)
