"""preview · previews — pictures of files on slow and remote drives, fetched once."""

from __future__ import annotations

import os
import tempfile
from collections import Counter

import click
from rich import box
from rich.table import Table
from rich.text import Text

from duplio.cli.app import cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.render import t
from duplio.compare.scheduler import Job, run_jobs
from duplio.config import save_settings
from duplio.errors import ConfigError
from duplio.preview.service import PICTURE_EXT, PreviewItem, Unavailable, extension
from duplio.preview.settings import KEEP, MODES, PreviewSettings
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import human_size, parse_size
from duplio.util.opener import open_in_viewer


def _item(app, text: str) -> PreviewItem:  # type: ignore[no-untyped-def]
    location = app.registry.open(text)
    info = location.backend.stat(location.path)
    if info is None or not info.is_file:
        raise ConfigError(f"not a file: {text}")
    return PreviewItem(location.backend, location.path, info.size, info.mtime_ns,
                       location.backend.basename(location.path), info.offline)


@cli.command("preview")
@click.argument("files", nargs=-1, required=True)
@click.option("--width", type=click.IntRange(10, 300), default=None, help="Width in text columns (default: the window).")
@click.option("--download", is_flag=True, help="Allow reading a whole picture that has no small preview inside it.")
@click.option("--online-only", "online_only", is_flag=True,
              help="Allow files that are only stored in the cloud (this downloads them).")
