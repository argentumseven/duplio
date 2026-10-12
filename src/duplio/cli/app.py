"""The root ``duplio`` command group and shared option helpers."""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any

import click
from rich.console import Console
from rich.text import Text

from duplio import __version__
from duplio.cli.context import AppContext, setup_logging
from duplio.cli.progress import RichProgress
from duplio.compare.model import Mode, ScanOptions, Verify
from duplio.errors import CancelledError, DuplioError
from duplio.events import CancelToken
from duplio.hashing.algorithms import full_hash_choices
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import parse_size

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}


class SizeType(click.ParamType):
    name = "size"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> int:
        if isinstance(value, int):
            return value
        try:
            return parse_size(str(value))
        except ValueError as exc:
            raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


SIZE = SizeType()


class DuplioGroup(click.Group):
    """Lists commands in a deliberate order with section headings."""

    SECTIONS = (
        ("Find & clean duplicates", ["scan", "dedupe", "compare", "similar", "plan", "apply"]),
        ("Safety net", ["undo", "history", "quarantine", "recover", "sessions"]),
        ("Back up", ["backup", "mirror", "ingest"]),
        ("Devices & setup", ["ui", "devices", "doctor", "rules", "config", "profile", "run", "schedule", "cache"]),
    )

    def list_commands(self, ctx: click.Context) -> list[str]:
        ordered = [name for _, names in self.SECTIONS for name in names if name in self.commands]
        return ordered + sorted(set(self.commands) - set(ordered))

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        for title, names in self.SECTIONS:
            rows = []
            for name in names:
                cmd = self.commands.get(name)
                if cmd is not None and not cmd.hidden:
                    rows.append((name, cmd.get_short_help_str(80)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


@click.group(cls=DuplioGroup, context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", prog_name="duplio")
@click.option("-q", "--quiet", is_flag=True, help="No progress bars; only results and errors.")
@click.option("-v", "--verbose", is_flag=True, help="Log more detail to the log file.")
@click.option("--no-color", is_flag=True, help="Disable colours.")
@click.pass_context
def cli(ctx: click.Context, quiet: bool, verbose: bool, no_color: bool) -> None:
    """Duplio — find duplicate files across computers, phones and network shares,
    and back them up safely.

    \b
    Quick start:
      duplio ui                                  guided terminal interface
      duplio scan ~/Pictures /media/phone        see what is duplicated
      duplio dedupe ~/Pictures                   clean up (quarantine, undoable)
      duplio compare adb:/sdcard/DCIM --against ~/Pictures   what's not backed up?
      duplio ingest adb:/sdcard/DCIM ~/Pictures/Library      import phone photos

    Nothing is ever removed without a plan you approve, fresh re-verification,
    and a journal entry. Default action: quarantine (instantly reversible).
    """
    console = Console(no_color=no_color, highlight=False)
    app = AppContext(console=console, quiet=quiet)
    setup_logging(app.dirs, verbose)
    ctx.obj = app
    ctx.call_on_close(app.close)
    if ctx.invoked_subcommand is None:
        from duplio.cli.render import banner

        banner(console)
        console.print(ctx.get_help())


def get_app(ctx: click.Context | None = None) -> AppContext:
    ctx = ctx or click.get_current_context()
    obj = ctx.find_object(AppContext)
    if obj is None:  # pragma: no cover - only when commands are invoked oddly
        obj = AppContext(console=Console())
        ctx.obj = obj
    return obj


def run_cancellable(fn: Callable[[CancelToken], Any]) -> Any:
    """Run work with Ctrl-C mapped to a cooperative cancel."""
    token = CancelToken()
    try:
        return fn(token)
    except KeyboardInterrupt:
        token.cancel()
        raise CancelledError("interrupted") from None


def progress_for(app: AppContext) -> RichProgress:
    return RichProgress(app.console, enabled=not app.quiet and not app.json_output)


# ---------------------------------------------------------------------------
# Shared scan options
# ---------------------------------------------------------------------------


def scan_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    options = [
        click.option("-r", "--ref", "refs", multiple=True, metavar="LOCATION",
                     help="Reference location: used for matching, never modified (repeatable)."),
        click.option("--mode", type=click.Choice([m.value for m in Mode]), default=None,
                     help="all (default) · cross · within · reference (default with --ref) · unique."),
        click.option("--verify", "verify", type=click.Choice([v.value for v in Verify]), default="hash", show_default=True,
                     help="quick = size+name only (report-only) · hash = cryptographic hash · bytes = hash + byte-for-byte."),
        click.option("--hash", "algorithm", type=click.Choice(full_hash_choices()), default=None,
                     help="Content hash (default: blake3 if installed, else blake2b)."),
        click.option("--no-sample", is_flag=True, help="Skip the head/middle/tail fingerprint stage."),
        click.option("-i", "--include", multiple=True, metavar="GLOB", help="Only scan matching files (repeatable)."),
        click.option("-x", "--exclude", multiple=True, metavar="GLOB", help="Skip matching files/folders (repeatable)."),
        click.option("--protect", multiple=True, metavar="GLOB", help="Scan but never remove matching files (repeatable)."),
        click.option("--min-size", type=SIZE, default=None, help="Ignore smaller files (default 1 B: skip empty files)."),
        click.option("--max-size", type=SIZE, default=None, help="Ignore larger files."),
        click.option("--skip-hidden", is_flag=True, help="Skip dot-files and dot-folders."),
        click.option("--include-vcs", is_flag=True, help="Also scan .git/.hg/.svn internals (normally skipped)."),
        click.option("--no-default-excludes", is_flag=True, help="Do not skip system junk (Thumbs.db, .Trash, @eaDir…)."),
        click.option("--no-default-protect", is_flag=True, help="Allow removing files inside app bundles, photo libraries, Android/data…"),
        click.option("--cross-filesystems", is_flag=True, help="Descend into other filesystems mounted inside a location."),
        click.option("--similar-images", is_flag=True, help="Also find visually similar images (needs Pillow)."),
        click.option("--similar-threshold", type=click.IntRange(0, 32), default=6, show_default=True,
                     help="Max dHash distance for similar images."),
        click.option("--within-targets", is_flag=True, help="Reference mode: also report duplicates among non-reference locations."),
        click.option("--workers", type=click.IntRange(1, 64), default=None, help="Parallel reads per local disk."),
        click.option("--no-cache", is_flag=True, help="Ignore and do not update the hash cache."),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def build_filter(app: AppContext, kw: dict[str, Any]) -> ScanFilter:
    settings = app.settings
    return ScanFilter(
        include=list(kw.get("include") or []),
        exclude=list(settings.exclude) + list(kw.get("exclude") or []),
        protect=list(settings.protect) + list(kw.get("protect") or []),
        min_size=kw["min_size"] if kw.get("min_size") is not None else settings.min_size,
        max_size=kw.get("max_size"),
        skip_hidden=bool(kw.get("skip_hidden")) or settings.skip_hidden,
        use_default_excludes=not kw.get("no_default_excludes"),
        use_default_protect=not kw.get("no_default_protect"),
        exclude_vcs=not kw.get("include_vcs"),
    )


def build_options(app: AppContext, kw: dict[str, Any], *, refs: bool, mode: Mode | None = None) -> ScanOptions:
    opts = ScanOptions()
    chosen = mode or (Mode(kw["mode"]) if kw.get("mode") else (Mode.REFERENCE if refs else Mode.ALL))
    opts.mode = chosen
    opts.verify = Verify(kw.get("verify") or "hash")
    if kw.get("algorithm"):
        opts.full_algorithm = kw["algorithm"]
    opts.use_sampling = not kw.get("no_sample")
    opts.similar_images = bool(kw.get("similar_images"))
    opts.similar_threshold = int(kw.get("similar_threshold") or 6)
    opts.within_targets = bool(kw.get("within_targets"))
    opts.one_file_system = not kw.get("cross_filesystems") and app.settings.one_file_system
    opts.local_workers = kw.get("workers")
    return opts


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except CancelledError:
            get_app().console.print(Text("Cancelled. Nothing further was changed.", style="yellow"))
            sys.exit(130)
        except DuplioError as exc:
            get_app().console.print(Text.assemble(("Error: ", "bold red"), str(exc)))
            sys.exit(2)

    return wrapper
