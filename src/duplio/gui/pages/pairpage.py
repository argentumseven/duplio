"""Shared by "Compare two folders" and "Is it backed up?": pick A and B, compare by content, show the answer side by side."""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Any

from duplio.backup.merge import MergeOptions, MergePlan, plan_merge, run_merge
from duplio.compare.diff import DiffOptions, DiffResult, diff_inventory, swap_sides
from duplio.errors import DuplioError
from duplio.gui.app import Page
from duplio.gui.folders import FolderPair
from duplio.gui.model import PairRow, RowKind, build_pair_rows, count_rows
from duplio.gui.pages.pairview import PairView
from duplio.gui.style import PALETTE, shorten
from duplio.gui.widgets import Btn, ChipBar, autowrap
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import human_size, plural

if TYPE_CHECKING:
    from duplio.gui.app import Context

CHIP_COLORS = {
    RowKind.ONLY_A: PALETTE["a"],
    RowKind.ONLY_B: PALETTE["b"],
    RowKind.CHANGED: PALETTE["warn"],
    RowKind.MOVED: PALETTE["a"],
    RowKind.UNREADABLE: PALETTE["muted"],
    RowKind.SAME: PALETTE["ok"],
}


class PairPage(Page):
    """Template: subclasses say what the two folders are called and how the answer is worded."""

    a_title = "Folder A"
    a_hint = "the first folder"
    b_title = "Folder B"
    b_hint = "the second folder"
    run_text = "Compare"
    again_text = "Compare again"
    allow_reverse = True
    selectable = True
    job_title = "Comparing the two folders"
    # (chip key, title, the kinds it shows, colour); {A} and {B} are replaced by the folder names
    chips: tuple[tuple[str, str, tuple[RowKind, ...], str], ...] = ()
    names = ("A", "B")
    summary_names = ("A", "B")  # how the folders are named in the line shown above the answer
    slot_labels: tuple[str, str] | None = None  # what an empty half of a row says; None: "Folder A" / "Folder B"

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        self.diff: DiffResult | None = None
        self.all_rows: list[PairRow] = []
        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 18))
        self.body = body
        self.folders = FolderPair(body, ctx, self.a_title, self.a_hint, self.b_title, self.b_hint)
        self.run_row = tk.Frame(body, bg=PALETTE["bg"])
        self.run_button = Btn(self.run_row, self.run_text, self.run, kind="primary", fonts=f, padx=22, pady=9)
        self.run_button.pack(side="left")
        self.problems = tk.Label(body, text="", font=f.small, bg=PALETTE["bg"], fg=PALETTE["warn"], cursor="hand2")
        self.problems.bind("<Button-1>", lambda _e: self.show_problems())
        self.summary = tk.Frame(body, bg=PALETTE["bg"])
        self.summary_a = tk.Label(self.summary, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w")
        self.summary_b = tk.Label(self.summary, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["text"], anchor="w")
        self.summary_a.pack(side="left")
        tk.Label(self.summary, text="  ⇄  ", font=f.big, bg=PALETTE["bg"], fg=PALETTE["muted"]).pack(side="left")
        self.summary_b.pack(side="left")
        Btn(self.summary, "Change folders", self.edit_folders, fonts=f, padx=10, pady=4).pack(side="right")
        Btn(self.summary, self.again_text, self.run, kind="primary", fonts=f, padx=12, pady=4).pack(side="right", padx=(0, 8))
        self.has_result = False
        self.banner = tk.Frame(body, bg=PALETTE["bg"])
        self.headline = tk.Label(self.banner, text="", font=f.heading, anchor="w", justify="left", wraplength=900)
        self.headline.pack(fill="x", padx=16, pady=(10, 0))
        self.detail = tk.Label(self.banner, text="", font=f.base, anchor="w", justify="left", wraplength=900)
        self.detail.pack(fill="x", padx=16, pady=(2, 10))
        autowrap(self.headline)
        autowrap(self.detail)
        self.chip_bar = ChipBar(body, f, self.filter)
        self.results = tk.Frame(body, bg=PALETTE["bg"])
        self.pairs = PairView(self.results, ctx, self.names, on_selection=self._selection_changed, selectable=self.selectable,
                              slot_labels=self.slot_labels)
        self.actions = tk.Frame(self.results, bg=PALETTE["bg"])
        self.actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.build_actions(self.actions)
        self.pairs.pack(fill="both", expand=True)
        self.placeholder = tk.Label(body, text=self.placeholder_text(), font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"], justify="left")
        self.layout()

    # ---- what is on screen, in order -------------------------------------------------------------
    def layout(self) -> None:
        for widget in (self.folders, self.run_row, self.summary, self.problems, self.banner, self.chip_bar, self.results, self.placeholder):
            widget.pack_forget()
        if self.has_result and not self.editing:
            self.summary.pack(fill="x", pady=(6, 0))
        else:
            self.folders.pack(fill="x", pady=(6, 10))
            self.run_row.pack(fill="x")
        if self.has_result:
            self.problems.pack(anchor="w")
            self.banner.pack(fill="x", pady=(8, 6))
            self.chip_bar.pack(fill="x", pady=(0, 8))
            self.results.pack(fill="both", expand=True)
        else:
            self.placeholder.pack(anchor="w", pady=30)

    editing = False

    def edit_folders(self) -> None:
        self.editing = True
        self.layout()

    def build_actions(self, bar: tk.Frame) -> None:
        f = self.ctx.fonts
        self.copy_ab = Btn(bar, "", lambda: self.copy(0), kind="success", fonts=f, padx=18, pady=9)
        self.copy_ab.pack(side="left")
        self.copy_ba = Btn(bar, "", lambda: self.copy(1), kind="primary", fonts=f, padx=18, pady=9)
        if self.allow_reverse:
            self.copy_ba.pack(side="left", padx=(10, 0))
        self.copy_note = tk.Label(bar, text="", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"])
        self.copy_note.pack(side="left", padx=14)

    # ---- wording a subclass may change ---------------------------------------------------------------
    def placeholder_text(self) -> str:
        return "Choose the two folders above, then press the button. Nothing is changed by looking."

    def banner_text(self, diff: DiffResult) -> tuple[str, str, str]:  # headline, detail, kind (ok|warn|bad)
        raise NotImplementedError

    # ---- running ---------------------------------------------------------------------------------
    def make_job(self, a: str, b: str):  # type: ignore[no-untyped-def]
        ctx = self.ctx

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            roots = make_roots([(ctx.registry.open(a), RootRole.NORMAL), (ctx.registry.open(b), RootRole.NORMAL)])
            inventory = scan(roots, ScanFilter(min_size=0, exclude=list(ctx.settings.exclude)), progress=progress, cancel=cancel)
            return diff_inventory(inventory, DiffOptions(), cache=ctx.fresh_cache, progress=progress, cancel=cancel)

        return job

    def run(self) -> None:
        a, b = self.folders.values()
        if not a or not b:
            self.ctx.show_error("Choose both folders first.", "Two folders are needed")
            return
        ctx = self.ctx
        for text in (a, b):
            ctx.state.remember(text)
        ctx.state.save()
        self.folders.a.refresh_recent()
        self.folders.b.refresh_recent()
        ctx.run_job(self.job_title, self.make_job(a, b), self.show_result)

    def show_result(self, diff: DiffResult) -> None:
        self.diff = diff
        headline, detail, kind = self.banner_text(diff)
        self.present(build_pair_rows(diff), headline, detail, kind, len(diff.errors) + len(diff.inventory.errors))
        self.problem_list = diff.errors + diff.inventory.errors

    problem_list: list[tuple[str, str]] = []

    def present(self, rows: list[PairRow], headline: str, detail: str, kind: str, problems: int = 0) -> None:
        """Show an answer: the folded folders, the banner, the filter chips and the rows."""
        self.all_rows = rows
        a, b = self.folders.values()
        self.summary_a.configure(text=shorten(f"{self.summary_names[0]}:  {a}", 420, self.ctx.fonts.base, middle=True))
        self.summary_b.configure(text=shorten(f"{self.summary_names[1]}:  {b}", 420, self.ctx.fonts.base, middle=True))
        self.has_result, self.editing = True, False
        color, soft = {"ok": (PALETTE["ok"], PALETTE["ok_soft"]), "warn": (PALETTE["warn"], PALETTE["warn_soft"]),
                       "bad": (PALETTE["bad"], PALETTE["bad_soft"])}[kind]
        self.banner.configure(bg=soft)
        self.headline.configure(text=headline, bg=soft, fg=color)
        self.detail.configure(text=detail, bg=soft, fg=PALETTE["text"])
        self.pairs.reset()  # a new answer: nothing from the last one stays selected
        counts = count_rows(self.all_rows)
        chips = []
        first = None
        for key, title, kinds, chip_color in self.chips:
            n = sum(counts[k] for k in kinds)
            if n:
                chips.append((key, title, n, chip_color))
                first = first or key
        self.layout()
        self.chip_bar.set_chips(chips, first)
        self.filter(first)
        self.problems.configure(text=f"⚠ {plural(problems, 'problem')} while reading (click to see)" if problems else "")
        self._selection_changed()

    def filter(self, key: str | None) -> None:
        kinds = next((k for c, _t, k, _col in self.chips if c == key), tuple(RowKind))
        shown = [r for r in self.all_rows if r.kind in kinds]
        empty = "Nothing here." if self.all_rows else "Both folders are empty."
        self.pairs.show_rows(shown, empty)

    def show_problems(self) -> None:
        lines = [f"{path}: {why}" for path, why in self.problem_list[:12]]
        self.ctx.info("Problems while reading", "Some files or folders could not be read.", lines, kind="warn")

    # ---- copying -------------------------------------------------------------------------------------
    def _selection_changed(self) -> None:
        if self.diff is None or not hasattr(self, "copy_ab"):
            return
        for side, button in ((0, self.copy_ab), (1, self.copy_ba)):
            n_sel = len(self.pairs.selected_tiles(side))
            n_only = sum(1 for r in self.all_rows if r.kind is (RowKind.ONLY_A if side == 0 else RowKind.ONLY_B))
            src, dst = self.names[side], self.names[1 - side]
            if n_sel:
                button.configure(text=f"Copy {n_sel:,} selected: {src} → {dst}")
            else:
                button.configure(text=f"Copy all {n_only:,} files only in {src}: {src} → {dst}")
            button.set_enabled(bool(n_sel or n_only))
        self.copy_note.configure(text="Nothing is overwritten · every copy is checked · undoable")

    def copy(self, direction: int) -> None:
        if self.diff is None:
            return
        diff = self.diff if direction == 0 else swap_sides(self.diff)
        src, dst = self.names[direction], self.names[1 - direction]
        wanted = {t.entry.id for t in self.pairs.selected_tiles(direction)}
        try:
            plan = plan_merge(diff, MergeOptions(on_conflict="rename"))
        except DuplioError as exc:
            self.ctx.show_error(exc)
            return
        if wanted:
            plan.actions = [a for a in plan.actions if a.entry.id in wanted]
        if not plan.actions:
            self.ctx.info("Nothing to copy", f"{dst} already has what you selected from {src}." if wanted else f"{dst} already has everything in {src}.")
            return
        rows = [(a.entry.rel, f"→ {a.dest_rel}" + (f"  ({a.note})" if a.note else "")) for a in plan.actions]
        details = [f"Nothing in {dst} is overwritten. A file with the same name but different content is kept under a new name.",
                   "Each copy is read back and compared before it counts, and 'History & undo' can reverse the whole copy."]
        if plan.warnings:
            details.extend(plan.warnings)
        if not self.ctx.confirm(f"Copy from {src} to {dst}", f"Copy {plural(len(plan.actions), 'file')} ({human_size(plan.bytes_to_copy)}) from {src} to {dst}?",
                                details, rows, confirm="Copy", columns=(f"In {src}", f"Becomes, in {dst}")):
            return
        self._run_copy(plan, src, dst)

    def _run_copy(self, plan: MergePlan, src: str, dst: str) -> None:
        ctx = self.ctx

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return run_merge(plan, ctx.registry, dirs=ctx.dirs, progress=progress, cancel=cancel)

        def done(report: Any) -> None:
            ok, failed = report.count("done"), report.count("failed")
            lines = [f"{ok:,} files copied ({human_size(report.bytes_copied)}) from {src} to {dst}."]
            lines += [f"{r.action.entry.rel}: {r.message}" for r in report.failures[:6]]
            if report.cancelled:
                lines.append("You stopped it early; what was copied is complete and checked.")
            buttons = [("Compare again", self.run, "primary")]
            ctx.info("Copy finished", "Done." if not failed else f"Done, but {failed:,} files could not be copied.", lines,
                     "ok" if not failed else "warn", buttons)

        ctx.run_job(f"Copying {src} → {dst}", job, done)
