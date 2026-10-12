"""sync — keep two folders identical in both directions, safely."""

from __future__ import annotations

import sys
import time
from typing import Any

import click
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.backup.sync import (
    CONFLICT_POLICIES,
    DELETION_POLICIES,
    SIDE,
    SyncOp,
    SyncOptions,
    SyncPlan,
    SyncReport,
    plan_sync,
    run_sync,
)
from duplio.backup.syncstate import SyncState
from duplio.cli.app import build_filter, cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.cmd_diff import make_compare_options
from duplio.cli.cmd_drive import guard_destination, guard_options, make_limiter
from duplio.cli.render import t
from duplio.compare.diff import Basis
from duplio.errors import ConfigError
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import human_duration, human_size, parse_duration, plural

# No age filters here: a file near the cut-off could look "deleted" on a drive that rounds its times.
sync_compare_options = make_compare_options(default_basis="time-size", bases=("time-size", "content"),
                                            comparing_only=False, ages=False)
MARK = {SyncOp.COPY: ("+", "green"), SyncOp.UPDATE: ("↻", "magenta"), SyncOp.DELETE: ("✗", "red3"),
        SyncOp.CONFLICT: ("⚠", "yellow")}


def plan_panel(plan: SyncPlan) -> Panel:
    a, b = plan.roots
    rows: list[Text] = []
    if plan.first_run:
        rows.append(Text("First sync of these two locations: there is no history yet, so nothing will be deleted.",
                         style="cyan"))
    for frm in (0, 1):
        copies = plan.count(SyncOp.COPY, frm)
        updates = plan.count(SyncOp.UPDATE, frm)
        if copies or updates:
            rows.append(Text.assemble((f"{SIDE[frm]} → {SIDE[1 - frm]}  ", "bold"), f"{plural(copies, 'new file')}, "
                                      f"{plural(updates, 'update')}  ({human_size(plan.bytes_towards(1 - frm))})"))
    for side in (0, 1):
        n = plan.count(SyncOp.DELETE, side)
        if n:
            rows.append(Text.assemble((f"remove from {SIDE[side]}  ", "bold red3"),
                                      f"{plural(n, 'file')} deleted on {SIDE[1 - side]} (moved to the quarantine, undoable)"))
    n = plan.count(SyncOp.CONFLICT)
    if n:
        rows.append(Text.assemble((f"{plural(n, 'conflict')}  ", "bold yellow"), "changed on both sides: both versions are kept"))
    rows.append(Text(f"{plan.unchanged:,} unchanged" + (f" · {len(plan.skipped):,} left alone" if plan.skipped else ""),
                     style="dim"))
    for w in plan.warnings:
        rows.append(Text(w, style="yellow"))
    title = Text.assemble(("Sync ", "bold"), ("A ", "bold"), t(a.location.display), ("  ⇄  B ", "bold"),
                          t(b.location.display))
    return Panel(Group(*rows), title=title, title_align="left", border_style="cyan")


def actions_table(plan: SyncPlan, limit: int) -> Table | None:
    shown = [a for a in plan.actions if a.op is not SyncOp.MARK]
    if not shown:
        return None
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("", no_wrap=True, width=2)
    table.add_column("Direction", no_wrap=True)
    table.add_column("File", ratio=3, overflow="fold")
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Why", ratio=2, style="dim", overflow="fold")
    for act in shown[:limit]:
        mark, style = MARK[act.op]
        if act.op is SyncOp.DELETE:
            direction = f"remove from {SIDE[act.side or 0]}"
        else:
            direction = f"{SIDE[act.frm or 0]} → {SIDE[1 - (act.frm or 0)]}"
        table.add_row(Text(mark, style=style), direction, t(act.rel), human_size(act.entry.size), t(act.reason))
    if len(shown) > limit:
        table.add_row("", "", Text(f"… {len(shown) - limit:,} more", style="dim"), "", "")
    return table


def result_panel(report: SyncReport) -> Panel:
    rows = [Text.assemble((f"{report.count('done'):,}", "bold green"),
                          f" done · {human_size(report.bytes_copied)} transferred in {human_duration(report.seconds)}")]
    if report.count("skipped"):
        rows.append(Text(f"{report.count('skipped'):,} skipped (changed since the scan; the next sync picks them up)",
                         style="yellow"))
    if report.count("failed"):
        rows.append(Text(f"{report.count('failed'):,} failed (they are retried next time)", style="bold red"))
    if report.cancelled:
        rows.append(Text("Cancelled: finished files stay as they are; nothing is half-written.", style="yellow"))
    if report.journal is not None:
        rows.append(Text.assemble(("Undo with: ", "dim"), (f"duplio undo {report.apply_id}", "bold")))
    ok = not report.count("failed") and not report.cancelled
    return Panel(Group(*rows), title="Sync complete" if ok else "Sync finished with problems", title_align="left",
                 border_style="green" if ok else "yellow")


@cli.command("sync")
@click.argument("a")
@click.argument("b")
@sync_compare_options
@click.option("--on-conflict", type=click.Choice(CONFLICT_POLICIES), default="keep-both", show_default=True,
              help="Changed on both sides: keep-both = newer keeps the name, the other is kept next to it · "
                   "newer · a · b (the loser is archived in .duplio-versions) · skip.")
@click.option("--deletions", type=click.Choice(DELETION_POLICIES), default="propagate", show_default=True,
              help="A file deleted on one side: propagate = move the other copy to the quarantine · "
                   "restore = copy it back · ignore.")
