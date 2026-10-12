"""History & undo: everything Duplio changed, the button that puts it back, and the one that frees the space."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.errors import DuplioError
from duplio.gui.app import Page
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn
from duplio.plan.journal import list_journals, summarize
from duplio.plan.undo import list_quarantine, purge_quarantine, undo
from duplio.util.fmt import human_size, plural

if TYPE_CHECKING:
    from duplio.gui.app import Context

COLUMNS = (("when", "When", 150), ("what", "What", 110), ("where", "Folders", 330), ("done", "Done", 60), ("skipped", "Left alone", 80),
           ("failed", "Failed", 60), ("undone", "Put back", 70), ("state", "", 110))


class HistoryPage(Page):
    title = "History & undo"
    subtitle = ("Every change Duplio has made. Files it removed are waiting in a quarantine, so any of this can be put back. "
                "When you are sure, you can free the space by deleting the quarantined copies for good.")

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        super().__init__(master, ctx)
        f = ctx.fonts
        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(8, 20))
        bar = tk.Frame(body, bg=PALETTE["bg"])
        bar.pack(side="bottom", fill="x", pady=(12, 0))
        Btn(bar, "Undo the selected change", self.undo_selected, kind="primary", fonts=f, padx=18, pady=9).pack(side="left")
        Btn(bar, "Free the space (delete its quarantined copies for good)…", self.free_selected, kind="danger", fonts=f, padx=14,
            pady=9).pack(side="left", padx=10)
        Btn(bar, "Refresh", self.refresh, fonts=f, padx=12, pady=9).pack(side="right")
        holder = tk.Frame(body, bg=PALETTE["line"], padx=1, pady=1)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key in ("when", "what", "where", "state") else "e", stretch=key == "where")
        bar_y = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar_y.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar_y.pack(side="right", fill="y")
        self.empty = tk.Label(body, text="", font=f.base, bg=PALETTE["bg"], fg=PALETTE["muted"])

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        ids = 0
        for path in list_journals(self.ctx.dirs.journals):
            try:
                s = summarize(path)
            except Exception:
                continue
            roots = s.header.get("roots", [])
            where = "  →  ".join(str(r.get("uri", ""))[-60:] for r in roots[:2] if isinstance(r, dict))
            state = "interrupted" if s.incomplete else ""
            self.tree.insert("", "end", iid=s.session, values=(str(s.header.get("created", ""))[:19].replace("T", " "),
                             str(s.header.get("action", "?")), where, s.count("done"), s.count("skipped"), s.count("failed"),
                             s.count("undone"), state))
            ids += 1
        if not ids:
            self.empty.configure(text="Nothing has been changed yet.")
            self.empty.pack(anchor="w", pady=16)
        else:
            self.empty.pack_forget()

    def _selected(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            self.ctx.info("Choose a change first", "Click one of the changes in the list.")
            return None
        return str(sel[0])

    def undo_selected(self) -> None:
        apply_id = self._selected()
        if apply_id is None:
            return
        ctx = self.ctx
        if not ctx.confirm("Undo this change", "Put the files of this change back where they were?",
                           ["Anything that was copied by this change is removed again if it is still exactly as it was copied. "
                            "A file that has changed since is never touched; you are told about it."], confirm="Undo"):
            return

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return undo(apply_id, ctx.registry, dirs=ctx.dirs, progress=progress, cancel=cancel)

        def done(report: Any) -> None:
            problems = [f"{line.path}: {line.status} {line.message}" for line in report.lines if line.status not in ("restored", "already-undone")]
            ctx.info("Undo finished", f"{report.count('restored'):,} files put back.", problems[:8],
                     "ok" if not problems else "warn")
            self.refresh()

        ctx.run_job("Putting the files back", job, done)

    def free_selected(self) -> None:
        apply_id = self._selected()
        if apply_id is None:
            return
        ctx = self.ctx
        held = next((q for q in list_quarantine(ctx.dirs) if q.apply_id == apply_id), None)
        if held is None:
            ctx.info("Nothing to free", "This change has no quarantined copies (they were put back or already deleted).")
            return
        if not ctx.confirm("Delete the quarantined copies for good?", f"{plural(held.files, 'file')} ({human_size(held.bytes)}) will be deleted permanently.",
                           ["This cannot be undone. The copies that Duplio kept are not touched.",
                            "Only do this when you have looked at the result and are happy with it."], confirm="Delete for good", danger=True):
            return

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return purge_quarantine(ctx.registry, apply_id=apply_id, dirs=ctx.dirs)

        def done(result: Any) -> None:
            files, nbytes, errors = result
            ctx.info("Space freed", f"{files:,} files deleted, {human_size(nbytes)} freed.", errors[:6], "ok" if not errors else "warn")
            self.refresh()

        def failed(error: BaseException) -> None:
            ctx.show_error(error if isinstance(error, DuplioError) else str(error))

        ctx.run_job("Deleting the quarantined copies", job, done, on_error=failed)
