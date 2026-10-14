"""Find duplicates: inside one folder, or between A and B, every copy shown as a picture, you choose which stays."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions, ScanResult, Verify
from duplio.errors import DuplioError
from duplio.gui import actions
from duplio.gui.app import Page
from duplio.gui.folders import FolderPair
from duplio.gui.model import (
    GroupRow,
    Tile,
    apply_rules,
    build_group_rows,
    chosen_group_ids,
    keep_overrides,
)
from duplio.gui.paint import THUMB_SIZES, Painter
from duplio.gui.sorting import GROUP_FIELDS, choose, label_of, sort_group_rows
from duplio.gui.style import PALETTE, shorten
from duplio.gui.widgets import Btn, ChipBar, FlowFrame, VirtualList, autowrap, tag_value
from duplio.plan.executor import ApplyOptions, apply_plan
from duplio.plan.model import ActionType
from duplio.plan.planner import build_plan
from duplio.plan.undo import undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import human_size, plural

if TYPE_CHECKING:
    from duplio.gui.app import Context

SCOPES = (
    ("reference", "Clean up A: remove files from A that B already has. B is never touched."),
    ("cross", "Show files that exist in both A and B, and let me choose which copy stays."),
    ("all", "Show every set of duplicates: inside A, inside B, and between them."),
)
RULE_PRESETS = {
    "The usual choice (see Settings)": None,
    "Keep the copy in folder A": ["prefer-root=1", "not-copy-named", "oldest", "shortest-path"],
    "Keep the copy in folder B": ["prefer-root=2", "not-copy-named", "oldest", "shortest-path"],
    "Keep the oldest copy": ["oldest", "not-copy-named", "shortest-path"],
    "Keep the newest copy": ["newest", "not-copy-named", "shortest-path"],
    "Keep the one with the shortest path": ["shortest-path", "not-copy-named", "oldest"],
}


class DuplicatesPage(Page):
    title = "Find duplicates"
    subtitle = ("Put one folder in A to look for copies inside it, or add a second folder in B to look between the two. Every copy of a "
                "file is shown as a picture; the ones marked REMOVE go to a quarantine you can undo, and at least one copy always stays.")

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        self.result: ScanResult | None = None
        self.rows: list[GroupRow] = []
        self.shown: list[GroupRow] = []
        self.by_tile: dict[str, tuple[GroupRow, Tile]] = {}
        self.scope = tk.StringVar(value="reference")
        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 18))
        self.body = body
        self.folders = FolderPair(body, ctx, "Folder A", "where to look for duplicates", "Folder B (optional)",
                                  "a second folder to compare with; leave empty to look inside A only")
        self.options = tk.Frame(body, bg=PALETTE["bg"])
        for value, text in SCOPES:
            ttk.Radiobutton(self.options, text=text, value=value, variable=self.scope).pack(anchor="w", pady=1)
        self.run_row = tk.Frame(body, bg=PALETTE["bg"])
        Btn(self.run_row, "Find duplicates", self.run, kind="primary", fonts=f, padx=22, pady=9).pack(side="left")
        self.summary = FlowFrame(body, PALETTE["bg"], hgap=10, vgap=6)
        self.summary_text = tk.Label(self.summary, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w")
        self.summary.add(self.summary_text, gap_after=24)
        self.summary.add(Btn(self.summary, "Search again", self.run, kind="primary", fonts=f, padx=12, pady=4))
        self.summary.add(Btn(self.summary, "Change folders", self.edit_folders, fonts=f, padx=10, pady=4))
        self.banner = tk.Frame(body, bg=PALETTE["bg"])
        self.headline = tk.Label(self.banner, text="", font=f.heading, anchor="w", justify="left")
        self.headline.pack(fill="x", padx=16, pady=(10, 0))
        self.detail = tk.Label(self.banner, text="", font=f.base, anchor="w", justify="left", wraplength=900)
        self.detail.pack(fill="x", padx=16, pady=(2, 10))
        autowrap(self.detail)
        self.chip_bar = ChipBar(body, f, self.filter)
        self.results = tk.Frame(body, bg=PALETTE["bg"])
        self.tools = FlowFrame(self.results, PALETTE["bg"], hgap=8, vgap=6)
        self.tools.pack(side="top", fill="x", pady=(0, 6))

        self.rule_box = self.tools.field("Which copy stays", lambda box: ttk.Combobox(box, state="readonly", values=list(RULE_PRESETS), width=32,
                                                                                    font=f.small), f.small, gap_after=4)
        self.rule_box.current(0)
        self.tools.add(Btn(self.tools, "Apply", self.choose_again, fonts=f, padx=10, pady=4), gap_after=18)
        self.size_box = self.tools.field("Picture size", lambda box: ttk.Combobox(box, state="readonly", values=list(THUMB_SIZES), width=8,
                                                                                 font=f.small), f.small)
        self.size_box.set(ctx.state.get("thumb_size", "Medium"))
        self.size_box.bind("<<ComboboxSelected>>", lambda _e: self._resize())
        self.sort_box = self.tools.field("Sort by", lambda box: ttk.Combobox(box, state="readonly", values=[v for _k, v in GROUP_FIELDS], width=17,
                                                                            font=f.small), f.small, gap_after=4)
        self.sort_box.set(label_of(GROUP_FIELDS, ctx.state.get("dupes_sort", "default")))
        self.sort_box.bind("<<ComboboxSelected>>", lambda _e: self._resort())
        self.descending = bool(ctx.state.get("dupes_desc", False))
        self.direction = Btn(self.tools, "", self._flip, fonts=f, padx=8, pady=3)
        self.tools.add(self.direction)
        self._paint_direction()
        self.actions_bar = tk.Frame(self.results, bg=PALETTE["bg"])
        self.actions_bar.pack(side="bottom", fill="x", pady=(10, 0))
        self.remove_button = Btn(self.actions_bar, "", self.remove, kind="danger", fonts=f, padx=18, pady=9)
        self.remove_button.pack(anchor="w")
        self.note = tk.Label(self.actions_bar, text="Removed copies go to a quarantine next to where they were; History & undo brings them back. "
                             "At least one copy of every file always stays.", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"],
                             anchor="w", justify="left")
        self.note.pack(fill="x", pady=(6, 0))
        autowrap(self.note, margin=4, minimum=200)
        holder = tk.Frame(self.results, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.view = VirtualList(holder, f, self._draw, on_click=self._click, on_context=self._context, on_hover=self._hover,
                                on_double=self._double)
        self.view.pack(fill="both", expand=True)
        self.painter = Painter(self.view, ctx.thumbs, f, THUMB_SIZES.get(self.size_box.get(), 132), favorites=ctx.favorites)
        self.view.relayout = self._heights
        self.placeholder = tk.Label(body, text="Choose a folder, then press the button. Looking changes nothing.", font=f.base,
                                    bg=PALETTE["bg"], fg=PALETTE["muted"])
        self.has_result = False
        self.editing = False
        self._last_hover = ""
        self.layout()

    # ---- layout ----------------------------------------------------------------------------------
    def layout(self) -> None:
        for widget in (self.folders, self.options, self.run_row, self.summary, self.banner, self.chip_bar, self.results, self.placeholder):
            widget.pack_forget()
        if self.has_result and not self.editing:
            self.summary.pack(fill="x", pady=(6, 0))
        else:
            self.folders.pack(fill="x", pady=(6, 8))
            self.options.pack(fill="x", pady=(0, 8))
            self.run_row.pack(fill="x")
        if self.has_result:
            self.banner.pack(fill="x", pady=(8, 6))
            self.chip_bar.pack(fill="x", pady=(0, 8))
            self.results.pack(fill="both", expand=True)
        else:
            self.placeholder.pack(anchor="w", pady=26)

    def edit_folders(self) -> None:
        self.editing = True
        self.layout()

    # ---- searching -----------------------------------------------------------------------------------
    def run(self) -> None:
        a, b = self.folders.values()
        if not a:
            self.ctx.show_error("Choose folder A first.", "A folder is needed")
            return
        ctx, scope = self.ctx, self.scope.get()
        settings = ctx.settings

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            locations = [(ctx.registry.open(a), RootRole.NORMAL)]
            mode = Mode.ALL
            if b:
                locations.append((ctx.registry.open(b), RootRole.REFERENCE if scope == "reference" else RootRole.NORMAL))
                mode = {"reference": Mode.REFERENCE, "cross": Mode.CROSS, "all": Mode.ALL}[scope]
            roots = make_roots(locations)
            flt = ScanFilter(exclude=list(settings.exclude), protect=list(settings.protect), min_size=settings.min_size,
                             skip_hidden=settings.skip_hidden)
            inventory = scan(roots, flt, progress=progress, cancel=cancel, one_file_system=settings.one_file_system)
            options = ScanOptions(mode=mode, verify=Verify.HASH, one_file_system=settings.one_file_system)
            return compare(inventory, options, cache=ctx.cache, progress=progress, cancel=cancel, previews=ctx.previews)

        for text in (a, b):
            ctx.state.remember(text)
        ctx.state.save()
        self.folders.a.refresh_recent()
        self.folders.b.refresh_recent()
        ctx.run_job("Looking for duplicates", job, self.show_result)

    def show_result(self, result: ScanResult) -> None:
        self.result = result
        a, b = self.folders.values()
        self.summary_text.configure(text=shorten(f"A: {a}" + (f"   B: {b}" if b else ""), 620, self.ctx.fonts.base, middle=True))
        self.has_result, self.editing = True, False
        roots = result.inventory.roots
        self.rows = build_group_rows(result.groups, roots, self._rules())
        self.by_tile = {t.key: (row, t) for row in self.rows for t in row.tiles}
        total = sum(g.wasted for g in result.groups)
        if not self.rows:
            self.headline.configure(text="No duplicates found.", fg=PALETTE["ok"], bg=PALETTE["ok_soft"])
            self.detail.configure(text=f"{plural(len(result.inventory.entries), 'file')} looked at; every one is different from all the others.",
                                  bg=PALETTE["ok_soft"], fg=PALETTE["text"])
            self.banner.configure(bg=PALETTE["ok_soft"])
        else:
            self.headline.configure(text=f"{plural(len(self.rows), 'set')} of identical files found.", fg=PALETTE["warn"], bg=PALETTE["warn_soft"])
            self.detail.configure(text=f"{human_size(total)} is taken up by extra copies. The ones marked REMOVE go to the quarantine; "
                                       "at least one copy of every file always stays. Click a copy to change its mark.",
                                  bg=PALETTE["warn_soft"], fg=PALETTE["text"])
            self.banner.configure(bg=PALETTE["warn_soft"])
        across = sum(1 for r in self.rows if len(r.group.roots()) > 1)
        in_a = sum(1 for r in self.rows if r.group.roots() == {0})
        in_b = sum(1 for r in self.rows if r.group.roots() == {1})
        chips = [("all", "All sets", len(self.rows), PALETTE["warn"])]
        if across:
            chips.append(("across", "Between A and B", across, PALETTE["a"]))
        if in_a:
            chips.append(("in-a", "Inside A", in_a, PALETTE["a"]))
        if in_b:
            chips.append(("in-b", "Inside B", in_b, PALETTE["b"]))
        self.layout()
        self.chip_bar.set_chips(chips, "all")
        self.filter("all")

    def _paint_direction(self) -> None:
        self.direction.configure(text="▼ Descending" if self.descending else "▲ Ascending")

    def _flip(self) -> None:
        self.descending = not self.descending
        self._paint_direction()
        self._resort()

    def _resort(self) -> None:
        self.ctx.state.set("dupes_sort", choose(GROUP_FIELDS, self.sort_box.get()))
        self.ctx.state.set("dupes_desc", self.descending)
        self.ctx.state.save()
        self.filter(self.chip_bar.current)

    def filter(self, key: str | None) -> None:
        def wanted(row: GroupRow) -> bool:
            roots = row.group.roots()
            return {"all": True, "across": len(roots) > 1, "in-a": roots == {0}, "in-b": roots == {1}}.get(key or "all", True)

        self.shown = sort_group_rows([r for r in self.rows if wanted(r)], choose(GROUP_FIELDS, self.sort_box.get()), self.descending)
        for n, row in enumerate(self.shown):
            row.index = n
        self.view.set_rows(self._heights(self.view.width), "No duplicates here.")
        self._update_totals()

    def _heights(self, width: int) -> list[int]:
        return [self.painter.group_height(r, width) for r in self.shown]

    def _resize(self) -> None:
        label = self.size_box.get()
        self.ctx.state.set("thumb_size", label)
        self.painter.box = THUMB_SIZES.get(label, 132)
        self.view.set_rows(self._heights(self.view.width), keep_position=True)

    def _draw(self, view: VirtualList, index: int, y: int, width: int) -> None:
        self.painter.group_row(self.shown[index], y, width, lambda side: "Folder " + "AB"[side])

    # ---- choosing -------------------------------------------------------------------------------------
    def _rules(self) -> list[str]:
        choice = RULE_PRESETS.get(self.rule_box.get())
        return list(choice) if choice else list(self.ctx.settings.keep)

    def choose_again(self) -> None:
        if self.result is None:
            return
        apply_rules(self.rows, self.result.inventory.roots, self._rules())
        self.view.redraw_all()
        self._update_totals()

    def _update_totals(self) -> None:
        marked = [t for r in self.rows for t in r.removals]
        size = sum(t.size for t in marked)
        if marked:
            self.remove_button.configure(text=f"Remove {len(marked):,} copies ({human_size(size)})")
        else:
            self.remove_button.configure(text="Nothing marked")
        self.remove_button.set_enabled(bool(marked))

    def _click(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        starred = tag_value(tags, "star:")
        if starred and starred in self.by_tile:
            actions.toggle_favorite(self.ctx, self.by_tile[starred][1])
            self.view.redraw_all()
            return
        key = tag_value(tags, "tile:")
        if key and key in self.by_tile:
            row, tile = self.by_tile[key]
            if row.toggle(tile):
                self.view.redraw_row(row.index)
                self._update_totals()

    def _double(self, tags: tuple[str, ...], _event: tk.Event) -> None:
        key = tag_value(tags, "tile:")
        if key and key in self.by_tile:
            actions.open_tile(self.ctx, self.by_tile[key][1])

    def _context(self, tags: tuple[str, ...], event: tk.Event) -> None:
        key = tag_value(tags, "tile:")
        if not key or key not in self.by_tile:
            return
        row, tile = self.by_tile[key]

        def keep_only() -> None:
            row.keep_only(tile)
            self.view.redraw_row(row.index)
            self._update_totals()

        def skip() -> None:
            row.skipped = not row.skipped
            self.view.redraw_row(row.index)
            self._update_totals()

        actions.context_menu(self.ctx, tile, event.x_root, event.y_root, [
            ("Keep only this copy", keep_only),
            ("Leave this whole set alone" if not row.skipped else "Include this set again", skip),
        ], None, self.view.redraw_all)

    def _hover(self, tags: tuple[str, ...], event: tk.Event | None) -> None:
        key = tag_value(tags, "tile:") if event is not None else None
        text = ""
        if key and key in self.by_tile:
            row, tile = self.by_tile[key]
            text = f"{tile.location}\n{tile.details}"
            if tile.entry.id in row.locked:
                text += f"\nCannot be removed: {row.locked[tile.entry.id]}"
        if text != self._last_hover:
            self._last_hover = text
            if text and event is not None:
                self.ctx.tooltip.schedule(text, event.x_root, event.y_root)
            else:
                self.ctx.tooltip.hide()

    # ---- removing ------------------------------------------------------------------------------------------
    def remove(self) -> None:
        if self.result is None:
            return
        ctx = self.ctx
        try:
            plan = build_plan(self.result, action=ActionType.QUARANTINE, rules=self._rules(), group_ids=chosen_group_ids(self.rows),
                              keep_overrides=keep_overrides(self.rows))
        except DuplioError as exc:
            ctx.show_error(exc)
            return
        if not plan.items:
            ctx.info("Nothing to remove", "Every copy is marked to stay.")
            return
        rows = [(item.victim.rel, f"moved to the quarantine; kept: {item.keeper.rel}") for item in plan.items]
        size = sum(item.victim.size for item in plan.items)
        if not ctx.confirm("Remove the marked copies", f"Move {plural(len(plan.items), 'file')} ({human_size(size)}) to the quarantine?",
                           ["Nothing is deleted: the files wait in a quarantine folder next to where they were, and 'History & undo' puts "
                            "them back.", "Just before each one moves, Duplio reads both it and the copy that stays again and checks they "
                            "are still identical."], rows, confirm="Move to quarantine", columns=("Copy being removed", "What happens")):
            return

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return apply_plan(plan, ctx.registry, options=ApplyOptions(verify="hash"), dirs=ctx.dirs, progress=progress, cancel=cancel)

        def done(report: Any) -> None:
            moved, skipped, failed = report.count("done"), report.count("skipped"), report.count("failed")
            lines = [f"{moved:,} copies moved to the quarantine."]
            if skipped:
                lines.append(f"{skipped:,} were left alone because something had changed since the search.")
            if failed:
                lines.append(f"{failed:,} could not be moved.")
            lines.append("Nothing is lost: undo it now, or use 'History & undo' later.")
            buttons = [("Undo this", lambda: self._undo(report.apply_id), "secondary"), ("Search again", self.run, "primary")]
            ctx.info("Done", f"{moved:,} copies removed." if moved else "Nothing was removed.", lines, "ok" if not failed else "warn", buttons)

        ctx.run_job("Moving the copies to the quarantine", job, done)

    def _undo(self, apply_id: str) -> None:
        ctx = self.ctx

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return undo(apply_id, ctx.registry, dirs=ctx.dirs, progress=progress, cancel=cancel)

        def done(report: Any) -> None:
            ctx.info("Put back", f"{report.count('restored'):,} files restored.", [], "ok", [("Search again", self.run, "primary")])

        ctx.run_job("Putting the files back", job, done)
