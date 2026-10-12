"""backup … · mirror · ingest"""

from __future__ import annotations

import getpass
import sys
from typing import Any

import click
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio.backup.crypto import MIN_PASSWORD_LEN, PasswordRequired, WrongPassword
from duplio.backup.export import export_snapshot
from duplio.backup.ingest import DEFAULT_LAYOUT, LAYOUT_TOKENS, IngestOptions, ingest
from duplio.backup.mirror import MirrorOptions, plan_mirror, run_mirror
from duplio.backup.repository import DEFAULT_CHUNK, Repository, password_from_environment
from duplio.backup.retention import RetentionPolicy, apply_policy
from duplio.cli.app import SIZE, cli, get_app, handle_errors, progress_for, run_cancellable
from duplio.cli.cmd_drive import guard_destination, guard_options, make_limiter
from duplio.cli.render import t
from duplio.errors import ConfigError
from duplio.scan.filters import ScanFilter
from duplio.util.fmt import format_ns, human_duration, human_size, parse_duration, plural
from duplio.util.text import printable


def open_repo(app, location) -> Repository:  # type: ignore[no-untyped-def]
    """Open a repository, asking for its password (up to three times) when it is encrypted."""
    password: str | None = _password_from_file(app)
    given = password is not None
    interactive = sys.stdin.isatty()
    for _ in range(3):
        try:
            return Repository.open(location, password=password)
        except PasswordRequired:
            if not interactive:
                raise
        except WrongPassword:
            if not interactive or given or password is None:
                raise  # a password from a file or the environment is never retried
            app.console.print(Text("Wrong password.", style="red"))
        password = getpass.getpass(f"Password for {location.display}: ")
        given = False
    raise WrongPassword("wrong password (three attempts)")


def _password_from_file(app) -> str | None:  # type: ignore[no-untyped-def]
    if not app.password_file:
        return None
    try:
        with open(app.password_file, encoding="utf-8") as handle:
            return handle.readline().rstrip("\r\n") or None
    except OSError as exc:
        raise ConfigError(f"cannot read the password file {app.password_file}: {exc}") from exc


def _repo(app, text: str) -> Repository:  # type: ignore[no-untyped-def]
    return open_repo(app, app.registry.open(text))


def _new_password(app) -> str:  # type: ignore[no-untyped-def]
    password = _password_from_file(app) or password_from_environment()
    if password is not None:
        return password
    if not sys.stdin.isatty():
        raise ConfigError("an encrypted repository needs a password: set DUPLIO_REPO_PASSWORD or DUPLIO_REPO_PASSWORD_FILE")
    for _ in range(3):
        first = getpass.getpass("New repository password: ")
        if len(first) < MIN_PASSWORD_LEN:
            app.console.print(Text(f"Use at least {MIN_PASSWORD_LEN} characters.", style="yellow"))
            continue
        if getpass.getpass("Repeat the password: ") == first:
            return first
        app.console.print(Text("The two entries differ.", style="yellow"))
    raise ConfigError("no password was set")


@cli.group()
@click.option("--password-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read the repository password from the first line of this file.")
def backup(password_file: str | None) -> None:
    """Versioned, deduplicated snapshots of folders, phones and shares.

    \b
    duplio backup init /mnt/usb/duplio-repo
    duplio backup run ~/Documents /mnt/usb/duplio-repo
    duplio backup run adb:/sdcard /mnt/usb/duplio-repo --label phone
    duplio backup snapshots /mnt/usb/duplio-repo
    duplio backup restore /mnt/usb/duplio-repo latest ~/Restored
    duplio backup forget /mnt/usb/duplio-repo --keep-daily 7 --keep-monthly 12 --prune

    Encrypted repositories: 'backup init --encrypt'; the password comes from a prompt,
    --password-file, or the DUPLIO_REPO_PASSWORD environment variable.
    """
    get_app().password_file = password_file


@backup.command("init")
@click.argument("repo")
@click.option("--chunk-size", type=SIZE, default=DEFAULT_CHUNK, show_default=True,
              help="Block size (fixed) or average chunk size (cdc).")
@click.option("--chunker", type=click.Choice(["fixed", "cdc"]), default="fixed", show_default=True,
              help="fixed = fast, shares whole files and edits that keep bytes in place · cdc = content-defined, also "
                   "shares data after bytes are inserted into big files (needs numpy: pip install 'duplio[cdc]').")
