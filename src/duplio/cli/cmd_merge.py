"""merge — bring over what the other folder lacks, skipping everything it already has."""

from __future__ import annotations

import shlex
import sys
import time
from typing import Any

import click
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.backup.merge import (
    CONFLICT_POLICIES,
    LAYOUT_TOKENS,
    NAME_POLICIES,
    MergeOptions,
    MergePlan,
    MergeReport,
    plan_merge,
    run_merge,
)
from duplio.cli.app import cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.cmd_diff import compare_options, do_diff
from duplio.cli.cmd_drive import guard_destination, guard_options, make_limiter
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.util.fmt import human_duration, human_size, plural

ACTION_MARK = {"copy": ("+", "green"), "rename": ("≈", "yellow"), "replace": ("↻", "magenta")}


def plan_panel(plan: MergePlan) -> Panel:
    opts = plan.options
    rows: list[Text] = []
    copies, renames, replaces = plan.count("copy"), plan.count("rename"), plan.count("replace")
    rows.append(Text.assemble((f"{copies:,}", "bold green"), f" new file{'s' if copies != 1 else ''} to copy"))
    if renames:
        rows.append(Text.assemble((f"{renames:,}", "bold yellow"), " different file(s) at the same path: both will be kept"))
    if replaces:
        rows.append(Text.assemble((f"{replaces:,}", "bold magenta"),
                                  " file(s) replaced by the newer/chosen version (old ones kept in .duplio-versions)"))
    present, moved = plan.skipped_count("present"), plan.skipped_count("moved")
    if present or moved:
        rows.append(Text(f"{present:,} already there at the same path · {moved:,} already there under another name or folder",
                         style="dim"))
    for reason, label in (("duplicate-in-source", "identical to another file being copied (copied once)"),
                          ("conflict", "left alone: a different file is already at the path (--on-conflict skip)"),
                          ("dest-newer", "left alone: the destination's version is newer"),
                          ("name", "left out: the destination cannot hold the name (--names skip)"),
                          ("unreadable", "could not be read")):
        n = plan.skipped_count(reason)
        if n:
            rows.append(Text(f"{n:,} {label}", style="red" if reason == "unreadable" else "dim"))
    if opts.move:
        rows.append(Text("After each copy is verified, the source file is moved to the quarantine (undoable).",
                         style="cyan"))
    for w in plan.warnings:
        rows.append(Text(w, style="yellow"))
    rows.append(Text(f"Total to transfer: {human_size(plan.bytes_to_copy)}", style="bold"))
    title = Text.assemble(("Merge ", "bold"), t(plan.src.location.display), " → ", t(plan.dest.location.display))
    return Panel(Group(*rows), title=title, title_align="left", border_style="cyan")


def actions_table(plan: MergePlan, limit: int) -> Table | None:
    if not plan.actions:
        return None
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("", no_wrap=True, width=2)
    table.add_column("From", ratio=2, overflow="fold")
    table.add_column("To (in destination)", ratio=2, overflow="fold")
    table.add_column("Size", justify="right", no_wrap=True)
    for a in plan.actions[:limit]:
        mark, style = ACTION_MARK[a.kind]
        target = Text.assemble(t(a.dest_rel), ("  " + a.note, "dim") if a.note else "")
        table.add_row(Text(mark, style=style), t(a.entry.rel), target, human_size(a.entry.size))
    if len(plan.actions) > limit:
        table.add_row("", Text(f"… {len(plan.actions) - limit:,} more", style="dim"), "", "")
    return table


def result_panel(report: MergeReport, plan: MergePlan, seconds: float) -> Panel:
    rows = [Text.assemble((f"{report.count('done'):,}", "bold green"), f" copied · {human_size(report.bytes_copied)} in "
                          f"{human_duration(seconds)}")]
    if report.count("skipped"):
        rows.append(Text(f"{report.count('skipped'):,} skipped (changed or removed since the comparison)", style="yellow"))
    if report.count("failed"):
        rows.append(Text(f"{report.count('failed'):,} failed", style="bold red"))
    if report.cancelled:
        rows.append(Text("Cancelled: files already copied stay copied; nothing is half-written.", style="yellow"))
    if report.moved is not None:
        mv = report.moved
        rows.append(Text(f"{mv.count('done'):,} source files moved out (quarantined after verification)", style="cyan"))
        if mv.count("failed") or mv.count("skipped"):
            rows.append(Text(f"{mv.count('skipped'):,} not moved (changed) · {mv.count('failed'):,} could not be moved",
                             style="yellow"))
    if report.journal is not None:
        rows.append(Text.assemble(("Undo everything with: ", "dim"), (f"duplio undo {report.apply_id}", "bold")))
    ok = not report.count("failed") and not report.cancelled
    return Panel(Group(*rows), title="Merge complete" if ok else "Merge finished with problems",
                 title_align="left", border_style="green" if ok else "yellow")


