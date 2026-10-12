"""Duplicate workflow: choose locations → scan → review → confirm → apply."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from duplio.compare.model import MODE_HELP, Mode, ScanOptions, ScanResult, Verify
from duplio.errors import ConfigError, DuplioError
from duplio.plan.executor import ApplyOptions, ApplyReport, apply_plan
from duplio.plan.model import ACTION_HELP, ActionType, Plan
from duplio.plan.planner import build_plan, build_similar_plan, decide
from duplio.plan.rules import parse_rules
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole
from duplio.tui.job import JobScreen
from duplio.tui.widgets import ConfirmDialog, location_suggestions, short
from duplio.util.fmt import format_ns, human_size, parse_size, plural
from duplio.vfs.location import location_kind


def _storage_label(text: str) -> str:
    kind = location_kind(text)
    if kind == "smb":
        return "SMB share (native)"
    if kind == "adb":
        return "Android device (ADB)"
    try:
        import os

        from duplio.vfs.local import LocalBackend

        path = os.path.abspath(os.path.expanduser(text))
        if not os.path.isdir(path):
            return "⚠ folder not found"
        return LocalBackend().capabilities(path).label
    except Exception:
        return "local"


class SetupScreen(Screen[None]):
    """Pick locations and options for a duplicate scan or a backup check."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("ctrl+s", "start", "Start scan")]

    def __init__(self, purpose: str) -> None:
        super().__init__()
        self.purpose = purpose  # "dedupe" or "compare"
        self.locations: list[tuple[str, RootRole]] = []

    # ---- layout -------------------------------------------------------
    def compose(self) -> ComposeResult:
        compare = self.purpose == "compare"
        yield Header()
        with Horizontal(id="setup"):
            with Vertical(id="setup-left"):
                yield Label("Is it backed up?" if compare else "Find duplicates", classes="screen-title")
                yield Static(
                    Text(
                        "Add the locations to CHECK (e.g. your phone) and the locations to check AGAINST "
                        "(your backups). Duplio lists every file that has no identical copy in the backups."
                        if compare
                        else "Add folders, mounted phones, SMB shares (smb://user@server/share) or Android devices "
                        "(adb:/sdcard/DCIM). Mark a location as reference to protect it: it is used for "
                        "matching but never changed."
                    ),
                    classes="muted",
                )
                yield DataTable(id="locations", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="row"):
                    yield Input(placeholder="Path, smb://server/share/folder or adb:/sdcard/DCIM", id="loc-input")
                    yield Button("Check this" if compare else "Add", id="add-normal", variant="primary")
                    yield Button("Against this" if compare else "Add as reference", id="add-ref")
                with Horizontal(classes="row"):
                    yield Select([], prompt="Quick add: detected devices and folders", id="suggest")
                    yield Button("Toggle role", id="toggle-role")
                    yield Button("Remove", id="remove-loc", variant="error")
            with VerticalScroll(id="setup-right"):
                if not compare:
                    yield Label("Mode", classes="field-label")
                    yield Select(
                        [(f"{m.value} — {MODE_HELP[m]}", m.value) for m in (Mode.ALL, Mode.CROSS, Mode.WITHIN, Mode.REFERENCE)],
                        value=Mode.ALL.value, allow_blank=False, id="mode",
                    )
                yield Label("Verification", classes="field-label")
                yield Select(
                    [
                        ("hash — cryptographic content hash (recommended)", "hash"),
                        ("bytes — hash, then byte-for-byte (paranoid)", "bytes"),
                        ("quick — size + name only (report only)", "quick"),
                    ],
                    value="hash", allow_blank=False, id="verify",
                )
                if not compare:
                    with Horizontal(classes="switch-row"):
                        yield Switch(value=False, id="similar")
                        yield Label("Also find similar images", markup=False)
                with Horizontal(classes="switch-row"):
                    yield Switch(value=False, id="hidden")
                    yield Label("Skip hidden files", markup=False)
                with Horizontal(classes="switch-row"):
                    yield Switch(value=True, id="sampling")
                    yield Label("Fingerprint samples first (faster)", markup=False)
                yield Label("Minimum file size", classes="field-label")
                yield Input(value="1", placeholder="e.g. 1, 100KB, 1MiB", id="min-size")
                yield Label("Exclude (comma-separated globs)", classes="field-label")
                yield Input(placeholder="*.tmp, **/cache/**", id="exclude")
                yield Label("Protect (never remove; comma-separated globs)", classes="field-label")
                yield Input(placeholder="**/Originals/**", id="protect")
                yield Button("Start scan  (Ctrl+S)", id="start", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#locations", DataTable)
        table.add_column("#", key="n")
        table.add_column("Role", key="role")
        table.add_column("Location", key="loc")
        table.add_column("Storage", key="kind")
        self.load_suggestions()
        self.query_one("#loc-input", Input).focus()

    def load_suggestions(self) -> None:
        self.run_worker(self._suggest, thread=True, exclusive=True, group="suggest")

    def _suggest(self) -> None:
        options = location_suggestions()
        self.app.call_from_thread(self._set_suggestions, options)

    def _set_suggestions(self, options: list[tuple[str, str]]) -> None:
        select = self.query_one("#suggest", Select)
        select.set_options([(f"{label}  —  {short(loc, 50)}", loc) for label, loc in options])

    # ---- events -------------------------------------------------------
    def _refresh_table(self) -> None:
        table = self.query_one("#locations", DataTable)
        table.clear()
        compare = self.purpose == "compare"
        for i, (text, role) in enumerate(self.locations, 1):
            if compare:
                role_text = Text("against", style="magenta") if role is RootRole.REFERENCE else Text("check", style="green")
            else:
                role_text = Text("reference", style="magenta") if role is RootRole.REFERENCE else Text("scan", style="green")
            table.add_row(str(i), role_text, Text(text), Text(_storage_label(text)), key=str(i - 1))

    def _add(self, role: RootRole) -> None:
        field = self.query_one("#loc-input", Input)
        text = field.value.strip()
        if not text:
            self.notify("Type a location first, or pick one from Quick add.", severity="warning")
            return
        if any(t == text for t, _ in self.locations):
            self.notify("That location is already in the list.", severity="warning")
            return
        self.locations.append((text, role))
        field.value = ""
        self._refresh_table()

    @on(Button.Pressed, "#add-normal")
    def add_normal(self) -> None:
        self._add(RootRole.NORMAL)

    @on(Button.Pressed, "#add-ref")
    def add_ref(self) -> None:
        self._add(RootRole.REFERENCE)

    @on(Input.Submitted, "#loc-input")
    def submitted(self) -> None:
        self._add(RootRole.NORMAL)

    @on(Select.Changed, "#suggest")
    def suggestion(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK and isinstance(event.value, str):
            self.query_one("#loc-input", Input).value = event.value
            self.query_one("#loc-input", Input).focus()

    def _selected_index(self) -> int | None:
        table = self.query_one("#locations", DataTable)
        if not self.locations or table.row_count == 0:
            return None
        return min(table.cursor_row, len(self.locations) - 1)

    @on(Button.Pressed, "#toggle-role")
    def toggle_role(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        text, role = self.locations[idx]
        self.locations[idx] = (text, RootRole.NORMAL if role is RootRole.REFERENCE else RootRole.REFERENCE)
        self._refresh_table()

    @on(Button.Pressed, "#remove-loc")
    def remove_location(self) -> None:
        idx = self._selected_index()
        if idx is not None:
            self.locations.pop(idx)
            self._refresh_table()

    @on(Button.Pressed, "#start")
    def action_start(self) -> None:
        try:
            scan_filter, options = self._collect()
        except (ConfigError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        locations = list(self.locations)
        app: Any = self.app

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            from duplio.compare.engine import compare
            from duplio.compare.session import save_session
            from duplio.scan.inventory import make_roots, scan

            opened = [(app.registry.open(text), role) for text, role in locations]
            roots = make_roots(opened)
            inventory = scan(roots, scan_filter, progress=progress, cancel=cancel, one_file_system=options.one_file_system)
            result = compare(inventory, options, cache=app.cache, progress=progress, cancel=cancel)
            try:
                save_session(result, app.dirs)
            except OSError:
                pass
            return result

        def done(result: ScanResult) -> None:
            if result.options.mode is Mode.UNIQUE:
                self.app.switch_screen(CompareResultScreen(result))
            else:
                self.app.switch_screen(ReviewScreen(result))

        self.app.push_screen(JobScreen("Scanning", job, on_done=done,
                                       subtitle=" · ".join(short(t, 40) for t, _ in locations)))

    def _collect(self) -> tuple[ScanFilter, ScanOptions]:
        compare = self.purpose == "compare"
        normal = [t for t, r in self.locations if r is RootRole.NORMAL]
        refs = [t for t, r in self.locations if r is RootRole.REFERENCE]
        if compare:
            if not normal or not refs:
                raise ConfigError("add at least one location to check and one to check against")
            mode = Mode.UNIQUE
        else:
            if not self.locations:
                raise ConfigError("add at least one location")
            mode = Mode(str(self.query_one("#mode", Select).value))
            if mode is Mode.REFERENCE and not refs:
                raise ConfigError("reference mode needs at least one reference location")
            if mode is Mode.REFERENCE and not normal:
                raise ConfigError("reference mode needs at least one location to clean up")
        settings = self.app.settings  # type: ignore[attr-defined]
        min_text = self.query_one("#min-size", Input).value.strip() or "1"
        exclude = [p.strip() for p in self.query_one("#exclude", Input).value.split(",") if p.strip()]
        protect = [p.strip() for p in self.query_one("#protect", Input).value.split(",") if p.strip()]
        scan_filter = ScanFilter(
            exclude=list(settings.exclude) + exclude,
            protect=list(settings.protect) + protect,
            min_size=parse_size(min_text),
            skip_hidden=self.query_one("#hidden", Switch).value,
        )
        options = ScanOptions(mode=mode, verify=Verify(str(self.query_one("#verify", Select).value)))
        options.use_sampling = self.query_one("#sampling", Switch).value
        if not compare:
            options.similar_images = self.query_one("#similar", Switch).value
        options.one_file_system = settings.one_file_system
        return scan_filter, options


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class ReviewScreen(Screen[None]):
    BINDINGS = [
        Binding("space", "toggle", "Keep/remove"),
        Binding("k", "keep_only", "Keep only this"),
        Binding("x", "exclude_group", "Skip group"),
        Binding("r", "reapply", "Re-apply rules"),
        Binding("p", "plan", "Review plan"),
        Binding("escape", "app.home", "Home"),
    ]

    def __init__(self, result: ScanResult) -> None:
        super().__init__()
        self.result = result
        self.settings = None
        self.rules: list[str] = []
        self.keep: dict[int, set[int]] = {}
        self.locked: dict[int, dict[int, str]] = {}
        self.excluded: set[int] = set()
        self.current_group: int | None = None
        self.current_similar: int | None = None
        self.similar_remove: dict[int, set[int]] = {}
        self.groups_by_id = {g.id: g for g in result.groups}
        self.similar_by_id = {g.id: g for g in result.similar}

    def compose(self) -> ComposeResult:
        res = self.result
        yield Header()
        yield Static("", id="review-summary")
        legend = Text()
        for r in res.inventory.roots:
            legend.append(f"{r.index + 1}", style="bold")
            legend.append(f" {short(r.location.display, 48)}")
            legend.append(f" ({r.caps.label}{', reference' if r.is_reference else ''})   ", style="dim")
        yield Static(legend, id="review-legend")
        with TabbedContent(id="review-tabs"):
            with TabPane(f"Duplicates ({len(res.groups):,})", id="tab-dupes"):
                with Horizontal(classes="split"):
                    yield DataTable(id="groups", cursor_type="row", zebra_stripes=True)
                    with Vertical(id="members-pane"):
                        yield DataTable(id="members", cursor_type="row", zebra_stripes=True)
                        yield Static("", id="member-detail")
            if res.similar:
                with TabPane(f"Similar images ({len(res.similar):,})", id="tab-similar"):
                    yield Static(Text("Similar images are not identical. Nothing is selected for removal unless you "
                                      "mark it (space). Marked images can only be quarantined.", style="yellow"))
                    with Horizontal(classes="split"):
                        yield DataTable(id="sgroups", cursor_type="row", zebra_stripes=True)
                        yield DataTable(id="smembers", cursor_type="row", zebra_stripes=True)
                    yield Button("Quarantine marked similar images…", id="similar-plan", variant="warning")
            problems = len(res.inventory.errors) + len(res.stats.errors) + len(res.unverified)
            with TabPane(f"Problems ({problems:,})", id="tab-problems"):
                yield RichLog(id="problems", wrap=True, markup=False)
        with Horizontal(id="review-bar"):
            yield Label("Action", classes="bar-label")
            yield Select([(a.value, a.value) for a in ActionType], allow_blank=False, id="action")
            yield Label("Keep rules", classes="bar-label")
            yield Input(id="rules")
            yield Button("Apply rules (r)", id="reapply")
            yield Button("Review plan (p)", id="plan", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        settings = self.app.settings  # type: ignore[attr-defined]
        self.rules = list(settings.keep)
        self.query_one("#rules", Input).value = ",".join(self.rules)
        self.query_one("#action", Select).value = settings.action.value
        groups = self.query_one("#groups", DataTable)
        for label, key in (("#", "id"), ("Copies", "n"), ("Size", "size"), ("Wasted", "wasted"), ("Plan", "plan"), ("Kept copy", "keeper")):
            groups.add_column(label, key=key)
        members = self.query_one("#members", DataTable)
        for label, key in (("", "mark"), ("Loc", "loc"), ("File", "path"), ("Modified", "mtime"), ("Note", "note")):
            members.add_column(label, key=key)
        if self.result.similar:
            sg = self.query_one("#sgroups", DataTable)
            for label, key in (("#", "id"), ("Images", "n"), ("Largest", "largest"), ("Marked", "marked")):
                sg.add_column(label, key=key)
            sm = self.query_one("#smembers", DataTable)
            for label, key in (("", "mark"), ("Loc", "loc"), ("File", "path"), ("Pixels", "px"), ("Size", "size"), ("Dist", "d")):
                sm.add_column(label, key=key)
            for g in self.result.similar:
                sg.add_row(f"S{g.id}", str(len(g.members)), Text(g.members[0].rel), "0", key=str(g.id))
            self.similar_remove = {g.id: set() for g in self.result.similar}
        self._apply_rules(self.rules)
        self._fill_problems()
        if self.result.groups:
            groups.focus()
        self._update_summary()

    # ---- state --------------------------------------------------------
    def _apply_rules(self, rules: list[str]) -> None:
        action = ActionType(str(self.query_one("#action", Select).value))
        parsed = parse_rules(rules)
        roots = self.result.inventory.roots
        self.keep.clear()
        self.locked.clear()
        for g in self.result.groups:
            d = decide(g, roots, parsed, action)
            self.keep[g.id] = {m.id for m in d.keep}
            self.locked[g.id] = dict(d.reasons)
        self.rules = rules
        self._fill_groups()

    def _fill_groups(self) -> None:
        table = self.query_one("#groups", DataTable)
        cursor = table.cursor_row
        table.clear()
        for g in self.result.groups:
            keeper = next((m for m in g.members if m.id in self.keep[g.id]), g.members[0])
            status = Text("skip", style="dim") if g.id in self.excluded else (
                Text("—", style="dim") if not self._victims(g.id) else Text(f"{len(self._victims(g.id))} ✗", style="red"))
            table.add_row(str(g.id), str(g.count), human_size(g.size), human_size(g.wasted), status,
                          Text(f"{keeper.root + 1}:{keeper.rel}"), key=str(g.id))
        if self.result.groups:
            table.move_cursor(row=min(cursor, len(self.result.groups) - 1))
            self._show_group(self.result.groups[min(cursor, len(self.result.groups) - 1)].id)

    def _victims(self, gid: int) -> list[int]:
        g = self.groups_by_id[gid]
        return [m.id for m in g.members if m.id not in self.keep[gid]]

    def _show_group(self, gid: int) -> None:
        self.current_group = gid
        g = self.groups_by_id[gid]
        table = self.query_one("#members", DataTable)
        cursor = table.cursor_row if table.row_count else 0
        table.clear()
        for m in g.members:
            reason = self.locked[gid].get(m.id)
            if reason:
                mark = Text("🔒", style="green")
            elif m.id in self.keep[gid]:
                mark = Text("★", style="bold green")
            else:
                mark = Text("✗", style="bold red")
            notes = []
            if reason:
                notes.append(reason)
            aliases = g.aliases.get(m.id)
            if aliases:
                notes.append(f"+{len(aliases)} hard link(s)")
            table.add_row(mark, str(m.root + 1), Text(m.rel), format_ns(m.mtime_ns),
                          Text(", ".join(notes), style="dim"), key=str(m.id))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))

    def _update_summary(self) -> None:
        files = nbytes = 0
        for g in self.result.groups:
            if g.id in self.excluded:
                continue
            victims = self._victims(g.id)
            files += len(victims)
            nbytes += len(victims) * g.size
        action = ActionType(str(self.query_one("#action", Select).value))
        text = Text()
        text.append(f"{len(self.result.inventory.entries):,} files scanned · ", style="dim")
        text.append(f"{len(self.result.groups):,} groups · ")
        text.append(f"plan: {action.value} {plural(files, 'file')}", style="bold")
        text.append(" · frees ")
        text.append(human_size(nbytes), style="bold red")
        text.append(f"   ({ACTION_HELP[action]})", style="dim")
        self.query_one("#review-summary", Static).update(text)

    def _fill_problems(self) -> None:
        log = self.query_one("#problems", RichLog)
        res = self.result
        if not (res.inventory.errors or res.stats.errors or res.unverified):
            log.write(Text("No problems. Every file was read and compared.", style="green"))
        for path, err in res.inventory.errors[:2000]:
            log.write(Text(f"scan   {path}: {err}", style="yellow"))
        for path, err in res.stats.errors[:2000]:
            log.write(Text(f"read   {path}: {err}", style="yellow"))
        for e in res.unverified[:2000]:
            log.write(Text(f"unverified  {e.rel}", style="yellow"))

    # ---- events -------------------------------------------------------
    @on(DataTable.RowHighlighted, "#groups")
    def group_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._show_group(int(event.row_key.value))

    @on(DataTable.RowHighlighted, "#members")
    def member_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None or self.current_group is None:
            return
        entry_id = int(event.row_key.value)
        group = self.groups_by_id[self.current_group]
        member = next((m for m in group.members if m.id == entry_id), None)
        if member is None:
            return
        root = self.result.inventory.roots[member.root]
        text = Text()
        text.append(root.backend.display(member.path))
        text.append(f"\n{human_size(member.size)} · modified {format_ns(member.mtime_ns)} · {root.caps.label}", style="dim")
        if group.digest:
            text.append(f" · {group.algorithm} {group.digest[:16]}…", style="dim")
        self.query_one("#member-detail", Static).update(text)

    @on(DataTable.RowHighlighted, "#sgroups")
    def similar_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._show_similar(int(event.row_key.value))

    def _show_similar(self, gid: int) -> None:
        self.current_similar = gid
        g = self.similar_by_id[gid]
        table = self.query_one("#smembers", DataTable)
        table.clear()
        for m, dist, dim in zip(g.members, g.distances, g.dimensions, strict=True):
            marked = m.id in self.similar_remove[gid]
            mark = Text("✗", style="bold red") if marked else Text("·", style="dim")
            table.add_row(mark, str(m.root + 1), Text(m.rel), f"{dim[0]}×{dim[1]}", human_size(m.size), str(dist), key=str(m.id))

    @on(DataTable.RowSelected, "#members")
    def member_selected(self) -> None:
        self.action_toggle()

    @on(DataTable.RowSelected, "#smembers")
    def similar_selected(self) -> None:
        self.action_toggle()

    def action_toggle(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable) and focused.id == "smembers":
            self._toggle_similar()
            return
        if self.current_group is None:
            return
        gid = self.current_group
        table = self.query_one("#members", DataTable)
        if not table.row_count:
            return
        entry_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or -1)
        if entry_id in self.locked[gid]:
            self.notify(f"This copy is locked ({self.locked[gid][entry_id]}) and is always kept.", severity="warning")
            return
        keep = self.keep[gid]
        if entry_id in keep:
            if len(keep) <= 1:
                self.notify("At least one copy must be kept.", severity="warning")
                return
            keep.discard(entry_id)
        else:
            keep.add(entry_id)
        self._after_change()

    def _toggle_similar(self) -> None:
        if self.current_similar is None:
            return
        gid = self.current_similar
        table = self.query_one("#smembers", DataTable)
        if not table.row_count:
            return
        entry_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or -1)
        marked = self.similar_remove[gid]
        group = self.similar_by_id[gid]
        if entry_id in marked:
            marked.discard(entry_id)
        else:
            if len(marked) + 1 >= len(group.members):
                self.notify("Keep at least one image of the group.", severity="warning")
                return
            marked.add(entry_id)
        row = table.cursor_row
        self._show_similar(gid)
        table.move_cursor(row=row)
        self.query_one("#sgroups", DataTable).update_cell(str(gid), "marked", str(len(marked)))

    def action_keep_only(self) -> None:
        if self.current_group is None:
            return
        gid = self.current_group
        table = self.query_one("#members", DataTable)
        if not table.row_count:
            return
        entry_id = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or -1)
        self.keep[gid] = {entry_id} | set(self.locked[gid])
        self._after_change()

    def action_exclude_group(self) -> None:
        if self.current_group is None:
            return
        self.excluded ^= {self.current_group}
        self._after_change()

    def _after_change(self) -> None:
        gid = self.current_group
        groups = self.query_one("#groups", DataTable)
        members = self.query_one("#members", DataTable)
        row = members.cursor_row
        if gid is not None:
            g = self.groups_by_id[gid]
            keeper = next((m for m in g.members if m.id in self.keep[gid]), g.members[0])
            victims = self._victims(gid)
            status = Text("skip", style="dim") if gid in self.excluded else (
                Text("—", style="dim") if not victims else Text(f"{len(victims)} ✗", style="red"))
            groups.update_cell(str(gid), "plan", status)
            groups.update_cell(str(gid), "keeper", Text(f"{keeper.root + 1}:{keeper.rel}"))
            self._show_group(gid)
            if members.row_count:
                members.move_cursor(row=min(row, members.row_count - 1))
        self._update_summary()

    @on(Button.Pressed, "#reapply")
    def action_reapply(self) -> None:
        text = self.query_one("#rules", Input).value
        rules = [r.strip() for r in text.split(",") if r.strip()]
        try:
            parse_rules(rules)
        except ConfigError as exc:
            self.notify(str(exc), severity="error")
            return
        self._apply_rules(rules)
        self._update_summary()
        self.notify("Keep rules applied to every group.")

    @on(Select.Changed, "#action")
    def action_changed(self) -> None:
        if self.keep:
            self._apply_rules(self.rules)
            self._update_summary()

    @on(Button.Pressed, "#plan")
    def action_plan(self) -> None:
        action = ActionType(str(self.query_one("#action", Select).value))
        included = [g.id for g in self.result.groups if g.id not in self.excluded]
        try:
            plan = build_plan(
                self.result, action=action, rules=self.rules, verify=self.app.settings.verify,  # type: ignore[attr-defined]
                group_ids=included, keep_overrides={gid: set(self.keep[gid]) for gid in included},
            )
        except (ConfigError, DuplioError) as exc:
            self.notify(str(exc), severity="error")
            return
        if not plan.items:
            self.notify("The plan is empty: nothing is marked for removal.", severity="warning")
            return
        self.app.push_screen(ConfirmPlanScreen(plan))

    @on(Button.Pressed, "#similar-plan")
    def similar_plan(self) -> None:
        picks = {gid: ids for gid, ids in self.similar_remove.items() if ids}
        if not picks:
            self.notify("Mark images to remove first (space).", severity="warning")
            return
        try:
            plan = build_similar_plan(self.result, picks)
        except ConfigError as exc:
            self.notify(str(exc), severity="error")
            return
        self.app.push_screen(ConfirmPlanScreen(plan))


