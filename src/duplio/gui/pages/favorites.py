"""Favorites: the pictures and movies you starred, from any folder or drive, and the button that makes an album of them.

A star is a note that says "this one". It changes nothing about the file. The album is a new folder of copies, made with the
same checked, undoable copy as every other copy Duplio makes.
"""

from __future__ import annotations

import math
import tkinter as tk
from functools import partial
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.backup.album import AlbumPlan, plan_album, run_album
from duplio.favorites import Favorite
from duplio.gui import actions
from duplio.gui.app import Page
from duplio.gui.dialogs import AlbumDialog
from duplio.gui.model import Tile
from duplio.gui.paint import BROWSE_SIZES, Painter
from duplio.gui.sorting import BrowseItem, natural_key
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, FlowFrame, VirtualList, autowrap, tag_value
from duplio.scan.inventory import FileEntry, Root, RootRole
from duplio.util.fmt import human_size, plural
from duplio.util.opener import open_in_viewer
from duplio.vfs.base import Capabilities
from duplio.vfs.location import Location

if TYPE_CHECKING:
    from duplio.gui.app import Context

ORDERS = (
    ("Newest starred first", lambda f: -f.added),
    ("Oldest starred first", lambda f: f.added),
    ("Name, A to Z", lambda f: natural_key(f.name)),
    ("Newest picture first", lambda f: -f.mtime_ns),
    ("Folder, then name", lambda f: (f.uri.rsplit("/", 1)[0].casefold(), natural_key(f.name))),
)


def tile_for(ctx: Context, favorite: Favorite, number: int) -> Tile | None:
    """A tile for a favorite, so it can be drawn like any other file. ``None`` if its address cannot even be understood."""
    try:
        where = ctx.registry.open(favorite.uri)
    except Exception:
        return None
    parent = Location(where.backend, where.backend.parent(where.path))
    root = Root(0, parent, RootRole.NORMAL, "", Capabilities())  # no probing of a drive that may not be there
    name = where.backend.basename(where.path) or favorite.name
    entry = FileEntry(number, 0, where.path, name, favorite.size, favorite.mtime_ns)
    return Tile(0, entry, root)