@click.option("--compression", type=click.Choice(["auto", "always", "never"]), default="auto", show_default=True)
@click.option("--encrypt", is_flag=True, help="Encrypt everything before it is stored (AES-256-GCM; needs a password).")
@handle_errors
def backup_init(repo: str, chunk_size: int, chunker: str, compression: str, encrypt: bool) -> None:
    """Create a new, empty repository."""
    app = get_app()
    password = _new_password(app) if encrypt else None
    r = Repository.init(app.registry.open(repo), chunk_size=chunk_size, compression=compression, password=password,
                        chunker=chunker)
    app.console.print(Text.assemble(("Repository created: ", "green"), r.loc.display, (f"  (id {r.config['id'][:12]})", "dim")))
    if encrypt:
        app.console.print(Panel(Text("Everything in this repository is encrypted with a key protected by your password. "
                                     "If you lose the password the backups cannot be recovered — there is no reset. "
                                     "Store it somewhere safe, separate from the backup.", style="yellow"),
                                title="Keep your password safe", border_style="yellow"))


@backup.command("run")
@click.argument("source")
@click.argument("repo")
@click.option("-l", "--label", default="", help="Name this backup series (e.g. 'phone', 'laptop-docs').")
@click.option("-t", "--tag", "tags", multiple=True)
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("-i", "--include", multiple=True, metavar="GLOB")
@click.option("--rehash", is_flag=True, help="Read every file even if unchanged since the last snapshot.")
@click.option("--ignore-ctime", is_flag=True,
              help="Do not read a file again just because its change time moved (permissions, extended attributes); "
                   "size and modification time must still match.")
