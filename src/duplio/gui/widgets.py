"""Small building blocks: coloured buttons, tooltips, filter chips and a list that draws only what is on screen."""

from __future__ import annotations

import bisect
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from functools import partial
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from duplio.gui.palette import DARK, shade
from duplio.gui.style import PALETTE, Fonts

BUTTON_COLORS = {  # kind: (fill, text, fill when the pointer is over it)
    "primary": (PALETTE["primary"], PALETTE["on_primary"], PALETTE["primary_hover"]),
    "danger": (PALETTE["danger_fill"], PALETTE["ink"], PALETTE["danger_hover"]),
    "success": (PALETTE["ok"], PALETTE["ink"], PALETTE["success_hover"]),
    "secondary": (PALETTE["raised"], PALETTE["text"], PALETTE["hover_raised"]),
    "quiet": (PALETTE["bg"], PALETTE["text"], PALETTE["hover_quiet"]),
}
_LABEL_OPTIONS = frozenset({"text", "font", "wraplength", "justify", "anchor", "width", "padx", "pady", "textvariable", "image"})


def _rules(fill: str, kind: str) -> tuple[str, str]:
    """The thin lines along the top and bottom edge of a flat button (a lighter one above, a darker one below)."""
    if kind == "quiet":
        return fill, fill
    if DARK:
        return shade(fill, 1.45), shade(fill, 0.5)
    return fill, PALETTE["line"]


class Btn(tk.Frame):
    """A flat button like the text screen's: a bold label on a filled bar with a thin rule above and below.

    It is built from widgets of our own because the system's buttons cannot be coloured the same way on every system. It
    answers ``configure(text=...)`` and ``cget("text")`` like a label, so callers treat it as one."""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None] | None = None, *, kind: str = "secondary",
                 fonts: Fonts | None = None, width: int | None = None, padx: int = 14, pady: int = 7) -> None:
        fill, fg, hover = BUTTON_COLORS[kind]
        self.kind = kind
        self._colors = (fill, fg, hover)
        self._command = command
        self._enabled = True
        super().__init__(master, bg=fill, cursor="hand2")
        hi, lo = _rules(fill, kind)
        self._top = tk.Frame(self, height=1, bg=hi)
        self._label = tk.Label(self, text=text, bg=fill, fg=fg, padx=padx, pady=max(pady - 2, 2), cursor="hand2", borderwidth=0,
                               width=width or 0)
        if fonts is not None:
            self._label.configure(font=fonts.bold)
        self._bottom = tk.Frame(self, height=1, bg=lo)
        self._top.pack(fill="x")
        self._label.pack(fill="both", expand=True)
        self._bottom.pack(fill="x")
        self._rules = (hi, lo)
        for widget in (self, self._top, self._label, self._bottom):
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
            widget.bind("<ButtonRelease-1>", self._clicked)

    # ---- behaves like a label for the text -----------------------------------------------------
    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        if cnf:
            kw = {**cnf, **kw}
        label_kw = {k: kw.pop(k) for k in list(kw) if k in _LABEL_OPTIONS}
        if label_kw and hasattr(self, "_label"):
            self._label.configure(**label_kw)
        if kw:
            return super().configure(**kw)
        return None

    config = configure  # type: ignore[assignment]

    def cget(self, key: str) -> Any:
        return self._label.cget(key) if key in _LABEL_OPTIONS else super().cget(key)

    # ---- the pointer ------------------------------------------------------------------------------
    def _paint(self, fill: str) -> None:
        super().configure(bg=fill)
        self._label.configure(bg=fill)

    def _enter(self, _event: tk.Event) -> None:
        if self._enabled:
            self._paint(self._colors[2])

    def _leave(self, _event: tk.Event) -> None:
        self._paint(self._colors[0] if self._enabled else PALETTE["trough"])

    def _clicked(self, event: tk.Event) -> None:
        if not self._enabled or self._command is None:
            return
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        if 0 <= x <= self.winfo_width() and 0 <= y <= self.winfo_height():
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        fill, fg, _hover = self._colors
        if enabled:
            self._paint(fill)
            self._label.configure(fg=fg, cursor="hand2")
            self._top.configure(bg=self._rules[0])
            self._bottom.configure(bg=self._rules[1])
            super().configure(cursor="hand2")
        else:
            self._paint(PALETTE["trough"])
            self._label.configure(fg=PALETTE["muted"], cursor="arrow")
            self._top.configure(bg=PALETTE["trough"])
            self._bottom.configure(bg=PALETTE["trough"])
            super().configure(cursor="arrow")

    def set_command(self, command: Callable[[], None] | None) -> None:
        self._command = command