# ---------------------------------------------------------------------------
# Confirm + apply
# ---------------------------------------------------------------------------


def plan_renderable(plan: Plan, limit: int = 40) -> Group:
    head = Text()
    head.append(f"{plan.action.value.upper()}  ", style="bold red" if plan.action is ActionType.DELETE else "bold")
    head.append(f"{plural(len(plan.items), 'file')} in {plural(len(plan.group_ids), 'group')} · frees ")
    head.append(human_size(plan.reclaim), style="bold red")
    head.append(f"\n{ACTION_HELP[plan.action]}", style="dim")
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("Will " + plan.action.value, ratio=1)
    table.add_column("Kept copy", ratio=1, style="green")
    table.add_column("Size", justify="right")
    for item in plan.items[:limit]:
        table.add_row(Text(f"{item.victim.root + 1}:{item.victim.rel}"), Text(f"{item.keeper.root + 1}:{item.keeper.rel}"),
                      human_size(item.victim.size))
    parts: list[Any] = [head, Text(""), table]
    if len(plan.items) > limit:
        parts.append(Text(f"… and {len(plan.items) - limit:,} more", style="dim"))
    for note in plan.notes[:5]:
        parts.append(Text(f"note: {note}", style="yellow"))
    safety = Text(
        "\nBefore each file is touched Duplio re-reads its size and time, re-hashes it and the kept copy, journals "
        "the change, and confirms the kept copy still exists afterwards.",
        style="dim",
    )
    parts.append(safety)
    return Group(*parts)


