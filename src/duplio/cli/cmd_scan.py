"""scan · dedupe · compare · similar"""

from __future__ import annotations

import sys
from typing import Any

import click
from rich.text import Text

from duplio.cli.app import (
    build_filter,
    build_options,
    cli,
    get_app,
    handle_errors,
    progress_for,
    run_cancellable,
    scan_options,
)
from duplio.cli.console import SafeConsole
from duplio.cli.context import AppContext
from duplio.cli.render import (
    apply_summary,
    groups_table,
    plan_summary,
    scan_summary,
    similar_table,
    unique_table,
)
from duplio.compare.engine import compare as run_compare
from duplio.compare.model import Mode, ScanResult
from duplio.compare.session import result_to_dict, save_session
from duplio.errors import ConfigError
from duplio.plan.executor import ApplyOptions, apply_plan
from duplio.plan.model import ActionType, Plan
from duplio.plan.planner import build_plan
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.util.fmt import human_size, plural
from duplio.util.text import fs_bytes, json_dumps


def do_scan(app: AppContext, roots: tuple[str, ...], refs: tuple[str, ...], kw: dict[str, Any], *, mode: Mode | None = None,
            trust_cache: bool = True) -> ScanResult:
    """``trust_cache=False`` is for questions whose answer is "this is safe elsewhere": see ``HashCache``."""
    if not roots and not refs:
        raise ConfigError("give at least one location to scan")
    locations = [(app.registry.open(r), RootRole.NORMAL) for r in roots]
    locations += [(app.registry.open(r), RootRole.REFERENCE) for r in refs]
    scan_roots = make_roots(locations)
    scan_filter = build_filter(app, kw)
    options = build_options(app, kw, refs=bool(refs), mode=mode)
    cache = app.cache(not kw.get("no_cache") and (trust_cache or bool(kw.get("use_cache"))))

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            inventory = scan(scan_roots, scan_filter, progress=progress, cancel=cancel,
                             one_file_system=options.one_file_system)
            return run_compare(inventory, options, cache=cache, progress=progress, cancel=cancel,
                               previews=app.previews)

    result: ScanResult = run_cancellable(work)
    try:
        save_session(result, app.dirs)
    except OSError as exc:
        app.console.print(Text(f"(could not save the session: {exc})", style="yellow"))
    return result


def _export(app: AppContext, result: ScanResult, path: str | None) -> None:
    if path:
        from duplio.reporting.export import export

        out = export(result, path)
        app.console.print(Text.assemble(("Report written: ", "dim"), str(out)))


def _print_json(result: ScanResult) -> None:
    click.echo(json_dumps(result_to_dict(result), indent=1))


@cli.command("scan")
@click.argument("locations", nargs=-1)
@scan_options
@click.option("--top", type=int, default=15, show_default=True, help="Groups to show.")
@click.option("--keep", "keep", default=None, help="Keep rules used for the ★ preview (comma-separated).")
@click.option("--export", "export_path", type=click.Path(dir_okay=False), help="Write .json, .csv or .html report.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable results to stdout.")
@handle_errors
def scan_cmd(locations: tuple[str, ...], refs: tuple[str, ...], top: int, keep: str | None,
             export_path: str | None, as_json: bool, **kw: Any) -> None:
    """Find duplicates (read-only). Shows what would be kept (★) and removed (✗).

    \b
    Examples:
      duplio scan ~/Pictures ~/Downloads
      duplio scan /run/user/1000/gvfs/mtp:host=Pixel/Internal\\ shared\\ storage ~/Phone-backup
      duplio scan smb://me@nas/photos --ref ~/Pictures         copies on the NAS of local photos
      duplio scan ~/Pictures --similar-images --export report.html
    """
    app = get_app()
    app.json_output = as_json
    result = do_scan(app, locations, refs, kw)
    if as_json:
        _print_json(result)
        return
    scan_summary(app.console, result)
    rules = keep.split(",") if keep else app.settings.keep
    if result.options.mode is Mode.UNIQUE:
        unique_table(app.console, result, top=top)
    else:
        groups_table(app.console, result, top=top, rules=rules)
        similar_table(app.console, result, top=min(top, 10))
    _export(app, result, export_path)
    if result.groups and result.options.mode is not Mode.UNIQUE and result.options.verify.value != "quick":
        app.console.print(
            Text.assemble(
                ("Next: ", "bold"), f"duplio plan {result.session_id}  ",
                ("(or 'duplio ui' to review interactively)", "dim"),
            )
        )


