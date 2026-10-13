"""Import from phone: bring only the new photos from a phone or camera card into your library."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.backup.ingest import DEFAULT_LAYOUT, IngestOptions, IngestReport, ingest
from duplio.gui.model import RowKind, ingest_rows
from duplio.gui.pages.pairpage import PairPage
from duplio.gui.style import PALETTE
from duplio.gui.widgets import Btn, autowrap
from duplio.util.fmt import human_size, plural

if TYPE_CHECKING:
    from duplio.gui.app import Context

LAYOUTS = (("By year and month  (2024/2024-05/IMG_1.jpg)", DEFAULT_LAYOUT), ("By year  (2024/IMG_1.jpg)", "{year}/{name}"),
           ("By exact date  (2024-05-17/IMG_1.jpg)", "{date}/{name}"), ("All in one folder", "{name}"))


class ImportPage(PairPage):
    title = "Import from phone"
    subtitle = ("Bring the pictures and videos on a phone or camera card into your library, skipping everything that is already there "
                "(even if it has another name). Nothing is removed from the phone.")
    a_title, a_hint = "Phone or camera card", "a plugged-in phone, a card, or any folder of photos"
    b_title, b_hint = "Your library", "the folder where your photos live"
    run_text = "Preview import"
    again_text = "Check again"
    job_title = "Looking at the phone and the library"
    selectable = False
    slot_labels = ("Phone or card", "Your library")
    summary_names = ("Phone", "Library")
    chips = (("new", "To import", (RowKind.ONLY_A,), PALETTE["ok"]), ("same", "Already in the library", (RowKind.SAME,), PALETTE["muted"]),
             ("failed", "Problems", (RowKind.UNREADABLE,), PALETTE["bad"]))

    def __init__(self, master: tk.Misc, ctx: Context) -> None:
        self.report: IngestReport | None = None
        self.checked: tuple[str, str] = ("", "")
        super().__init__(master, ctx)
        f = ctx.fonts
        opts = tk.Frame(self.body, bg=PALETTE["bg"])
        row = tk.Frame(opts, bg=PALETTE["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="Sort the new files into folders", font=f.base, bg=PALETTE["bg"], width=30, anchor="w").pack(side="left")
        self.layout_box = ttk.Combobox(row, state="readonly", values=[x[0] for x in LAYOUTS], width=52, font=f.base)
        self.layout_box.current(0)
        self.layout_box.pack(side="left")
        self.media_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Only pictures and videos", variable=self.media_only).pack(anchor="w", pady=(4, 6))
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
        self.import_button = Btn(bar, "Import new files", self.import_now, kind="success", fonts=f, padx=22, pady=9)
        self.import_button.pack(anchor="w")
        note = tk.Label(bar, text="Every file is checked after it is copied, and nothing is removed from the phone.", font=f.small,
                        bg=PALETTE["bg"], fg=PALETTE["muted"], anchor="w", justify="left")
        note.pack(fill="x", pady=(6, 0))
        autowrap(note, margin=4, minimum=200)

    def build_options(self, dry_run: bool) -> IngestOptions:
        return IngestOptions(layout=LAYOUTS[self.layout_box.current()][1], media_only=bool(self.media_only.get()), dry_run=dry_run)

    def make_job(self, a: str, b: str):  # type: ignore[no-untyped-def]
        ctx = self.ctx
        options = self.build_options(True)
        self.checked = (a, b)

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            source, library = ctx.registry.open(a), ctx.registry.open(b)
            return ingest(source, library, options=options, cache=ctx.cache, dirs=ctx.dirs, progress=progress, cancel=cancel), source

        return job

    def show_result(self, outcome: Any) -> None:  # type: ignore[override]
        report, source = outcome
        self.report = report
        self.source = source
        rows = ingest_rows(report, source)
        new = [i for i in report.items if i.status in ("would-import", "maybe-duplicate")]
        unsure = report.count("maybe-duplicate")
        have = len(report.items) - len(new) - report.count("failed")
        if not report.items:
            headline, detail, kind = "Nothing to import.", "No pictures or videos were found on the phone.", "ok"
        elif not new:
            headline, detail, kind = "Everything is already in the library.", f"{plural(have, 'file')} checked.", "ok"
        else:
            headline = f"{plural(len(new), 'file')} to import ({human_size(sum(i.size for i in new))})."
            detail = f"{plural(have, 'file')} {'is' if have == 1 else 'are'} already in the library and will be skipped."
            if unsure:
                detail += (f" {plural(unsure, 'file')} {'has' if unsure == 1 else 'have'} the same size as something in the library; "
                           "they are compared by content while importing and skipped if identical.")
            kind = "warn"
        self.import_button.set_enabled(bool(new))
        self.problem_list = list(report.errors)
        self.present(rows, headline, detail, kind, len(report.errors))

    def import_now(self) -> None:
        report = self.report
        if report is None:
            return
        ctx = self.ctx
        a, b = self.checked  # the folders the preview was made from, even if the boxes were edited since
        new = [i for i in report.items if i.status in ("would-import", "maybe-duplicate")]
        if not new:
            return
        if not ctx.confirm("Import the new files", f"Copy {plural(len(new), 'file')} ({human_size(sum(i.size for i in new))}) into the library?",
                           ["Nothing is removed from the phone. Each file is read back and checked, and a file that is already in the library is skipped."],
                           [(i.source_rel, "→ into the library") for i in new], confirm="Import"):
            return
        options = self.build_options(False)

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return ingest(ctx.registry.open(a), ctx.registry.open(b), options=options, cache=ctx.cache, dirs=ctx.dirs, progress=progress,
                          cancel=cancel)

        def done(result: IngestReport) -> None:
            imported, failed = result.count("imported"), result.count("failed")
            lines = [f"{imported:,} files imported ({human_size(result.imported_bytes)})."]
            if result.count("duplicate"):
                lines.append(f"{result.count('duplicate'):,} turned out to be in the library already.")
            lines += [f"{rel}: {why}" for rel, why in result.errors[:5]]
            ctx.info("Import finished", "Done." if not failed else f"Done, but {failed:,} files failed.", lines, "ok" if not failed else "warn",
                     [("Check again", self.run, "primary")])

        ctx.run_job("Importing", job, done)