def apply_renderable(report: ApplyReport) -> Group:
    text = Text()
    if report.dry_run:
        text.append(f"Verified {report.count('would-do'):,} files: ready to apply.\n", style="bold green")
    else:
        text.append(f"{report.count('done'):,} done", style="bold green")
        text.append(f" · {report.count('skipped'):,} skipped · ")
        text.append(f"{report.count('failed'):,} failed", style="bold red" if report.count("failed") else "dim")
        text.append(f" · freed {human_size(report.reclaimed)}\n")
    issues = [o for o in report.outcomes if o.status in ("skipped", "failed")]
    table = Table(expand=True, box=None)
    table.add_column("Status")
    table.add_column("File", ratio=1)
    table.add_column("Why", ratio=1, style="dim")
    for o in issues[:50]:
        table.add_row(Text(o.status, style="yellow" if o.status == "skipped" else "red"), Text(o.item.victim.rel), Text(o.message))
    parts: list[Any] = [text]
    if issues:
        parts.append(table)
    if not report.dry_run and report.count("done") and report.action in (ActionType.QUARANTINE, ActionType.HARDLINK, ActionType.REFLINK):
        parts.append(Text(f"\nUndo any time from History (6 on the home screen) — id {report.apply_id}.", style="cyan"))
    return Group(*parts)


class ConfirmPlanScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, plan: Plan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="confirm"):
            yield Label("Review the plan", classes="screen-title")
            with VerticalScroll(id="confirm-body"):
                yield Static(plan_renderable(self.plan))
            with Horizontal(classes="row"):
                yield Button("Back", id="back")
                yield Button("Verify only (dry run)", id="dry")
                yield Button(f"{self.plan.action.value.capitalize()} {len(self.plan.items):,} files",
                             id="apply", variant="error" if self.plan.action is ActionType.DELETE else "success")
        yield Footer()

    @on(Button.Pressed, "#back")
    def back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#dry")
    def dry(self) -> None:
        self._run(dry_run=True)

    @on(Button.Pressed, "#apply")
    def apply(self) -> None:
        if self.plan.action is ActionType.DELETE:
            dialog = ConfirmDialog(
                "Permanent deletion",
                f"{plural(len(self.plan.items), 'file')} will be deleted permanently. This cannot be undone.",
                confirm_label="Delete permanently", require_word="DELETE", danger=True,
            )
            self.app.push_screen(dialog, lambda ok: self._run(dry_run=False) if ok else None)
        else:
            self._run(dry_run=False)

    def _run(self, *, dry_run: bool) -> None:
        app: Any = self.app
        plan = self.plan
        options = ApplyOptions(verify=plan.verify if plan.verify in ("hash", "bytes") else "hash",
                               confirm_delete=plan.action is ActionType.DELETE, dry_run=dry_run)

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            return apply_plan(plan, app.registry, options=options, dirs=app.dirs, progress=progress, cancel=cancel)

        def finished(result: ApplyReport | None) -> None:
            if result is not None and not result.dry_run:
                app.go_home()

        self.app.push_screen(
            JobScreen("Verifying (dry run)" if dry_run else f"Applying: {plan.action.value}", job, summarise=apply_renderable),
            finished,
        )