@cli.command("merge")
@click.argument("source")
@click.argument("dest")
@compare_options
@click.option("--move", is_flag=True,
              help="Remove each source file after it is verified in DEST (to the quarantine; 'duplio undo' brings it back).")
@click.option("--clean-source", is_flag=True,
              help="With --move: also remove source files that are already in DEST (verified by content).")
@click.option("--on-conflict", type=click.Choice(CONFLICT_POLICIES), default="rename", show_default=True,
              help="A different file already at the same path: rename = keep both · skip · newer = take the newer "
                   "(old kept) · replace = take SOURCE's (old kept).")
@click.option("--layout", default="{rel}", show_default=True,
              help="Where files go in DEST. Tokens: " + " ".join(f"{{{k}}}" for k in LAYOUT_TOKENS) + "  e.g. '{year}/{month}/{name}'.")
@click.option("--names", type=click.Choice(NAME_POLICIES), default="auto", show_default=True,
              help="Names DEST cannot store (: ? * trailing dot, CON…): auto/fix = rewrite safely · skip · abort · keep.")
@click.option("--keep-dupes", is_flag=True, help="Copy every identical file of SOURCE, not just one of each.")
@click.option("--no-verify", is_flag=True, help="Do not read copies back to check them (not allowed with --move).")
@click.option("--jobs", type=click.IntRange(1, 16), default=None, help="Parallel copies (default: automatic).")
@click.option("--tag", default="", help="Label for kept-both names (default: the source folder's name).")
@click.option("--limit", type=int, default=20, show_default=True, help="Planned actions to list.")
@click.option("--dry-run", is_flag=True, help="Show what would happen; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before replacing or moving files.")
@guard_options
@handle_errors
def merge_cmd(source: str, dest: str, move: bool, clean_source: bool, on_conflict: str, layout: str, names: str,
              keep_dupes: bool, no_verify: bool, jobs: int | None, tag: str, limit: int, dry_run: bool, yes: bool,
              bwlimit: str | None, expect_mounted: bool | None, sentinel: str | None, **kw: Any) -> None:
    """Copy into DEST everything from SOURCE that DEST does not already have.

    Files are compared by content, so a photo that is already in DEST under another
    name or in another folder is not copied again. Nothing in DEST is overwritten
    silently, every copy is verified, and the whole run can be undone.

    \b
    duplio merge ~/Pictures/Phone-dump smb://me@nas/photos --dry-run
    duplio merge /media/usb/old-laptop ~/Documents --on-conflict newer
    duplio merge adb:/sdcard/DCIM ~/Photos --layout '{year}/{month}/{name}' --move
    """
    app = get_app()
    dst_loc = app.registry.open(dest)
    guard_destination(app, dst_loc, expect_mounted=expect_mounted, sentinel=sentinel)
    options = MergeOptions(
        move=move, clean_source=clean_source, on_conflict=on_conflict, layout=layout, names=names,
        keep_dupes=keep_dupes, verify=not no_verify, verify_bytes=kw.get("verify") == "bytes", jobs=jobs,
        limiter=make_limiter(bwlimit), algorithm=kw.get("algorithm"), expect_mounted=expect_mounted, sentinel=sentinel,
        tag=tag,
    )
    options.validate()
    result = do_diff(app, source, dest, kw)
    plan = plan_merge(result, options)
    app.console.print(plan_panel(plan))
    table = actions_table(plan, limit)
    if table is not None:
        app.console.print(table)
    if not plan.has_work:
        if plan.skipped_count("unreadable"):
            app.console.print(Text("Some files could not be read, so nothing further was done for them.", style="red"))
            sys.exit(1)
        app.console.print(Text("Nothing to copy: DEST already has all of SOURCE's content. ✓", style="green"))
        return
    if dry_run:
        app.console.print(Text(f"Dry run: nothing was changed. To do it:  duplio merge {shlex.quote(source)} "
                               f"{shlex.quote(dest)} ...  (same options, without --dry-run)", style="dim"))
        return
    if plan.disruptive and not yes:
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes (or use --dry-run to preview)")
        what = []
        if plan.count("replace"):
            what.append(f"{plural(plan.count('replace'), 'file')} in the destination will be replaced (old versions kept)")
        if move:
            what.append("source files will be moved out after verification")
        if not click.confirm("; ".join(what).capitalize() + ". Continue?", default=False):
            return

    started = time.monotonic()

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return run_merge(plan, app.registry, dirs=app.dirs, progress=progress, cancel=cancel)

    report: MergeReport = run_cancellable(work)
    app.console.print(result_panel(report, plan, time.monotonic() - started))
    for r in report.failures[:20]:
        app.console.print(t(f"{r.action.entry.rel}: {r.message}", "red"))
    if report.count("failed") or (report.moved is not None and report.moved.count("failed")):
        sys.exit(1)
