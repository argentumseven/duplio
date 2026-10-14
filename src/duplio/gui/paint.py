"""Drawing the cards: a file with its thumbnail, side by side (A | B) or as the copies of one duplicate group."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from duplio.favorites import Favorites
from duplio.gui.model import (
    ARCHIVE,
    AUDIO,
    DOCUMENT,
    PICTURE,
    VIDEO,
    GroupRow,
    Mark,
    PairRow,
    RowKind,
    Tile,
    side_letter,
)
from duplio.gui.sorting import BrowseItem
from duplio.gui.style import PALETTE, SIDE_COLOR, Fonts, shorten
from duplio.gui.thumbs import Thumbnailer
from duplio.gui.widgets import VirtualList
from duplio.util.fmt import format_ns, human_size

KIND_GLYPH = {VIDEO: "▶", AUDIO: "♪", DOCUMENT: "▤", ARCHIVE: "▦"}
THUMB_SIZES = {"Small": 96, "Medium": 132, "Large": 184}
BROWSE_SIZES = {"Tiny": 72, "Small": 96, "Medium": 132, "Large": 184}
GAP = 10  # between the cells of the folder browser

RELATION_STYLE = {
    RowKind.ONLY_A: ("✗", PALETTE["bad"]),
    RowKind.ONLY_B: ("✗", PALETTE["bad"]),
    RowKind.SAME: ("✓", PALETTE["ok"]),
    RowKind.MOVED: ("⇄", PALETTE["a"]),
    RowKind.CHANGED: ("≠", PALETTE["warn"]),
    RowKind.UNREADABLE: ("?", PALETTE["muted"]),
}


def short_reason(reason: str) -> str:
    low = reason.lower()
    if reason == "online-only" or "cloud" in low:
        return "☁\nIn the cloud\n(not downloaded)"
    if "movie" in low and "ffmpeg" in low:
        return "Needs ffmpeg\nto show movies"
    if "pillow or ffmpeg" in low:
        return "Needs Pillow or\nffmpeg to show"
    if "pillow" in low:
        return "Needs the Pillow\nlibrary to show"
    if "small preview" in low or "needs-download" in low or "fetch key" in low:
        return "No small preview\ninside this file"
    if "limit" in low or "larger" in low:
        return "Too large to\npreview"
    return "No preview"


class Painter:
    """Draws cards onto a :class:`VirtualList` and keeps their thumbnails coming."""

    def __init__(self, view: VirtualList, thumbs: Thumbnailer, fonts: Fonts, box: int = 132, favorites: Favorites | None = None) -> None:
        self.view = view
        self.favorites = favorites  # when there is a list of favorites, every picture and movie gets a star to click
        self.thumbs = thumbs
        self.fonts = fonts
        self.box = box
        self.pad = 12
        self.fetch_online_only = False
        self.selectable = True  # the sync and import previews have nothing to select
        self.slot_labels = ("Folder A", "Folder B")  # what the empty half of a row says it stands for

    @property
    def canvas(self) -> Any:
        return self.view.canvas

    # ---- sizes ----------------------------------------------------------
    @property
    def pair_row_height(self) -> int:
        return self.box + 26

    @property
    def group_card_width(self) -> int:
        return max(self.box + 24, 150)

    @property
    def group_card_height(self) -> int:
        return self.box + 96

    # ---- thumbnails -----------------------------------------------------
    def _thumb(self, tile: Tile, x: int, y: int, row: int, tags: tuple[str, ...], box: int | None = None) -> None:
        """The picture square of a card, with its star."""
        self._picture(tile, x, y, row, tags, box)
        if self.favorites is not None and tile.kind in (PICTURE, VIDEO) and (box or self.box) >= 64:
            self._star(tile, x + (box or self.box), y, tags)

    def _star(self, tile: Tile, right: int, top: int, tags: tuple[str, ...]) -> None:
        """A star in the top right corner of a picture: filled when it is a favorite. Clicking it is handled by the page."""
        assert self.favorites is not None
        c = self.canvas
        starred = tile.uri in self.favorites
        mark = (tags[0], f"star:{tile.key}")
        c.create_rectangle(right - 23, top + 2, right - 2, top + 22, fill="#000000", outline="", stipple="gray50", tags=mark)
        c.create_text(right - 12, top + 12, text="★" if starred else "☆", fill=PALETTE["warn"] if starred else "#ffffff",
                      font=self.fonts.base, tags=mark)

    def star_in_list(self, tile: Tile, cx: int, cy: int, tags: tuple[str, ...]) -> None:
        """The star of a line in the list view: beside the little picture, not on it."""
        if self.favorites is None or tile.kind not in (PICTURE, VIDEO):
            return
        starred = tile.uri in self.favorites
        c = self.canvas
        mark = (tags[0], f"star:{tile.key}")
        c.create_rectangle(cx - 11, cy - 11, cx + 11, cy + 11, fill=PALETTE["panel"], outline="", tags=mark)
        c.create_text(cx, cy, text="★" if starred else "☆", fill=PALETTE["warn"] if starred else PALETTE["muted"], font=self.fonts.base,
                      tags=mark)

    def _picture(self, tile: Tile, x: int, y: int, row: int, tags: tuple[str, ...], box: int | None = None) -> None:
        """The picture square of a card: the thumbnail if it is at hand, otherwise a placeholder that is replaced when it arrives."""
        c = self.canvas
        box = box or self.box
        key = tile.picture_key or tile.key  # what the picture is, not where it sits in this scan: a late answer from an earlier scan cannot land on another file
        movie = ("movie",) if tile.kind == VIDEO else ()
        background = c.create_rectangle(x, y, x + box, y + box, fill=PALETTE["thumb_bg"], outline="",
                                        tags=(*tags, f"thumbbg:{key}", *movie))
        image = self.thumbs.peek(tile, box)
        if image is not None:
            item = c.create_image(x + box // 2, y + box // 2, image=image, tags=(*tags, f"img:{key}"))
            c.tag_raise(item, background)
            if movie:
                self._play_badge(x, y + box, tags, key)
            return
        ext = tile.ext[:5]
        if tile.kind in (PICTURE, VIDEO):
            reason = self.thumbs.reason(tile)
            c.create_text(x + box // 2, y + box // 2, text=short_reason(reason) if reason else "…", fill=PALETTE["muted"],
                          font=self.fonts.small, justify="center", width=box - 10, tags=(*tags, f"ph:{key}", f"phmsg:{key}"))
            if ext:
                c.create_text(x + box - 6, y + box - 6, text=ext, anchor="se", fill=PALETTE["muted"], font=self.fonts.tiny,
                              tags=(*tags, f"ph:{key}"))
            if not reason or (self.fetch_online_only and reason == "online-only"):
                self.thumbs.request(tile, box, self._ready, wanted=partial(self.view.in_window, row),
                                    allow_online_only=self.fetch_online_only)
            return
        glyph = KIND_GLYPH.get(tile.kind, "")
        if box < 64:  # a small square (the list view): the symbol alone, or the extension alone
            c.create_text(x + box // 2, y + box // 2, text=glyph or (ext or "FILE")[:4], fill=PALETTE["muted"],
                          font=self.fonts.base if glyph else self.fonts.tiny, tags=tags)
            return
        if glyph:
            c.create_text(x + box // 2, y + box // 2 - 12, text=glyph, fill=PALETTE["muted"], font=self.fonts.title, tags=tags)
        c.create_text(x + box // 2, y + box // 2 + (24 if glyph else 0), text=ext or "FILE", fill=PALETTE["muted"],
                      font=self.fonts.big, tags=tags)

    def _play_badge(self, x: float, bottom: float, tags: tuple[str, ...], key: str) -> None:
        """A small play button in the corner of a movie's picture, so a movie is told from a photo at a glance."""
        c = self.canvas
        c.create_rectangle(x + 4, bottom - 22, x + 26, bottom - 4, fill="#000000", outline="", stipple="gray50", tags=(*tags, f"play:{key}"))
        c.create_text(x + 15, bottom - 13, text="▶", fill="#ffffff", font=self.fonts.tiny, tags=(*tags, f"play:{key}"))

    def ready(self, key: str, image: Any, reason: str) -> None:
        """Where finished pictures are delivered (for requests made on this painter's behalf)."""
        self._ready(key, image, reason)

    def _ready(self, key: str, image: Any, reason: str) -> None:
        """A thumbnail finished (or turned out to be unavailable): update every card that shows this file."""
        c = self.canvas
        try:
            if image is not None:
                for rect in c.find_withtag(f"thumbbg:{key}"):
                    x1, y1, x2, y2 = c.coords(rect)
                    own = c.gettags(rect)
                    tags = tuple(t for t in own if not t.startswith("thumbbg:") and t not in ("current", "movie"))
                    item = c.create_image((x1 + x2) / 2, (y1 + y2) / 2, image=image, tags=(*tags, f"img:{key}"))
                    c.tag_raise(item, rect)
                    if "movie" in own:
                        self._play_badge(x1, y2, tags, key)
                c.delete(f"ph:{key}")
            else:
                c.itemconfigure(f"phmsg:{key}", text=short_reason(reason))
        except Exception:
            pass  # the row scrolled away between the request and the answer

    # ---- a card, A or B, side by side ---------------------------------------
    def card(self, tile: Tile, x: int, y: int, w: int, h: int, row: int, *, selected: bool, extra: int = 0,
             selectable: bool = True) -> None:
        c, f = self.canvas, self.fonts
        rtag = self.view.row_tag(row)
        tags = (rtag, f"tile:{tile.key}")
        color = SIDE_COLOR[tile.side]
        c.create_rectangle(x, y, x + w, y + h, fill=PALETTE["panel"], outline=color if selected else PALETTE["line"],
                           width=2 if selected else 1, tags=tags)
        c.create_rectangle(x, y, x + 6, y + h, fill=color, outline=color, tags=tags)
        tx, ty = x + 16, y + (h - self.box) // 2
        self._thumb(tile, tx, ty, row, tags)
        if selectable:
            self._checkbox(tile, tx + 6, ty + 6, selected, tags)
        text_x = tx + self.box + 14
        text_w = x + w - text_x - 38
        c.create_text(text_x, ty + 2, text=shorten(tile.name, text_w, f.bold), anchor="nw", font=f.bold, fill=PALETTE["text"], tags=tags)
        c.create_text(text_x, ty + 24, text=shorten(tile.folder or "(top folder)", text_w, f.small, middle=True), anchor="nw",
                      font=f.small, fill=PALETTE["muted"], tags=tags)
        line = ty + 44
        for part in [x.strip() for x in tile.details.split("·")][:2]:  # the size, then the date: each on a line of its own
            c.create_text(text_x, line, text=shorten(part, text_w, f.small), anchor="nw", font=f.small, fill=PALETTE["muted"], tags=tags)
            line += 18
        line += 2
        if tile.online_only:
            c.create_text(text_x, line, text="☁ only in the cloud", anchor="nw", font=f.small, fill=PALETTE["warn"], tags=tags)
            line += 20
        if extra:
            more = f"+{extra} more identical cop{'y' if extra == 1 else 'ies'} here"
            c.create_text(text_x, line, text=more, anchor="nw", font=f.small, fill=PALETTE["muted"], tags=tags)
        self._side_badge(tile.side, x + w - 14, y + 14, tags)

    def _side_badge(self, side: int, cx: int, cy: int, tags: tuple[str, ...]) -> None:
        c = self.canvas
        c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, fill=SIDE_COLOR[side], outline="", tags=tags)
        c.create_text(cx, cy, text=side_letter(side), fill=PALETTE["ink"], font=self.fonts.bold, tags=tags)

    def _checkbox(self, tile: Tile, x: int, y: int, checked: bool, tags: tuple[str, ...]) -> None:
        c = self.canvas
        ctags = (*tags, f"chk:{tile.key}")
        c.create_rectangle(x, y, x + 20, y + 20, fill=PALETTE["select"] if checked else PALETTE["field"],
                           outline=PALETTE["select"] if checked else PALETTE["muted"], width=2, tags=ctags)
        if checked:
            c.create_text(x + 10, y + 10, text="✓", fill=PALETTE["on_primary"], font=self.fonts.bold, tags=ctags)

    def empty_slot(self, side: int, x: int, y: int, w: int, h: int, row: int, text: str) -> None:
        c = self.canvas
        tags = (self.view.row_tag(row),)
        c.create_rectangle(x, y, x + w, y + h, outline=SIDE_COLOR[side], dash=(5, 4), fill=PALETTE["slot"], tags=tags)
        c.create_text(x + w // 2, y + h // 2 - 8, text=text, fill=SIDE_COLOR[side], font=self.fonts.bold, tags=tags)
        c.create_text(x + w // 2, y + h // 2 + 14, text=self.slot_labels[side], fill=PALETTE["muted"], font=self.fonts.small, tags=tags)

    # ---- one comparison row: A | relation | B ---------------------------------
    def pair_row(self, row: PairRow, y: int, width: int, selected: set[str], names: tuple[str, str] = ("A", "B")) -> None:
        rh = self.pair_row_height
        mid_w = 190 if width > 900 else 130
        pad = 12
        card_w = max((width - mid_w - 4 * pad) // 2, 120)
        top = y + 6
        card_h = rh - 12
        lx = pad
        rx = pad + card_w + pad + mid_w + pad
        rtag = self.view.row_tag(row.index)
        if row.left is not None:
            self.card(row.left, lx, top, card_w, card_h, row.index, selected=row.left.key in selected, extra=row.extra_left,
                      selectable=self.selectable and row.kind is not RowKind.SAME)
        else:
            self.empty_slot(0, lx, top, card_w, card_h, row.index, row.left_text or f"Not in {names[0]}")
        if row.right is not None:
            self.card(row.right, rx, top, card_w, card_h, row.index, selected=row.right.key in selected, extra=row.extra_right,
                      selectable=self.selectable and row.kind is not RowKind.SAME)
        else:
            self.empty_slot(1, rx, top, card_w, card_h, row.index, row.right_text or f"Not in {names[1]}")
        glyph, color = RELATION_STYLE[row.kind]
        glyph, color = row.glyph or glyph, row.color or color
        self._middle(row, pad + card_w + pad + mid_w // 2, top, card_h, mid_w - 10, glyph, color, names)
        self.canvas.create_line(pad, y + rh - 1, width - pad, y + rh - 1, fill=PALETTE["line"], tags=(rtag,))

    def _middle(self, row: PairRow, cx: int, top: int, height: int, width: int, glyph: str, color: str, names: tuple[str, str]) -> None:
        """The column between the cards: a symbol, what it means, a short note; stacked by measuring, so nothing overlaps."""
        c, f = self.canvas, self.fonts
        rtag = self.view.row_tag(row.index)
        relation = row.relation.replace("A", names[0]).replace("B", names[1]) if names != ("A", "B") else row.relation
        items = [c.create_text(cx, 0, text=glyph, fill=color, font=f.title, anchor="n", tags=(rtag,)),
                 c.create_text(cx, 0, text=relation, fill=color, font=f.bold, width=width, justify="center", anchor="n", tags=(rtag,))]
        note = row.note
        if note.lower().startswith(relation.lower()):  # the note would only repeat what is printed above it
            note = note[len(relation):].lstrip(" :;,-")
        if note:
            items.append(c.create_text(cx, 0, text=shorten(note, width * 3, f.tiny), fill=PALETTE["muted"], font=f.tiny, width=width,
                                       justify="center", anchor="n", tags=(rtag,)))
        heights = []
        for item in items:
            box = c.bbox(item)
            heights.append((box[3] - box[1]) if box else 0)
        gaps = (2, 6)
        total = sum(heights) + sum(gaps[: len(items) - 1])
        y = top + max((height - total) // 2, 2)
        for n, item in enumerate(items):
            c.coords(item, cx, y)
            y += heights[n] + (gaps[n] if n < len(gaps) else 0)

    # ---- the folder browser: a wall of pictures, or a plain list ---------------------
    def browse_cell_size(self, box: int) -> tuple[int, int]:
        return box + 20, box + 78

    def browse_columns(self, width: int, box: int) -> int:
        return max(1, (width - 2 * self.pad + GAP) // (self.browse_cell_size(box)[0] + GAP))

    def browse_row_height(self, box: int) -> int:
        return self.browse_cell_size(box)[1] + GAP

    DETAIL_BOX = 40

    @property
    def detail_row_height(self) -> int:
        return self.DETAIL_BOX + 16

    def browse_grid_row(self, cells: Sequence[tuple[str, BrowseItem]], y: int, width: int, row: int, box: int, selected: str | None) -> None:
        """One row of the wall: ``cells`` are ``(key, item)``."""
        cell_w, cell_h = self.browse_cell_size(box)
        for column, (key, item) in enumerate(cells):
            x = self.pad + column * (cell_w + GAP)
            self._browse_cell(key, item, x, y + GAP // 2, cell_w, cell_h, row, box, key == selected)

    def _browse_cell(self, key: str, item: BrowseItem, x: int, y: int, w: int, h: int, row: int, box: int, selected: bool) -> None:
        c, f = self.canvas, self.fonts
        tags = (self.view.row_tag(row), f"tile:{key}")
        c.create_rectangle(x, y, x + w, y + h, fill=PALETTE["panel"], outline=PALETTE["primary"] if selected else PALETTE["line"],
                           width=2 if selected else 1, tags=tags)
        tx, ty = x + (w - box) // 2, y + 8
        if item.tile is not None:
            self._thumb(item.tile, tx, ty, row, tags, box)
        else:
            self._folder_icon(tx, ty, box, tags)
        if item.offline:
            c.create_text(tx + box - 28 if self.favorites is not None else tx + box - 4, ty + 4, text="☁", anchor="ne", fill=PALETTE["warn"],
                          font=f.base, tags=tags)
        c.create_text(x + w // 2, ty + box + 8, text=shorten(item.name, w - 12, f.bold), anchor="n", font=f.bold, fill=PALETTE["text"], tags=tags)
        lines = item.tile.details.split(" · ") if item.tile is not None else ["folder"]  # the size, then the date: two short lines
        for n, line in enumerate(lines[:2]):
            c.create_text(x + w // 2, ty + box + 28 + n * 15, text=shorten(line, w - 8, f.mono), anchor="n", font=f.mono,
                          fill=PALETTE["muted"], tags=tags)

    def _folder_icon(self, x: int, y: int, box: int, tags: tuple[str, ...]) -> None:
        """A folder, drawn with shapes (no image to load): a tab and a body."""
        c = self.canvas
        c.create_rectangle(x, y, x + box, y + box, fill=PALETTE["thumb_bg"], outline="", tags=tags)
        pad, top = box // 6, y + box // 4
        right, bottom = x + box - pad, y + box - box // 5
        c.create_polygon(x + pad, top, x + pad + box // 4, top, x + pad + box // 4 + 8, top + 10, right, top + 10, right, bottom, x + pad, bottom,
                         fill=PALETTE["warn"], outline=PALETTE["warn"], tags=tags)
        c.create_rectangle(x + pad, top + 14, right, bottom, fill=PALETTE["select"], outline=PALETTE["select"], tags=tags)

    def browse_detail_row(self, key: str, item: BrowseItem, y: int, width: int, row: int, selected: bool) -> None:
        """One entry as a line: a small picture, the name, then type, size and date."""
        c, f = self.canvas, self.fonts
        box = self.DETAIL_BOX
        tags = (self.view.row_tag(row), f"tile:{key}")
        h = self.detail_row_height
        c.create_rectangle(self.pad, y + 2, width - self.pad, y + h - 2, fill=PALETTE["header"] if selected else PALETTE["panel"],
                           outline=PALETTE["primary"] if selected else PALETTE["line"], tags=tags)
        tx, ty = self.pad + 8 + (28 if self.favorites is not None else 0), y + 8
        if item.tile is not None:
            self._thumb(item.tile, tx, ty, row, tags, box)
            self.star_in_list(item.tile, self.pad + 8 + 12, y + h // 2, tags)
        else:
            self._folder_icon(tx, ty, box, tags)
        columns = width - self.pad - 10
        name_w = max(columns - 8 - 330 - (tx + box + 12), 80)
        c.create_text(tx + box + 12, y + h // 2, text=shorten(item.name + ("   ☁" if item.offline else ""), name_w, f.bold), anchor="w",
                      font=f.bold, fill=PALETTE["text"], tags=tags)
        for x, text in ((columns - 320, item.type.upper() or ("FOLDER" if item.is_dir else "")),
                        (columns - 240, "" if item.is_dir else human_size(item.size)),
                        (columns - 120, "" if not item.mtime_ns else format_ns(item.mtime_ns)[:16])):
            c.create_text(x, y + h // 2, text=text, anchor="w", font=f.mono, fill=PALETTE["muted"], tags=tags)

    # ---- a duplicate group: every copy as a card -----------------------------
    def group_columns(self, width: int) -> int:
        return max(1, (width - 2 * self.pad) // (self.group_card_width + 12))

    def group_height(self, group: GroupRow, width: int) -> int:
        rows = math.ceil(len(group.tiles) / self.group_columns(width))
        return 40 + rows * (self.group_card_height + 12) + 8

    def group_row(self, group: GroupRow, y: int, width: int, label_for: Callable[[int], str]) -> None:
        c, f = self.canvas, self.fonts
        rtag = self.view.row_tag(group.index)
        n = len(group.tiles)
        header = f"Group {group.group.id}  ·  {n} identical copies  ·  {self._size(group.tiles[0].size)} each"
        c.create_rectangle(0, y, width, y + 34, fill=PALETTE["header"], outline="", tags=(rtag,))
        c.create_text(self.pad, y + 17, text=header, anchor="w", font=f.bold, fill=PALETTE["text"], tags=(rtag,))
        if group.skipped:
            status, status_color = "Skipped: nothing will be removed", PALETTE["muted"]
        elif group.removals:
            status, status_color = f"Removes {len(group.removals)}  ·  frees {self._size(group.reclaimable)}", PALETTE["bad"]
        else:
            status, status_color = "Nothing to remove", PALETTE["muted"]
        c.create_text(width - self.pad, y + 17, text=status, anchor="e", font=f.base, fill=status_color, tags=(rtag,))
        cols = self.group_columns(width)
        cw, ch = self.group_card_width, self.group_card_height
        for i, tile in enumerate(group.tiles):
            col, line = i % cols, i // cols
            x = self.pad + col * (cw + 12)
            top = y + 42 + line * (ch + 12)
            self.group_card(group, tile, x, top, cw, ch, label_for(tile.side))

    def group_card(self, group: GroupRow, tile: Tile, x: int, y: int, w: int, h: int, where: str) -> None:
        c, f = self.canvas, self.fonts
        rtag = self.view.row_tag(group.index)
        tags = (rtag, f"tile:{tile.key}")
        mark = Mark.KEEP if group.skipped and tile.entry.id not in group.locked else group.mark(tile)
        outline, label, label_bg = {
            Mark.KEEP: (PALETTE["ok"], "KEEP", PALETTE["ok"]),
            Mark.REMOVE: (PALETTE["bad"], "REMOVE", PALETTE["bad"]),
            Mark.LOCKED: (PALETTE["muted"], "LOCKED", PALETTE["muted"]),
        }[mark]
        c.create_rectangle(x, y, x + w, y + h, fill=PALETTE["panel"], outline=outline, width=3, tags=tags)
        tx = x + (w - self.box) // 2
        self._thumb(tile, tx, y + 10, group.index, tags)
        self._side_badge(tile.side, x + w - 18, y + 22, tags)
        by = y + 10 + self.box + 8
        c.create_rectangle(x + 8, by, x + 8 + 72, by + 22, fill=label_bg, outline="", tags=tags)
        c.create_text(x + 8 + 36, by + 11, text=label, fill=PALETTE["ink"], font=f.bold, tags=tags)
        c.create_text(x + 88, by + 11, text=where, anchor="w", fill=PALETTE["muted"], font=f.small, tags=tags)
        text_w = w - 16
        c.create_text(x + 8, by + 30, text=shorten(tile.name, text_w, f.bold), anchor="nw", font=f.bold, fill=PALETTE["text"], tags=tags)
        c.create_text(x + 8, by + 50, text=shorten(tile.folder or "(top folder)", text_w, f.small, middle=True), anchor="nw",
                      font=f.small, fill=PALETTE["muted"], tags=tags)

    @staticmethod
    def _size(n: int) -> str:
        from duplio.util.fmt import human_size

        return human_size(n)