# ---------------------------------------------------------------------------
# Compare results
# ---------------------------------------------------------------------------


class CompareResultScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.home", "Home")]
    MAX_ROWS = 5000

    def __init__(self, result: ScanResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        res = self.result
        missing = sum(e.size for e in res.unique)
        present = sum(1 for g in res.groups for m in g.members if not res.inventory.roots[m.root].is_reference)
        summary = Text()
        if res.unique:
            summary.append(f"{len(res.unique):,} files ({human_size(missing)}) have NO copy in the backup locations.", style="bold red")
        else:
            summary.append("Everything has a copy in the backup locations. ✓", style="bold green")
        summary.append(f"\n{present:,} files are already backed up.", style="green")
        if res.unverified:
            summary.append(f"\n{len(res.unverified):,} files could not be read and are unknown.", style="yellow")
        yield Header()
        with Vertical(id="compare"):
            yield Label("Is it backed up?", classes="screen-title")
            yield Static(summary)
            yield DataTable(id="unique", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Input(placeholder="Copy the missing files to… (folder, smb://…, adb:/…)", id="copy-dest")
                yield Button("Copy missing files", id="copy", variant="success", disabled=not res.unique)
                yield Button("Export HTML report", id="export")
                yield Button("Home", id="home")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#unique", DataTable)
        table.add_columns("Loc", "File", "Size", "Modified")
        for e in self.result.unique[: self.MAX_ROWS]:
            table.add_row(str(e.root + 1), Text(e.rel), human_size(e.size), format_ns(e.mtime_ns))
        if len(self.result.unique) > self.MAX_ROWS:
            table.add_row("", Text(f"… {len(self.result.unique) - self.MAX_ROWS:,} more (see the HTML report)", style="dim"), "", "")

    @on(Button.Pressed, "#home")
    def home(self) -> None:
        self.app.go_home()  # type: ignore[attr-defined]

    @on(Button.Pressed, "#export")
    def export(self) -> None:
        from duplio.reporting.export import export

        app: Any = self.app
        path = app.dirs.state / "reports" / f"{self.result.session_id}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        export(self.result, path)
        self.notify(f"Report written to {path}", timeout=10)

    @on(Button.Pressed, "#copy")
    def copy(self) -> None:
        dest_text = self.query_one("#copy-dest", Input).value.strip()
        if not dest_text:
            self.notify("Enter where to copy the missing files.", severity="warning")
            return
        app: Any = self.app
        result = self.result

        def job(progress, cancel):  # type: ignore[no-untyped-def]
            from duplio.hashing.algorithms import default_full_algorithm
            from duplio.transfer import copy_verified

            dest = app.registry.open(dest_text)
            multi = len({e.root for e in result.unique}) > 1
            copied, problems = 0, []
            progress.stage("Copying missing files", total_items=len(result.unique), total_bytes=sum(e.size for e in result.unique))
            for e in result.unique:
                cancel.check()
                root = result.inventory.roots[e.root]
                prefix = (root.label or root.backend.basename(root.path) or f"location-{e.root + 1}") if multi else ""
                rel = f"{prefix}/{e.rel}" if prefix else e.rel
                try:
                    copy_verified((root.backend, e.path), (dest.backend, dest.backend.join(dest.path, rel)),
                                  algorithm=default_full_algorithm(), expected_size=e.size, mtime_ns=e.mtime_ns,
                                  cancel=cancel, on_bytes=lambda n: progress.advance(nbytes=n))
                    copied += 1
                except OSError as exc:
                    problems.append(f"{rel}: {exc}")
                    progress.message(f"{rel}: {exc}", level="warning")
                progress.advance(items=1, detail=rel)
            return copied, problems, dest.display

        def summarise(res: tuple[int, list[str], str]) -> Text:
            copied, problems, where = res
            text = Text(f"{copied:,} files copied and verified into {where}.", style="bold green")
            if problems:
                text.append(f"\n{len(problems):,} not copied:", style="yellow")
                for p in problems[:30]:
                    text.append(f"\n  {p}", style="yellow")
            return text

        self.app.push_screen(JobScreen("Copying missing files", job, summarise=summarise))