class FlowFrame(tk.Frame):
    """Widgets laid out left to right that wrap onto the next line when the window is too narrow, so none is ever cut off.

    Create the children with the flow frame as their parent and hand each one to :meth:`add`."""

    def __init__(self, master: tk.Misc, bg: str, hgap: int = 8, vgap: int = 6) -> None:
        super().__init__(master, bg=bg, height=1)
        self._children: list[tk.Widget] = []
        self._gaps = (hgap, vgap)
        self._pending = False
        self.bind("<Configure>", lambda _e: self._soon())

    def add(self, widget: tk.Widget, *, gap_after: int | None = None) -> tk.Widget:
        self._children.append(widget)
        if gap_after is not None:
            widget._flow_gap = gap_after  # type: ignore[attr-defined]
        widget.bind("<Configure>", lambda _e: self._soon(), add="+")
        self._soon()
        return widget

    def field(self, text: str, make: Callable[[tk.Frame], tk.Widget], font: tkfont.Font, *, gap_after: int = 16) -> Any:
        """A small label with the widget it names, kept together on one line; returns the widget ``make`` built."""
        box = tk.Frame(self, bg=str(self.cget("bg")))
        tk.Label(box, text=text, font=font, bg=str(self.cget("bg")), fg=PALETTE["muted"]).pack(side="left", padx=(0, 6))
        widget = make(box)
        widget.pack(side="left")
        self.add(box, gap_after=gap_after)
        return widget

    def remove(self, widget: tk.Widget) -> None:
        if widget in self._children:
            self._children.remove(widget)
            widget.place_forget()
            self._soon()

    def _soon(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._layout)

    def _layout(self) -> None:
        self._pending = False
        width = self.winfo_width()
        if width <= 1:
            return
        hgap, vgap = self._gaps
        rows: list[list[tuple[tk.Widget, int, int, int]]] = [[]]  # (widget, x, width, height)
        x = 0
        for widget in self._children:
            w, h = widget.winfo_reqwidth(), widget.winfo_reqheight()
            if x > 0 and x + w > width:
                rows.append([])
                x = 0
            rows[-1].append((widget, x, w, h))
            x += w + getattr(widget, "_flow_gap", hgap)
        y = 0
        for line in rows:
            tallest = max((h for _w, _x, _wd, h in line), default=0)
            for widget, left, _wd, h in line:
                widget.place(x=left, y=y + (tallest - h) // 2)  # centred on the line, so labels sit level with their boxes
            y += tallest + vgap
        total = max(y - vgap, 1)
        if int(str(self.cget("height"))) != total:
            self.configure(height=total)


class Tooltip:
    """A small label that appears when the pointer rests on a widget, or on a canvas item (see ``show_at``)."""

    def __init__(self, master: tk.Misc, fonts: Fonts) -> None:
        self._master = master
        self._fonts = fonts
        self._win: tk.Toplevel | None = None
        self._after: str | None = None

    def schedule(self, text: str, x_root: int, y_root: int, delay: int = 450) -> None:
        self.hide()
        if text:
            self._after = self._master.after(delay, lambda: self._show(text, x_root, y_root))

    def _show(self, text: str, x: int, y: int) -> None:
        self._after = None
        win = tk.Toplevel(self._master)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x + 14}+{y + 18}")
        tk.Label(win, text=text, justify="left", bg=PALETTE["tip_bg"], fg=PALETTE["tip_fg"], highlightthickness=1, highlightbackground=PALETTE["line"], padx=8, pady=5, font=self._fonts.small,
                 wraplength=560).pack()
        self._win = win

    def hide(self) -> None:
        if self._after is not None:
            self._master.after_cancel(self._after)
            self._after = None
        if self._win is not None:
            self._win.destroy()
            self._win = None