class FavoritesPage(Page):
    title = "Favorites"
    subtitle = ("The pictures and movies you starred, from any folder or drive. Click a star to take it away. "
                "Make an album to gather copies of them in one new folder.")
    hints = (("Enter", "open"), ("F", "unstar"))

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f, st = ctx.fonts, ctx.state
        self.items: list[BrowseItem] = []
        self.by_key: dict[str, BrowseItem] = {}
        self.item_keys: list[str] = []
        self.selected: str | None = None
        self._seen_version = -1
        self._generation = 0
        self._scroll_after: str | None = None
        self._last_hover = ""
        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 14))
        tools = FlowFrame(body, PALETTE["bg"], hgap=10, vgap=6)
        tools.pack(fill="x", pady=(4, 6))
        self.album_button = Btn(tools, "Make an album…", self.make_album, kind="primary", fonts=f, padx=16, pady=7)
        tools.add(self.album_button, gap_after=20)
        self.order_box = tools.field("Order", lambda box: ttk.Combobox(box, state="readonly", values=[o[0] for o in ORDERS], width=22, font=f.small),
                                     f.small, gap_after=14)
        self.order_box.set(st.get("favorites_order", ORDERS[0][0]) if st.get("favorites_order") in [o[0] for o in ORDERS] else ORDERS[0][0])
        self.order_box.bind("<<ComboboxSelected>>", lambda _e: self._ordered())
        self.size_box = tools.field("Picture size", lambda box: ttk.Combobox(box, state="readonly", values=list(BROWSE_SIZES), width=7, font=f.small),
                                    f.small, gap_after=14)
        self.size_box.set(st.get("favorites_size", "Medium") if st.get("favorites_size", "Medium") in BROWSE_SIZES else "Medium")
        self.size_box.bind("<<ComboboxSelected>>", lambda _e: self._ordered())
        tools.add(Btn(tools, "Remove all stars…", self.clear_all, fonts=f, padx=10, pady=4))
        self.summary = tk.Label(body, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w", justify="left")
        self.summary.pack(fill="x", pady=(2, 6))
        autowrap(self.summary, margin=4, minimum=300)
        self.selected_label = tk.Label(body, text="", font=f.mono, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w")
        self.selected_label.pack(side="bottom", fill="x", pady=(4, 0))
        holder = tk.Frame(body, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.view = VirtualList(holder, f, self._draw, on_click=self._click, on_double=self._double, on_context=self._context, on_hover=self._hover)
        self.view.pack(fill="both", expand=True)
        self.painter = Painter(self.view, ctx.thumbs, f, self.box, favorites=ctx.favorites)
        self.painter.selectable = False
        self.view.relayout = self._heights
        self.view.on_scroll = self._scrolled
        canvas = self.view.canvas
        canvas.bind("<Return>", lambda _e: self._open_selected())
        canvas.bind("<Key-f>", lambda _e: self._unstar_selected())
        canvas.bind("<Key-F>", lambda _e: self._unstar_selected())

    # ---- what is shown ------------------------------------------------------------------------
    @property
    def box(self) -> int:
        return BROWSE_SIZES.get(self.size_box.get(), 132)

    def on_show(self) -> None:
        if self._seen_version != self.ctx.favorites.version:
            self.refresh()
        else:
            self.view.redraw_all()

    def _ordered(self) -> None:
        self.ctx.state.set("favorites_order", self.order_box.get())
        self.ctx.state.set("favorites_size", self.size_box.get())
        self.ctx.state.save()
        self.refresh(keep_position=True)

    def refresh(self, keep_position: bool = False) -> None:
        favorites = self.ctx.favorites
        self._seen_version = favorites.version
        self._generation += 1  # read-ahead requested for the old list is not wanted any more
        key = dict(ORDERS)[self.order_box.get()]
        chosen = sorted(favorites.items(), key=key)  # type: ignore[arg-type]
        self.items, unreadable = [], 0
        for number, fav in enumerate(chosen, start=1):
            tile = tile_for(self.ctx, fav, number)
            if tile is None:
                unreadable += 1
                continue
            self.items.append(BrowseItem(tile.name, False, fav.size, fav.mtime_ns, tile, False))
        self.item_keys = [i.tile.key for i in self.items if i.tile is not None]
        self.by_key = dict(zip(self.item_keys, self.items, strict=True))
        if self.selected not in self.by_key:
            self.selected = None
            self.selected_label.configure(text="")
        pictures = sum(1 for i in self.items if i.tile is not None and i.tile.kind == "picture")
        movies = len(self.items) - pictures
        if self.items:
            text = f"{plural(len(self.items), 'favorite')}: {plural(pictures, 'picture')}" + (f", {plural(movies, 'movie')}" if movies else "")
            text += f" · {human_size(sum(i.size for i in self.items))}"
            if unreadable:
                text += f" · {plural(unreadable, 'favorite')} could not be understood and {'is' if unreadable == 1 else 'are'} not shown"
        else:
            text = "Nothing is starred yet."
        self.summary.configure(text=text, fg=PALETTE["text"])
        self.album_button.set_enabled(bool(self.items))
        self.painter.box = self.box
        empty = ("Nothing is starred yet.\n\nClick the ☆ in the corner of a picture or movie anywhere in Duplio (Browse a folder is a good place), "
                 "or select one and press F.")
        self.view.set_rows(self._heights(self.view.width), empty, keep_position=keep_position)
        self._scrolled()

    def _heights(self, width: int) -> list[int]:
        columns = self.painter.browse_columns(width, self.box)
        return [self.painter.browse_row_height(self.box)] * math.ceil(len(self.items) / columns)

    def _draw(self, view: VirtualList, index: int, y: int, width: int) -> None:
        columns = self.painter.browse_columns(width, self.box)
        start = index * columns
        cells = list(zip(self.item_keys[start : start + columns], self.items[start : start + columns], strict=True))
        self.painter.browse_grid_row(cells, y, width, index, self.box, self.selected)

    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _scrolled(self) -> None:
        if self._scroll_after is not None:
            self.after_cancel(self._scroll_after)
        self._scroll_after = self.after(120, self._settled)

    def _settled(self) -> None:
        """Make the pictures a few screens ahead and behind, so scrolling shows them already."""
        self._scroll_after = None
        if not self.items:
            return
        first_row, last_row = self.view.visible_range()
        per_row = self.painter.browse_columns(self.view.width, self.box)
        span = max(3, last_row - first_row)
        low, high = max(first_row - span, 0), min(last_row + span, math.ceil(len(self.items) / per_row))
        generation = self._generation
        for row in range(low, high):
            if first_row <= row < last_row:
                continue
            for item in self.items[row * per_row : (row + 1) * per_row]:
                if item.tile is not None and item.tile.picture_key and self.ctx.thumbs.peek(item.tile, self.box) is None \
                        and not self.ctx.thumbs.reason(item.tile):
                    self.ctx.thumbs.request(item.tile, self.box, self.painter.ready, wanted=partial(self._current, generation), background=True)

    # ---- the mouse and keys -------------------------------------------------------------------
    def _tile(self, tags: tuple[str, ...], prefix: str = "tile:") -> Tile | None:
        key = tag_value(tags, prefix)
        item = self.by_key.get(key) if key else None
        return item.tile if item is not None else None

    def _click(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        starred = self._tile(tags, "star:")
        if starred is not None:
            self._unstar(starred)
            return
        tile = self._tile(tags)
        self.selected = tile.key if tile is not None else None
        self.view.redraw_all()
        self.selected_label.configure(text=f"{tile.name} · {tile.details} · {tile.location}" if tile is not None else "")

    def _double(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        tile = self._tile(tags)
        if tile is not None:
            actions.open_tile(self.ctx, tile)

    def _open_selected(self) -> None:
        item = self.by_key.get(self.selected or "")
        if item is not None and item.tile is not None:
            actions.open_tile(self.ctx, item.tile)

    def _unstar(self, tile: Tile) -> None:
        actions.toggle_favorite(self.ctx, tile)
        self.refresh(keep_position=True)

    def _unstar_selected(self) -> None:
        item = self.by_key.get(self.selected or "")
        if item is not None and item.tile is not None:
            self._unstar(item.tile)

    def _context(self, tags: tuple[str, ...], event: tk.Event) -> None:
        tile = self._tile(tags)
        if tile is None:
            return
        self.selected = tile.key
        self.view.redraw_all()
        actions.context_menu(self.ctx, tile, event.x_root, event.y_root, [("Make an album from my favorites…", self.make_album)],
                             None, partial(self.refresh, True))

    def _hover(self, tags: tuple[str, ...], event: tk.Event | None) -> None:
        tile = self._tile(tags) if event is not None else None
        text = f"{tile.location}\n{tile.details}" if tile is not None else ""
        if text != self._last_hover:
            self._last_hover = text
            if text and event is not None:
                self.ctx.tooltip.schedule(text, event.x_root, event.y_root)
            else:
                self.ctx.tooltip.hide()

    # ---- the whole list ---------------------------------------------------------------------------
    def clear_all(self) -> None:
        favorites = self.ctx.favorites
        if not len(favorites):
            return
        if not self.ctx.confirm("Remove all stars?", f"Take the star off all {len(favorites):,} favorites?",
                                ["Only the stars go. The pictures and movies themselves are not touched."], confirm="Remove the stars", danger=True):
            return
        favorites.clear()
        self.refresh()

    # ---- the album -----------------------------------------------------------------------------------
    def make_album(self) -> None:
        ctx = self.ctx
        favorites = ctx.favorites.items()
        if not favorites:
            ctx.info("Nothing is starred", "Star some pictures first.", ["Click the ☆ in the corner of a picture or movie."])
            return
        choice = AlbumDialog(ctx, len(favorites)).show()
        if choice is None:
            return

        def plan_job(progress: Any, cancel: Any) -> AlbumPlan:
            progress.stage("Looking at your favorites", total_items=len(favorites))
            return plan_album(favorites, ctx.registry, ctx.registry.open(choice.destination), choice.options)

        ctx.run_job("Getting the album ready", plan_job, partial(self._confirm_album, choice.destination), on_error=ctx.show_error)

    def _confirm_album(self, destination: str, plan: AlbumPlan) -> None:
        ctx = self.ctx
        if not plan.actions:
            lines = [f"{fav.name}: {why}" for fav, why in plan.skipped[:8]]
            ctx.info("Nothing to copy", "None of the favorites can be reached right now.", lines, "warn")
            return
        rows = [(a.favorite.name, f"→ {a.dest_rel}" + (f"  ({a.note})" if a.note else "")) for a in plan.actions]
        details = ["The originals are not touched. Each copy is read back and compared, nothing is overwritten, and History & undo can take "
                   "the album away again.", *plan.warnings]
        if plan.skipped:
            shown = "; ".join(f"{fav.name} {why}" for fav, why in plan.skipped[:3])
            details.append(f"{plural(len(plan.skipped), 'favorite')} will be left out: {shown}" + (" …" if len(plan.skipped) > 3 else ""))
        if not ctx.confirm("Make the album", f"Copy {plural(len(plan.actions), 'file')} ({human_size(plan.bytes_to_copy)}) into a new folder, "
                           f"'{plan.folder_name}'?", details, rows, confirm="Make the album", columns=("Favorite", f"Becomes, in {destination}")):
            return

        def job(progress: Any, cancel: Any) -> Any:
            return run_album(plan, ctx.registry, dirs=ctx.dirs, progress=progress, cancel=cancel)

        def done(report: Any) -> None:
            ok, failed = report.count("done"), report.count("failed")
            lines = [f"{ok:,} files copied ({human_size(report.bytes_copied)}) into {plan.album_location.display}."]
            lines += [f"{r.action.entry.rel}: {r.message}" for r in report.failures[:6]]
            if report.cancelled:
                lines.append("You stopped it early; what was copied is complete and checked.")
            album = plan.album_location
            buttons = []
            if album.backend.id == "local":
                buttons.append(("Open the album folder", lambda: open_in_viewer(album.path), "primary"))
            ctx.info("Album made", "Done." if not failed else f"Done, but {failed:,} files could not be copied.", lines,
                     "ok" if not failed else "warn", buttons)

        ctx.run_job(f"Making the album '{plan.folder_name}'", job, done)

