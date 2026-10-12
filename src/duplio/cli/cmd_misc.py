"""ui · devices · doctor · rules · config · profile · run · schedule · cache"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.table import Table
from rich.text import Text

from duplio import __version__
from duplio.cli.app import cli, get_app, handle_errors
from duplio.cli.render import banner, t
from duplio.config import (
    PROFILE_FIELDS,
    PROFILE_TYPES,
    load_settings,
    save_settings,
    validate_profile,
)
from duplio.errors import ConfigError
from duplio.plan.rules import RULE_HELP
from duplio.util.fmt import human_size


@cli.command()
@handle_errors
def ui() -> None:
    """Open the interactive terminal interface."""
    try:
        from duplio.tui.app import DuplioApp
    except ImportError as exc:
        raise ConfigError("the terminal UI needs Textual: pip install 'duplio[tui]'") from exc
    app = get_app()
    app.close()  # the TUI manages its own backends and caches
    DuplioApp(dirs=app.dirs).run()


@cli.command()
@handle_errors
def devices() -> None:
    """Phones, cameras, network shares and drives that Duplio can see."""
    from duplio.devices import discover, platform_tip

    app = get_app()
    found = discover()
    if not found:
        app.console.print(Text("No phones, shares or removable drives detected.", style="yellow"))
    else:
        table = Table(box=box.SIMPLE_HEAD, expand=True)
        table.add_column("Kind", no_wrap=True)
        table.add_column("Device")
        table.add_column("Location (use in any command)", ratio=1)
        table.add_column("Notes", style="dim")
        for d in found:
            table.add_row(t(d.kind, "green" if d.ready else "yellow"), t(d.name), t(d.location), t(d.detail))
        app.console.print(table)
    app.console.print(Text(platform_tip(), style="dim"))


@cli.command()
@click.option("--benchmark/--no-benchmark", default=True, help="Measure hash speed (about one second).")
@handle_errors
def doctor(benchmark: bool) -> None:
    """Check optional components, tools and performance."""
    from duplio.hashing.algorithms import (
        available_algorithms,
        default_full_algorithm,
        get_algorithm,
    )
    from duplio.vfs.adb import find_adb

    app = get_app()
    banner(app.console)
    table = Table(box=box.SIMPLE_HEAD)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Used for", style="dim")

    def row(name: str, ok: bool, detail: str, purpose: str) -> None:
        table.add_row(name, t(("✓ " if ok else "✗ ") + detail, "green" if ok else "yellow"), purpose)

    row("Python", sys.version_info >= (3, 11), platform.python_version(), "runtime")
    for module, purpose, extra in (
        ("textual", "terminal UI (duplio ui)", "tui"),
        ("blake3", "fastest cryptographic hashing", "fast"),
        ("xxhash", "fast sample fingerprints", "fast"),
        ("smbclient", "native smb:// access", "smb"),
        ("PIL", "similar images, EXIF dates", "media"),
        ("send2trash", "system trash action", "trash"),
        ("keyring", "stored SMB passwords", "-"),
    ):
        present = importlib.util.find_spec(module) is not None
        row(module, present, "installed" if present else f"missing (pip install 'duplio[{extra}]')", purpose)
    adb = find_adb()
    row("adb", adb is not None, adb or "not found (Android platform-tools)", "phones over USB debugging")
    for tool, purpose in (("jmtpfs", "mount phones (MTP)"), ("gio", "GVFS phone/SMB mounts")):
        found = shutil.which(tool)
        row(tool, found is not None, found or "not found", purpose)
    app.console.print(table)
    app.console.print(Text.assemble(("State: ", "dim"), str(app.dirs.state), ("   Cache: ", "dim"), str(app.dirs.cache),
                                    ("   Config: ", "dim"), str(app.dirs.config_file)))
    if benchmark:
        data = os.urandom(64 * 1024 * 1024)
        bench = Table(box=box.SIMPLE_HEAD, title="Hash speed (single core, in memory)")
        bench.add_column("Algorithm")
        bench.add_column("Speed", justify="right")
        bench.add_column("Role", style="dim")
        for alg in available_algorithms():
            h = get_algorithm(alg.name).new()
            start = time.perf_counter()
            h.update(data)
            h.hexdigest()
            speed = len(data) / max(time.perf_counter() - start, 1e-9)
            if alg.name == default_full_algorithm():
                role = "default content hash"
            elif alg.cryptographic:
                role = "trusted content hash"
            elif alg.name in ("md5", "sha1"):
                role = "legacy (matches are byte-verified)"
            else:
                role = "fast sampling / byte-verified"
            bench.add_row(alg.name, f"{human_size(speed)}/s", role)
        app.console.print(bench)


@cli.command()
def rules() -> None:
    """Explain the keep rules that decide which copy survives."""
    app = get_app()
    table = Table(box=box.SIMPLE_HEAD, expand=True, title="Keep rules (first rule that differs decides)")
    table.add_column("Rule", style="bold", no_wrap=True)
    table.add_column("Meaning")
    for name, text in RULE_HELP:
        table.add_row(t(name), t(text))
    app.console.print(table)
    app.console.print(Text("Default: " + ", ".join(app.settings.keep), style="dim"))
    app.console.print(Text("Always kept, whatever the rules: reference locations, protected paths, read-only storage.", style="dim"))


@cli.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Show or initialise the configuration file."""
    if ctx.invoked_subcommand is None:
        app = get_app()
        path = app.dirs.config_file
        if path.exists():
            app.console.print(Text(f"# {path}", style="dim"))
            app.console.print(Text(path.read_text(encoding="utf-8")))
        else:
            app.console.print(f"No configuration yet ({path}). Create one with 'duplio config init'.")