@click.option("--max-delete", type=click.FloatRange(0.01, 1.0), default=0.5, show_default=True,
              help="Safety brake: stop if more than this fraction of a side would be removed.")
@click.option("--force", is_flag=True, help="Override the safety brakes (empty side, too many deletions).")
@click.option("--ignore-ctime", is_flag=True,
              help="Do not compare content just because a file's change time moved (permissions, attributes) while its "
                   "size and modification time did not.")
@click.option("--reset", is_flag=True, help="Forget the history of this pair: the next run is a first sync (no deletions).")
@click.option("--no-verify", is_flag=True, help="Do not read copies back to check them.")
@click.option("--jobs", type=click.IntRange(1, 16), default=None, help="Parallel copies (default: automatic).")
@click.option("--limit", type=int, default=25, show_default=True, help="Planned actions to list.")
@click.option("--dry-run", is_flag=True, help="Show what would happen; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before replacing, removing or resolving conflicts.")
@guard_options
@handle_errors
def sync_cmd(a: str, b: str, on_conflict: str, deletions: str, max_delete: float, force: bool, ignore_ctime: bool, reset: bool,
             no_verify: bool, jobs: int | None, limit: int, dry_run: bool, yes: bool, bwlimit: str | None,
             expect_mounted: bool | None, sentinel: str | None, **kw: Any) -> None:
    """Two-way sync: changes on either side are carried to the other.

    The first run only adds what is missing on each side (nothing is deleted). Later
    runs compare against the previous run to tell "new here" from "deleted there".
    Deleted and replaced files are never destroyed: they go to .duplio-quarantine or
    .duplio-versions, and 'duplio undo' reverses a whole sync.

    \b
    duplio sync ~/Documents smb://me@nas/docs --dry-run
    duplio sync ~/Music /media/usb/Music --on-conflict newer --deletions restore
    """
    app = get_app()
    loc_a, loc_b = app.registry.open(a), app.registry.open(b)
    for loc in (loc_a, loc_b):
        guard_destination(app, loc, expect_mounted=expect_mounted, sentinel=sentinel)
    tolerance = None
    if kw.get("time_tolerance"):
        try:
            tolerance = int(parse_duration(str(kw["time_tolerance"])) * 1_000_000_000)
        except ValueError as exc:
            raise ConfigError(f"not a duration: {kw['time_tolerance']!r} (examples: 2s, 90s, 1h)") from exc
    options = SyncOptions(
        basis=Basis(kw.get("basis") or "time-size"), on_conflict=on_conflict, deletions=deletions, max_delete=max_delete,
        force=force, ignore_dst=bool(kw.get("ignore_dst")), ignore_ctime=ignore_ctime, time_tolerance_ns=tolerance,
        verify=not no_verify,
        jobs=jobs, limiter=make_limiter(bwlimit), algorithm=kw.get("algorithm"), expect_mounted=expect_mounted,
        sentinel=sentinel,
    )
    options.validate()
    kw.setdefault("protect", ())
    if kw.get("min_size") is None:
        kw["min_size"] = 0
    roots = make_roots([(loc_a, RootRole.NORMAL), (loc_b, RootRole.NORMAL)])
    scan_filter = build_filter(app, kw)
    cache = app.cache(bool(kw.get("use_cache")))
    state = SyncState.open(app.dirs, roots[0].location.uri, roots[1].location.uri)
    try:
        baseline = {} if reset else state.load()  # --reset plans as a first run; the history is only cleared for real

        def planning(cancel):  # type: ignore[no-untyped-def]
            with progress_for(app) as progress:
                inventory = scan(roots, scan_filter, progress=progress, cancel=cancel,
                                 one_file_system=not kw.get("cross_filesystems"))
                return plan_sync(inventory, baseline, options, cache=cache, progress=progress, cancel=cancel)

        plan: SyncPlan = run_cancellable(planning)
        app.console.print(plan_panel(plan))
        table = actions_table(plan, limit)
        if table is not None:
            app.console.print(table)
        for skip in plan.skipped[:10]:
            app.console.print(t(f"  left alone: {skip.rel}: {skip.reason}", "dim"))
        if len(plan.skipped) > 10:
            app.console.print(t(f"  … and {len(plan.skipped) - 10:,} more left alone", "dim"))
        if not plan.has_work:
            state_note = "Both sides are in sync. ✓" if not plan.skipped else "Nothing to do for the rest."
            if not dry_run:
                if reset:
                    state.reset()
                run_sync(plan, app.registry, state, dirs=app.dirs)  # refreshes the history of identical files
            app.console.print(Text(state_note, style="green"))
            return
        if dry_run:
            app.console.print(Text("Dry run: nothing was changed.", style="dim"))
            return
        if plan.disruptive and not yes:
            if not sys.stdin.isatty():
                raise ConfigError("not running interactively; pass --yes (or use --dry-run to preview)")
            if not click.confirm("Apply these changes?", default=False):
                return
        started = time.monotonic()
        if reset:
            state.reset()

        def work(cancel):  # type: ignore[no-untyped-def]
            with progress_for(app) as progress:
                return run_sync(plan, app.registry, state, dirs=app.dirs, progress=progress, cancel=cancel)

        report: SyncReport = run_cancellable(work)
        report.seconds = report.seconds or time.monotonic() - started
        app.console.print(result_panel(report))
        for r in report.failures[:20]:
            app.console.print(t(f"{r.action.rel}: {r.message}", "red"))
        if report.count("failed"):
            sys.exit(1)
    finally:
        state.close()
