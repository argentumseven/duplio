"""Browse a folder: any folder, on any drive, as a wall of pictures you can move around freely.

The folder is read once (a remote one in a single trip), its pictures are fetched in the background into a buffer on disk
(nearest the part you are looking at first) and shown from there, so scrolling back and forth, going up and coming back
never asks the drive again. How the buffer is kept in bounds, and when it gives up and says so, is described in
``duplio.gui.prefetch`` and ``duplio.gui.thumbs``.
"""

from __future__ import annotations

import math
import os
import tkinter as tk
from functools import partial
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.gui import actions
from duplio.gui.app import Page
from duplio.gui.dialogs import ConnectDialog
from duplio.gui.foldertree import FolderTree
from duplio.gui.jobs import Job, explain
from duplio.gui.listing import FolderListing, items_of, read_folder
from duplio.gui.memory import describe
from duplio.gui.paint import BROWSE_SIZES, Painter
from duplio.gui.sorting import BROWSE_ORDERS, BrowseItem, order_label, order_of, sort_browse
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, FlowFrame, VirtualList, autowrap, tag_value
from duplio.preview.service import PreviewItem
from duplio.util.fmt import human_size, plural
from duplio.vfs.location import Location

if TYPE_CHECKING:
    from duplio.gui.app import Context

VIEWS = ("Thumbnails", "Details")


