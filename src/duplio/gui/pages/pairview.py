"""The side-by-side list: folder A on the left, folder B on the right, one row per file, real thumbnails."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING

from duplio.gui import actions
from duplio.gui.model import PairRow, RowKind, Tile
from duplio.gui.paint import THUMB_SIZES, Painter
from duplio.gui.sorting import PAIR_FIELDS, choose, label_of, sort_pair_rows
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, VirtualList, tag_value
from duplio.util.fmt import human_size

if TYPE_CHECKING:
    from duplio.gui.app import Context


class PairView(tk.Frame):
    """Rows of ``A | what is between them | B`` with thumbnails, selection, and a small toolbar."""

    def __init__(self, master: tk.Misc, ctx: Context, names: tuple[str, str] = ("A", "B"),
                 on_selection: Callable[[], None] | None = None, selectable: bool = True,
                 slot_labels: tuple[str, str] | None = None) -> None:
        super().__init__(master, bg=PALETTE["bg"])
        self.ctx = ctx
        self.names = names
        self._on_selection = on_selection
        self.rows: list[PairRow] = []
        self._given: list[PairRow] = []  # the rows as they were handed over, before sorting
        self.selected: set[str] = set()
        self.tiles: dict[str, Tile] = {}
        f = ctx.fonts
        bar = tk.Frame(self, bg=PALETTE["bg"])
        bar.pack(fill="x", pady=(0, 6), side="top")
        tk.Label(bar, text="Picture size", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="left")
        self.size_box = ttk.Combobox(bar, state="readonly", values=list(THUMB_SIZES), width=8, font=f.small)
        self.size_box.set(ctx.state.get("thumb_size", "Medium"))
        self.size_box.pack(side="left", padx=(6, 18))
        self.size_box.bind("<<ComboboxSelected>>", lambda _e: self._resize())
        tk.Label(bar, text="Sort by", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="left")
        self.sort_box = ttk.Combobox(bar, state="readonly", values=[v for _k, v in PAIR_FIELDS], width=16, font=f.small)
        self.sort_box.set(label_of(PAIR_FIELDS, ctx.state.get("pair_sort", "default")))
        self.sort_box.pack(side="left", padx=(6, 4))
        self.sort_box.bind("<<ComboboxSelected>>", lambda _e: self._resort())
        self.descending = bool(ctx.state.get("pair_desc", False))
        self.direction = Btn(bar, "", self._flip, fonts=f, padx=8, pady=3)
        self.direction.pack(side="left", padx=(0, 18))
        self._paint_direction()
        self.picked = tk.Label(bar, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"])
        self.picked.pack(side="left")
        self.select_all_button = Btn(bar, "Select all", self.select_shown, fonts=f, padx=10, pady=4)
        self.select_all_button.pack(side="right")
        self.clear_button = Btn(bar, "Clear", self.clear_selection, fonts=f, padx=10, pady=4)
        self.clear_button.pack(side="right", padx=(0, 8))
        holder = tk.Frame(self, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.view = VirtualList(holder, f, self._draw_row, on_click=self._click, on_double=self._double, on_context=self._context,
                                on_hover=self._hover)
        self.view.pack(fill="both", expand=True)
        self.painter = Painter(self.view, ctx.thumbs, f, THUMB_SIZES.get(self.size_box.get(), 132))
        self.painter.selectable = selectable
        if slot_labels:
            self.painter.slot_labels = slot_labels
        if not selectable:
            self.select_all_button.pack_forget()
            self.clear_button.pack_forget()
        self.view.relayout = lambda _w: [self.painter.pair_row_height] * len(self.rows)
        self._last_hover = ""

    # ---- content ---------------------------------------------------------------
    @property
    def sort_field(self) -> str:
        return choose(PAIR_FIELDS, self.sort_box.get())

    def _paint_direction(self) -> None:
        self.direction.configure(text="▼ Descending" if self.descending else "▲ Ascending")

    def _flip(self) -> None:
        self.descending = not self.descending
        self._paint_direction()
        self._resort()

    def _resort(self) -> None:
        self.ctx.state.set("pair_sort", self.sort_field)
        self.ctx.state.set("pair_desc", self.descending)
        self.ctx.state.save()
        self.show_rows(self._given, self._empty_text, keep_position=False)

    _empty_text = "Nothing to show here."

    def show_rows(self, rows: Sequence[PairRow], empty_text: str = "Nothing to show here.", *, keep_position: bool = False) -> None:
        self._given = list(rows)
        self._empty_text = empty_text
        self.rows = sort_pair_rows(self._given, self.sort_field, self.descending)
        for n, row in enumerate(self.rows):
            row.index = n
            for tile in row.tiles:
                self.tiles[tile.key] = tile
        self.view.set_rows([self.painter.pair_row_height] * len(self.rows), empty_text, keep_position=keep_position)
        self._update_selection_label()

    def reset(self) -> None:
        """Forget the selection and every tile: for a new answer (the numbers that name the tiles start again with each scan)."""
        self.selected.clear()
        self.tiles.clear()
        self.rows = []
        self._given = []

    def _draw_row(self, view: VirtualList, index: int, y: int, width: int) -> None:
        self.painter.pair_row(self.rows[index], y, width, self.selected, self.names)

    def _resize(self) -> None:
        label = self.size_box.get()
        self.ctx.state.set("thumb_size", label)
        self.painter.box = THUMB_SIZES.get(label, 132)
        self.view.set_rows([self.painter.pair_row_height] * len(self.rows), keep_position=True)

    # ---- selection -----------------------------------------------------------------
    def selected_tiles(self, side: int | None = None) -> list[Tile]:
        return [self.tiles[k] for k in self.selected if k in self.tiles and (side is None or self.tiles[k].side == side)]

    def toggle(self, tile: Tile) -> None:
        if tile.key in self.selected:
            self.selected.discard(tile.key)
        else:
            self.selected.add(tile.key)
        for row in self._rows_of(tile):
            self.view.redraw_row(row)
        self._update_selection_label()

    def _rows_of(self, tile: Tile) -> list[int]:
        return [r.index for r in self.rows if tile.key in (t.key for t in r.tiles)]

    def select_shown(self) -> None:
        for row in self.rows:
            if row.kind is RowKind.SAME:
                continue
            for tile in row.tiles:
                self.selected.add(tile.key)
        self.view.redraw_all()
        self._update_selection_label()

    def clear_selection(self) -> None:
        self.selected.clear()
        self.view.redraw_all()
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        tiles = self.selected_tiles()
        if tiles:
            size = sum(t.size for t in tiles)
            self.picked.configure(text=f"{len(tiles):,} selected  ({human_size(size)})")
        else:
            self.picked.configure(text="Click to select" if self.painter.selectable else "")
        if self._on_selection:
            self._on_selection()

    # ---- the mouse ---------------------------------------------------------------------
    def _tile_at(self, tags: tuple[str, ...]) -> Tile | None:
        key = tag_value(tags, "tile:")
        return self.tiles.get(key) if key else None

    def _click(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        tile = self._tile_at(tags)
        if tile is not None:
            row = next((r for r in self.rows if tile.key in (t.key for t in r.tiles)), None)
            if self.painter.selectable and row is not None and row.kind is not RowKind.SAME:
                self.toggle(tile)

    def _double(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        tile = self._tile_at(tags)
        if tile is not None:
            actions.open_tile(self.ctx, tile)

    def _context(self, tags: tuple[str, ...], event: tk.Event) -> None:
        tile = self._tile_at(tags)
        if tile is None:
            return
        fetch = None
        if tile.kind == "picture":
            def fetch() -> None:
                self.ctx.thumbs.forget_reason(tile)
                self.painter.fetch_online_only = True
                self.view.redraw_all()
                self.painter.fetch_online_only = False

        actions.context_menu(self.ctx, tile, event.x_root, event.y_root, [
            ("Select" if tile.key not in self.selected else "Unselect", lambda: self.toggle(tile)),
        ], fetch)

    def _hover(self, tags: tuple[str, ...], event: tk.Event | None) -> None:
        tile = self._tile_at(tags) if event is not None else None
        text = f"{tile.location}\n{tile.details}" if tile is not None else ""
        if text != self._last_hover:
            self._last_hover = text
            if text and event is not None:
                self.ctx.tooltip.schedule(text, event.x_root, event.y_root)
            else:
                self.ctx.tooltip.hide()
