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
from duplio.gui.folders import choose_folder, connect_server
from duplio.gui.jobs import Job
from duplio.gui.listing import FolderListing, items_of, read_folder
from duplio.gui.memory import describe
from duplio.gui.paint import BROWSE_SIZES, Painter
from duplio.gui.sorting import BROWSE_FIELDS, BrowseItem, choose, label_of, sort_browse
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, VirtualList, tag_value
from duplio.preview.service import PreviewItem
from duplio.util.fmt import human_size, plural
from duplio.vfs.location import Location

if TYPE_CHECKING:
    from duplio.gui.app import Context

VIEWS = ("Thumbnails", "Details")


class BrowsePage(Page):
    title = "Browse a folder"
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

        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 14))
        # ---- where we are
        bar = tk.Frame(body, bg=PALETTE["bg"])
        bar.pack(fill="x", pady=(4, 6))
        self.back_button = Btn(bar, "◀", self.go_back, fonts=f, padx=10, pady=5)
        self.back_button.pack(side="left")
        self.up_button = Btn(bar, "▲ Up", self.go_up, fonts=f, padx=10, pady=5)
        self.up_button.pack(side="left", padx=(6, 8))
        self.address = ttk.Combobox(bar, values=ctx.state.recent, font=f.base)
        self.address.pack(side="left", fill="x", expand=True)
        self.address.bind("<Return>", lambda _e: self.open_text(self.address.get()))
        self.address.bind("<<ComboboxSelected>>", lambda _e: self.after(10, lambda: self.open_text(self.address.get())))
        Btn(bar, "Go", lambda: self.open_text(self.address.get()), kind="primary", fonts=f, padx=12, pady=5).pack(side="left", padx=(8, 0))
        Btn(bar, "Choose folder…", self.choose, fonts=f, padx=10, pady=5).pack(side="left", padx=(6, 0))
        Btn(bar, "Phone or server…", self.connect, fonts=f, padx=10, pady=5).pack(side="left", padx=(6, 0))
        Btn(bar, "Refresh", self.refresh, fonts=f, padx=10, pady=5).pack(side="left", padx=(6, 0))
        # ---- how it is shown
        tools = tk.Frame(body, bg=PALETTE["bg"])
        tools.pack(fill="x", pady=(0, 2))
        more = tk.Frame(body, bg=PALETTE["bg"])
        more.pack(fill="x", pady=(0, 6))

        def label(text: str) -> None:
            tk.Label(tools, text=text, font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="left", padx=(0, 4))

        label("Show")
        self.view_box = ttk.Combobox(tools, state="readonly", values=VIEWS, width=11, font=f.small)
        self.view_box.set(st.get("browse_view", VIEWS[0]))
        self.view_box.pack(side="left", padx=(0, 12))
        label("Picture size")
        self.size_box = ttk.Combobox(tools, state="readonly", values=list(BROWSE_SIZES), width=7, font=f.small)
        self.size_box.set(st.get("browse_size", "Medium") if st.get("browse_size", "Medium") in BROWSE_SIZES else "Medium")
        self.size_box.pack(side="left", padx=(0, 12))
        label("Sort by")
        self.sort_box = ttk.Combobox(tools, state="readonly", values=[v for _k, v in BROWSE_FIELDS], width=14, font=f.small)
        self.sort_box.set(label_of(BROWSE_FIELDS, st.get("browse_sort", "name")))
        self.sort_box.pack(side="left", padx=(0, 4))
        self.descending = bool(st.get("browse_desc", False))
        self.direction = Btn(tools, "", self._flip, fonts=f, padx=8, pady=3)
        self.direction.pack(side="left", padx=(0, 12))
        self.folders_first = tk.BooleanVar(value=bool(st.get("browse_folders_first", True)))
        self.hidden = tk.BooleanVar(value=bool(st.get("browse_hidden", False)))
        self.only_pictures = tk.BooleanVar(value=False)
        self.cloud = tk.BooleanVar(value=ctx.settings.previews.online_only)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._filter_soon())
        ttk.Entry(tools, textvariable=self.filter_var, width=14).pack(side="right")
        tk.Label(tools, text="Filter", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="right", padx=(0, 4))
        for text, var, command in (("Folders first", self.folders_first, self._view_changed),
                                   ("Show hidden files", self.hidden, self.refresh), ("Only pictures", self.only_pictures, self._view_changed),
                                   ("Also fetch pictures of cloud-only files (this downloads them)", self.cloud, self._cloud_changed)):
            ttk.Checkbutton(more, text=text, variable=var, command=command).pack(side="left", padx=(0, 14))
        self._paint_direction()
        for box in (self.view_box, self.size_box, self.sort_box):
            box.bind("<<ComboboxSelected>>", lambda _e: self._view_changed())
        # ---- what is going on
        status = tk.Frame(body, bg=PALETTE["bg"])
        status.pack(fill="x", pady=(0, 2))
        self.summary = tk.Label(status, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w")
        self.summary.pack(side="left")
        self.fetch_button = Btn(status, "", self._fetch_clicked, fonts=f, padx=10, pady=3)
        self.fetch_button.pack(side="right")
        self.fetch_label = tk.Label(status, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="e")
        self.fetch_label.pack(side="right", padx=10)
        self.buffer_label = tk.Label(body, text="", font=f.mono, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w")
        self.buffer_label.pack(fill="x", pady=(0, 6))
        # ---- the folder
        self.selected_label = tk.Label(body, text="", font=f.mono, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w")
        self.selected_label.pack(side="bottom", fill="x", pady=(4, 0))
        holder = tk.Frame(body, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.view = VirtualList(holder, f, self._draw, on_click=self._click, on_double=self._double, on_context=self._context,
                                on_hover=self._hover)
        self.view.pack(fill="both", expand=True)
        self.painter = Painter(self.view, ctx.thumbs, f, self.box)
        self.painter.selectable = False
        self.painter.fetch_online_only = self.cloud.get()
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
        return choose(BROWSE_FIELDS, self.sort_box.get())

    def _paint_direction(self) -> None:
        self.direction.configure(text="▼ Descending" if self.descending else "▲ Ascending")

    def _flip(self) -> None:
        self.descending = not self.descending
        self._paint_direction()
        self._view_changed()

    def _remember_view(self) -> None:
        st = self.ctx.state
        st.set("browse_view", self.view_box.get())
        st.set("browse_size", self.size_box.get())
        st.set("browse_sort", self.sort_field)
        st.set("browse_desc", self.descending)
        st.set("browse_folders_first", bool(self.folders_first.get()))
        st.set("browse_hidden", bool(self.hidden.get()))
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

    def _cloud_changed(self) -> None:
        self.painter.fetch_online_only = bool(self.cloud.get())
        if self.listing is not None:
            self.view.redraw_all()
            self._start_prefetch()

    # ---- going places ------------------------------------------------------------------------------
    def on_show(self) -> None:
        self._visible = True
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
            self.ctx.show_error(exc)
            return
        self.navigate(location)

    def choose(self) -> None:
        picked = choose_folder(self.ctx, self.address.get())
        if picked:
            self.open_text(picked)

    def connect(self) -> None:
        picked = connect_server(self.ctx)
        if picked:
            self.open_text(picked)

    def navigate(self, location: Location, *, push: bool = True, refresh: bool = False) -> None:
        if push and self.here is not None and (self.here.uri != location.uri):
            self.history.append(self.here)
            del self.history[:-100]
        self.here = location
        self.selected = None
        self.address.set(location.display)
        self.ctx.state.remember(location.display)
        self.ctx.state.set("browse_last", location.display)
        self.ctx.state.save()
        self.address.configure(values=self.ctx.state.recent)
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
        location, hidden = self.here, bool(self.hidden.get())
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
        if self.only_pictures.get():
            items = [i for i in items if i.is_dir or (i.tile is not None and i.tile.picture_key)]
        self.shown = sort_browse(items, self.sort_field, self.descending, bool(self.folders_first.get()))
        self.item_keys = [i.tile.key if i.tile is not None else f"d:{i.name}" for i in self.shown]
        self.by_key = dict(zip(self.item_keys, self.shown, strict=True))
        self._ordinal, seen = [], 0
        for item in self.shown:
            self._ordinal.append(seen)
            seen += 1 if item.tile is not None and item.tile.picture_key else 0
        empty = ("Nothing matches." if (text or self.only_pictures.get()) and self.items else
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
        self.ctx.prefetcher.start(self._preview_items(), first=first, allow_online_only=bool(self.cloud.get()))
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
            text, colour, button = f"All {st.total:,} pictures are at hand", PALETTE["ok"], "Check again"
            if st.skipped:
                text += f" ({st.skipped:,} left alone: cloud-only or too large)"
            if st.failed:
                text += f" · {st.failed:,} could not be read"
                colour = PALETTE["warn"]
        elif st.state == "stopped":
            text, colour, button = f"Stopped: {st.reason}", PALETTE["warn"], "Fetch again"
        elif self.listing is not None and any(i.tile is not None and i.tile.picture_key for i in self.items):
            text, button = "Pictures are fetched as you scroll", "Fetch all pictures"
        self.fetch_label.configure(text=text, fg=colour)
        self.fetch_button.configure(text=button)
        if button:
            if not self.fetch_button.winfo_ismapped():
                self.fetch_button.pack(side="right", before=self.fetch_label)
        else:
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
                self.painter.fetch_online_only = bool(self.cloud.get())

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