@config.command("init")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@handle_errors
def config_init(force: bool) -> None:
    app = get_app()
    path = app.dirs.config_file
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    settings = load_settings(app.dirs, path=Path("/nonexistent/duplio.toml"))
    settings.path = path
    save_settings(settings, app.dirs)
    app.console.print(Text(f"Wrote {path}", style="green"))


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def _parse_setting(kind: str, item: str) -> tuple[str, Any]:
    if "=" not in item:
        raise ConfigError(f"--set expects KEY=VALUE, got {item!r}")
    key, _, raw = item.partition("=")
    key = key.strip()
    expected = PROFILE_FIELDS[kind].get(key)
    if expected is None:
        raise ConfigError(f"{key!r} is not a {kind} setting; valid: {', '.join(PROFILE_FIELDS[kind])}")
    if expected is list:
        return key, [v.strip() for v in raw.split(",") if v.strip()]
    if expected is bool:
        if raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
            raise ConfigError(f"{key} must be true or false")
        return key, raw.lower() in ("true", "yes", "1")
    if expected is int:
        try:
            return key, int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a whole number") from exc
    return key, raw


@cli.group()
def profile() -> None:
    """Saved jobs you can run (and schedule) by name."""


@profile.command("list")
@handle_errors
def profile_list() -> None:
    app = get_app()
    profiles = app.settings.profiles
    if not profiles:
        app.console.print("No profiles. Add one, e.g.:\n  duplio profile add phone --type ingest --set source=adb:/sdcard/DCIM --set dest=~/Pictures/Library")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Settings", ratio=1)
    for name, prof in profiles.items():
        settings = ", ".join(f"{k}={v}" for k, v in prof.items() if k != "type")
        table.add_row(t(name), t(prof["type"]), t(settings))
    app.console.print(table)


