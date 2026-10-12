"""snapshot — browsable hard-link snapshots of a folder."""

from __future__ import annotations

import sys
import time
from typing import Any

import click
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.backup.retention import RetentionPolicy, apply_policy
from duplio.backup.snapshots import SnapshotSet
from duplio.cli.app import cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.cmd_drive import guard_destination, guard_options, make_limiter
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import human_duration, human_size, parse_duration, plural


@cli.group()
def snapshot() -> None:
    """Time-machine style snapshots: plain folders where unchanged files are hard links."""


@snapshot.command("create")
@click.argument("source")
@click.argument("dest")
@click.option("-l", "--label", default="", help="Name this series (several sources can share one DEST).")
@click.option("-t", "--tag", "tags", multiple=True)
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("-i", "--include", multiple=True, metavar="GLOB")
@click.option("--cross-filesystems", is_flag=True)
@click.option("--verify-links", is_flag=True,
              help="Before hard-linking an unchanged-looking file, compare its content with the previous snapshot's "
                   "copy (reads both; catches edits that kept the size and time).")
@guard_options
@handle_errors
def snapshot_create(source: str, dest: str, label: str, tags: tuple[str, ...], exclude: tuple[str, ...],
                    include: tuple[str, ...], cross_filesystems: bool, verify_links: bool, bwlimit: str | None,
                    expect_mounted: bool | None, sentinel: str | None) -> None:
    """Add a snapshot of SOURCE to DEST (a local or hard-link capable drive).

    \b
    duplio snapshot create ~/Documents /media/usb/snapshots --label documents
    """
    app = get_app()
    src, dst = app.registry.open(source), app.registry.open(dest)
    guard_destination(app, dst, expect_mounted=expect_mounted, sentinel=sentinel)
    flt = ScanFilter.for_backup(include=list(include), exclude=list(app.settings.exclude) + list(exclude))
    snaps = SnapshotSet(dst)
    started = time.monotonic()

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return snaps.create(src, scan_filter=flt, label=label, tags=list(tags),
                                one_file_system=not cross_filesystems, limiter=make_limiter(bwlimit), verify_links=verify_links,
                                progress=progress, cancel=cancel)

    report = run_cancellable(work)
    m = report.meta
    rows = [
        Text.assemble(("Snapshot ", "dim"), (report.name, "bold")),
        Text(f"{m.files:,} files · {human_size(m.bytes)}  —  {report.linked:,} unchanged (hard-linked, no extra space), "
             f"{report.copied:,} new or changed ({human_size(m.new_bytes)} written) in "
             f"{human_duration(time.monotonic() - started)}"),
    ]
    if report.changed_during:
        rows.append(Text(f"{len(report.changed_during):,} files changed while being copied; they are kept as read but "
                         "flagged, and will be copied again next time:", style="yellow"))
        rows.extend(Text(f"  {rel}", style="yellow") for rel in report.changed_during[:10])
        rows.append(Text("For a consistent copy of live data (databases, VM disks) snapshot the file system first "
                         "and copy from that (see the README, 'Files that change while they are backed up').", style="dim"))
    for rel, err in report.errors[:10]:
        rows.append(Text(f"could not copy {rel}: {err}", style="yellow"))
    from rich.console import Group

    clean = not report.errors and not report.changed_during
    app.console.print(Panel(Group(*rows), title="Snapshot complete" if clean else "Snapshot finished with problems",
                            border_style="green" if clean else "yellow"))
    if not clean:
        sys.exit(1)


@snapshot.command("list")
@click.argument("dest")
@handle_errors
def snapshot_list(dest: str) -> None:
    """Show the snapshots in DEST."""
    app = get_app()
    found = SnapshotSet(app.registry.open(dest)).snapshots()
    if not found:
        app.console.print("No snapshots here.")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col, justify in (("Folder", "left"), ("Source", "left"), ("Label", "left"), ("Files", "right"),
                         ("Size", "right"), ("New data", "right")):
        table.add_column(col, justify=justify, overflow="fold")  # type: ignore[arg-type]
    for name, m in found:
        table.add_row(t(name), t(m.source), t(m.label), f"{m.files:,}", human_size(m.bytes), human_size(m.new_bytes))
    app.console.print(table)


@snapshot.command("forget")
@click.argument("dest")
@click.option("--keep-last", type=int, default=0)
@click.option("--keep-hourly", type=int, default=0)
@click.option("--keep-daily", type=int, default=0)
@click.option("--keep-weekly", type=int, default=0)
@click.option("--keep-monthly", type=int, default=0)
@click.option("--keep-yearly", type=int, default=0)
@click.option("--keep-within", default=None, help="Keep everything newer than this, e.g. 14d.")
@click.option("--keep-tag", multiple=True)
@click.option("--apply", "do_apply", is_flag=True, help="Actually delete (default: only show the decision).")
@click.option("-y", "--yes", is_flag=True)
@handle_errors
def snapshot_forget(dest: str, do_apply: bool, yes: bool, **kw: Any) -> None:
    """Thin out old snapshots with a retention policy. Deleting a snapshot never harms the others."""
    app = get_app()
    snaps = SnapshotSet(app.registry.open(dest))
    listed = snaps.snapshots()
    try:
        keep_within = parse_duration(kw["keep_within"]) if kw.get("keep_within") else 0.0
    except ValueError as exc:
        raise ConfigError(f"not a duration: {kw['keep_within']!r}") from exc
    policy = RetentionPolicy(
        keep_last=kw["keep_last"], keep_hourly=kw["keep_hourly"], keep_daily=kw["keep_daily"],
        keep_weekly=kw["keep_weekly"], keep_monthly=kw["keep_monthly"], keep_yearly=kw["keep_yearly"],
        keep_within_s=keep_within, keep_tags=list(kw.get("keep_tag") or []),
    )
    decision = apply_policy([m for _, m in listed], policy)
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Snapshot", "Label", "Decision"):
        table.add_column(col, overflow="fold")
    for meta, reasons in decision.keep:
        table.add_row(t(meta.id), t(meta.label), t("keep: " + ", ".join(reasons), "green"))
    for meta in decision.remove:
        table.add_row(t(meta.id), t(meta.label), t("forget", "red"))
    app.console.print(table)
    if not decision.remove:
        app.console.print("Nothing to forget.")
        return
    if not do_apply:
        app.console.print(Text(f"Dry run: {plural(len(decision.remove), 'snapshot')} would be deleted. Add --apply.",
                               style="yellow"))
        return
    if not yes:
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes")
        if not click.confirm(f"Permanently delete {plural(len(decision.remove), 'snapshot')}?", default=False):
            return
    freed = sum(snaps.forget(meta.id) for meta in decision.remove)
    app.console.print(Text(f"Deleted {plural(len(decision.remove), 'snapshot')}; freed {human_size(freed)}.", style="green"))
