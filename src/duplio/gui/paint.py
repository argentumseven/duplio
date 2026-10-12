"""Drawing the cards: a file with its thumbnail, side by side (A | B) or as the copies of one duplicate group."""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import partial
from typing import Any

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
from duplio.gui.style import PALETTE, SIDE_COLOR, Fonts, shorten
from duplio.gui.thumbs import Thumbnailer
from duplio.gui.widgets import VirtualList

KIND_GLYPH = {VIDEO: "▶", AUDIO: "♪", DOCUMENT: "▤", ARCHIVE: "▦"}
THUMB_SIZES = {"Small": 96, "Medium": 132, "Large": 184}

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
    if "pillow" in low:
        return "Needs the Pillow\nlibrary to show"
    if "small preview" in low or "needs-download" in low or "fetch key" in low:
        return "No small preview\ninside this file"
    if "limit" in low or "larger" in low:
        return "Too large to\npreview"
    return "No preview"


class Painter:
    """Draws cards onto a :class:`VirtualList` and keeps their thumbnails coming."""

    def __init__(self, view: VirtualList, thumbs: Thumbnailer, fonts: Fonts, box: int = 132) -> None:
        self.view = view
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
    def _thumb(self, tile: Tile, x: int, y: int, row: int, tags: tuple[str, ...]) -> None:
        """The picture square of a card: the thumbnail if it is at hand, otherwise a placeholder that is replaced when it arrives."""
        c, box = self.canvas, self.box
        key = tile.picture_key or tile.key  # what the picture is, not where it sits in this scan: a late answer from an earlier scan cannot land on another file
        background = c.create_rectangle(x, y, x + box, y + box, fill=PALETTE["thumb_bg"], outline="", tags=(*tags, f"thumbbg:{key}"))
        image = self.thumbs.peek(tile, box)
        if image is not None:
            item = c.create_image(x + box // 2, y + box // 2, image=image, tags=(*tags, f"img:{key}"))
            c.tag_raise(item, background)
            return
        ext = tile.ext[:5]
        if tile.kind == PICTURE:
            reason = self.thumbs.reason(tile)
            c.create_text(x + box // 2, y + box // 2, text=short_reason(reason) if reason else "…", fill=PALETTE["muted"],
                          font=self.fonts.small, justify="center", width=box - 10, tags=(*tags, f"ph:{key}", f"phmsg:{key}"))
            if ext:
                c.create_text(x + box - 6, y + box - 6, text=ext, anchor="se", fill=PALETTE["muted"], font=self.fonts.tiny,
                              tags=(*tags, f"ph:{key}"))
            if not reason or self.fetch_online_only:
                self.thumbs.request(tile, box, self._ready, wanted=partial(self.view.in_window, row),
                                    allow_online_only=self.fetch_online_only)
            return
        glyph = KIND_GLYPH.get(tile.kind, "")
        if glyph:
            c.create_text(x + box // 2, y + box // 2 - 12, text=glyph, fill=PALETTE["muted"], font=self.fonts.title, tags=tags)
        c.create_text(x + box // 2, y + box // 2 + (24 if glyph else 0), text=ext or "FILE", fill=PALETTE["muted"],
                      font=self.fonts.big, tags=tags)

    def _ready(self, key: str, image: Any, reason: str) -> None:
        """A thumbnail finished (or turned out to be unavailable): update every card that shows this file."""
        c = self.canvas
        try:
            if image is not None:
                for rect in c.find_withtag(f"thumbbg:{key}"):
                    x1, y1, x2, y2 = c.coords(rect)
                    tags = tuple(t for t in c.gettags(rect) if not t.startswith("thumbbg:") and t != "current")
                    item = c.create_image((x1 + x2) / 2, (y1 + y2) / 2, image=image, tags=(*tags, f"img:{key}"))
                    c.tag_raise(item, rect)
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
        c.create_rectangle(x, y, x + w, y + h, fill="#ffffff", outline=color if selected else PALETTE["line"],
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
        c.create_text(text_x, ty + 44, text=tile.details, anchor="nw", font=f.small, fill=PALETTE["muted"], tags=tags)
        line = ty + 66
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
        c.create_text(cx, cy, text=side_letter(side), fill="#ffffff", font=self.fonts.bold, tags=tags)

    def _checkbox(self, tile: Tile, x: int, y: int, checked: bool, tags: tuple[str, ...]) -> None:
        c = self.canvas
        ctags = (*tags, f"chk:{tile.key}")
        c.create_rectangle(x, y, x + 20, y + 20, fill=PALETTE["select"] if checked else "#ffffff",
                           outline=PALETTE["select"] if checked else PALETTE["muted"], width=2, tags=ctags)
        if checked:
            c.create_text(x + 10, y + 10, text="✓", fill="#ffffff", font=self.fonts.bold, tags=ctags)

    def empty_slot(self, side: int, x: int, y: int, w: int, h: int, row: int, text: str) -> None:
        c = self.canvas
        tags = (self.view.row_tag(row),)
        c.create_rectangle(x, y, x + w, y + h, outline=SIDE_COLOR[side], dash=(5, 4), fill="#fafbfd", tags=tags)
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
        if row.note:
            items.append(c.create_text(cx, 0, text=shorten(row.note, width * 2, f.tiny), fill=PALETTE["muted"], font=f.tiny, width=width,
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
        c.create_rectangle(0, y, width, y + 34, fill="#eef0f6", outline="", tags=(rtag,))
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
        c.create_rectangle(x, y, x + w, y + h, fill="#ffffff", outline=outline, width=3, tags=tags)
        tx = x + (w - self.box) // 2
        self._thumb(tile, tx, y + 10, group.index, tags)
        self._side_badge(tile.side, x + w - 18, y + 22, tags)
        by = y + 10 + self.box + 8
        c.create_rectangle(x + 8, by, x + 8 + 72, by + 22, fill=label_bg, outline="", tags=tags)
        c.create_text(x + 8 + 36, by + 11, text=label, fill="#ffffff", font=f.bold, tags=tags)
        c.create_text(x + 88, by + 11, text=where, anchor="w", fill=PALETTE["muted"], font=f.small, tags=tags)
        text_w = w - 16
        c.create_text(x + 8, by + 30, text=shorten(tile.name, text_w, f.bold), anchor="nw", font=f.bold, fill=PALETTE["text"], tags=tags)
        c.create_text(x + 8, by + 50, text=shorten(tile.folder or "(top folder)", text_w, f.small, middle=True), anchor="nw",
                      font=f.small, fill=PALETTE["muted"], tags=tags)

    @staticmethod
    def _size(n: int) -> str:
        from duplio.util.fmt import human_size

        return human_size(n)
