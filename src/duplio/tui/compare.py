"""Compare & merge, and two-way sync."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, Switch

from duplio.backup.merge import MergeOptions, MergePlan, plan_merge, run_merge
from duplio.backup.sync import SyncOptions, SyncPlan, plan_sync, run_sync
from duplio.backup.syncstate import SyncState
from duplio.cli.cmd_diff import summary_panel, verdict
from duplio.cli.cmd_merge import actions_table as merge_actions
from duplio.cli.cmd_merge import plan_panel as merge_panel
from duplio.cli.cmd_merge import result_panel as merge_result
from duplio.cli.cmd_sync import actions_table as sync_actions
from duplio.cli.cmd_sync import plan_panel as sync_panel
from duplio.cli.cmd_sync import result_panel as sync_result
from duplio.compare.diff import DiffOptions, DiffResult, diff_inventory
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.tui.job import JobScreen
from duplio.tui.tools import _SuggestMixin
from duplio.tui.widgets import ConfirmDialog, ptext
from duplio.util.fmt import human_size, plural

CONFLICT_CHOICES = [("Keep both (rename the new one)", "rename"), ("Skip", "skip"),
                    ("Take the newer (old one is kept)", "newer"), ("Replace (old one is kept)", "replace")]
SYNC_CONFLICT_CHOICES = [("Keep both (newer keeps the name)", "keep-both"), ("Take the newer", "newer"),
                         ("A always wins", "a"), ("B always wins", "b"), ("Skip", "skip")]
DELETION_CHOICES = [("Carry deletions over (moved to the quarantine)", "propagate"),
                    ("Copy deleted files back", "restore"), ("Leave them alone", "ignore")]


def _inventory(app: Any, a: str, b: str, progress, cancel):  # type: ignore[no-untyped-def]
    roots = make_roots([(app.registry.open(a), RootRole.NORMAL), (app.registry.open(b), RootRole.NORMAL)])
    flt = ScanFilter(min_size=0, exclude=list(app.settings.exclude))
    return scan(roots, flt, progress=progress, cancel=cancel)


class CompareMergeScreen(_SuggestMixin, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="compare-merge"):
            yield Label("Compare & merge", classes="screen-title")
            yield Static(Text("Compares two folders by content — names do not matter, so a renamed or moved photo is "
                              "not a difference. Works across local folders, mounted drives, SMB shares and phones. "
                              "'Merge' copies into B everything A has that B lacks; nothing in B is overwritten "
                              "silently and everything can be undone.", style="dim"))
            with Horizontal(classes="row"):
                yield Input(placeholder="Folder A (the source)", id="a")
                yield Select([], prompt="Quick pick", id="suggest")
            yield Input(placeholder="Folder B (the destination)", id="b")
            yield Label("If B has a different file at the same path", classes="field-label")
            yield Select(CONFLICT_CHOICES, value="rename", allow_blank=False, id="conflict")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="move")
                yield Label("Move: remove each file from A after it is verified in B (kept in the quarantine)", markup=False)
            with Horizontal(classes="row"):
                yield Button("Compare", id="compare", variant="primary")
                yield Button("Preview merge", id="preview")
                yield Button("Merge A → B", id="merge", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.start_suggestions("suggest")

    @on(Select.Changed, "#suggest")
    def picked(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#a", Input).value = event.value

    def _paths(self) -> tuple[str, str] | None:
        a = self.query_one("#a", Input).value.strip()
        b = self.query_one("#b", Input).value.strip()
        if not a or not b:
            self.notify("Enter both folders.", severity="warning")
            return None
        return a, b

    @on(Button.Pressed, "#compare")
    def compare(self) -> None:
        paths = self._paths()
        if paths is None:
            return
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            inv = _inventory(app, paths[0], paths[1], progress, cancel)
            return diff_inventory(inv, DiffOptions(), cache=app.cache, progress=progress, cancel=cancel)

        def summarise(result: DiffResult) -> Group:
            return Group(summary_panel(result), *verdict(result))

        self.app.push_screen(JobScreen("Comparing", job, summarise=summarise))

    def _options(self) -> MergeOptions:
        return MergeOptions(on_conflict=str(self.query_one("#conflict", Select).value),
                            move=self.query_one("#move", Switch).value)

    def _plan_job(self, paths: tuple[str, str], options: MergeOptions):  # type: ignore[no-untyped-def]
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            inv = _inventory(app, paths[0], paths[1], progress, cancel)
            diff = diff_inventory(inv, DiffOptions(), cache=app.cache, progress=progress, cancel=cancel)
            return plan_merge(diff, options)

        return job

    @staticmethod
    def _summarise_plan(plan: MergePlan) -> Group:
        parts: list[Any] = [merge_panel(plan)]
        table = merge_actions(plan, 40)
        if table is not None:
            parts.append(table)
        return Group(*parts)

    @on(Button.Pressed, "#preview")
    def preview(self) -> None:
        paths = self._paths()
        if paths is not None:
            self.app.push_screen(JobScreen("Merge preview", self._plan_job(paths, self._options()),
                                           summarise=self._summarise_plan))

    @on(Button.Pressed, "#merge")
    def merge(self) -> None:
        paths = self._paths()
        if paths is None:
            return
        self.app.push_screen(
            JobScreen("Merge preview — press Done to continue", self._plan_job(paths, self._options()),
                      summarise=self._summarise_plan),
            self._confirm,
        )

    def _confirm(self, plan: MergePlan | None) -> None:
        if plan is None:
            return
        if not plan.has_work:
            self.notify("Nothing to copy: B already has everything in A.")
            return
        details = f"Copy {plural(len(plan.actions), 'file')} ({human_size(plan.bytes_to_copy)}) from A to B."
        if plan.options.move:
            details += " Files are then removed from A (moved to the quarantine; 'undo' brings them back)."
        if plan.count("replace"):
            details += f" {plural(plan.count('replace'), 'file')} in B will be replaced; the old versions are kept."
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return run_merge(plan, app.registry, dirs=app.dirs, progress=progress, cancel=cancel)

        def after(ok: bool | None) -> None:
            if ok:
                self.app.push_screen(JobScreen("Merging", job, summarise=lambda report: merge_result(report, plan, report.seconds)))

        self.app.push_screen(ConfirmDialog("Merge A into B", details, confirm_label="Merge", danger=plan.disruptive), after)


class SyncScreen(_SuggestMixin, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="sync"):
            yield Label("Two-way sync", classes="screen-title")
            yield Static(Text("Keeps two folders identical: changes on either side are carried to the other. The first "
                              "run only adds what is missing; later runs remember what was in sync, so a file deleted "
                              "on one side is moved to the quarantine on the other — never destroyed. Replaced files "
                              "are kept in .duplio-versions and 'undo' reverses a whole sync.", style="dim"))
            with Horizontal(classes="row"):
                yield Input(placeholder="Folder A", id="a")
                yield Select([], prompt="Quick pick", id="suggest")
            yield Input(placeholder="Folder B", id="b")
            yield Label("Changed on both sides", classes="field-label")
            yield Select(SYNC_CONFLICT_CHOICES, value="keep-both", allow_blank=False, id="conflict")
            yield Label("A file deleted on one side", classes="field-label")
            yield Select(DELETION_CHOICES, value="propagate", allow_blank=False, id="deletions")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="content")
                yield Label("Compare contents, not just size and time (slower, avoids re-copying touched files)", markup=False)
            with Horizontal(classes="row"):
                yield Button("Preview", id="preview", variant="primary")
                yield Button("Sync now", id="run", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.start_suggestions("suggest")

    @on(Select.Changed, "#suggest")
    def picked(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#a", Input).value = event.value

    def _setup(self):  # type: ignore[no-untyped-def]
        a = self.query_one("#a", Input).value.strip()
        b = self.query_one("#b", Input).value.strip()
        if not a or not b:
            self.notify("Enter both folders.", severity="warning")
            return None
        from duplio.compare.diff import Basis

        options = SyncOptions(on_conflict=str(self.query_one("#conflict", Select).value),
                              deletions=str(self.query_one("#deletions", Select).value),
                              basis=Basis.CONTENT if self.query_one("#content", Switch).value else Basis.TIME_SIZE)
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            inv = _inventory(app, a, b, progress, cancel)
            state = SyncState.open(app.dirs, inv.roots[0].location.uri, inv.roots[1].location.uri)
            try:
                return plan_sync(inv, state.load(), options, cache=app.cache, progress=progress, cancel=cancel)
            finally:
                state.close()

        return job

    @staticmethod
    def _summarise_plan(plan: SyncPlan) -> Group:
        parts: list[Any] = [sync_panel(plan)]
        table = sync_actions(plan, 40)
        if table is not None:
            parts.append(table)
        for skip in plan.skipped[:10]:
            parts.append(ptext(f"left alone: {skip.rel}: {skip.reason}", style="dim"))
        return Group(*parts)

    @on(Button.Pressed, "#preview")
    def preview(self) -> None:
        job = self._setup()
        if job is not None:
            self.app.push_screen(JobScreen("Sync preview", job, summarise=self._summarise_plan))

    @on(Button.Pressed, "#run")
    def run_pressed(self) -> None:
        job = self._setup()
        if job is not None:
            self.app.push_screen(JobScreen("Sync preview — press Done to continue", job, summarise=self._summarise_plan),
                                 self._confirm)

    def _confirm(self, plan: SyncPlan | None) -> None:
        if plan is None:
            return
        if not plan.has_work:
            self.notify("Both sides are already in sync.")
            return
        app: Any = self.app
        lines = f"{plural(len([a for a in plan.actions if a.op.value != 'mark']), 'change')} will be carried over."

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            state = SyncState.open(app.dirs, plan.roots[0].location.uri, plan.roots[1].location.uri)
            try:
                return run_sync(plan, app.registry, state, dirs=app.dirs, progress=progress, cancel=cancel)
            finally:
                state.close()

        def after(ok: bool | None) -> None:
            if ok:
                self.app.push_screen(JobScreen("Synchronising", job, summarise=sync_result))

        self.app.push_screen(ConfirmDialog("Synchronise A and B", lines, confirm_label="Sync", danger=plan.disruptive), after)