def attach_tooltip(widget: tk.Misc, tip: Tooltip, text: str | Callable[[], str]) -> None:
    def enter(e: tk.Event) -> None:
        tip.schedule(text() if callable(text) else text, e.x_root, e.y_root)

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", lambda _e: tip.hide(), add="+")
    widget.bind("<ButtonPress>", lambda _e: tip.hide(), add="+")


class ChipBar(FlowFrame):
    """A row of filter chips, each with a count. Clicking one selects it. Wraps rather than running off the edge."""

    def __init__(self, master: tk.Misc, fonts: Fonts, on_select: Callable[[str], None]) -> None:
        super().__init__(master, PALETTE["bg"], hgap=8, vgap=6)
        self._fonts = fonts
        self._on_select = on_select
        self._chips: dict[str, tk.Label] = {}
        self._labels: dict[str, tuple[str, str]] = {}
        self.current: str | None = None

    def set_chips(self, chips: Sequence[tuple[str, str, int, str]], selected: str | None) -> None:
        """``chips`` is ``(key, title, count, color)``."""
        for child in self.winfo_children():
            child.destroy()
        self._children.clear()
        self._chips.clear()
        self._labels.clear()
        self.current = selected
        for key, title, count, color in chips:
            chip = tk.Label(self, text=f"{title}  {count:,}", font=self._fonts.bold, padx=10, pady=4, cursor="hand2",
                            borderwidth=0, highlightthickness=1)
            self.add(chip)
            bind_to(chip, "<Button-1>", partial(self.select, key))
            self._chips[key] = chip
            self._labels[key] = (color, title)
        self._paint()

    def select(self, key: str) -> None:
        if key != self.current:
            self.current = key
            self._paint()
            self._on_select(key)

    def _paint(self) -> None:
        for key, chip in self._chips.items():
            color, _title = self._labels[key]
            if key == self.current:
                chip.configure(bg=color, fg=PALETTE["ink"], highlightbackground=color)
            else:
                chip.configure(bg=PALETTE["panel"], fg=color if color != PALETTE["muted"] else PALETTE["text"],
                               highlightbackground=PALETTE["line"])


