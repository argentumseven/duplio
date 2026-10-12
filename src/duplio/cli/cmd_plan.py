"""plan · apply · undo · history · quarantine · recover · sessions"""

from __future__ import annotations

import sys

import click
from rich import box
from rich.table import Table
from rich.text import Text

from duplio.cli.app import cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.cmd_scan import confirm_plan
from duplio.cli.render import apply_summary, plan_summary, t, undo_summary
from duplio.compare.session import find_session, list_sessions, load_session
from duplio.errors import ConfigError
from duplio.plan.executor import ApplyOptions, apply_plan
from duplio.plan.journal import list_journals, summarize
from duplio.plan.model import ActionType, Plan
from duplio.plan.planner import build_plan
from duplio.plan.undo import inspect_incomplete, list_quarantine, purge_quarantine, undo
from duplio.util.fmt import human_size, plural


@cli.command()
@click.argument("session", required=False)
@click.option("-a", "--action", type=click.Choice([a.value for a in ActionType]), default=None)
@click.option("-k", "--keep", default=None, help="Keep rules, comma-separated.")
@click.option("-g", "--group", "groups", multiple=True, type=int, help="Only these group numbers (repeatable).")
@click.option("--verify-before", type=click.Choice(["hash", "bytes"]), default=None)
@click.option("-o", "--out", type=click.Path(dir_okay=False), help="Where to save the plan (default: state folder).")
@click.option("--top", type=int, default=15, show_default=True)
@handle_errors
def plan(session: str | None, action: str | None, keep: str | None, groups: tuple[int, ...],
         verify_before: str | None, out: str | None, top: int) -> None:
    """Build a reviewable plan from a saved scan (default: the latest one)."""
    app = get_app()
    path = find_session(session, app.dirs)
    result = load_session(path, app.registry)
    act = ActionType(action) if action else app.settings.action
    rules = keep.split(",") if keep else app.settings.keep
    built = build_plan(result, action=act, rules=rules, verify=verify_before or app.settings.verify,
                       group_ids=groups or None)
    plan_summary(app.console, built, top=top)
    target = out or str(app.dirs.state / "plans" / f"{built.plan_id}.json")
    built.save(target)
    app.console.print(Text.assemble(("Saved: ", "dim"), target))
    if built.items:
        app.console.print(Text.assemble(("Apply with: ", "bold"), f"duplio apply {target}"))


