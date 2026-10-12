"""probe · sentinel — find out how a drive behaves and mark it so it cannot be confused."""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from typing import Any

import click
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.cli.app import cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.context import AppContext
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.util.fmt import human_size
from duplio.util.text import json_dumps
from duplio.util.throttle import RateLimiter, limiter_for, parse_rate
from duplio.vfs.guards import (
    DEFAULT_SENTINEL,
    check_destination,
    is_really_mounted,
    looks_like_mount_point,
    write_sentinel,
)
from duplio.vfs.location import Location
from duplio.vfs.probe import probe_location


def guard_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--bwlimit``, ``--expect-mounted`` and ``--sentinel`` for commands that write to a destination."""
    options = [
        click.option("--bwlimit", default=None, metavar="RATE",
                     help="Cap the transfer speed, e.g. 5MB/s or 800k (keeps a home network usable)."),
        click.option("--expect-mounted/--no-expect-mounted", default=None,
                     help="Refuse to write unless the destination is a really mounted drive. "
                          "Default: checked for paths under /mnt, /media and /Volumes."),
        click.option("--sentinel", default=None, is_flag=False, flag_value=DEFAULT_SENTINEL, metavar="[NAME]",
                     help=f"Refuse to write unless this marker file exists in the destination "
                          f"(create it with 'duplio sentinel create'; default name {DEFAULT_SENTINEL})."),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def make_limiter(bwlimit: str | None) -> RateLimiter | None:
    return limiter_for(parse_rate(bwlimit))


def guard_destination(app: AppContext, location: Location, *, expect_mounted: bool | None, sentinel: str | None,
                      needed_bytes: int = 0, probe_timeout: float | None = 15.0) -> None:
    """Run the destination checks; raises a :class:`~duplio.vfs.guards.GuardError` that stops the command."""
    report = check_destination(location, expect_mounted=expect_mounted, sentinel=sentinel,
                               needed_bytes=needed_bytes, probe_timeout=probe_timeout)
    for warning in report.warnings:
        app.console.print(t(warning, "yellow"))


# ---------------------------------------------------------------------------


def _yes_no(value: bool | None, yes_text: str, no_text: str) -> Text:
    if value is None:
        return Text("unknown", style="dim")
    return Text(yes_text if value else no_text, style="green" if value else "yellow")


def _resolution(ns: int | None) -> str:
    if ns is None:
        return "unknown"
    if ns >= 1_000_000_000:
        return f"{ns // 1_000_000_000} s"
    if ns >= 1_000_000:
        return f"{ns // 1_000_000} ms"
    if ns >= 1_000:
        return f"{ns // 1_000} µs"
    return f"{ns} ns"


@cli.command()
@click.argument("location")
@click.option("--no-speed", is_flag=True, help="Skip the throughput test.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@handle_errors
def probe(location: str, no_speed: bool, as_json: bool) -> None:
    """Test how a drive really behaves (names, timestamps, speed) before trusting it with a backup.

    Writes a few tiny files into a temporary folder inside LOCATION and removes them again.

    \b
    duplio probe smb://me@nas/backups
    duplio probe /mnt/nas
    duplio probe adb:/sdcard
    """
    app = get_app()
    app.json_output = as_json
    loc = app.registry.open(location)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return probe_location(loc, throughput_mib=0 if no_speed else 8, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    if as_json:
        click.echo(json_dumps(dataclasses.asdict(report), indent=1))
        return
    table = Table(box=box.SIMPLE_HEAD, show_header=False, expand=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Type", t(f"{report.label} [{report.kind}]"))
    table.add_row("Writable", _yes_no(report.writable, "yes", "NO"))
    if report.free_bytes is not None:
        table.add_row("Free space", t(human_size(report.free_bytes)))
    if report.latency_ms is not None:
        table.add_row("Latency", t(f"{report.latency_ms:.2f} ms per request"))
    if report.write_mib_s is not None and report.read_mib_s is not None:
        table.add_row("Speed", t(f"write {report.write_mib_s:.0f} MiB/s · read {report.read_mib_s:.0f} MiB/s"))
    table.add_row("Names", _yes_no(report.case_sensitive, "case-sensitive", "case-INSENSITIVE"))
    table.add_row("Accents", _yes_no(None if report.normalization_insensitive is None
                                     else not report.normalization_insensitive, "exact", "Unicode-folded"))
    table.add_row("Timestamps", t(_resolution(report.mtime_resolution_ns)))
    if report.rejected_chars:
        table.add_row("Rejected characters", Text(" ".join(report.rejected_chars), style="yellow"))
    table.add_row("Trailing dots", _yes_no(None if report.strips_trailing_dots is None
                                           else not report.strips_trailing_dots, "kept", "dropped/refused"))
    if report.max_name_ok is not None:
        table.add_row("Longest name", t(f"{report.max_name_ok} characters"))
    table.add_row("Hard links", _yes_no(report.hardlinks, "supported", "not supported"))
    app.console.print(Panel(table, title=t(report.location), border_style="cyan"))
    for note in report.notes:
        app.console.print(t(f"note: {note}", "dim"))
    for line in report.recommendations:
        app.console.print(Text.assemble(("• ", "cyan"), t(line)))
    if not report.writable:
        sys.exit(1)


@cli.group()
def sentinel() -> None:
    """Marker files that prove a destination is the drive you think it is."""


@sentinel.command("create")
@click.argument("location")
@click.option("--name", default=DEFAULT_SENTINEL, show_default=True)
@handle_errors
def sentinel_create(location: str, name: str) -> None:
    """Put a marker file in LOCATION. Later: duplio mirror ... --sentinel refuses to run without it."""
    app = get_app()
    loc = app.registry.open(location)
    mounted, why = is_really_mounted(loc)
    if not mounted and looks_like_mount_point(loc):
        raise ConfigError(f"{loc.display} is not a mounted drive ({why}); mount it first, or the marker would be "
                          "written to the wrong disk")
    try:
        path = write_sentinel(loc, name)
    except FileExistsError:
        app.console.print(t(f"{name} already exists in {loc.display}.", "yellow"))
        return
    app.console.print(Text.assemble(("Created ", "green"), t(loc.backend.display(path))))


@sentinel.command("check")
@click.argument("location")
@click.option("--name", default=DEFAULT_SENTINEL, show_default=True)
@handle_errors
def sentinel_check(location: str, name: str) -> None:
    """Exit 0 if the marker file is present in LOCATION."""
    app = get_app()
    loc = app.registry.open(location)
    check_destination(loc, expect_mounted=False, sentinel=name)
    app.console.print(Text.assemble(("Marker present in ", "green"), t(loc.display)))