@profile.command("add")
@click.argument("name")
@click.option("--type", "kind", type=click.Choice(PROFILE_TYPES), required=True)
@click.option("--set", "items", multiple=True, metavar="KEY=VALUE", help="A setting (repeatable). Lists are comma-separated.")
@click.option("--replace", is_flag=True)
@handle_errors
def profile_add(name: str, kind: str, items: tuple[str, ...], replace: bool) -> None:
    app = get_app()
    settings = app.settings
    if name in settings.profiles and not replace:
        raise ConfigError(f"profile {name!r} exists (use --replace)")
    prof: dict[str, Any] = {"type": kind}
    for item in items:
        key, value = _parse_setting(kind, item)
        prof[key] = value
    settings.profiles[name] = validate_profile(name, prof)
    path = save_settings(settings, app.dirs)
    app.console.print(Text(f"Saved profile {name!r} in {path}. Run it with 'duplio run {name}'.", style="green"))


@profile.command("remove")
@click.argument("name")
@handle_errors
def profile_remove(name: str) -> None:
    app = get_app()
    settings = app.settings
    if settings.profiles.pop(name, None) is None:
        raise ConfigError(f"no profile named {name!r}")
    save_settings(settings, app.dirs)
    app.console.print(f"Removed profile {name!r}.")