@cli.command()
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Re-verify every file but change nothing.")
@click.option("-y", "--yes", is_flag=True)
@click.option("--i-understand", "understand", is_flag=True, help="Required to apply a delete plan with --yes.")
@click.option("--verify-before", type=click.Choice(["hash", "bytes"]), default=None)
@click.option("--prune-empty-dirs", is_flag=True)
@handle_errors
def apply(plan_file: str, dry_run: bool, yes: bool, understand: bool, verify_before: str | None, prune_empty_dirs: bool) -> None:
    """Apply a saved plan, re-verifying every file at the last moment."""
    app = get_app()
    loaded = Plan.load(plan_file)
    if loaded.action is ActionType.DELETE and yes and not understand:
        raise ConfigError("delete plans with --yes also require --i-understand")
    plan_summary(app.console, loaded, top=10)
    if not loaded.items:
        app.console.print("Nothing to do.")
        return
    if not dry_run and not confirm_plan(app, loaded, yes):
        app.console.print("Not applied.")
        return
    options = ApplyOptions(verify=verify_before or loaded.verify or "hash",
                           confirm_delete=loaded.action is ActionType.DELETE, dry_run=dry_run,
                           prune_empty_dirs=prune_empty_dirs)
    if options.verify not in ("hash", "bytes"):
        options.verify = "hash"

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return apply_plan(loaded, app.registry, options=options, dirs=app.dirs, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    apply_summary(app.console, report)
    if report.count("failed"):
        sys.exit(1)


@cli.command("undo")
@click.argument("apply_id", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would be restored.")
@click.option("-y", "--yes", is_flag=True)
@handle_errors
def undo_cmd(apply_id: str | None, dry_run: bool, yes: bool) -> None:
    """Reverse an applied plan (default: the most recent). Quarantined files go back;
    hard links and clones become independent files again."""
    app = get_app()
    if not dry_run and not yes:
        preview = undo(apply_id, app.registry, dirs=app.dirs, dry_run=True)
        undo_summary(app.console, preview)
        if not preview.count("would-restore"):
            app.console.print("Nothing to restore.")
            return
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes")
        if not click.confirm(f"Restore {plural(preview.count('would-restore'), 'file')}?", default=True):
            return

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return undo(apply_id, app.registry, dirs=app.dirs, dry_run=dry_run, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    undo_summary(app.console, report)


@cli.command()
@click.option("-n", "--limit", type=int, default=20, show_default=True)
@handle_errors
def history(limit: int) -> None:
    """Every plan that was applied, with results and undo status."""
    app = get_app()
    journals = list_journals(app.dirs.journals)[:limit]
    if not journals:
        app.console.print("No changes have been applied yet.")
        return
    compact = app.console.width < 100
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    columns = ("Apply id", "Action", "Done", "Freed", "State") if compact else (
        "Apply id", "When", "Action", "Done", "Skipped", "Failed", "Undone", "Freed", "State")
    for col in columns:
        table.add_column(col, overflow="fold")
    for path in journals:
        s = summarize(path)
        state = "interrupted" if s.incomplete else ("undone" if s.count("undone") else "")
        values = {
            "Apply id": t(s.session), "When": t(str(s.header.get("created", ""))[:19].replace("T", " ")),
            "Action": t(s.header.get("action", "?")), "Done": str(s.count("done")), "Skipped": str(s.count("skipped")),
            "Failed": str(s.count("failed")), "Undone": str(s.count("undone")), "Freed": human_size(s.reclaimed),
            "State": t(state, "yellow" if s.incomplete else "dim"),
        }
        table.add_row(*(values[c] for c in columns))
    app.console.print(table)


@cli.group()
def quarantine() -> None:
    """List or purge quarantined files."""


@quarantine.command("list")
@handle_errors
def quarantine_list() -> None:
    app = get_app()
    sessions = list_quarantine(app.dirs)
    if not sessions:
        app.console.print("The quarantine is empty.")
        return
    table = Table(box=box.SIMPLE_HEAD)
    for col in ("Apply id", "When", "Files", "Size"):
        table.add_column(col)
    for s in sessions:
        table.add_row(t(s.apply_id), t(s.created[:19].replace("T", " ")), f"{s.files:,}", human_size(s.bytes))
    app.console.print(table)
    total = sum(s.bytes for s in sessions)
    app.console.print(Text(f"{human_size(total)} held. Restore with 'duplio undo ID'; free with 'duplio quarantine purge'.", style="dim"))


@quarantine.command("purge")
@click.argument("apply_id", required=False)
@click.option("--older-than", default=None, help="Only quarantines older than this, e.g. 30d.")
@click.option("--dry-run", is_flag=True)
@click.option("-y", "--yes", is_flag=True)
@handle_errors
def quarantine_purge(apply_id: str | None, older_than: str | None, dry_run: bool, yes: bool) -> None:
    """Permanently delete quarantined files."""
    app = get_app()
    files, nbytes, _ = purge_quarantine(app.registry, apply_id=apply_id, older_than=older_than, dirs=app.dirs, dry_run=True)
    if not files:
        app.console.print("Nothing to purge.")
        return
    app.console.print(Text(f"{plural(files, 'quarantined file')} ({human_size(nbytes)}) would be deleted permanently.", style="bold"))
    if dry_run:
        return
    if not yes:
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes")
        if not click.confirm("Delete them permanently?", default=False):
            return
    files, nbytes, errors = purge_quarantine(app.registry, apply_id=apply_id, older_than=older_than, dirs=app.dirs)
    app.console.print(Text(f"Purged {plural(files, 'file')}, freed {human_size(nbytes)}.", style="green"))
    for err in errors[:20]:
        app.console.print(Text(err, style="red"))


@cli.command()
@handle_errors
def recover() -> None:
    """Check for operations interrupted by a crash or power loss."""
    app = get_app()
    findings = inspect_incomplete(app.registry, app.dirs)
    if not findings:
        app.console.print(Text("No interrupted operations. ✓", style="green"))
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Journal", "Operation", "File", "State"):
        table.add_column(col)
    for path, op, state in findings:
        table.add_row(t(path.stem), t(op.intent.get("op", "")), t(op.intent.get("src", "")), t(state, "yellow"))
    app.console.print(table)
    app.console.print(Text("Quarantined originals can be restored with 'duplio undo ID'.", style="dim"))


@cli.command()
@click.option("-n", "--limit", type=int, default=20, show_default=True)
@handle_errors
def sessions(limit: int) -> None:
    """Saved scans, newest first (use their id with 'duplio plan')."""
    app = get_app()
    found = list_sessions(app.dirs)[:limit]
    if not found:
        app.console.print("No saved scans.")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Session", "When", "Mode", "Groups", "Reclaimable", "Locations"):
        table.add_column(col, no_wrap=col != "Locations")
    for s in found:
        table.add_row(t(s.session_id), t(s.created[:19].replace("T", " ")), t(s.mode), f"{s.groups:,}",
                      human_size(s.wasted), t("\n".join(s.roots)))
    app.console.print(table)
