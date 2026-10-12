"""History/undo, snapshot backup, mirror and phone-import screens."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch

from duplio.backup.ingest import DEFAULT_LAYOUT, LAYOUT_TOKENS, IngestOptions, IngestReport, ingest
from duplio.backup.mirror import MirrorOptions, MirrorPlan, MirrorReport, plan_mirror, run_mirror
from duplio.backup.repository import Repository
from duplio.backup.retention import RetentionPolicy, apply_policy
from duplio.errors import DuplioError
from duplio.plan.journal import list_journals, summarize
from duplio.plan.undo import UndoReport, purge_quarantine, undo
from duplio.scan.filters import ScanFilter
from duplio.tui.job import JobScreen
from duplio.tui.widgets import ConfirmDialog, location_suggestions, short
from duplio.util.fmt import human_size, plural


class _SuggestMixin:
    """Fills a Select with detected devices and folders, copying the pick into an Input."""

    def start_suggestions(self, select_id: str) -> None:
        self._suggest_select = select_id
        self.run_worker(self._load_suggestions, thread=True, group="suggest")  # type: ignore[attr-defined]

    def _load_suggestions(self) -> None:
        options = location_suggestions()
        self.app.call_from_thread(self._set_suggestions, options)  # type: ignore[attr-defined]

    def _set_suggestions(self, options: list[tuple[str, str]]) -> None:
        select = self.query_one(f"#{self._suggest_select}", Select)  # type: ignore[attr-defined]
        select.set_options([(f"{label} — {short(loc, 46)}", loc) for label, loc in options])


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class HistoryScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("u", "undo", "Undo selected"), Binding("f5", "reload", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="history"):
            yield Label("History & undo", classes="screen-title")
            yield Static(Text("Every plan Duplio applied. Quarantines, hard links and clones can be undone; "
                              "purging the quarantine frees the space for good.", style="dim"))
            yield DataTable(id="journals", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Button("Undo selected (u)", id="undo", variant="primary")
                yield Button("Purge its quarantine…", id="purge", variant="error")
                yield Button("Refresh", id="reload")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#journals", DataTable)
        for label in ("Apply id", "When", "Action", "Done", "Skipped", "Failed", "Undone", "Purged", "Freed", "State"):
            table.add_column(label)
        self.action_reload()

    def action_reload(self) -> None:
        app: Any = self.app
        table = self.query_one("#journals", DataTable)
        table.clear()
        self.ids: list[str] = []
        for path in list_journals(app.dirs.journals):
            s = summarize(path)
            state = Text("interrupted", style="yellow") if s.incomplete else Text("")
            table.add_row(Text(s.session), str(s.header.get("created", ""))[:19].replace("T", " "),
                          Text(str(s.header.get("action", "?"))), str(s.count("done")), str(s.count("skipped")),
                          str(s.count("failed")), str(s.count("undone")), str(s.count("purged")),
                          human_size(s.reclaimed), state, key=s.session)
            self.ids.append(s.session)
        if not self.ids:
            table.add_row(Text("Nothing applied yet.", style="dim"), "", "", "", "", "", "", "", "", "")

    def _selected(self) -> str | None:
        table = self.query_one("#journals", DataTable)
        if not getattr(self, "ids", None) or table.row_count == 0:
            return None
        return self.ids[min(table.cursor_row, len(self.ids) - 1)]

    @on(Button.Pressed, "#reload")
    def reload_pressed(self) -> None:
        self.action_reload()

    @on(Button.Pressed, "#back")
    def back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#undo")
    def action_undo(self) -> None:
        apply_id = self._selected()
        if apply_id is None:
            return
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return undo(apply_id, app.registry, dirs=app.dirs, progress=progress, cancel=cancel)

        def summarise(report: UndoReport) -> Text:
            text = Text(f"{report.count('restored'):,} restored", style="bold green")
            text.append(f" · {report.count('conflict'):,} conflicts · {report.count('failed'):,} failed · "
                        f"{report.count('not-undoable'):,} not undoable")
            for line in report.lines[:60]:
                if line.status != "restored":
                    text.append(f"\n{line.status}: {line.path} {line.message}", style="yellow")
            return text

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.app.push_screen(JobScreen(f"Undoing {apply_id}", job, summarise=summarise), lambda _: self.action_reload())

        self.app.push_screen(ConfirmDialog("Undo", f"Restore everything that can be restored from {apply_id}?",
                                           confirm_label="Undo"), confirmed)

    @on(Button.Pressed, "#purge")
    def purge(self) -> None:
        apply_id = self._selected()
        if apply_id is None:
            return
        app: Any = self.app
        try:
            files, nbytes, _ = purge_quarantine(app.registry, apply_id=apply_id, dirs=app.dirs, dry_run=True)
        except DuplioError as exc:
            self.notify(str(exc), severity="error")
            return
        if not files:
            self.notify("Nothing from this run is in quarantine.")
            return

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return

            def job(progress, cancel):  # type: ignore[no-untyped-def]
                progress.stage("Purging quarantine")
                return purge_quarantine(app.registry, apply_id=apply_id, dirs=app.dirs)

            def summarise(res: tuple[int, int, list[str]]) -> Text:
                text = Text(f"Deleted {plural(res[0], 'file')}, freed {human_size(res[1])}.", style="green")
                for err in res[2][:30]:
                    text.append(f"\n{err}", style="red")
                return text

            self.app.push_screen(JobScreen("Purging quarantine", job, summarise=summarise), lambda _: self.action_reload())

        self.app.push_screen(
            ConfirmDialog("Purge quarantine", f"Permanently delete {plural(files, 'quarantined file')} ({human_size(nbytes)})?",
                          confirm_label="Delete permanently", require_word="PURGE", danger=True),
            confirmed,
        )


# ---------------------------------------------------------------------------
# Snapshot backup
# ---------------------------------------------------------------------------


class BackupScreen(_SuggestMixin, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="backup"):
            yield Label("Snapshot backup", classes="screen-title")
            yield Static(Text("Versioned and deduplicated: unchanged files are not re-read, identical data is stored "
                              "once, every chunk is verified on restore. The repository can live on a USB drive, "
                              "another disk or an SMB share.", style="dim"))
            with Horizontal(classes="row"):
                yield Input(placeholder="What to back up (folder, adb:/sdcard, smb://…)", id="source")
                yield Select([], prompt="Quick pick", id="suggest")
            with Horizontal(classes="row"):
                yield Input(placeholder="Repository location", id="repo")
                yield Input(placeholder="Label (e.g. phone, laptop)", id="label")
            with Horizontal(classes="row"):
                yield Button("Create repository", id="init")
                yield Button("Back up now", id="run", variant="success")
                yield Button("Check integrity", id="check")
                yield Button("List snapshots", id="list")
            yield DataTable(id="snapshots", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Input(placeholder="Restore selected snapshot into… (folder)", id="target")
                yield Button("Restore", id="restore", variant="primary")
            with Horizontal(classes="row"):
                yield Label("Keep", classes="bar-label")
                yield Input(value="7", id="keep-daily", classes="num")
                yield Label("daily", classes="bar-label")
                yield Input(value="4", id="keep-weekly", classes="num")
                yield Label("weekly", classes="bar-label")
                yield Input(value="12", id="keep-monthly", classes="num")
                yield Label("monthly", classes="bar-label")
                yield Input(value="3", id="keep-yearly", classes="num")
                yield Label("yearly", classes="bar-label")
                yield Button("Apply retention & prune", id="forget", variant="warning")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#snapshots", DataTable)
        for label in ("Snapshot", "Time", "Label", "Source", "Files", "Size", "New data"):
            table.add_column(label)
        self.snapshot_ids: list[str] = []
        self.start_suggestions("suggest")

    @on(Select.Changed, "#suggest")
    def picked(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#source", Input).value = event.value

    def _repo_text(self) -> str | None:
        text = self.query_one("#repo", Input).value.strip()
        if not text:
            self.notify("Enter the repository location first.", severity="warning")
            return None
        return text

    @on(Button.Pressed, "#init")
    def init_repo(self) -> None:
        text = self._repo_text()
        if text is None:
            return
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            progress.stage("Creating repository")
            return Repository.init(app.registry.open(text))

        self.app.push_screen(JobScreen("Create repository", job,
                                       summarise=lambda r: Text(f"Repository ready at {r.loc.display}", style="green")))

    @on(Button.Pressed, "#run")
    def run_backup(self) -> None:
        text = self._repo_text()
        source = self.query_one("#source", Input).value.strip()
        if text is None:
            return
        if not source:
            self.notify("Enter what to back up.", severity="warning")
            return
        label = self.query_one("#label", Input).value.strip()
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            repo = Repository.open(app.registry.open(text))
            flt = ScanFilter.for_backup(exclude=list(app.settings.exclude))
            return repo.backup(app.registry.open(source), scan_filter=flt, label=label, progress=progress, cancel=cancel)

        def summarise(report) -> Text:  # type: ignore[no-untyped-def]
            s = report.snapshot
            out = Text(f"Snapshot {s.id}\n", style="bold green")
            out.append(f"{s.files:,} files · {human_size(s.bytes)} · {s.reused_files:,} unchanged (not re-read)\n")
            out.append(f"new data {human_size(s.new_bytes)} stored as {human_size(s.stored_bytes)}")
            for path, err in report.errors[:30]:
                out.append(f"\n{path}: {err}", style="yellow")
            return out

        self.app.push_screen(JobScreen("Backing up", job, summarise=summarise), lambda _: self.list_snapshots())

    @on(Button.Pressed, "#list")
    def list_snapshots(self) -> None:
        text = self.query_one("#repo", Input).value.strip()
        if text:
            self._load_snapshots(text)

    @work(thread=True, exclusive=True, group="snapshots")
    def _load_snapshots(self, text: str) -> None:
        app: Any = self.app
        try:
            snaps = Repository.open(app.registry.open(text)).snapshots()
        except DuplioError as exc:
            self.app.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self._show_snapshots, snaps)

    def _show_snapshots(self, snaps) -> None:  # type: ignore[no-untyped-def]
        table = self.query_one("#snapshots", DataTable)
        table.clear()
        self.snapshot_ids = []
        for s in reversed(snaps):
            table.add_row(Text(s.id), s.time[:19].replace("T", " "), Text(s.label), Text(short(s.source, 40)),
                          f"{s.files:,}", human_size(s.bytes), human_size(s.new_bytes), key=s.id)
            self.snapshot_ids.append(s.id)
        if not snaps:
            self.notify("No snapshots yet.")

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        text = self._repo_text()
        if text is None:
            return
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return Repository.open(app.registry.open(text)).check(read_data=True, progress=progress, cancel=cancel)

        def summarise(report) -> Text:  # type: ignore[no-untyped-def]
            if report.ok:
                return Text(f"Healthy ✓ — {report.snapshots:,} snapshots, {report.read_objects:,} chunks re-read and verified.",
                            style="bold green")
            out = Text("Problems found:\n", style="bold red")
            for snap, missing in report.missing.items():
                out.append(f"snapshot {snap}: {len(missing)} chunks missing\n", style="red")
            for oid in report.corrupt[:30]:
                out.append(f"corrupt chunk {oid}\n", style="red")
            return out

        self.app.push_screen(JobScreen("Checking repository", job, summarise=summarise))

    @on(Button.Pressed, "#restore")
    def restore(self) -> None:
        text = self._repo_text()
        target = self.query_one("#target", Input).value.strip()
        table = self.query_one("#snapshots", DataTable)
        if text is None:
            return
        if not self.snapshot_ids:
            self.notify("List the snapshots and select one first.", severity="warning")
            return
        if not target:
            self.notify("Enter where to restore to.", severity="warning")
            return
        snap = self.snapshot_ids[min(table.cursor_row, len(self.snapshot_ids) - 1)]
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            repo = Repository.open(app.registry.open(text))
            return repo.restore(snap, app.registry.open(target), conflict="rename", progress=progress, cancel=cancel)

        def summarise(report) -> Text:  # type: ignore[no-untyped-def]
            out = Text(f"{report.restored:,} files restored ({human_size(report.bytes)}) · {report.unchanged:,} already identical"
                       f" · {len(report.renamed):,} restored next to a different existing file", style="green")
            for path, err in report.errors[:30]:
                out.append(f"\n{path}: {err}", style="red")
            return out

        self.app.push_screen(JobScreen(f"Restoring {snap}", job, summarise=summarise))

    @on(Button.Pressed, "#forget")
    def forget(self) -> None:
        text = self._repo_text()
        if text is None:
            return
        try:
            policy = RetentionPolicy(
                keep_daily=int(self.query_one("#keep-daily", Input).value or 0),
                keep_weekly=int(self.query_one("#keep-weekly", Input).value or 0),
                keep_monthly=int(self.query_one("#keep-monthly", Input).value or 0),
                keep_yearly=int(self.query_one("#keep-yearly", Input).value or 0),
            )
        except ValueError:
            self.notify("Retention counts must be whole numbers.", severity="error")
            return
        app: Any = self.app
        try:
            repo = Repository.open(app.registry.open(text))
            decision = apply_policy(repo.snapshots(), policy)
        except DuplioError as exc:
            self.notify(str(exc), severity="error")
            return
        if not decision.remove:
            self.notify("The policy keeps every snapshot; nothing to do.")
            return

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            progress.stage("Forgetting snapshots", total_items=len(decision.remove))
            with repo.lock("forget"):
                for snap in decision.remove:
                    repo.delete_snapshot(snap.id)
                    progress.advance(items=1)
            return repo.prune(progress=progress)

        self.app.push_screen(
            ConfirmDialog("Apply retention", f"Forget {plural(len(decision.remove), 'snapshot')} and free their space? "
                          f"{len(decision.keep):,} snapshots are kept.", confirm_label="Forget & prune", danger=True),
            lambda ok: self.app.push_screen(
                JobScreen("Applying retention", job,
                          summarise=lambda r: Text(f"Freed {human_size(r[1])} ({r[0]:,} chunks).", style="green")),
                lambda _: self.list_snapshots(),
            ) if ok else None,
        )


# ---------------------------------------------------------------------------
# Mirror
# ---------------------------------------------------------------------------


def _mirror_summary(plan: MirrorPlan) -> Text:
    text = Text()
    text.append(f"{plan.count('copy'):,} new · {plan.count('update'):,} changed · {plan.count('move'):,} moved · "
                f"{plan.count('archive'):,} retired · {plan.unchanged:,} unchanged · {human_size(plan.bytes_to_copy)} to copy\n",
                style="bold")
    for warning in plan.warnings[:10]:
        text.append(f"{warning}\n", style="yellow")
    marks = {"copy": "+", "update": "M", "move": "→", "archive": "↓"}
    for action in plan.actions[:60]:
        extra = f"  (from {action.from_rel})" if action.from_rel else ""
        text.append(f" {marks[action.kind]} {action.rel}{extra}\n", style="dim")
    if len(plan.actions) > 60:
        text.append(f" … {len(plan.actions) - 60:,} more\n", style="dim")
    return text


class MirrorScreen(_SuggestMixin, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="mirror"):
            yield Label("Mirror a folder", classes="screen-title")
            yield Static(Text("Makes the destination an exact, verified copy of the source. Replaced or removed files "
                              "are moved into .duplio-versions — never deleted. A safety brake stops the run if the "
                              "source looks empty (a locked phone!) or most of the destination would change.", style="dim"))
            with Horizontal(classes="row"):
                yield Input(placeholder="Source", id="source")
                yield Select([], prompt="Quick pick", id="suggest")
            yield Input(placeholder="Destination", id="dest")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="delete")
                yield Label("Retire files that are no longer in the source (moved to versions)", markup=False)
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="checksum")
                yield Label("Compare contents, not just size and time (slower)", markup=False)
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="force")
                yield Label("Override the safety brake", markup=False)
            with Horizontal(classes="row"):
                yield Button("Preview", id="preview", variant="primary")
                yield Button("Mirror now", id="run", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.start_suggestions("suggest")

    @on(Select.Changed, "#suggest")
    def picked(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#source", Input).value = event.value

    def _job(self, *, execute: bool):  # type: ignore[no-untyped-def]
        source = self.query_one("#source", Input).value.strip()
        dest = self.query_one("#dest", Input).value.strip()
        if not source or not dest:
            self.notify("Enter a source and a destination.", severity="warning")
            return None
        options = MirrorOptions(delete_extraneous=self.query_one("#delete", Switch).value,
                                checksum=self.query_one("#checksum", Switch).value,
                                force=self.query_one("#force", Switch).value, dry_run=not execute)
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            src, dst = app.registry.open(source), app.registry.open(dest)
            plan = plan_mirror(src, dst, scan_filter=ScanFilter.for_backup(exclude=list(app.settings.exclude)),
                               options=options, cache=app.cache, progress=progress, cancel=cancel)
            if not execute:
                return plan
            return run_mirror(plan, options=options, progress=progress, cancel=cancel)

        return job

    @on(Button.Pressed, "#preview")
    def preview(self) -> None:
        job = self._job(execute=False)
        if job is not None:
            self.app.push_screen(JobScreen("Mirror preview", job, summarise=_mirror_summary))

    @on(Button.Pressed, "#run")
    def run_pressed(self) -> None:
        job = self._job(execute=True)
        if job is None:
            return

        def summarise(report: MirrorReport) -> Group:
            done = " · ".join(f"{v:,} {k}" for k, v in report.done.items()) or "nothing to do"
            head = Text(f"Done: {done} · {human_size(report.bytes_copied)} copied", style="bold green")
            parts: list[Any] = [head]
            if report.versions_dir:
                parts.append(Text(f"Previous versions kept in {report.versions_dir}", style="dim"))
            for rel, err in report.errors[:30]:
                parts.append(Text(f"{rel}: {err}", style="red"))
            return Group(*parts)

        self.app.push_screen(JobScreen("Mirroring", job, summarise=summarise))


# ---------------------------------------------------------------------------
# Import from phone
# ---------------------------------------------------------------------------


class IngestScreen(_SuggestMixin, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="ingest"):
            yield Label("Import from phone", classes="screen-title")
            yield Static(Text("Streams each new photo and video once, verifies it, files it by capture date and skips "
                              "anything already in the library — or imported before, even if you deleted it since.",
                              style="dim"))
            with Horizontal(classes="row"):
                yield Input(placeholder="Phone or card folder (e.g. adb:/sdcard/DCIM or a mounted DCIM)", id="source")
                yield Select([], prompt="Detected devices", id="suggest")
            yield Input(placeholder="Library folder", id="library")
            yield Label("Folder layout  — tokens: " + ", ".join("{" + t + "}" for t in LAYOUT_TOKENS), classes="field-label")
            yield Input(value=DEFAULT_LAYOUT, id="layout")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="all-files")
                yield Label("Import every file type, not only photos and videos", markup=False)
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="free")
                yield Label("Afterwards, delete from the phone what is verified in the library", markup=False)
            with Horizontal(classes="row"):
                yield Button("Preview", id="preview", variant="primary")
                yield Button("Import now", id="run", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.start_suggestions("suggest")

    @on(Select.Changed, "#suggest")
    def picked(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#source", Input).value = event.value

    def _start(self, *, dry_run: bool) -> None:
        source = self.query_one("#source", Input).value.strip()
        library = self.query_one("#library", Input).value.strip()
        if not source or not library:
            self.notify("Enter the phone folder and the library folder.", severity="warning")
            return
        free = self.query_one("#free", Switch).value and not dry_run
        options = IngestOptions(layout=self.query_one("#layout", Input).value.strip() or DEFAULT_LAYOUT,
                                media_only=not self.query_one("#all-files", Switch).value, dry_run=dry_run,
                                free_source=free)
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return ingest(app.registry.open(source), app.registry.open(library), options=options, cache=app.cache,
                          dirs=app.dirs, progress=progress, cancel=cancel, confirm_free=free)

        def summarise(report: IngestReport) -> Text:
            counts = [(s, report.count(s)) for s in ("imported", "duplicate", "previously-imported", "would-import",
                                                    "maybe-duplicate", "failed")]
            text = Text(" · ".join(f"{n:,} {s.replace('-', ' ')}" for s, n in counts if n) or "No media found.", style="bold")
            text.append(f"\n{human_size(report.imported_bytes)} imported")
            if report.freed:
                text.append(f"\nFreed {human_size(report.freed_bytes)} on the phone ({report.freed:,} files).", style="green")
            for item in [i for i in report.items if i.status == "imported"][:40]:
                text.append(f"\n + {item.dest_rel}  ({item.date_source})", style="dim")
            for rel, err in report.errors[:30]:
                text.append(f"\n{rel}: {err}", style="red")
            return text

        screen = JobScreen("Import preview" if dry_run else "Importing", job, summarise=summarise)
        if free:
            self.app.push_screen(
                ConfirmDialog("Free space on the phone", "After importing, files whose content is verified in the "
                              "library will be DELETED from the phone.", confirm_label="Import and free space", danger=True),
                lambda ok: self.app.push_screen(screen) if ok else None,
            )
        else:
            self.app.push_screen(screen)

    @on(Button.Pressed, "#preview")
    def preview(self) -> None:
        self._start(dry_run=True)

    @on(Button.Pressed, "#run")
    def run_pressed(self) -> None:
        self._start(dry_run=False)
