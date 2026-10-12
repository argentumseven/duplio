"""manifest · versions — prove archives are intact, and tidy old versions."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click
from rich import box
from rich.table import Table
from rich.text import Text

from duplio.backup.manifest import Manifest, create_manifest, verify_manifest
from duplio.backup.versions import list_versions, prune_versions, select_for_pruning
from duplio.cli.app import SIZE, cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.hashing.algorithms import full_hash_choices
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import format_ns, human_size, plural


@cli.group()
def manifest() -> None:
    """Checksum manifests: detect silent corruption and missing files in an archive."""


@manifest.command("create")
@click.argument("folder")
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None,
              help="Where to write the manifest (default: ./duplio-manifest-<folder>-<date>.json).")
@click.option("--hash", "algorithm", type=click.Choice(full_hash_choices()), default=None,
              help="Content hash (default: blake3 if installed, else blake2b).")
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("--min-size", type=SIZE, default=0, show_default=True)
@handle_errors
def manifest_create(folder: str, output: str | None, algorithm: str | None, exclude: tuple[str, ...],
                    min_size: int) -> None:
    """Record the size, time and hash of every file in FOLDER."""
    app = get_app()
    loc = app.registry.open(folder)
    flt = ScanFilter.for_backup(exclude=list(app.settings.exclude) + list(exclude))
    flt.min_size = min_size

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return create_manifest(loc, scan_filter=flt, algorithm=algorithm, progress=progress, cancel=cancel)

    result = run_cancellable(work)
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (loc.backend.basename(loc.path) or "root"))
    target = Path(output) if output else Path(f"duplio-manifest-{name}-{time.strftime('%Y%m%d')}.json")
    result.save(target)
    total = sum(e.size for e in result.entries)
    app.console.print(Text.assemble(("Manifest written: ", "green"), t(str(target)),
                                    f"  ({plural(len(result.entries), 'file')}, {human_size(total)}, {result.algorithm})"))
    for rel, err in result.errors[:10]:
        app.console.print(t(f"  could not read {rel}: {err}", "yellow"))
    if result.errors:
        app.console.print(t(f"{len(result.errors)} file(s) are not in the manifest.", "yellow"))
        sys.exit(1)


@manifest.command("verify")
@click.argument("folder")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--quick", is_flag=True, help="Trust size and time instead of reading the data (cannot find bit rot).")
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("--limit", type=int, default=15, show_default=True, help="Files to list per category.")
@handle_errors
def manifest_verify(folder: str, manifest_file: str, quick: bool, exclude: tuple[str, ...], limit: int) -> None:
    """Check FOLDER against a manifest; exit status 1 if anything is missing, damaged or edited."""
    app = get_app()
    loc = app.registry.open(folder)
    loaded = Manifest.load(manifest_file)
    flt = ScanFilter.for_backup(exclude=list(app.settings.exclude) + list(exclude))

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return verify_manifest(loc, loaded, scan_filter=flt, quick=quick, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    table = Table(box=box.SIMPLE_HEAD, show_header=False)
    table.add_column(no_wrap=True)
    table.add_column(justify="right")
    styles = {"ok": "green", "touched": "cyan", "new": "cyan", "modified": "yellow", "corrupt": "bold red",
              "missing": "red", "unreadable": "red"}
    for status in ("ok", "touched", "new", "modified", "corrupt", "missing", "unreadable"):
        n = report.count(status)
        if n or status == "ok":
            table.add_row(Text(status, style=styles[status]), f"{n:,}")
    app.console.print(table)
    for status in ("corrupt", "missing", "modified", "unreadable", "new"):
        items = report.select(status)
        for item in items[:limit]:
            app.console.print(Text.assemble((f"{status:>10}  ", styles[status]), t(item.rel),
                                            (f"  {item.detail}" if item.detail else "", "dim")))
        if len(items) > limit:
            app.console.print(Text(f"{'':>10}  … {len(items) - limit:,} more {status}", style="dim"))
    if report.clean:
        app.console.print(Text("Everything in the manifest is intact. ✓", style="bold green"))
    else:
        app.console.print(Text("The folder does not match the manifest.", style="bold red"))
        sys.exit(1)


@cli.group()
def versions() -> None:
    """The old copies kept by mirror, merge and sync (.duplio-versions)."""


@versions.command("list")
@click.argument("location")
@handle_errors
def versions_list(location: str) -> None:
    """Show every kept run in LOCATION with its size."""
    app = get_app()
    runs = list_versions(app.registry.open(location))
    if not runs:
        app.console.print("No old versions are kept in this location.")
        return
    table = Table(box=box.SIMPLE_HEAD)
    for col, justify in (("Run", "left"), ("When (UTC)", "left"), ("Files", "right"), ("Size", "right")):
        table.add_column(col, justify=justify)  # type: ignore[arg-type]
    for r in runs:
        when = format_ns(int(r.when * 1e9)) if r.when else "?"
        table.add_row(t(r.name), t(when), f"{r.files:,}", human_size(r.bytes))
    app.console.print(table)
    app.console.print(Text(f"{sum(r.files for r in runs):,} files, {human_size(sum(r.bytes for r in runs))} in total. "
                           "Prune with: duplio versions prune LOCATION --older-than 90d --apply", style="dim"))


@versions.command("prune")
@click.argument("location")
@click.option("--older-than", default=None, metavar="AGE", help="Remove runs older than this, e.g. 90d.")
@click.option("--keep-last", type=click.IntRange(1), default=None, help="Always keep this many newest runs.")
@click.option("--apply", "do_apply", is_flag=True, help="Actually delete (default: only show what would go).")
@click.option("-y", "--yes", is_flag=True)
@handle_errors
def versions_prune(location: str, older_than: str | None, keep_last: int | None, do_apply: bool, yes: bool) -> None:
    """Permanently delete old kept versions. This cannot be undone."""
    app = get_app()
    loc = app.registry.open(location)
    chosen = select_for_pruning(list_versions(loc), older_than=older_than, keep_last=keep_last)
    if not chosen:
        app.console.print("Nothing to prune.")
        return
    files, size = sum(r.files for r in chosen), sum(r.bytes for r in chosen)
    for r in chosen[:20]:
        app.console.print(Text.assemble(("  ", ""), t(r.name), (f"  {r.files:,} files, {human_size(r.bytes)}", "dim")))
    app.console.print(Text(f"{plural(len(chosen), 'run')}: {plural(files, 'file')}, {human_size(size)}"
                           + ("" if do_apply else "  (preview; add --apply to delete)"), style="bold"))
    if not do_apply:
        return
    if not yes:
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes")
        if not click.confirm("Permanently delete these old versions?", default=False):
            return
    prune_versions(loc, older_than=older_than, keep_last=keep_last)
    app.console.print(Text(f"Removed {plural(len(chosen), 'run')}, freeing {human_size(size)}.", style="green"))