def confirm_plan(app: AppContext, plan: Plan, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigError("not running interactively; pass --yes to apply (or --dry-run to only verify)")
    if plan.action is ActionType.DELETE:
        app.console.print(Text(f"This permanently deletes {plural(len(plan.items), 'file')}. It cannot be undone.", style="bold red"))
        typed = click.prompt("Type DELETE to continue", default="", show_default=False)
        return typed.strip() == "DELETE"
    return click.confirm(f"{plan.action.value.capitalize()} {plural(len(plan.items), 'file')} ({human_size(plan.reclaim)})?", default=False)


@cli.command()
@click.argument("locations", nargs=-1)
@scan_options
@click.option("-a", "--action", type=click.Choice([a.value for a in ActionType]), default=None,
              help="quarantine (default, undoable) · trash · delete · hardlink · reflink.")
@click.option("-k", "--keep", default=None, help="Keep rules, comma-separated (see 'duplio rules').")
@click.option("--dry-run", is_flag=True, help="Verify everything, change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation (except nothing: DELETE still needs --i-understand).")
@click.option("--i-understand", "understand", is_flag=True, help="Required with --action delete and --yes.")
@click.option("--verify-before", type=click.Choice(["hash", "bytes"]), default=None,
              help="How to re-verify each file right before acting (default from config: hash).")
@click.option("--prune-empty-dirs", is_flag=True, help="Remove folders left empty afterwards.")
@click.option("--top", type=int, default=10, show_default=True)
@handle_errors
def dedupe(locations: tuple[str, ...], refs: tuple[str, ...], action: str | None, keep: str | None, dry_run: bool,
           yes: bool, understand: bool, verify_before: str | None, prune_empty_dirs: bool, top: int, **kw: Any) -> None:
    """Scan, plan, confirm and clean up duplicates in one go.

    \b
    Examples:
      duplio dedupe ~/Pictures                         quarantine extra copies (undoable)
      duplio dedupe ~/Phone-dump --ref ~/Pictures      remove what's already in ~/Pictures
      duplio dedupe ~/Pictures -k "prefer-path=**/Camera/**,oldest"
      duplio dedupe /data --action reflink             share storage on btrfs/XFS/APFS
    """
    app = get_app()
    act = ActionType(action) if action else app.settings.action
    if act is ActionType.DELETE and yes and not understand:
        raise ConfigError("--action delete with --yes also requires --i-understand")
    result = do_scan(app, locations, refs, kw)
    scan_summary(app.console, result)
    if result.options.mode is Mode.UNIQUE:
        raise ConfigError("dedupe does not work in 'unique' mode; use 'duplio compare'")
    if result.options.verify.value == "quick":
        raise ConfigError("quick (name + size) matches are report-only; use --verify hash or bytes to clean up")
    rules = keep.split(",") if keep else app.settings.keep
    verify = verify_before or app.settings.verify
    plan = build_plan(result, action=act, rules=rules, verify=verify)
    plan_summary(app.console, plan, top=top)
    if not plan.items:
        app.console.print(Text("Nothing to do.", style="green"))
        return
    plans_dir = app.dirs.state / "plans"
    plan.save(plans_dir / f"{plan.plan_id}.json")
    if not dry_run and not confirm_plan(app, plan, yes):
        app.console.print(Text.assemble("Not applied. The plan is saved: ", (f"duplio apply {plans_dir / (plan.plan_id + '.json')}", "bold")))
        return
    options = ApplyOptions(verify=verify, confirm_delete=act is ActionType.DELETE, dry_run=dry_run,
                           prune_empty_dirs=prune_empty_dirs)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return apply_plan(plan, app.registry, options=options, dirs=app.dirs, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    apply_summary(app.console, report)
    if report.count("failed"):
        sys.exit(1)


@cli.command()
@click.argument("sources", nargs=-1, required=True)
@click.option("--against", "-a", "against", multiple=True, required=True, metavar="LOCATION",
              help="Where copies should exist (repeatable). Never modified.")
@scan_options
@click.option("--top", type=int, default=25, show_default=True)
@click.option("--list", "list_all", is_flag=True, help="Print every missing file path (one per line, raw bytes).")
@click.option("-0", "--print0", is_flag=True, help="With --list: separate paths with NUL, for 'xargs -0'.")
@click.option("--copy-missing-to", type=str, default=None, metavar="LOCATION",
              help="Copy the missing files there (verified), keeping their folder structure.")
@click.option("--export", "export_path", type=click.Path(dir_okay=False))
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def compare(sources: tuple[str, ...], against: tuple[str, ...], refs: tuple[str, ...], top: int, list_all: bool,
            print0: bool, copy_missing_to: str | None, export_path: str | None, as_json: bool, **kw: Any) -> None:
    """What in SOURCES is not anywhere in --against? (Is my phone backed up?)

    \b
    Examples:
      duplio compare adb:/sdcard/DCIM --against ~/Pictures --against smb://nas/photos
      duplio compare ~/Old-Laptop --against /mnt/backup --copy-missing-to /mnt/backup/rescued
    """
    app = get_app()
    app.json_output = as_json
    if list_all:  # stdout carries only the paths, so the summary and progress go to stderr
        app.console = SafeConsole(stderr=True, highlight=False, no_color=app.console.no_color)
    result = do_scan(app, sources, tuple(against) + tuple(refs), kw, mode=Mode.UNIQUE, trust_cache=False)
    if as_json:
        _print_json(result)
        return
    scan_summary(app.console, result)
    if list_all:
        sys.stdout.flush()
        out = sys.stdout.buffer
        for e in result.unique:
            out.write(fs_bytes(result.inventory.roots[e.root].backend.display(e.path)) + (b"\0" if print0 else b"\n"))
        out.flush()
    else:
        unique_table(app.console, result, top=top)
    _export(app, result, export_path)
    if copy_missing_to and result.unique:
        _copy_missing(app, result, copy_missing_to)
    elif not result.unique:
        app.console.print(Text("Everything has a copy in the reference locations. ✓", style="bold green"))


def _copy_missing(app: AppContext, result: ScanResult, dest_text: str) -> None:
    from duplio.hashing.algorithms import default_full_algorithm
    from duplio.transfer import copy_verified

    dest = app.registry.open(dest_text)
    multi = len({e.root for e in result.unique}) > 1
    algo = default_full_algorithm()
    copied = failed = 0

    def work(cancel):  # type: ignore[no-untyped-def]
        nonlocal copied, failed
        with progress_for(app) as progress:
            progress.stage("Copying missing files", total_items=len(result.unique),
                           total_bytes=sum(e.size for e in result.unique))
            for e in result.unique:
                root = result.inventory.roots[e.root]
                prefix = (root.label or root.backend.basename(root.path) or f"location-{e.root + 1}") if multi else ""
                rel = f"{prefix}/{e.rel}" if prefix else e.rel
                try:
                    copy_verified((root.backend, e.path), (dest.backend, dest.backend.join(dest.path, rel)),
                                  algorithm=algo, expected_size=e.size, mtime_ns=e.mtime_ns, cancel=cancel,
                                  on_bytes=lambda n: progress.advance(nbytes=n))
                    copied += 1
                except FileExistsError:
                    progress.message(f"exists, not overwritten: {rel}", level="warning")
                    failed += 1
                except OSError as exc:
                    progress.message(f"{rel}: {exc}", level="error")
                    failed += 1
                progress.advance(items=1)

    run_cancellable(work)
    app.console.print(Text.assemble((f"{copied:,} copied", "bold green"), f" to {dest.display} · {failed:,} not copied"))


@cli.command()
@click.argument("locations", nargs=-1, required=True)
@scan_options
@click.option("--top", type=int, default=20, show_default=True)
@click.option("--export", "export_path", type=click.Path(dir_okay=False))
@handle_errors
def similar(locations: tuple[str, ...], refs: tuple[str, ...], top: int, export_path: str | None, **kw: Any) -> None:
    """Find visually similar images (resized, re-encoded, lightly edited). Review only."""
    app = get_app()
    kw["similar_images"] = True
    result = do_scan(app, locations, refs, kw, mode=Mode.ALL)
    scan_summary(app.console, result)
    if not result.similar:
        app.console.print(Text("No similar images found.", style="green"))
    similar_table(app.console, result, top=top)
    _export(app, result, export_path)
    if result.similar:
        app.console.print(Text("Hand-pick which ones to remove in 'duplio ui' (Similar tab).", style="dim"))