class BrowsePage(Page):
    title = "Browse a folder"
    hints = (("Enter", "open"), ("Backspace", "up"), ("F5", "reload"))
    subtitle = ("Look inside any folder, on this computer, a phone, a NAS or a cloud drive. Pictures are fetched once, kept in a "
                "buffer, and shown from it, so moving around a big remote folder stays quick. Looking changes nothing.")

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f, st = ctx.fonts, ctx.state
        self.here: Location | None = None
        self.history: list[Location] = []
        self.listing: FolderListing | None = None
        self.items: list[BrowseItem] = []
        self.shown: list[BrowseItem] = []
        self.item_keys: list[str] = []
        self.by_key: dict[str, BrowseItem] = {}
        self.selected: str | None = None
        self._ordinal: list[int] = []  # how many pictures come before each shown entry
        self._generation = 0
        self._job: Job | None = None
        self._visible = False
        self._resume_after_hide = False
        self._scroll_after: str | None = None
        self._filter_after: str | None = None
        self._tick_after: str | None = None
        self._last_far_move = -1
        self._hidden_shown = self.show_hidden

        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 14))
        # ---- where we are: back, up, the address (typing is optional: the tree on the left is the way around), reload
        bar = tk.Frame(body, bg=PALETTE["bg"])
        bar.pack(fill="x", pady=(4, 6))
        self.back_button = Btn(bar, "◀ Back", self.go_back, fonts=f, padx=10, pady=5)
        self.back_button.pack(side="left")
        self.up_button = Btn(bar, "▲ Up", self.go_up, fonts=f, padx=10, pady=5)
        self.up_button.pack(side="left", padx=(6, 0))
        tk.Frame(bar, width=8, bg=PALETTE["bg"]).pack(side="left")
        Btn(bar, "Reload", self.refresh, fonts=f, padx=10, pady=5).pack(side="right", padx=(8, 0))
        self.address = ttk.Entry(bar, font=f.base)
        self.address.pack(side="left", fill="x", expand=True)
        self.address.bind("<Return>", lambda _e: self.open_text(self.address.get()))
        self.address.bind("<FocusIn>", lambda _e: self.after(10, lambda: self.address.select_range(0, "end")))
        # ---- the folder tree on the left, the folder on the right; the divider can be dragged
        self.split = tk.PanedWindow(body, orient="horizontal", sashwidth=8, sashrelief="flat", bg=PALETTE["bg"], bd=0, opaqueresize=True,
                                    sashcursor="sb_h_double_arrow")
        self.split.pack(fill="both", expand=True)
        self.folder_tree = FolderTree(self.split, ctx, on_pick=self.open_text, on_connect=self.connect, show_hidden=lambda: self.show_hidden)
        main = tk.Frame(self.split, bg=PALETTE["bg"])
        self.split.add(self.folder_tree, minsize=200, width=int(st.get("browse_tree_width", 260)), stretch="never")
        self.split.add(main, minsize=380, stretch="always")
        self.split.bind("<ButtonRelease-1>", lambda _e: self._remember_sash())
        # ---- how it is shown (these wrap onto more lines when the window is narrow)
        tools = FlowFrame(main, PALETTE["bg"], hgap=8, vgap=6)
        tools.pack(fill="x", pady=(0, 6))
        self.view_box = tools.field("Show", lambda box: ttk.Combobox(box, state="readonly", values=VIEWS, width=11, font=f.small), f.small)
        self.view_box.set(st.get("browse_view", VIEWS[0]))
        self.size_box = tools.field("Picture size", lambda box: ttk.Combobox(box, state="readonly", values=list(BROWSE_SIZES), width=7,
                                                                            font=f.small), f.small)
        self.size_box.set(st.get("browse_size", "Medium") if st.get("browse_size", "Medium") in BROWSE_SIZES else "Medium")
        self.sort_box = tools.field("Order", lambda box: ttk.Combobox(box, state="readonly", values=[o[2] for o in BROWSE_ORDERS], width=16,
                                                                     font=f.small), f.small)
        self.sort_box.set(order_label(st.get("browse_sort", "name"), bool(st.get("browse_desc", False))))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._filter_soon())
        tools.field("Filter", lambda box: ttk.Entry(box, textvariable=self.filter_var, width=18, font=f.small), f.small)
        for box in (self.view_box, self.size_box, self.sort_box):
            box.bind("<<ComboboxSelected>>", lambda _e: self._view_changed())
        # ---- what is going on
        self.summary = tk.Label(main, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w", justify="left")
        self.summary.pack(fill="x")
        autowrap(self.summary, margin=4, minimum=300)
        status = tk.Frame(main, bg=PALETTE["bg"])
        status.pack(fill="x", pady=(2, 2))
        self.fetch_label = tk.Label(status, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.fetch_label.pack(fill="x")
        autowrap(self.fetch_label, margin=4, minimum=300)
        self.fetch_button = Btn(status, "", self._fetch_clicked, fonts=f, padx=10, pady=3)
        self.buffer_label = tk.Label(main, text="", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.buffer_label.pack(fill="x", pady=(0, 6))
        autowrap(self.buffer_label, margin=4, minimum=300)
        # ---- the folder
        self.selected_label = tk.Label(main, text="", font=f.mono, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w")
        self.selected_label.pack(side="bottom", fill="x", pady=(4, 0))
        holder = tk.Frame(main, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.view = VirtualList(holder, f, self._draw, on_click=self._click, on_double=self._double, on_context=self._context,
                                on_hover=self._hover)
        self.view.pack(fill="both", expand=True)
        self.painter = Painter(self.view, ctx.thumbs, f, self.box)
        self.painter.selectable = False
        self.painter.fetch_online_only = self.fetch_cloud_only
        self.view.relayout = self._heights
        self.view.on_scroll = self._scrolled
        canvas = self.view.canvas
        canvas.bind("<BackSpace>", lambda _e: self.go_up())
        canvas.bind("<F5>", lambda _e: self.refresh())
        canvas.bind("<Return>", lambda _e: self._open_selected())
        self._last_hover = ""
        self._set_buttons()

    # ---- settings of the view ------------------------------------------------------------------
    @property
    def box(self) -> int:
        return BROWSE_SIZES.get(self.size_box.get(), 132)

    @property
    def details(self) -> bool:
        return self.view_box.get() == "Details"

    @property
    def sort_field(self) -> str:
        return order_of(self.sort_box.get())[0]

    @property
    def descending(self) -> bool:
        return order_of(self.sort_box.get())[1]

    @property
    def show_hidden(self) -> bool:
        """Hidden files follow the one Settings choice ("Skip hidden files and folders"), so there is nothing to tick here."""
        return not self.ctx.settings.skip_hidden

    @property
    def fetch_cloud_only(self) -> bool:
        """Whether pictures of cloud-only files are fetched (and so downloaded); also a Settings choice."""
        return bool(self.ctx.settings.previews.online_only)

    def _remember_sash(self) -> None:
        try:
            self.ctx.state.set("browse_tree_width", int(self.split.sash_coord(0)[0]))
            self.ctx.state.save()
        except (tk.TclError, ValueError):
            pass

    def _remember_view(self) -> None:
        st = self.ctx.state
        st.set("browse_view", self.view_box.get())
        st.set("browse_size", self.size_box.get())
        st.set("browse_sort", self.sort_field)
        st.set("browse_desc", self.descending)
        st.save()

    def _view_changed(self) -> None:
        self._remember_view()
        self.painter.box = self.box
        self._apply_view(reset=False)
        if self.listing is not None:
            self._start_prefetch()

    def _filter_soon(self) -> None:
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
        self._filter_after = self.after(200, lambda: self._apply_view(reset=True))

    # ---- going places ------------------------------------------------------------------------------
    def on_show(self) -> None:
        self._visible = True
        self.ctx.places.refresh()  # drives and phones are looked for in the background; the tree shows them as they turn up
        if self.painter.fetch_online_only != self.fetch_cloud_only or (self.here is not None and self.show_hidden != self._hidden_shown):
            self._settings_changed()
        if self.here is None:
            start = self.ctx.state.get("browse_last") or os.path.expanduser("~")
            try:
                self.navigate(self.ctx.registry.open(start), push=False)
            except Exception:
                self.navigate(self.ctx.registry.open(os.path.expanduser("~")), push=False)
        elif self._resume_after_hide:
            self.ctx.prefetcher.resume()
        self._resume_after_hide = False
        self._tick()

    def _settings_changed(self) -> None:
        """Settings (hidden files, cloud-only pictures) were changed while this page was away: apply them to the folder on show."""
        self.painter.fetch_online_only = self.fetch_cloud_only
        self.folder_tree.clear_known()
        if self.here is not None:
            self.navigate(self.here, push=False)

    def on_hide(self) -> None:
        self._visible = False
        if self._tick_after is not None:
            self.after_cancel(self._tick_after)
            self._tick_after = None
        if self.ctx.prefetcher.status().state == "running":
            self.ctx.prefetcher.pause()  # the drive is free for whatever the person does next
            self._resume_after_hide = True

    def open_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            location = self.ctx.registry.open(text)
        except Exception as exc:
            self.summary.configure(text=f"Cannot open that: {explain(exc)}", fg=PALETTE["bad"])
            return
        self.navigate(location)

    def connect(self) -> None:
        """Pick a phone, a drive or a server; its top folder opens here, in the page itself."""
        picked = ConnectDialog(self.ctx).show()
        if picked:
            self.open_text(picked)

    def navigate(self, location: Location, *, push: bool = True, refresh: bool = False) -> None:
        if push and self.here is not None and (self.here.uri != location.uri):
            self.history.append(self.here)
            del self.history[:-100]
        self.here = location
        self.selected = None
        self.folder_tree.reveal(location)
        self.address.delete(0, "end")
        self.address.insert(0, location.display)
        self.ctx.state.remember(location.display)
        self.ctx.state.set("browse_last", location.display)
        self.ctx.state.save()
        self._load(refresh)

    def go_back(self) -> None:
        if self.history:
            self.navigate(self.history.pop(), push=False)

    def go_up(self) -> None:
        if self.here is None:
            return
        backend, path = self.here.backend, self.here.path
        parent = backend.parent(path)
        if parent and parent != path:
            self.navigate(Location(backend, parent))

    def refresh(self) -> None:
        if self.here is not None:
            self.ctx.browse_cache.forget(self.here.backend, self.here.path)
            self.folder_tree.reload(self.here)
            self.navigate(self.here, push=False, refresh=True)

    def _set_buttons(self) -> None:
        self.back_button.set_enabled(bool(self.history))
        can_up = False
        if self.here is not None:
            parent = self.here.backend.parent(self.here.path)
            can_up = bool(parent) and parent != self.here.path
        self.up_button.set_enabled(can_up)

    # ---- reading the folder ----------------------------------------------------------------------------
    def _load(self, refresh: bool) -> None:
        assert self.here is not None
        location, hidden = self.here, self.show_hidden
        self._hidden_shown = hidden
        self._generation += 1
        generation = self._generation
        self.ctx.prefetcher.stop("moved to another folder")
        if self._job is not None:
            self._job.cancel.cancel("moved on")
        self._set_buttons()
        cached = None if refresh else self.ctx.browse_cache.get(location.backend, location.path, hidden)
        if cached is not None:
            self._loaded(cached, location, generation)
            return
        self.summary.configure(text="Reading the folder…", fg=PALETTE["muted"])
        self.view.set_rows([], "Reading the folder…")

        def work(progress: Any, cancel: Any) -> FolderListing:
            return read_folder(location.backend, location.path, show_hidden=hidden, should_stop=lambda: cancel.cancelled)

        job = Job(f"Reading {location.display}", work)
        self._job = job
        job.start()
        self._wait_for(job, location, hidden, generation)

    def _wait_for(self, job: Job, location: Location, hidden: bool, generation: int) -> None:
        if generation != self._generation:
            return
        if not job.finished:
            self.after(60, partial(self._wait_for, job, location, hidden, generation))
            return
        if job.error is not None:
            self.listing = None
            self.items, self.shown, self.item_keys = [], [], []
            self.summary.configure(text="The folder could not be read.", fg=PALETTE["bad"])
            self.view.set_rows([], f"The folder could not be read: {job.error}")
            return
        if job.cancelled or job.result is None:
            return
        listing: FolderListing = job.result
        self.ctx.browse_cache.put(listing, hidden)
        self._loaded(listing, location, generation)

    def _loaded(self, listing: FolderListing, location: Location, generation: int) -> None:
        if generation != self._generation:
            return
        self.listing = listing
        self.folder_tree.learn(location, listing.dirs)  # the tree needs no second trip to the drive for these
        self.items = items_of(listing, location)
        pictures = [i.tile.preview_item for i in self.items if i.tile is not None and i.tile.preview_item is not None]
        self.ctx.previews.pin([p for p in pictures if p is not None])  # the buffer keeps this folder's pictures when it must make room
        self._apply_view(reset=True)
        self._start_prefetch()
        self._set_buttons()

    # ---- what is shown -------------------------------------------------------------------------------------
    def _apply_view(self, *, reset: bool) -> None:
        text = self.filter_var.get().strip().casefold()
        items = self.items
        if text:
            items = [i for i in items if text in i.name.casefold()]
        self.shown = sort_browse(items, self.sort_field, self.descending, True)
        self.item_keys = [i.tile.key if i.tile is not None else f"d:{i.name}" for i in self.shown]
        self.by_key = dict(zip(self.item_keys, self.shown, strict=True))
        self._ordinal, seen = [], 0
        for item in self.shown:
            self._ordinal.append(seen)
            seen += 1 if item.tile is not None and item.tile.picture_key else 0
        empty = ("Nothing matches." if text and self.items else
                 "This folder is empty." if self.listing is not None and not self.listing.errors else
                 "The folder could not be read: " + "; ".join(f"{why}" for _p, why in (self.listing.errors if self.listing else [])[:2]))
        self.painter.box = self.box
        self.view.set_rows(self._heights(self.view.width), empty, keep_position=not reset)
        self._summarise()
        self._scrolled()

    def _heights(self, width: int) -> list[int]:
        if self.details:
            return [self.painter.detail_row_height] * len(self.shown)
        columns = self.painter.browse_columns(width, self.box)
        return [self.painter.browse_row_height(self.box)] * math.ceil(len(self.shown) / columns)

    def _draw(self, view: VirtualList, index: int, y: int, width: int) -> None:
        if self.details:
            self.painter.browse_detail_row(self.item_keys[index], self.shown[index], y, width, index, self.item_keys[index] == self.selected)
            return
        columns = self.painter.browse_columns(width, self.box)
        start = index * columns
        cells = list(zip(self.item_keys[start : start + columns], self.shown[start : start + columns], strict=True))
        self.painter.browse_grid_row(cells, y, width, index, self.box, self.selected)

    def _summarise(self) -> None:
        if self.listing is not None and self.listing.errors and not self.items:
            why = "; ".join(str(reason) for _path, reason in self.listing.errors[:2])
            self.summary.configure(text=f"This folder could not be read: {why}", fg=PALETTE["bad"])
            return
        folders = sum(1 for i in self.items if i.is_dir)
        files = [i for i in self.items if not i.is_dir]
        pictures = sum(1 for i in files if i.tile is not None and i.tile.picture_key)
        text = f"{plural(folders, 'folder')}, {plural(len(files), 'file')} ({plural(pictures, 'picture')}, {human_size(sum(i.size for i in files))})"
        if len(self.shown) != len(self.items):
            text += f" · showing {len(self.shown):,}"
        colour = PALETTE["text"]
        if self.listing is not None and self.listing.truncated:
            text += " · this folder is huge: only the first part is shown"
            colour = PALETTE["warn"]
        elif self.listing is not None and self.listing.errors:
            text += f" · {plural(len(self.listing.errors), 'problem')} reading it"
            colour = PALETTE["warn"]
        self.summary.configure(text=text, fg=colour)

    # ---- pictures: ahead of the viewer and into the buffer ----------------------------------------------------
    def _preview_items(self) -> list[PreviewItem]:
        return [i.tile.preview_item for i in self.shown if i.tile is not None and i.tile.preview_item is not None]  # type: ignore[misc]

    def _start_prefetch(self) -> None:
        if not self.ctx.settings.previews.prefetch_folder or self.ctx.settings.previews.mode == "off":
            self.ctx.prefetcher.stop("fetching a whole folder is switched off in Settings")
            self._tick()
            return
        first = self._first_shown()
        self.ctx.prefetcher.start(self._preview_items(), first=first, allow_online_only=self.fetch_cloud_only)
        self._tick()

    def _first_shown(self) -> int:
        if not self.shown:
            return 0
        row, _last = self.view.visible_range()
        index = row if self.details else row * self.painter.browse_columns(self.view.width, self.box)
        return min(index, len(self.shown) - 1)

    def _scrolled(self) -> None:
        if self._scroll_after is not None:
            self.after_cancel(self._scroll_after)
        self._scroll_after = self.after(120, self._settled)

    def _settled(self) -> None:
        """The view stopped moving: fetch what is near it first, and make images a few screens ahead and behind."""
        self._scroll_after = None
        if not self.shown or self.listing is None:
            return
        first_row, last_row = self.view.visible_range()
        per_row = 1 if self.details else self.painter.browse_columns(self.view.width, self.box)
        first = min(first_row * per_row, len(self.shown) - 1)
        if abs(first - self._last_far_move) > per_row * 2:
            self._last_far_move = first
            self.ctx.prefetcher.move_to(self._ordinal[first])
        span = max(4, (last_row - first_row) * 2)
        low, high = max(first_row - span, 0), min(last_row + span, math.ceil(len(self.shown) / per_row))
        wanted = [r for r in range(low, high) if not first_row <= r < last_row]
        wanted.sort(key=lambda r: -abs(r - (first_row + last_row) // 2))  # nearest last: the queue serves the last first
        generation = self._generation
        box = self.painter.DETAIL_BOX if self.details else self.box
        budget = 400
        for r in wanted:
            for item in self.shown[r * per_row : (r + 1) * per_row]:
                if item.tile is None or not item.tile.picture_key or budget <= 0:
                    continue
                if self.ctx.thumbs.peek(item.tile, box) is None and not self.ctx.thumbs.reason(item.tile):
                    budget -= 1
                    self.ctx.thumbs.request(item.tile, box, self.painter.ready, wanted=partial(self._current, generation), background=True)

    # ---- the status lines -----------------------------------------------------------------------------------------
    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _tick(self) -> None:
        if self._tick_after is not None:
            self.after_cancel(self._tick_after)
            self._tick_after = None
        self.folder_tree.sync_places()
        self._show_progress()
        if self._visible:
            self._tick_after = self.after(300, self._tick)

    def _show_progress(self) -> None:
        pre, thumbs = self.ctx.prefetcher, self.ctx.thumbs
        thumbs.check_memory()
        st = pre.status()
        text, colour, button = "", PALETTE["muted"], ""
        if st.state == "running":
            text, button = f"Fetching pictures: {st.ready:,} of {st.total:,} ready", "Pause"
        elif st.state == "paused":
            text, button = f"Paused: {st.ready:,} of {st.total:,} pictures ready", "Resume"
        elif st.state == "finished":
            text, colour = f"All {st.total:,} pictures are at hand", PALETTE["ok"]
            if st.skipped:
                text += f" ({st.skipped:,} left alone: cloud-only or too large)"
            if st.failed:
                text += f" · {st.failed:,} could not be read"
                colour = PALETTE["warn"]
        elif st.state == "stopped":
            text, colour, button = f"Stopped: {st.reason}", PALETTE["warn"], "Try again"
        elif self.listing is not None and any(i.tile is not None and i.tile.picture_key for i in self.items):
            text, button = "Pictures are fetched as you scroll", "Fetch all pictures"
        self.fetch_label.configure(text=text, fg=colour)
        self.fetch_button.configure(text=button)
        if button and not self.fetch_button.winfo_ismapped():
            self.fetch_button.pack(anchor="w", pady=(4, 0))
        elif not button:
            self.fetch_button.pack_forget()
        self.buffer_label.configure(text=self._buffer_text(), fg=PALETTE["warn"] if thumbs.low_memory else PALETTE["muted"])

    def _buffer_text(self) -> str:
        thumbs = self.ctx.thumbs
        stats = thumbs.stats()
        disk = ""
        items = self._preview_items()
        store = self.ctx.previews.buffer_for(items[0]) if items else None
        if store is not None:
            disk = f"on disk {store.count:,} pictures, {describe(store.total_bytes)} of {describe(store.max_bytes)}"
            if not store.persistent:
                disk += " (until Duplio closes; Settings can keep them between runs)"
        memory = f"in memory {describe(stats['bytes'])} of {describe(stats['budget'])}"
        text = "Buffer: " + (disk + " · " if disk else "") + memory
        if stats["low_memory"]:
            text += " · the computer is short of memory, so fewer pictures are held at once"
        return text

    def _fetch_clicked(self) -> None:
        pre = self.ctx.prefetcher
        state = pre.status().state
        if state == "running":
            pre.pause()
        elif state == "paused":
            pre.resume()
        else:
            self._start_prefetch()
        self._tick()

    # ---- the mouse ---------------------------------------------------------------------------------------------------
    def _item_at(self, tags: tuple[str, ...]) -> tuple[str, BrowseItem] | None:
        key = tag_value(tags, "tile:")
        item = self.by_key.get(key) if key else None
        return (key, item) if key and item is not None else None

    def _click(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        found = self._item_at(tags)
        self.selected = found[0] if found else None
        self.view.redraw_all()
        if found:
            item = found[1]
            where = item.tile.location if item.tile is not None else (self.here.child(item.name).display if self.here else item.name)
            detail = item.tile.details if item.tile is not None else "folder"
            self.selected_label.configure(text=f"{item.name} · {detail} · {where}")
        else:
            self.selected_label.configure(text="")

    def _double(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        found = self._item_at(tags)
        if found:
            self._open(found[1])

    def _open_selected(self) -> None:
        if self.selected is not None and self.selected in self.by_key:
            self._open(self.by_key[self.selected])

    def _open(self, item: BrowseItem) -> None:
        if item.is_dir:
            if self.here is not None:
                self.navigate(self.here.child(item.name))
        elif item.tile is not None:
            actions.open_tile(self.ctx, item.tile)

    def _context(self, tags: tuple[str, ...], event: tk.Event) -> None:
        found = self._item_at(tags)
        if not found:
            return
        key, item = found
        self.selected = key
        self.view.redraw_all()
        if item.tile is None:
            menu = tk.Menu(self.ctx.window, tearoff=0)
            menu.add_command(label="Open", command=lambda: self._open(item))
            where = self.here.child(item.name).display if self.here else item.name
            menu.add_command(label="Copy its address", command=lambda: self._copy(where))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return
        tile = item.tile
        fetch = None
        if tile.kind == "picture":
            def fetch() -> None:
                self.ctx.thumbs.forget_reason(tile)
                self.painter.fetch_online_only = True
                self.view.redraw_all()
                self.painter.fetch_online_only = self.fetch_cloud_only

        actions.context_menu(self.ctx, tile, event.x_root, event.y_root, None, fetch)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def _hover(self, tags: tuple[str, ...], event: tk.Event | None) -> None:
        found = self._item_at(tags) if event is not None else None
        text = ""
        if found:
            item = found[1]
            text = f"{item.tile.location}\n{item.tile.details}" if item.tile is not None else item.name
        if text != self._last_hover:
            self._last_hover = text
            if text and event is not None:
                self.ctx.tooltip.schedule(text, event.x_root, event.y_root)
            else:
                self.ctx.tooltip.hide()