@cli.command()
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Non-interactive (for schedules).")
@click.option("--dry-run", is_flag=True)
@click.pass_context
@handle_errors
def run(ctx: click.Context, name: str, yes: bool, dry_run: bool) -> None:
    """Run a saved profile.

    Scheduled dedupe profiles only ever quarantine (reversible), whatever
    action they name, unless run interactively.
    """
    from duplio.cli import cmd_backup, cmd_scan

    app = get_app()
    prof = app.settings.profiles.get(name)
    if prof is None:
        raise ConfigError(f"no profile named {name!r} (see 'duplio profile list')")
    kind = prof["type"]
    try:
        _check_reachable(app, prof)
    except ConfigError as exc:
        app.console.print(Text(f"Skipping {name}: {exc}", style="yellow"))
        sys.exit(3)
    if kind == "dedupe":
        action = prof.get("action", app.settings.action.value)
        if yes and action not in ("quarantine", "hardlink", "reflink"):
            action = "quarantine"
        ctx.invoke(cmd_scan.dedupe, locations=tuple(prof["roots"]), refs=tuple(prof.get("reference", [])),
                   action=action, keep=",".join(prof["keep"]) if prof.get("keep") else None, dry_run=dry_run,
                   yes=yes, understand=False, verify_before=prof.get("verify"), prune_empty_dirs=False, top=10,
                   mode=prof.get("mode"), **_scan_defaults())
    elif kind == "compare":
        ctx.invoke(cmd_scan.compare, sources=(prof["source"],), against=tuple(prof["against"]), refs=(), top=25,
                   list_all=False, copy_missing_to=None if dry_run else prof.get("copy_missing_to"), export_path=None,
                   as_json=False, mode=None, **_scan_defaults())
    elif kind == "backup":
        ctx.invoke(cmd_backup.backup_run, source=prof["source"], repo=prof["repo"], label=prof.get("label", name),
                   tags=tuple(prof.get("tags", [])), exclude=tuple(prof.get("exclude", [])), include=(), rehash=False,
                   cross_filesystems=False)
        keeps = {k: prof.get(k, 0) for k in ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")}
        if any(keeps.values()) or prof.get("keep_within"):
            ctx.invoke(cmd_backup.backup_forget, repo=prof["repo"], do_apply=not dry_run, prune=bool(prof.get("prune", True)),
                       keep_within=prof.get("keep_within"), keep_tag=(), **keeps)
    elif kind == "mirror":
        ctx.invoke(cmd_backup.mirror, source=prof["source"], dest=prof["dest"], delete_extraneous=bool(prof.get("delete")),
                   checksum=bool(prof.get("checksum")), no_verify=False, keep_versions=prof.get("keep_versions"),
                   exclude=tuple(prof.get("exclude", [])), max_change=0.5, force=False, dry_run=dry_run, yes=yes)
    elif kind == "ingest":
        ctx.invoke(cmd_backup.ingest_cmd, source=prof["source"], library=prof["dest"],
                   layout=prof.get("layout", cmd_backup.DEFAULT_LAYOUT), all_files=not prof.get("media_only", True),
                   device_label=prof.get("device", ""), reimport_deleted=False,
                   free_source=bool(prof.get("free_source")) and not dry_run, dry_run=dry_run, yes=yes)


def _scan_defaults() -> dict[str, Any]:
    return {
        "verify": "hash", "algorithm": None, "no_sample": False, "include": (), "exclude": (), "protect": (),
        "min_size": None, "max_size": None, "skip_hidden": False, "include_vcs": False, "no_default_excludes": False,
        "no_default_protect": False, "cross_filesystems": False, "similar_images": False, "similar_threshold": 6,
        "within_targets": False, "workers": None, "no_cache": False,
    }


def _check_reachable(app, prof: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    """Scheduled runs: a phone that is not plugged in is not an error."""
    from duplio.errors import DuplioError

    for key in ("source", "dest", "repo"):
        if key in prof:
            try:
                loc = app.registry.open(prof[key])
            except DuplioError as exc:
                raise ConfigError(f"{prof[key]} is not reachable ({exc})") from exc
            if key == "source" and loc.backend.stat(loc.path) is None:
                raise ConfigError(f"{loc.display} is not connected")
    for root in prof.get("roots", []):
        try:
            loc = app.registry.open(root)
        except DuplioError as exc:
            raise ConfigError(f"{root} is not reachable ({exc})") from exc
        if loc.backend.stat(loc.path) is None:
            raise ConfigError(f"{loc.display} is not connected")


@cli.command()
@click.argument("name")
@click.option("--every", type=click.Choice(["hourly", "daily", "weekly", "monthly"]), default="daily", show_default=True)
@click.option("--write", "write_dir", type=click.Path(file_okay=False), default=None,
              help="Write the unit files into this folder instead of printing them.")
@handle_errors
def schedule(name: str, every: str, write_dir: str | None) -> None:
    """Generate an OS-native schedule (systemd / launchd / Task Scheduler / cron) for a profile."""
    from duplio.schedule import make_schedule

    app = get_app()
    if name not in app.settings.profiles:
        raise ConfigError(f"no profile named {name!r}")
    files = make_schedule(name, every)
    for filename, content in files.files.items():
        if write_dir:
            target = Path(write_dir).expanduser() / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            app.console.print(Text(f"wrote {target}", style="green"))
        else:
            app.console.print(Text(f"# {filename}", style="bold"))
            app.console.print(Text(content))
    app.console.print(Text(files.instructions, style="cyan"))


@cli.group()
def cache() -> None:
    """Inspect or clear the hash cache."""


@cache.command("stats")
@handle_errors
def cache_stats() -> None:
    from duplio.hashing.cache import HashCache

    app = get_app()
    with HashCache(app.dirs.hash_cache) as c:
        s = c.stats()
    app.console.print(f"{s['entries']:,} cached digests · {human_size(s['bytes'])} · {app.dirs.hash_cache}")


@cache.command("clear")
@handle_errors
def cache_clear() -> None:
    from duplio.hashing.cache import HashCache

    app = get_app()
    with HashCache(app.dirs.hash_cache) as c:
        c.clear()
    app.console.print("Hash cache cleared.")


@cache.command("prune")
@click.option("--older-than", default="180d", show_default=True)
@handle_errors
def cache_prune(older_than: str) -> None:
    from duplio.hashing.cache import HashCache
    from duplio.util.fmt import parse_duration

    app = get_app()
    with HashCache(app.dirs.hash_cache) as c:
        removed = c.prune(parse_duration(older_than))
    app.console.print(f"Removed {removed:,} stale entries.")


@cli.command(hidden=True)
def version() -> None:
    click.echo(f"duplio {__version__}")