@click.option("--cross-filesystems", is_flag=True)
@guard_options
@handle_errors
def backup_run(source: str, repo: str, label: str, tags: tuple[str, ...], exclude: tuple[str, ...],
               include: tuple[str, ...], rehash: bool, ignore_ctime: bool, cross_filesystems: bool, bwlimit: str | None,
               expect_mounted: bool | None, sentinel: str | None) -> None:
    """Snapshot SOURCE into REPO (only new data is stored)."""
    app = get_app()
    repo_loc = app.registry.open(repo)
    guard_destination(app, repo_loc, expect_mounted=expect_mounted, sentinel=sentinel)
    r = open_repo(app, repo_loc)
    r.limiter = make_limiter(bwlimit)
    src = app.registry.open(source)
    flt = ScanFilter.for_backup(include=list(include), exclude=list(app.settings.exclude) + list(exclude))

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return r.backup(src, scan_filter=flt, label=label, tags=list(tags), rehash=rehash, ignore_ctime=ignore_ctime,
                            one_file_system=not cross_filesystems, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    s = report.snapshot
    body = [
        Text.assemble(("Snapshot ", "dim"), (s.id, "bold"), ("  ", ""), (s.source, "dim")),
        Text(f"{s.files:,} files · {human_size(s.bytes)} · {s.reused_files:,} unchanged (not re-read)"),
        Text.assemble("New data: ", (human_size(s.new_bytes), "bold"), f" → stored as {human_size(s.stored_bytes)} after deduplication/compression"),
    ]
    if report.errors:
        body.append(Text(f"{len(report.errors):,} files could not be read:", style="yellow"))
        body.extend(Text(f"  {p}: {e}", style="yellow") for p, e in report.errors[:10])
    if report.changed_during_backup:
        body.append(Text(f"{len(report.changed_during_backup):,} files kept changing while being read. They are stored as "
                         "read but flagged (restore warns about them) and are read again next time:", style="yellow"))
        body.extend(Text(f"  {p}", style="yellow") for p in report.changed_during_backup[:10])
        body.append(Text("For a consistent copy of live data (databases, VM disks) back up from a file-system snapshot "
                         "(see the README, 'Files that change while they are backed up').", style="dim"))
    from rich.console import Group

    clean = not report.errors and not report.changed_during_backup
    app.console.print(Panel(Group(*body), title="Backup complete" if clean else "Backup finished with problems",
                            border_style="green" if clean else "yellow"))
    if not clean:
        sys.exit(1)


@backup.command("snapshots")
@click.argument("repo")
@handle_errors
def backup_snapshots(repo: str) -> None:
    """List snapshots."""
    app = get_app()
    r = _repo(app, repo)
    snaps = r.snapshots()
    if not snaps:
        app.console.print("No snapshots yet.")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Snapshot", "Time", "Label", "Source", "Files", "Size", "New", "Problems", "Tags"):
        table.add_column(col, no_wrap=col not in ("Source",))
    for s in snaps:
        table.add_row(t(s.id), t(s.time[:19].replace("T", " ")), t(s.label), t(s.source), f"{s.files:,}",
                      human_size(s.bytes), human_size(s.new_bytes),
                      t(f"{s.errors} unreadable, {s.unstable} unstable", "yellow") if s.problems else "",
                      t(",".join(s.tags)))
    app.console.print(table)


@backup.command("ls")
@click.argument("repo")
@click.argument("snapshot", default="latest")
@click.argument("prefix", default="")
@handle_errors
def backup_ls(repo: str, snapshot: str, prefix: str) -> None:
    """List files in a snapshot (optionally under PREFIX)."""
    app = get_app()
    r = _repo(app, repo)
    snap = r.find_snapshot(snapshot)
    for e in r.tree(snap.id):
        if not prefix or e.path == prefix or e.path.startswith(prefix.rstrip("/") + "/"):
            click.echo(f"{human_size(e.size):>10}  {format_ns(e.mtime_ns)}  {printable(e.path)}")


@backup.command("restore")
@click.argument("repo")
@click.argument("snapshot")
@click.argument("target")
@click.option("-i", "--include", multiple=True, metavar="GLOB", help="Only restore matching paths.")
@click.option("--conflict", type=click.Choice(["skip", "rename", "overwrite"]), default="skip", show_default=True,
              help="When a different file already exists at the target path.")
@handle_errors
def backup_restore(repo: str, snapshot: str, target: str, include: tuple[str, ...], conflict: str) -> None:
    """Restore a snapshot (or part of it) into TARGET. Every chunk is verified."""
    app = get_app()
    r = _repo(app, repo)
    dst = app.registry.open(target)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return r.restore(snapshot, dst, include=list(include), conflict=conflict, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    app.console.print(Text.assemble((f"{report.restored:,} restored", "bold green"), f" ({human_size(report.bytes)}) · "
                                    f"{report.unchanged:,} already identical · {len(report.conflicts):,} conflicts skipped · "
                                    f"{len(report.renamed):,} restored beside existing files"))
    for p, e in report.errors[:20]:
        app.console.print(Text(f"{p}: {e}", style="red"))
    if report.unstable:
        app.console.print(Text(f"{plural(len(report.unstable), 'restored file')} had been changing while it was backed up and "
                               "may be internally inconsistent (for example a half-written database): "
                               + ", ".join(printable(p) for p in report.unstable[:5])
                               + ("…" if len(report.unstable) > 5 else ""), style="yellow"))
    if report.conflicts:
        app.console.print(Text("Use --conflict rename to restore conflicting files next to the existing ones.", style="dim"))
    if report.errors:
        sys.exit(1)


@backup.command("check")
@click.argument("repo")
@click.option("--read-data", is_flag=True, help="Re-read and verify every chunk (detects bit rot).")
@click.option("--subset", type=click.FloatRange(0.0001, 1.0), default=None, help="With --read-data: verify a random fraction, e.g. 0.1.")
@handle_errors
def backup_check(repo: str, read_data: bool, subset: float | None) -> None:
    """Verify repository integrity."""
    app = get_app()
    r = _repo(app, repo)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return r.check(read_data=read_data, subset=subset, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    app.console.print(f"{report.snapshots:,} snapshots · {report.objects:,} objects · {report.orphans:,} unreferenced (removed by prune)")
    if read_data:
        app.console.print(f"{report.read_objects:,} objects re-read and verified")
    if report.orphan_trees:
        app.console.print(Text(
            f"{plural(len(report.orphan_trees), 'snapshot')} {'has' if len(report.orphan_trees) == 1 else 'have'} a file list "
            "but no summary (a lost file, or an interrupted backup or forget): " + ", ".join(report.orphan_trees[:5])
            + ("…" if len(report.orphan_trees) > 5 else "")
            + ". 'duplio backup repair' brings them back; until then prune keeps their data for a week.", style="yellow"))
    if report.ok:
        app.console.print(Text("Repository is healthy. ✓", style="bold green"))
        return
    for snap, missing in report.missing.items():
        in_trash = report.in_trash.get(snap, 0)
        extra = f" ({in_trash} of them are in the trash: 'duplio backup trash restore')" if in_trash else ""
        app.console.print(Text(f"snapshot {snap}: {len(missing)} chunks missing{extra}", style="red"))
    for oid in report.corrupt[:20]:
        app.console.print(Text(f"corrupt chunk {oid}", style="red"))
    for line in report.unreadable_trees:
        app.console.print(Text(f"unreadable snapshot {line}", style="red"))
    for line in report.unreadable_summaries:
        app.console.print(Text(f"unreadable snapshot summary {line}", style="red"))
    sys.exit(1)


def _policy(kw: dict[str, Any]) -> RetentionPolicy:
    return RetentionPolicy(
        keep_last=kw["keep_last"], keep_hourly=kw["keep_hourly"], keep_daily=kw["keep_daily"],
        keep_weekly=kw["keep_weekly"], keep_monthly=kw["keep_monthly"], keep_yearly=kw["keep_yearly"],
        keep_within_s=parse_duration(kw["keep_within"]) if kw.get("keep_within") else 0.0,
        keep_tags=list(kw.get("keep_tag") or []),
    )


@backup.command("forget")
@click.argument("repo")
@click.option("--keep-last", type=int, default=0)
@click.option("--keep-hourly", type=int, default=0)
@click.option("--keep-daily", type=int, default=0)
@click.option("--keep-weekly", type=int, default=0)
@click.option("--keep-monthly", type=int, default=0)
@click.option("--keep-yearly", type=int, default=0)
@click.option("--keep-within", default=None, help="Keep everything newer than this, e.g. 14d.")
@click.option("--keep-tag", multiple=True)
@click.option("--apply", "do_apply", is_flag=True, help="Actually forget (default: only show the decision).")
@click.option("--prune", is_flag=True, help="Free the space right away.")
@handle_errors
def backup_forget(repo: str, do_apply: bool, prune: bool, **kw: Any) -> None:
    """Thin out old snapshots with a retention policy (dry run unless --apply)."""
    app = get_app()
    r = _repo(app, repo)
    decision = apply_policy(r.snapshots(), _policy(kw))
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Snapshot", "Time", "Label", "Decision"):
        table.add_column(col)
    for snap, reasons in decision.keep:
        table.add_row(t(snap.id), t(snap.time[:19].replace("T", " ")), t(snap.label), t("keep: " + ", ".join(reasons), "green"))
    for snap in decision.remove:
        table.add_row(t(snap.id), t(snap.time[:19].replace("T", " ")), t(snap.label), t("forget", "red"))
    app.console.print(table)
    if not decision.remove:
        app.console.print("Nothing to forget.")
        return
    if not do_apply:
        app.console.print(Text(f"Dry run: {plural(len(decision.remove), 'snapshot')} would be forgotten. Add --apply.", style="yellow"))
        return
    with r.lock("forget") as lease:
        for snap in decision.remove:
            lease.check()
            r.delete_snapshot(snap.id)
    app.console.print(Text(f"Forgot {plural(len(decision.remove), 'snapshot')}.", style="green"))
    if prune:
        _print_prune(app, r.prune())


def _print_prune(app, report) -> None:  # type: ignore[no-untyped-def]
    verb = "Would set aside" if report.dry_run else "Set aside"
    if report.objects:
        app.console.print(Text(f"{verb} {human_size(report.bytes)} ({report.objects:,} chunks) nobody uses.", style="green"))
    else:
        app.console.print("Nothing to set aside.")
    if report.too_young:
        app.console.print(Text(f"{report.too_young:,} unused chunks are newer than the safety margin and were left alone.", style="dim"))
    if report.purged_objects:
        verb = "Would delete" if report.dry_run else "Deleted"
        app.console.print(Text(f"{verb} {human_size(report.purged_bytes)} that had been in the trash long enough.", style="green"))
    if report.trees_protected:
        app.console.print(Text(f"{plural(len(report.trees_protected), 'snapshot')} without a summary are protected for now "
                               "('duplio backup repair' brings them back).", style="yellow"))
    for line in report.trees_skipped:
        app.console.print(Text(f"left alone, damaged and belonging to no snapshot: {line}", style="yellow"))
    if report.batch and not report.dry_run:
        app.console.print(Text("Set-aside data stays recoverable ('duplio backup trash') until a later prune removes it.", style="dim"))


@backup.command("prune")
@click.argument("repo")
@click.option("--dry-run", is_flag=True)
@click.option("--grace", default="1d", show_default=True,
              help="Keep set-aside data in the repository's trash this long before deleting it (0 = delete at once).")
@click.option("--min-age", default="1d", show_default=True,
              help="Never touch data written more recently than this (protects a backup that is still running; 0 = off).")
@click.option("--immediate", is_flag=True, help="Same as --grace 0 --min-age 0: free the space now, no recovery.")
@handle_errors
def backup_prune(repo: str, dry_run: bool, grace: str, min_age: str, immediate: bool) -> None:
    """Set aside chunks no snapshot uses; later runs delete them for good.

    Space comes back after the grace period, not at once, so that a mistake in what counts as "unused" can be
    undone with 'duplio backup trash restore'.
    """
    app = get_app()
    r = _repo(app, repo)
    grace_s = 0.0 if immediate else _seconds(grace)
    min_age_s = 0.0 if immediate else _seconds(min_age)
    _print_prune(app, r.prune(dry_run=dry_run, grace_s=grace_s, min_age_s=min_age_s))


def _seconds(text: str) -> float:
    return 0.0 if text.strip() in ("0", "0s") else float(parse_duration(text))


@backup.command("repair")
@click.argument("repo")
@handle_errors
def backup_repair(repo: str) -> None:
    """Bring back snapshots whose summary file was lost (their file list is still there)."""
    app = get_app()
    ids = _repo(app, repo).repair()
    if ids:
        app.console.print(Text(f"Recovered {plural(len(ids), 'snapshot')}: " + ", ".join(ids), style="green"))
    else:
        app.console.print("Nothing to repair.")


@backup.group("trash")
def backup_trash() -> None:
    """Data that prune has set aside (recoverable until it is purged)."""


@backup_trash.command("list")
@click.argument("repo")
@handle_errors
def trash_list(repo: str) -> None:
    app = get_app()
    batches = _repo(app, repo).trash()
    if not batches:
        app.console.print("The trash is empty.")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col in ("Batch", "Chunks", "Size", "File lists"):
        table.add_column(col)
    for b in batches:
        table.add_row(t(b.name), f"{b.objects:,}", human_size(b.bytes), str(b.trees))
    app.console.print(table)


@backup_trash.command("restore")
@click.argument("repo")
@click.argument("batch", required=False)
@handle_errors
def trash_restore(repo: str, batch: str | None) -> None:
    """Put set-aside chunks (and snapshots) back; all batches unless BATCH is given."""
    app = get_app()
    chunks, snaps = _repo(app, repo).trash_restore(batch)
    app.console.print(Text(f"Restored {chunks:,} chunks and {plural(snaps, 'snapshot')}.", style="green"))


@backup_trash.command("purge")
@click.argument("repo")
@click.option("--older-than", default=None, help="Only batches older than this, e.g. 7d (default: everything).")
@click.option("--dry-run", is_flag=True)
@click.option("-y", "--yes", is_flag=True, help="Do not ask.")
@handle_errors
def trash_purge(repo: str, older_than: str | None, dry_run: bool, yes: bool) -> None:
    """Delete set-aside data for good (this cannot be undone)."""
    app = get_app()
    r = _repo(app, repo)
    age = _seconds(older_than) if older_than else 0.0
    files, size = r.trash_purge(older_than_s=age, dry_run=True)
    if not files:
        app.console.print("Nothing to purge.")
        return
    if dry_run:
        app.console.print(Text(f"Would delete {files:,} files ({human_size(size)}).", style="yellow"))
        return
    if not yes and not (sys.stdin.isatty() and click.confirm(f"Permanently delete {files:,} files ({human_size(size)})?")):
        raise ConfigError("not confirmed; nothing was deleted (use --yes to skip the question)")
    files, size = r.trash_purge(older_than_s=age)
    app.console.print(Text(f"Deleted {files:,} files ({human_size(size)}).", style="green"))


@backup.command("diff")
@click.argument("repo")
@click.argument("snapshot_a")
@click.argument("snapshot_b", default="latest")
@handle_errors
def backup_diff(repo: str, snapshot_a: str, snapshot_b: str) -> None:
    """What changed between two snapshots."""
    app = get_app()
    r = _repo(app, repo)
    changes = r.diff(snapshot_a, snapshot_b)
    marks = {"added": ("+", "green"), "removed": ("-", "red"), "modified": ("M", "yellow"), "metadata": ("m", "dim")}
    for kind, paths in changes.items():
        mark, style = marks[kind]
        for path in paths:
            app.console.print(Text(f"{mark} {path}", style=style))
    app.console.print(Text(" · ".join(f"{len(v):,} {k}" for k, v in changes.items()), style="bold"))


@backup.command("stats")
@click.argument("repo")
@handle_errors
def backup_stats(repo: str) -> None:
    """Storage used versus data protected."""
    app = get_app()
    s = _repo(app, repo).stats()
    ratio = (s["logical_bytes"] / s["stored_bytes"]) if s["stored_bytes"] else 0
    app.console.print(f"{s['snapshots']:,} snapshots protect {human_size(s['logical_bytes'])} using {human_size(s['stored_bytes'])} "
                      f"in {s['objects']:,} chunks (×{ratio:.1f} efficiency)")


@backup.command("find")
@click.argument("repo")
@click.argument("pattern")
@click.option("-s", "--snapshot", default=None, help="Search only this snapshot (default: all).")
@click.option("--regex", is_flag=True, help="PATTERN is a regular expression instead of a glob such as '*.docx'.")
@click.option("--limit", type=int, default=50, show_default=True)
@handle_errors
def backup_find(repo: str, pattern: str, snapshot: str | None, regex: bool, limit: int) -> None:
    """Find a file by name in every snapshot — including files deleted long ago.

    \b
    duplio backup find /mnt/usb/repo '*tax*2023*.pdf'
    duplio backup find /mnt/usb/repo 'invoice-\\d+\\.pdf' --regex
    """
    app = get_app()
    r = _repo(app, repo)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return r.find(pattern, snapshot=snapshot, regex=regex, progress=progress)

    hits = run_cancellable(work)
    if not hits:
        app.console.print("Nothing matches.")
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col, justify in (("Path", "left"), ("Versions", "right"), ("In snapshots", "right"), ("Latest size", "right"),
                         ("Last seen", "left")):
        table.add_column(col, justify=justify, overflow="fold")  # type: ignore[arg-type]
    for h in hits[:limit]:
        last = max(h.versions, key=lambda v: max(v.times))
        table.add_row(t(h.path), str(len(h.versions)), str(h.snapshot_count), human_size(last.size),
                      t(max(last.times)[:19].replace("T", " ")))
    app.console.print(table)
    if len(hits) > limit:
        app.console.print(Text(f"… {len(hits) - limit:,} more matches (use --limit)", style="dim"))
    app.console.print(Text("See every version of one file with: duplio backup history REPO PATH", style="dim"))


@backup.command("history")
@click.argument("repo")
@click.argument("path")
@handle_errors
def backup_history(repo: str, path: str) -> None:
    """Every different version of one file (exact path as shown by 'backup find')."""
    app = get_app()
    found = _repo(app, repo).history(path.strip("/"))
    if found is None:
        raise ConfigError(f"{path!r} is in no snapshot; check the exact path with 'duplio backup find'")
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    for col, justify in (("#", "right"), ("Size", "right"), ("Modified", "left"), ("First seen", "left"),
                         ("Last seen", "left"), ("Snapshots", "right")):
        table.add_column(col, justify=justify)  # type: ignore[arg-type]
    for i, v in enumerate(sorted(found.versions, key=lambda v: min(v.times)), 1):
        table.add_row(str(i), human_size(v.size), t(format_ns(v.mtime_ns)), t(min(v.times)[:19].replace("T", " ")),
                      t(max(v.times)[:19].replace("T", " ")), str(len(v.snapshots)))
    app.console.print(table)
    first = min(found.versions, key=lambda v: min(v.times))
    app.console.print(Text(f"Bring one back with: duplio backup restore REPO {first.snapshots[0]} TARGET --include "
                           f"'{printable(path)}'", style="dim"))


@backup.command("export")
@click.argument("repo")
@click.argument("snapshot")
@click.argument("archive", type=click.Path(dir_okay=False))
@click.option("-i", "--include", multiple=True, metavar="GLOB", help="Only export matching paths.")
@handle_errors
def backup_export(repo: str, snapshot: str, archive: str, include: tuple[str, ...]) -> None:
    """Write a snapshot as one .zip / .tar / .tar.gz / .tar.xz file (plaintext, even from an encrypted repository)."""
    app = get_app()
    r = _repo(app, repo)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return export_snapshot(r, snapshot, archive, include=list(include) or None, progress=progress, cancel=cancel)

    files, nbytes = run_cancellable(work)
    app.console.print(Text.assemble(("Wrote ", "green"), t(archive), f"  ({plural(files, 'file')}, {human_size(nbytes)})"))
    if r.encrypted:
        app.console.print(Text("The archive is NOT encrypted. Protect it, or delete it after use.", style="yellow"))


@backup.command("passwd")
@click.argument("repo")
@handle_errors
def backup_passwd(repo: str) -> None:
    """Change an encrypted repository's password (the data is not re-encrypted, so this is instant)."""
    app = get_app()
    r = _repo(app, repo)
    if not r.encrypted:
        raise ConfigError("this repository is not encrypted")
    if not sys.stdin.isatty():
        raise ConfigError("changing the password needs a terminal (the new password is typed twice)")
    for _ in range(3):
        first = getpass.getpass("New password: ")
        if len(first) < MIN_PASSWORD_LEN:
            app.console.print(Text(f"Use at least {MIN_PASSWORD_LEN} characters.", style="yellow"))
            continue
        if getpass.getpass("Repeat the new password: ") != first:
            app.console.print(Text("The two entries differ.", style="yellow"))
            continue
        r.change_password(first)
        app.console.print(Text("Password changed. A copy of the old duplio-repo.json (for example in a backup of the repository) still opens with the old password.", style="green"))
        return
    raise ConfigError("the password was not changed")


@backup.command("unlock")
@click.argument("repo")
@handle_errors
def backup_unlock(repo: str) -> None:
    """Remove a stale repository lock (only if no backup is running!)."""
    app = get_app()
    removed = _repo(app, repo).force_unlock()
    app.console.print("Lock removed." if removed else "The repository was not locked.")


# ---------------------------------------------------------------------------


@cli.command()
@click.argument("source")
@click.argument("dest")
@click.option("--delete", "delete_extraneous", is_flag=True,
              help="Also retire files missing from SOURCE (moved to .duplio-versions, never deleted).")
@click.option("--checksum", is_flag=True, help="Compare contents, not just size and time.")
@click.option("--no-verify", is_flag=True, help="Skip re-reading copies to verify them.")
@click.option("--keep-versions", default=None, help="Delete versions older than this after the run, e.g. 90d.")
@click.option("-x", "--exclude", multiple=True, metavar="GLOB")
@click.option("--max-change", type=click.FloatRange(0.01, 1.0), default=0.5, show_default=True,
              help="Safety brake: refuse if more than this fraction of DEST would change.")
@click.option("--force", is_flag=True, help="Override the safety brake.")
@click.option("--dry-run", is_flag=True)
@click.option("-y", "--yes", is_flag=True)
@click.option("--use-cache", is_flag=True,
              help="Trust cached digests for --checksum and rename detection (faster; misses content that changed "
                   "without its size or time changing).")
@guard_options
@handle_errors
def mirror(source: str, dest: str, delete_extraneous: bool, checksum: bool, no_verify: bool, keep_versions: str | None,
           exclude: tuple[str, ...], max_change: float, force: bool, dry_run: bool, yes: bool, bwlimit: str | None,
           expect_mounted: bool | None, sentinel: str | None, use_cache: bool) -> None:
    """Keep DEST an exact, verified copy of SOURCE — with old versions kept.

    \b
    duplio mirror adb:/sdcard/DCIM ~/Phone-Mirror/DCIM
    duplio mirror ~/Documents smb://me@nas/home/Documents --delete --keep-versions 90d
    """
    app = get_app()
    src, dst = app.registry.open(source), app.registry.open(dest)
    guard_destination(app, dst, expect_mounted=expect_mounted, sentinel=sentinel)
    options = MirrorOptions(delete_extraneous=delete_extraneous, checksum=checksum, verify=not no_verify,
                            dry_run=dry_run, max_change_ratio=max_change, force=force, keep_versions=keep_versions,
                            limiter=make_limiter(bwlimit))
    flt = ScanFilter.for_backup(exclude=list(app.settings.exclude) + list(exclude))
    cache = app.cache(use_cache)

    def planning(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return plan_mirror(src, dst, scan_filter=flt, options=options, cache=cache, progress=progress, cancel=cancel)

    mplan = run_cancellable(planning)
    for w in mplan.warnings:
        app.console.print(Text(w, style="yellow"))
    summary = (f"{mplan.count('copy'):,} new · {mplan.count('update'):,} changed · {mplan.count('move'):,} moved · "
               f"{mplan.count('archive'):,} retired · {mplan.unchanged:,} unchanged · {human_size(mplan.bytes_to_copy)} to copy")
    app.console.print(Panel(Text(summary), title=f"Mirror {src.display} → {dst.display}", border_style="cyan"))
    if not mplan.actions:
        app.console.print(Text("Already in sync. ✓", style="green"))
        return
    for a in mplan.actions[:15]:
        mark = {"copy": "+", "update": "M", "move": "→", "archive": "↓"}[a.kind]
        extra = f"  (from {a.from_rel})" if a.from_rel else ""
        app.console.print(Text(f" {mark} {a.rel}{extra}", style="dim"))
    if len(mplan.actions) > 15:
        app.console.print(Text(f" … {len(mplan.actions) - 15:,} more", style="dim"))
    if dry_run:
        return
    guard_destination(app, dst, expect_mounted=False, sentinel=None, needed_bytes=mplan.bytes_to_copy, probe_timeout=None)
    disruptive = mplan.count("update") + mplan.count("archive")
    if disruptive and not yes:
        if not sys.stdin.isatty():
            raise ConfigError("not running interactively; pass --yes")
        if not click.confirm(f"{plural(disruptive, 'destination file')} will be moved to .duplio-versions. Continue?", default=True):
            return

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return run_mirror(mplan, options=options, progress=progress, cancel=cancel)

    report = run_cancellable(work)
    done = " · ".join(f"{v:,} {k}" for k, v in report.done.items()) or "nothing"
    app.console.print(Text.assemble(("Done: ", "bold green"), done, f" · {human_size(report.bytes_copied)} copied"))
    if report.versions_dir:
        app.console.print(Text(f"Previous versions: {dst.backend.display(report.versions_dir)}", style="dim"))
    for rel, err in report.errors[:20]:
        app.console.print(Text(f"{rel}: {err}", style="red"))
    if report.errors:
        sys.exit(1)


@cli.command("ingest")
@click.argument("source")
@click.argument("library")
@click.option("--layout", default=DEFAULT_LAYOUT, show_default=True,
              help=f"Folder layout. Tokens: {', '.join('{' + tok + '}' for tok in LAYOUT_TOKENS)}.")
@click.option("--all-files", is_flag=True, help="Import every file, not only photos and videos.")
@click.option("--device", "device_label", default="", help="Value for the {device} token.")
@click.option("--reimport-deleted", is_flag=True, help="Re-import files you deleted from the library since.")
@click.option("--free-source", is_flag=True, help="Afterwards delete from SOURCE every file verified in the library.")
@click.option("--dry-run", is_flag=True)
@click.option("-y", "--yes", is_flag=True)
@click.option("--use-cache", is_flag=True,
              help="Trust cached digests of library files when deciding a photo is already there (faster; misses "
                   "library files that changed without their size or time changing).")
@guard_options
@handle_errors
def ingest_cmd(source: str, library: str, layout: str, all_files: bool, device_label: str, reimport_deleted: bool,
               free_source: bool, dry_run: bool, yes: bool, bwlimit: str | None, expect_mounted: bool | None,
               sentinel: str | None, use_cache: bool) -> None:
    """Import new photos/videos from a phone into a dated library, skipping anything already there.

    \b
    duplio ingest adb:/sdcard/DCIM ~/Pictures/Library
    duplio ingest "/run/user/1000/gvfs/mtp:host=Pixel_8/Internal shared storage/DCIM" ~/Pictures/Library
    duplio ingest /media/SDCARD/DCIM ~/Pictures/Library --layout "{year}/{date}/{name}"
    """
    app = get_app()
    src, lib = app.registry.open(source), app.registry.open(library)
    guard_destination(app, lib, expect_mounted=expect_mounted, sentinel=sentinel)
    if free_source and not dry_run and not yes:
        if not sys.stdin.isatty():
            raise ConfigError("--free-source deletes files on the source; confirm interactively or pass --yes")
        if not click.confirm(f"After importing, delete from {src.display} every file verified in the library?", default=False):
            free_source = False
    options = IngestOptions(layout=layout, media_only=not all_files, dry_run=dry_run, free_source=free_source,
                            reimport_deleted=reimport_deleted, device_label=device_label,
                            limiter=make_limiter(bwlimit))
    cache = app.cache(use_cache)

    def work(cancel):  # type: ignore[no-untyped-def]
        with progress_for(app) as progress:
            return ingest(src, lib, options=options, cache=cache, dirs=app.dirs, progress=progress, cancel=cancel,
                          confirm_free=free_source)

    import time

    started = time.monotonic()
    report = run_cancellable(work)
    counts = {s: report.count(s) for s in ("imported", "duplicate", "previously-imported", "would-import", "maybe-duplicate", "failed")}
    parts = [f"{v:,} {k.replace('-', ' ')}" for k, v in counts.items() if v]
    app.console.print(Panel(Text(" · ".join(parts) or "No media found.") , title=f"Import into {lib.display}",
                            subtitle=f"{human_size(report.imported_bytes)} imported in {human_duration(time.monotonic() - started)}",
                            border_style="green" if not counts["failed"] else "yellow"))
    for item in [i for i in report.items if i.status == "imported"][:10]:
        app.console.print(Text(f" + {item.dest_rel}  ({item.date_source})", style="dim"))
    if report.freed:
        app.console.print(Text(f"Freed {human_size(report.freed_bytes)} on the source ({report.freed:,} files).", style="green"))
    for rel, err in report.errors[:20]:
        app.console.print(Text(f"{rel}: {err}", style="red"))
    if counts["failed"]:
        sys.exit(1)
