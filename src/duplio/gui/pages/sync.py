"""Keep in sync: make A and B match, in both directions, without ever destroying anything."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.backup.sync import SyncOp, SyncOptions, SyncPlan, plan_sync, run_sync
from duplio.backup.syncstate import SyncState
from duplio.compare.diff import Basis
from duplio.gui.model import RowKind, sync_rows
from duplio.gui.pages.pairpage import PairPage
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, autowrap
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import human_size, plural

if TYPE_CHECKING:
    from duplio.gui.app import Context

CONFLICTS = (("Keep both versions (the newer keeps the name)", "keep-both"), ("Take the newer one", "newer"), ("A always wins", "a"),
             ("B always wins", "b"), ("Leave it alone", "skip"))
DELETIONS = (("Carry the deletion over (the file goes to the quarantine)", "propagate"), ("Copy the deleted file back", "restore"),
             ("Leave it alone", "ignore"))


class SyncPage(PairPage):
    title = "Keep in sync"
    subtitle = ("Make folders A and B match. The first run only adds what is missing on each side. Later runs remember what was in sync, so "
                "a file you deleted on one side is moved to the quarantine on the other, never destroyed. Preview first; nothing changes "
                "until you press Sync.")
    a_title, a_hint = "Folder A", "one of the two folders"
    b_title, b_hint = "Folder B", "the other one"
    run_text = "Preview sync"
    again_text = "Preview again"
    job_title = "Working out what would change"
    selectable = False
    chips = (("all", "Changes", tuple(RowKind), PALETTE["warn"]),)

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        self.plan: SyncPlan | None = None
        super().__init__(master, ctx)
        f = ctx.fonts
        # options sit between the folder boxes and the button
        opts = tk.Frame(self.body, bg=PALETTE["bg"])
        row = tk.Frame(opts, bg=PALETTE["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="If a file changed on both sides", font=f.base, bg=PALETTE["bg"], width=34, anchor="w").pack(side="left")
        self.conflict = ttk.Combobox(row, state="readonly", values=[c[0] for c in CONFLICTS], width=52, font=f.base)
        self.conflict.current(0)
        self.conflict.pack(side="left")
        row = tk.Frame(opts, bg=PALETTE["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="If a file was deleted on one side", font=f.base, bg=PALETTE["bg"], width=34, anchor="w").pack(side="left")
        self.deletions = ttk.Combobox(row, state="readonly", values=[d[0] for d in DELETIONS], width=52, font=f.base)
        self.deletions.current(0)
        self.deletions.pack(side="left")
        self.content = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Compare file contents, not just size and date", variable=self.content).pack(anchor="w", pady=(4, 0))
        content_note = tk.Label(opts, text="Slower, but a file that was only touched is not copied again.", font=f.small, bg=PALETTE["bg"],
                                fg=PALETTE["muted"], anchor="w", justify="left")
        content_note.pack(fill="x", padx=(24, 0), pady=(0, 6))
        autowrap(content_note, margin=28, minimum=200)
        self.options_frame = opts
        self.layout()

    def layout(self) -> None:
        super().layout()
        frame = getattr(self, "options_frame", None)
        if frame is None:
            return
        frame.pack_forget()
        if not (self.has_result and not self.editing):
            frame.pack(fill="x", pady=(0, 8), before=self.run_row)

    def build_actions(self, bar: tk.Frame) -> None:
        f = self.ctx.fonts
        self.sync_button = Btn(bar, "Sync now", self.sync_now, kind="success", fonts=f, padx=22, pady=9)
        self.sync_button.pack(anchor="w")
        self.copy_note = tk.Label(bar, text="Replaced files are kept in .duplio-versions, deleted ones go to the quarantine, and History & undo "
                                  "reverses a whole sync.", font=f.small, bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.copy_note.pack(fill="x", pady=(6, 0))
        autowrap(self.copy_note, margin=4, minimum=200)

    def build_options(self) -> SyncOptions:
        return SyncOptions(on_conflict=CONFLICTS[self.conflict.current()][1], deletions=DELETIONS[self.deletions.current()][1],
                           basis=Basis.CONTENT if self.content.get() else Basis.TIME_SIZE)

    def make_job(self, a: str, b: str):  # type: ignore[no-untyped-def]
        ctx = self.ctx
        options = self.build_options()

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            roots = make_roots([(ctx.registry.open(a), RootRole.NORMAL), (ctx.registry.open(b), RootRole.NORMAL)])
            inventory = scan(roots, ScanFilter(min_size=0, exclude=list(ctx.settings.exclude)), progress=progress, cancel=cancel)
            state = SyncState.open(ctx.dirs, inventory.roots[0].location.uri, inventory.roots[1].location.uri)
            try:
                return plan_sync(inventory, state.load(), options, cache=ctx.fresh_cache, progress=progress, cancel=cancel)
            finally:
                state.close()

        return job

    def show_result(self, plan: SyncPlan) -> None:  # type: ignore[override]
        self.plan = plan
        rows = sync_rows(plan)
        changes = [a for a in plan.actions if a.op is not SyncOp.MARK]
        to_b, to_a = plan.bytes_towards(1), plan.bytes_towards(0)
        if not changes:
            headline, detail, kind = "A and B are already in sync.", f"{plural(plan.unchanged, 'file')} identical on both sides.", "ok"
        else:
            headline = f"{plural(len(changes), 'change')} would be made."
            parts = [f"{human_size(to_b)} written to B", f"{human_size(to_a)} written to A"]
            deletes = plan.count(SyncOp.DELETE)
            if deletes:
                parts.append(f"{plural(deletes, 'file')} moved to the quarantine")
            if plan.warnings:
                parts.extend(plan.warnings[:2])
            detail = "; ".join(parts) + "."
            kind = "warn"
        self.sync_button.set_enabled(plan.has_work)
        self.problem_list = [(s.rel, s.reason) for s in plan.skipped]
        self.present(rows, headline, detail, kind, len(plan.skipped))

    def sync_now(self) -> None:
        plan = self.plan
        if plan is None or not plan.has_work:
            return
        ctx = self.ctx
        changes = [a for a in plan.actions if a.op is not SyncOp.MARK]
        rows = [(a.rel, row.relation) for a, row in zip(changes, self.all_rows, strict=False)]
        if not ctx.confirm("Synchronise A and B", f"Make {plural(len(rows), 'change')} so that A and B match?",
                           ["Nothing is destroyed: replaced and deleted files are kept (in .duplio-versions and the quarantine), and "
                            "'History & undo' reverses the whole sync."], rows, confirm="Sync now", danger=plan.disruptive,
                           columns=("File", "What happens")):
            return

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            state = SyncState.open(ctx.dirs, plan.roots[0].location.uri, plan.roots[1].location.uri)
            try:
                return run_sync(plan, ctx.registry, state, dirs=ctx.dirs, progress=progress, cancel=cancel)
            finally:
                state.close()

        def done(report: Any) -> None:
            failed = report.count("failed")
            lines = [f"{report.count('done'):,} changes made."]
            lines += [f"{r.action.rel}: {r.message}" for r in report.results if r.status == "failed"][:6]
            ctx.info("Sync finished", "Done." if not failed else f"Done, but {failed:,} changes failed.", lines, "ok" if not failed else "warn",
                     [("Preview again", self.run, "primary")])

        ctx.run_job("Synchronising", job, done)

