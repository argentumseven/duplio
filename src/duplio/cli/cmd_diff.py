"""diff — compare two folders by content, whatever the files are called."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from typing import Any

import click
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.cli.app import (
    SIZE,
    build_filter,
    cli,
    get_app,
    handle_errors,
    progress_for,
    run_cancellable,
)
from duplio.cli.context import AppContext
from duplio.cli.render import t
from duplio.compare.diff import (
    KIND_TITLE,
    Basis,
    DiffItem,
    DiffOptions,
    DiffResult,
    Kind,
    diff_inventory,
)
from duplio.compare.model import Verify
from duplio.errors import ConfigError
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import format_ns, human_size, parse_duration, plural
from duplio.util.text import fs_bytes

KIND_STYLE = {Kind.SAME: "green", Kind.MOVED: "cyan", Kind.CHANGED: "yellow", Kind.ONLY: "red3",
              Kind.UNREADABLE: "bold red"}


def compare_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Options shared by every command that compares two locations (diff, merge, sync)."""
    options = [
        click.option("--basis", type=click.Choice([b.value for b in Basis]), default="content", show_default=True,
                     help="content = compare what is inside (finds renames) · time-size = same path, size and time · "
                          "size = same path and size."),
        click.option("--verify", "verify", type=click.Choice(["hash", "bytes"]), default="hash", show_default=True,
                     help="hash = cryptographic hash · bytes = hash, then byte-for-byte confirmation."),
        click.option("--hash", "algorithm", default=None, help="Content hash (default: blake3 if installed, else blake2b)."),
        click.option("--moves/--no-moves", "moves", default=True,
                     help="With time-size/size: also read files that did not match, to find renamed and moved ones."),
        click.option("--time-tolerance", default=None, metavar="DURATION",
                     help="Treat modification times this close as equal, e.g. 2s (default: what the two drives can store)."),
        click.option("--ignore-dst", is_flag=True, help="Treat an exact one-hour time difference as equal (FAT/NTFS vs Unix)."),
        click.option("-i", "--include", multiple=True, metavar="GLOB", help="Only compare matching files (repeatable)."),
        click.option("-x", "--exclude", multiple=True, metavar="GLOB", help="Skip matching files/folders (repeatable)."),
        click.option("--min-size", type=SIZE, default=None, help="Ignore smaller files (default: none; empty files count)."),
        click.option("--max-size", type=SIZE, default=None, help="Ignore larger files."),
        click.option("--skip-hidden", is_flag=True, help="Skip dot-files and dot-folders."),
        click.option("--include-vcs", is_flag=True, help="Also compare .git/.hg/.svn internals (normally skipped)."),
        click.option("--no-default-excludes", is_flag=True, help="Do not skip system junk (Thumbs.db, .Trash, @eaDir…)."),
        click.option("--cross-filesystems", is_flag=True, help="Descend into other filesystems mounted inside a location."),
        click.option("--no-cache", is_flag=True, help="Ignore and do not update the hash cache."),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def diff_options_from(kw: dict[str, Any]) -> DiffOptions:
    tolerance = None
    if kw.get("time_tolerance"):
        try:
            tolerance = int(parse_duration(str(kw["time_tolerance"])) * 1_000_000_000)
        except ValueError as exc:
            raise ConfigError(f"not a duration: {kw['time_tolerance']!r} (examples: 2s, 90s, 1h)") from exc
    return DiffOptions(
        basis=Basis(kw.get("basis") or "content"),
        verify=Verify(kw.get("verify") or "hash"),
        detect_moves=bool(kw.get("moves", True)),
        time_tolerance_ns=tolerance,
        ignore_dst=bool(kw.get("ignore_dst")),
        full_algorithm=kw.get("algorithm"),
        one_file_system=not kw.get("cross_filesystems"),
    )


def do_diff(app: AppContext, a: str, b: str, kw: dict[str, Any], *, options: DiffOptions | None = None) -> DiffResult:
    """Scan both locations and classify every file (read-only)."""
    kw = dict(kw)
    if kw.get("min_size") is None:
        kw["min_size"] = 0
    kw.setdefault("protect", ())
    options = options or diff_options_from(kw)
    roots = make_roots([(app.registry.open(a), RootRole.NORMAL), (app.registry.open(b), RootRole.NORMAL)])
    scan_filter = build_filter(app, kw)
    cache = app.cache(not kw.get("no_cache"))

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            inventory = scan(roots, scan_filter, progress=progress, cancel=cancel, one_file_system=options.one_file_system)
            return diff_inventory(inventory, options, cache=cache, progress=progress, cancel=cancel)

    result: DiffResult = run_cancellable(work)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _duration_ns(ns: int) -> str:
    if ns >= 1_000_000_000:
        return f"{ns / 1e9:g} s"
    if ns >= 1_000_000:
        return f"{ns / 1e6:g} ms"
    if ns >= 1_000:
        return f"{ns / 1e3:g} µs"
    return f"{ns} ns"


def _cell(files: int, size: int) -> Text:
    if not files:
        return Text("–", style="dim")
    return Text.assemble(f"{files:,}", (f"  {human_size(size)}", "dim"))


def summary_panel(result: DiffResult) -> Panel:
    a, b = result.inventory.roots
    table = Table(box=box.SIMPLE_HEAD, expand=False)
    table.add_column("")
    table.add_column("in A", justify="right")
    table.add_column("in B", justify="right")
    for kind in (Kind.SAME, Kind.MOVED, Kind.CHANGED, Kind.ONLY, Kind.UNREADABLE):
        fa, fb = result.count(kind, 0), result.count(kind, 1)
        if kind is Kind.UNREADABLE and not (fa or fb):
            continue
        table.add_row(Text(KIND_TITLE[kind], style=KIND_STYLE[kind]), _cell(fa, result.size(kind, 0)),
                      _cell(fb, result.size(kind, 1)))
    where = Table.grid(padding=(0, 1))
    for name, root in (("A", a), ("B", b)):
        where.add_row(Text(name, style="bold"), t(root.location.display), t(f"[{root.caps.label}]", "dim"))
    lines: list[Any] = [where, table]
    dup_a, dup_b = result.duplicates_within(0), result.duplicates_within(1)
    if dup_a[0] or dup_b[0]:
        lines.append(Text(f"Redundant copies inside a side: A {plural(dup_a[0], 'file')} ({human_size(dup_a[1])}) · "
                          f"B {plural(dup_b[0], 'file')} ({human_size(dup_b[1])})", style="dim"))
    basis = result.options.basis
    how = f"basis: {basis.value}"
    if basis is not Basis.CONTENT:
        how += f" · times equal within {_duration_ns(result.tolerance_ns)}" + (
            " (or exactly 1 h apart)" if result.options.ignore_dst else "")
    if result.case_folded:
        how += " · paths matched ignoring case"
    lines.append(Text(how, style="dim"))
    return Panel(Group(*lines), title="Comparison", title_align="left", border_style="cyan")


def verdict(result: DiffResult) -> list[Text]:
    out: list[Text] = []
    if result.identical_layout:
        out.append(Text("A and B are identical. ✓", style="bold green"))
        return out
    if result.identical_content:
        out.append(Text("A and B hold the same files, arranged differently (renamed or moved).", style="green"))
    uris = [shlex.quote(r.location.uri) for r in result.inventory.roots]
    for side, name, other in ((0, "A", "B"), (1, "B", "A")):
        missing = result.select(Kind.ONLY, side)
        differing = result.select(Kind.CHANGED, side)
        if missing or differing:
            size = sum(i.entry.size for i in missing + differing)
            what = plural(len(missing), "file") + (f" + {len(differing)} different" if differing else "")
            out.append(Text(f"{name} has {what} ({human_size(size)}) that {other} lacks. "
                            f"Preview moving them over:  duplio merge {uris[side]} {uris[1 - side]}", style="yellow"))
    if result.count(Kind.UNREADABLE, 0) or result.count(Kind.UNREADABLE, 1):
        out.append(Text("Some files could not be read; nothing is claimed about them.", style="bold red"))
    return out


def _detail(item: DiffItem) -> Text:
    if item.kind is Kind.MOVED:
        shown = ", ".join(printable_rel(o.rel) for o in item.others[:2])
        more = f" +{len(item.others) - 2}" if len(item.others) > 2 else ""
        return Text.assemble(("→ ", "cyan"), t(shown + more))
    if item.kind is Kind.CHANGED and item.others:
        o = item.others[0]
        who = {1: "A is newer", -1: "B is newer", 0: "same time"}[item.newer if item.side == 0 else -item.newer]
        return Text(f"{human_size(item.entry.size)} vs {human_size(o.size)} · {who}", style="dim")
    if item.note:
        return t(item.note, "dim")
    if item.extra_copies:
        return Text(f"+{plural(item.extra_copies, 'identical copy', 'identical copies')} on this side", style="dim")
    return Text("")


def printable_rel(rel: str) -> str:
    from duplio.util.text import printable

    return printable(rel)


def listing(result: DiffResult, kind: Kind, side: int | None, title: str, limit: int) -> Table | None:
    items = result.select(kind, side)
    if kind in (Kind.CHANGED, Kind.MOVED) and side is None:
        items = [i for i in items if i.side == 0]  # one row per pair, seen from A
    if not items:
        return None
    table = Table(box=box.SIMPLE_HEAD, expand=True, title=title, title_justify="left", title_style=KIND_STYLE[kind])
    table.add_column("File", ratio=3, overflow="fold")
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Modified", no_wrap=True, style="dim")
    table.add_column("Detail", ratio=2, overflow="fold")
    for item in items[:limit]:
        table.add_row(t(item.entry.rel), t(human_size(item.entry.size)), t(format_ns(item.entry.mtime_ns)), _detail(item))
    if len(items) > limit:
        table.add_row(Text(f"… {len(items) - limit:,} more (use --limit, --list or --export)", style="dim"), "", "", "")
    return table


LIST_CHOICES = ("only-a", "only-b", "changed", "moved", "same", "unreadable")


def _selection(result: DiffResult, name: str) -> list[DiffItem]:
    if name == "only-a":
        return result.select(Kind.ONLY, 0)
    if name == "only-b":
        return result.select(Kind.ONLY, 1)
    if name == "unreadable":
        return result.select(Kind.UNREADABLE)
    return result.select(Kind(name), 0)


@cli.command("diff")
@click.argument("a")
@click.argument("b")
@compare_options
@click.option("--limit", type=int, default=10, show_default=True, help="Rows to show per category.")
@click.option("--summary", "summary_only", is_flag=True, help="Only the counts.")
@click.option("--list", "list_kinds", type=click.Choice(LIST_CHOICES), multiple=True,
              help="Print just the relative paths of one category (for scripts); changed/moved/same list A's paths.")
@click.option("--print0", is_flag=True, help="With --list: separate paths with NUL instead of newlines.")
@click.option("--export", "export_path", type=click.Path(dir_okay=False), help="Write a .json, .csv or .html report.")
@click.option("--json", "as_json", is_flag=True, help="Print the full result as JSON.")
@click.option("--check", is_flag=True, help="Exit with status 1 if the two sides do not hold the same content.")
@click.option("--strict", is_flag=True, help="With --check: also fail if files are in different places.")
@handle_errors
def diff_cmd(a: str, b: str, limit: int, summary_only: bool, list_kinds: tuple[str, ...], print0: bool,
             export_path: str | None, as_json: bool, check: bool, strict: bool, **kw: Any) -> None:
    """Compare two folders by content — names do not matter. Read-only.

    Shows what is identical, what was renamed or moved, what differs, and what exists on only one side.
    Works across any mix of local folders, mounted network drives, SMB shares and phones.

    \b
    duplio diff ~/Photos smb://me@nas/photos
    duplio diff /media/usb/backup ~/Documents --basis time-size --ignore-dst
    duplio diff A B --list only-a --print0 | xargs -0 ...
    """
    app = get_app()
    app.json_output = as_json or bool(list_kinds)
    result = do_diff(app, a, b, kw)
    if as_json:
        from duplio.reporting.diff_export import diff_to_json

        click.echo(diff_to_json(result))
    elif list_kinds:
        out = sys.stdout.buffer
        sep = b"\0" if print0 else b"\n"
        for name in list_kinds:
            for item in _selection(result, name):
                out.write(fs_bytes(item.entry.rel) + sep)
        out.flush()
    else:
        app.console.print(summary_panel(result))
        if not summary_only:
            for kind, side, title in ((Kind.ONLY, 0, "Only in A"), (Kind.ONLY, 1, "Only in B"),
                                      (Kind.CHANGED, None, "Different content at the same path (as seen from A)"),
                                      (Kind.MOVED, None, "Same content, different path (as seen from A)"),
                                      (Kind.UNREADABLE, None, "Could not be read")):
                table = listing(result, kind, side, title, limit)
                if table is not None:
                    app.console.print(table)
        for line in verdict(result):
            app.console.print(line)
        for path, err in result.errors[:5]:
            app.console.print(t(f"  {path}: {err}", "red"))
    if export_path:
        from duplio.reporting.diff_export import export_diff

        out_path = export_diff(result, export_path)
        if as_json or list_kinds:
            click.echo(f"Report written: {out_path}", err=True)
        else:
            app.console.print(Text.assemble(("Report written: ", "dim"), t(str(out_path))))
    if check:
        failed = not (result.identical_layout if strict else result.identical_content)
        if failed:
            sys.exit(1)