class VirtualList(tk.Frame):
    """A scrolling list of rows of different heights that only draws the rows that are on screen.

    ``draw_row(list, index, y, width)`` draws one row on ``list.canvas`` with its top edge at ``y`` and tags every item it
    creates with ``list.row_tag(index)``. Clicks are reported through ``on_click(tags, event)`` with the tags of the item
    under the pointer.
    """

    def __init__(self, master: tk.Misc, fonts: Fonts, draw_row: Callable[[VirtualList, int, int, int], None],
                 on_click: Callable[[tuple[str, ...], tk.Event], None] | None = None,
                 on_hover: Callable[[tuple[str, ...], tk.Event | None], None] | None = None,
                 on_double: Callable[[tuple[str, ...], tk.Event], None] | None = None,
                 on_context: Callable[[tuple[str, ...], tk.Event], None] | None = None) -> None:
        super().__init__(master, bg=PALETTE["panel"])
        self.fonts = fonts
        self._draw_row = draw_row
        self._on_click = on_click
        self._on_hover = on_hover
        self._on_double = on_double
        self._on_context = on_context
        self.canvas = tk.Canvas(self, bg=PALETTE["panel"], highlightthickness=0, borderwidth=0, takefocus=1)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._heights: list[int] = []
        self._offsets: list[int] = [0]
        self._drawn: set[int] = set()
        self._window: tuple[int, int] = (0, 0)  # first and last visible row, readable from other threads (no Tk calls)
        self._empty: str = ""
        self._width = 1
        self.relayout: Callable[[int], Sequence[int]] | None = None  # new row heights when the width changes
        self.on_scroll: Callable[[], None] | None = None  # called after the list scrolled or was resized
        self.canvas.bind("<Configure>", self._configure)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", self._double)
        self.canvas.bind("<Button-3>", self._context)
        if sys.platform == "darwin":  # a Mac's right button is button 2, or Control + click
            self.canvas.bind("<Button-2>", self._context)
            self.canvas.bind("<Control-Button-1>", self._context)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._hover(None))
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        self.canvas.bind("<Prior>", lambda e: self.canvas.yview_scroll(-1, "pages"))
        self.canvas.bind("<Next>", lambda e: self.canvas.yview_scroll(1, "pages"))
        self.canvas.bind("<Up>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Down>", lambda e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<Home>", lambda e: self.canvas.yview_moveto(0))
        self.canvas.bind("<End>", lambda e: self.canvas.yview_moveto(1))
        self.canvas.configure(yscrollincrement=40)

    # ---- model ----------------------------------------------------------
    @staticmethod
    def row_tag(index: int) -> str:
        return f"row{index}"

    @property
    def width(self) -> int:
        return self._width

    @property
    def count(self) -> int:
        return len(self._heights)

    def set_rows(self, heights: Sequence[int], empty_text: str = "", *, keep_position: bool = False) -> None:
        top = self.canvas.yview()[0] if keep_position else 0.0
        self._heights = list(heights)
        self._offsets = [0]
        for h in self._heights:
            self._offsets.append(self._offsets[-1] + h)
        self._empty = empty_text
        self._drawn.clear()
        self.canvas.delete("all")
        total = self._offsets[-1]
        self.canvas.configure(scrollregion=(0, 0, max(self._width, 1), max(total, 1)))
        self.canvas.yview_moveto(top)
        self.refresh()

    def offset(self, index: int) -> int:
        return self._offsets[index]

    def height_of(self, index: int) -> int:
        return self._heights[index]

    # ---- drawing --------------------------------------------------------
    def visible_range(self) -> tuple[int, int]:
        if not self._heights:
            return 0, 0
        top = self.canvas.canvasy(0)
        bottom = self.canvas.canvasy(self.canvas.winfo_height())
        first = max(bisect.bisect_right(self._offsets, top) - 1, 0)
        last = min(bisect.bisect_left(self._offsets, bottom), len(self._heights))
        if last <= first:
            last = min(first + 1, len(self._heights))
        return first, last

    def is_visible(self, index: int) -> bool:
        first, last = self.visible_range()
        return first <= index < last

    def in_window(self, index: int) -> bool:
        """Is this row (about) on screen? Safe to call from any thread."""
        first, last = self._window
        return first - 3 <= index < last + 3

    def refresh(self) -> None:
        """Draw the rows that came into view and forget the ones that left."""
        self.canvas.delete("empty")
        if not self._heights:
            if self._empty:
                self.canvas.create_text(max(self._width // 2, 40), 90, text=self._empty, fill=PALETTE["muted"], font=self.fonts.big,
                                        tags=("empty",), width=max(self._width - 80, 200), justify="center")
            return
        first, last = self.visible_range()
        self._window = (first, last)
        margin = 2
        want = set(range(max(first - margin, 0), min(last + margin, len(self._heights))))
        for index in sorted(self._drawn - want):
            self.canvas.delete(self.row_tag(index))
        self._drawn &= want
        for index in sorted(want - self._drawn):
            self._draw_row(self, index, self._offsets[index], self._width)
            self._drawn.add(index)

    def redraw_row(self, index: int) -> None:
        if index in self._drawn:
            self.canvas.delete(self.row_tag(index))
            self._draw_row(self, index, self._offsets[index], self._width)

    def redraw_all(self) -> None:
        for index in list(self._drawn):
            self.canvas.delete(self.row_tag(index))
        self._drawn.clear()
        self.refresh()

    def scroll_to(self, index: int) -> None:
        total = max(self._offsets[-1], 1)
        self.canvas.yview_moveto(self._offsets[min(index, len(self._heights) - 1)] / total)

    # ---- events ---------------------------------------------------------
    def _on_yscroll(self, first: float, last: float) -> None:
        self.vbar.set(first, last)
        self.refresh()
        if self.on_scroll is not None:
            self.on_scroll()

    def _configure(self, event: tk.Event) -> None:
        if event.width != self._width:
            self._width = event.width
            if self.relayout is not None and self._heights:
                self.set_rows(self.relayout(self._width), self._empty, keep_position=True)
                return
            self.canvas.configure(scrollregion=(0, 0, self._width, max(self._offsets[-1], 1)))
            self.redraw_all()
        else:
            self.refresh()

    def _tags_at(self, event: tk.Event) -> tuple[str, ...]:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(x, y, x, y)
        for item in reversed(items):  # topmost first
            tags = self.canvas.gettags(item)
            if any(t.startswith(("tile:", "act:", "chk:", "hdr:")) for t in tags):
                return tags
        return tuple(self.canvas.gettags(items[-1])) if items else ()

    def _click(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self._on_click:
            self._on_click(self._tags_at(event), event)

    def _double(self, event: tk.Event) -> None:
        if self._on_double:
            self._on_double(self._tags_at(event), event)

    def _context(self, event: tk.Event) -> None:
        if self._on_context:
            self._on_context(self._tags_at(event), event)

    def _motion(self, event: tk.Event) -> None:
        self._hover(event)

    def _hover(self, event: tk.Event | None) -> None:
        if self._on_hover:
            self._on_hover(self._tags_at(event) if event is not None else (), event)

    def _wheel(self, event: tk.Event) -> str:
        if event.num == 4:
            steps = -3
        elif event.num == 5:
            steps = 3
        elif sys.platform == "darwin":
            steps = -int(event.delta)
        else:
            steps = -int(event.delta / 120) * 3
        if steps:
            self.canvas.yview_scroll(steps, "units")
        return "break"

    def destroy(self) -> None:
        self._drawn.clear()
        super().destroy()


class ScrollFrame(tk.Frame):
    """A column of widgets that scrolls when it is taller than the window. Put the widgets in ``inner``."""

    def __init__(self, master: tk.Misc, bg: str) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.bind("<Enter>", lambda _e: self._grab_wheel(True))
        self.bind("<Leave>", lambda _e: self._grab_wheel(False))

    def _grab_wheel(self, inside: bool) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            if inside:
                self.bind_all(seq, self._wheel)
            else:
                self.unbind_all(seq)

    def _wheel(self, event: tk.Event) -> None:
        if self.bbox_height() <= self.canvas.winfo_height():
            return  # it all fits: nothing to scroll
        if event.num == 4:
            steps = -3
        elif event.num == 5:
            steps = 3
        elif sys.platform == "darwin":
            steps = -int(event.delta)
        else:
            steps = -int(event.delta / 120) * 3
        self.canvas.yview_scroll(steps, "units")

    def bbox_height(self) -> int:
        box = self.canvas.bbox("all")
        return (box[3] - box[1]) if box else 0


def autowrap(label: tk.Label, margin: int = 36, minimum: int = 200) -> None:
    """Make a label's text wrap at the width it is given, so nothing is cut off when the window is resized."""
    # A label asks for its wrapped width plus about 6px of border and padding. If the text were wrapped at the full width it is
    # given, that request would be bigger than the box and the window would keep growing, so always leave room for the border.
    room = max(margin, 10)

    def fit(event: tk.Event) -> None:  # type: ignore[type-arg]
        width = max(event.width - room, minimum)
        if int(float(str(label.cget("wraplength")))) != width:
            label.configure(wraplength=width)

    label.bind("<Configure>", fit, add="+")


def bind_to(widget: tk.Misc, sequence: str, callback: Callable[[], object]) -> None:
    """Run ``callback`` (it takes no arguments) whenever ``sequence`` happens on ``widget``."""
    widget.bind(sequence, lambda _event: callback())


def tag_value(tags: Sequence[str], prefix: str) -> str | None:
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def center_on(win: tk.Toplevel, parent: tk.Misc, width: int, height: int) -> None:
    parent.update_idletasks()
    x = parent.winfo_rootx() + max((parent.winfo_width() - width) // 2, 0)
    y = parent.winfo_rooty() + max((parent.winfo_height() - height) // 3, 0)
    win.geometry(f"{width}x{height}+{x}+{y}")


def font_height(font: Any) -> int:
    return int(font.metrics("linespace"))