@click.option("--open", "open_it", is_flag=True, help="Open the picture in your normal image viewer instead.")
@handle_errors
def preview_cmd(files: tuple[str, ...], width: int | None, download: bool, online_only: bool, open_it: bool) -> None:
    """Show a picture of one or more files, fetching as little as possible.

    A photo's own thumbnail is read from its first kilobytes, so a picture of a multi-megabyte photo on a slow drive
    costs a few kilobytes; the picture is then kept so it is not fetched again.

    \b
    duplio preview "T:\\Photos\\IMG_0042.jpg"
    duplio preview smb://me@nas/photos/trip/*.jpg --width 60
    """
    app = get_app()
    service = app.previews
    cols = width or max(20, min(app.console.size.width - 2, 100))
    for text in files:
        item = _item(app, text)
        try:
            preview = service.get(item, allow_download=True if download else None, allow_online_only=online_only)
        except Unavailable as exc:
            app.console.print(Text.assemble((item.name, "bold"), f": {exc.reason}", style="yellow"))
            continue
        origin = "from the cache" if preview.from_cache else f"fetched {human_size(preview.bytes_read)} from the drive"
        app.console.print(Text.assemble((item.name, "bold"), f"  {human_size(item.size)} · {origin}", style="dim"))
        if open_it:
            suffix = "." + preview.thumb.fmt
            handle, path = tempfile.mkstemp(prefix="duplio-preview-", suffix=suffix)
            with os.fdopen(handle, "wb") as out:
                out.write(preview.thumb.data)
            if not open_in_viewer(path):
                app.console.print(Text(f"No viewer found; the preview is at {path}", style="yellow"))
        else:
            app.console.print(service.render(preview, cols=cols, rows=max(8, cols // 3),
                                             color=app.console.color_system is not None))


@cli.group("previews")
def previews_group() -> None:
    """Manage the picture cache: warm it up ahead of time, see its size, clear it, change how long it is kept."""


@previews_group.command("warm")
@click.argument("location")
@click.option("--limit", type=int, default=0, help="Stop after this many pictures (0 = all).")
@click.option("--download", is_flag=True, help="Also read whole pictures that have no small preview inside them.")
@click.option("--online-only", "online_only", is_flag=True,
              help="Also fetch files that are only stored in the cloud (this downloads them).")
@handle_errors
def previews_warm(location: str, limit: int, download: bool, online_only: bool) -> None:
    """Fetch the pictures of a folder in advance, so reviewing it later does not wait for the drive.

    Run it before you start (or overnight). It reads each photo's own small preview where there is one, keeps it in the
    cache, and reports what it could not do and why.
    """
    app = get_app()
    service = app.previews
    loc = app.registry.open(location)
    backend = loc.backend

    def work(cancel):  # type: ignore[no-untyped-def]
        items: list[PreviewItem] = []
        with progress_for(app) as progress:
            progress.stage("Listing pictures")
            for raw in backend.walk(loc.path, walk_filter=ScanFilter.for_backup(), should_stop=lambda: cancel.cancelled):
                if extension(raw.rel) in PICTURE_EXT:
                    items.append(PreviewItem(backend, raw.path, raw.size, raw.mtime_ns, raw.rel, raw.offline))
                    if limit and len(items) >= limit:
                        break
            outcome: Counter[str] = Counter()
            progress.stage("Fetching previews", total_items=len(items))

            def make(item: PreviewItem):  # type: ignore[no-untyped-def]
                def run() -> None:
                    try:
                        preview = service.get(item, cancel=cancel, allow_download=True if download else None, allow_online_only=online_only)
                        outcome["already cached" if preview.from_cache else "fetched"] += 1
                        progress.advance(items=1, nbytes=preview.bytes_read)
                    except Unavailable as exc:
                        outcome[exc.reason] += 1
                        progress.advance(items=1)
                    except OSError as exc:
                        outcome[f"could not read: {exc.strerror or exc}"] += 1
                        progress.advance(items=1)

                return run

            caps = loc.caps
            run_jobs([Job(backend.device_key(i.path), min(2, caps.max_parallel_reads), make(i)) for i in items], cancel=cancel)
            return len(items), outcome

    total, outcome = run_cancellable(work)
    stats = service.stats
    app.console.print(Text(f"{total:,} pictures looked at; {human_size(stats.bytes_read)} read from the drive.", style="bold"))
    table = Table(box=box.SIMPLE, show_header=False)
    for what, count in outcome.most_common():
        table.add_row(t(what), f"{count:,}")
    app.console.print(table)
    for kind, files, size, path in service.storage_summary():
        app.console.print(Text(f"cache ({kind}): {files:,} pictures, {human_size(size)} in {path}", style="dim"))
    if service.settings.keep == "session":
        app.console.print(Text("This cache is temporary: it is deleted when this command ends. To keep it between runs: "
                               "duplio previews settings --keep persistent", style="yellow"))


@previews_group.command("stats")
@handle_errors
def previews_stats() -> None:
    """How big the saved picture cache is."""
    from duplio.preview.store import ThumbStore

    app = get_app()
    path = app.dirs.cache / "previews"
    if not path.exists():
        app.console.print("No saved pictures yet (nothing is kept between runs unless you choose "
                          "'duplio previews settings --keep persistent').")
        return
    store = ThumbStore(path, max_bytes=app.settings.previews.max_bytes, persistent=True)
    try:
        s = store.stats()
    finally:
        store.close()
    app.console.print(f"{s.count:,} pictures, {human_size(s.bytes)} (limit {human_size(app.settings.previews.max_bytes)}) in {s.path}")


@previews_group.command("clear")
@click.option("-y", "--yes", is_flag=True, help="Do not ask.")
@handle_errors
def previews_clear(yes: bool) -> None:
    """Delete every saved picture."""
    from duplio.preview.store import ThumbStore

    app = get_app()
    path = app.dirs.cache / "previews"
    if not path.exists():
        app.console.print("Nothing saved.")
        return
    if not yes and not click.confirm("Delete all saved pictures?", default=False):
        return
    store = ThumbStore(path, max_bytes=app.settings.previews.max_bytes, persistent=True)
    try:
        removed = store.clear()
    finally:
        store.close()
    app.console.print(Text(f"Deleted {removed:,} pictures.", style="green"))


@previews_group.command("settings")
@click.option("--keep", type=click.Choice(KEEP), default=None,
              help="session = forgotten when Duplio closes · persistent = kept between runs (stored unencrypted).")
@click.option("--mode", type=click.Choice(MODES), default=None,
              help="auto = cache pictures from slow, remote and cloud drives · always · off.")
@click.option("--max-size", default=None, help="Size limit of the saved cache, e.g. 2GB.")
@click.option("--online-only/--no-online-only", default=None,
              help="Fetch pictures of cloud-only files without asking (this downloads them).")
@click.option("--keep-days", type=click.IntRange(0, 3650), default=None,
              help="Forget saved pictures nobody has looked at for this many days (0: never).")
@click.option("--download-whole/--no-download-whole", default=None,
              help="Read a whole picture or movie that has no small preview inside, to make one (needed for most movies).")
@click.option("--ffmpeg", "ffmpeg_path", default=None, help="The ffmpeg program to use for movies and unusual pictures ('' to look for one).")
@handle_errors
def previews_settings(keep: str | None, mode: str | None, max_size: str | None, online_only: bool | None, keep_days: int | None,
                      download_whole: bool | None, ffmpeg_path: str | None) -> None:
    """Show or change how pictures are cached."""
    app = get_app()
    p = app.settings.previews
    changed = False
    new = PreviewSettings(**{**p.__dict__})
    if keep is not None:
        new.keep, changed = keep, True
    if mode is not None:
        new.mode, changed = mode, True
    if max_size is not None:
        new.max_bytes, changed = parse_size(max_size), True
    if online_only is not None:
        new.online_only, changed = online_only, True
    if keep_days is not None:
        new.keep_days, changed = keep_days, True
    if download_whole is not None:
        new.download_whole, changed = download_whole, True
    if ffmpeg_path is not None:
        new.ffmpeg, changed = ffmpeg_path, True
    if changed:
        new.validate()
        app.settings.previews = new
        save_settings(app.settings, app.dirs)
        p = new
        app.console.print(Text("Saved.", style="green"))
    table = Table(box=box.SIMPLE, show_header=False)
    rows = [
        ("Cache pictures from", {"auto": "slow, remote and cloud drives", "always": "every drive", "off": "nowhere"}[p.mode]),
        ("Keep them", "until Duplio closes" if p.keep == "session" else
         f"between runs, up to {human_size(p.max_bytes)} (unencrypted)" + (f", forgetting those not looked at for {p.keep_days} days" if p.keep_days else "")),
        ("Pictures with no small preview", "read whole to make one (up to " + human_size(p.max_download) + ")" if p.download_whole
         else "left alone unless small or asked for"),
        ("ffmpeg (movies, unusual pictures)", p.ffmpeg or "looked for on this computer"),
        ("Cloud-only files", "fetched without asking" if p.online_only else "never fetched without asking"),
        ("Prefetch ahead", f"{p.prefetch} pictures"),
        ("Picture size", f"{p.size_px} pixels on the long side"),
    ]
    for key, value in rows:
        table.add_row(t(key, "bold"), t(value))
    app.console.print(table)
