"""notify · watch — automation helpers."""

from __future__ import annotations

import sys
import time
from typing import Any

import click
from rich.text import Text

from duplio.cli.app import cli, get_app, handle_errors
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.events import CancelToken
from duplio.notify import STATUSES, Notification, send, validate_target
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import format_ts, parse_duration
from duplio.watch import tree_fingerprint, watch


@cli.command("notify")
@click.argument("targets", nargs=-1, required=True)
@click.option("--title", default="Duplio test", show_default=True)
@click.option("--status", type=click.Choice(STATUSES), default="ok", show_default=True)
@click.option("--message", default="This is a test notification from Duplio.", show_default=True)
@handle_errors
def notify_cmd(targets: tuple[str, ...], title: str, status: str, message: str) -> None:
    """Send a notification (to test a target, or from your own scripts).

    \b
    duplio notify webhook:https://hooks.example.com/abc
    duplio notify email:me@example.com --status failed --message "Backup did not run"
    duplio notify 'command:notify-send "$DUPLIO_TITLE" "$DUPLIO_MESSAGE"'
    """
    app = get_app()
    for target in targets:
        validate_target(target)
    failed = 0
    for label, error in send(list(targets), Notification(title, status, message), smtp=app.settings.notify_smtp):
        if error:
            failed += 1
            app.console.print(Text.assemble(("failed  ", "red"), t(label), (f"  {error}", "dim")))
        else:
            app.console.print(Text.assemble(("sent    ", "green"), t(label)))
    if failed:
        sys.exit(1)


@cli.command("watch")
@click.argument("source")
@click.argument("dest")
@click.option("--action", "job", type=click.Choice(["mirror", "merge", "sync"]), default="mirror", show_default=True,
              help="mirror = keep DEST an exact copy · merge = copy over what DEST lacks · sync = two-way.")
@click.option("--interval", default="30s", show_default=True, help="How often to look for changes, e.g. 30s or 5m.")
@click.option("--settle", default="5s", show_default=True, help="Wait until nothing changed for this long before acting.")
@click.option("--delete", "delete_extraneous", is_flag=True, help="mirror: also retire files missing from SOURCE.")
@click.option("--now/--no-now", "run_first", default=True, help="Run once immediately, before waiting for changes.")
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("--max-runs", type=int, default=None, hidden=True)
@click.pass_context
@handle_errors
def watch_cmd(ctx: click.Context, source: str, dest: str, job: str, interval: str, settle: str,
              delete_extraneous: bool, run_first: bool, exclude: tuple[str, ...], max_runs: int | None) -> None:
    """Keep DEST up to date: run a job whenever SOURCE changes (Ctrl-C to stop).

    \b
    duplio watch ~/Documents smb://me@nas/docs --action mirror
    duplio watch ~/Music /media/usb/Music --action sync --interval 5m
    """
    from duplio.cli import cmd_backup, cmd_merge, cmd_sync

    try:
        poll, quiet_for = parse_duration(interval), parse_duration(settle)
    except ValueError as exc:
        raise ConfigError(f"not a duration: {exc} (examples: 30s, 5m)") from exc
    if poll < 1:
        raise ConfigError("--interval must be at least 1 second")
    app = get_app()
    src, dst = app.registry.open(source), app.registry.open(dest)
    flt = ScanFilter.for_backup(exclude=list(app.settings.exclude) + list(exclude))
    watched = [src] + ([dst] if job == "sync" else [])
    cancel = CancelToken()
    app.quiet = True  # no progress bars on every pass; each run still prints its summary

    def fingerprint() -> Any:
        return tuple(tree_fingerprint(loc, flt) for loc in watched)

    def run_once() -> bool:
        app.console.print(Text(f"── {format_ts(time.time())}  running {job}", style="bold cyan"))
        try:
            if job == "mirror":
                ctx.invoke(cmd_backup.mirror, source=source, dest=dest, delete_extraneous=delete_extraneous,
                           checksum=False, no_verify=False, keep_versions=None, exclude=exclude, max_change=0.5,
                           force=False, dry_run=False, yes=True)
            elif job == "merge":
                ctx.invoke(cmd_merge.merge_cmd, source=source, dest=dest, dry_run=False, yes=True, exclude=exclude)
            else:
                ctx.invoke(cmd_sync.sync_cmd, a=source, b=dest, dry_run=False, yes=True, exclude=exclude)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 0
            if code:
                app.console.print(Text(f"The run ended with status {code}; watching continues.", style="yellow"))
            return code == 0
        return True

    app.console.print(Text.assemble(("Watching ", "bold"), t(src.display), (f" every {interval}; {job} → ", ""),
                                    t(dst.display), ("   (Ctrl-C stops)", "dim")))
    try:
        stats = watch(fingerprint, run_once, interval=poll, settle=quiet_for, cancel=cancel, run_first=run_first,
                      max_runs=max_runs)
    except KeyboardInterrupt:
        cancel.cancel()
        stats = None
    if stats is not None:
        app.console.print(Text(f"Stopped after {stats.runs} run(s), {stats.failures} with problems.", style="dim"))
    else:
        app.console.print(Text("Stopped.", style="dim"))
