"""Rich renderers for every kind of result. User data is never parsed as markup."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from duplio import __version__
from duplio.assets.logo import logo_text
from duplio.compare.model import Confidence, Mode, ScanResult
from duplio.plan.executor import ApplyReport
from duplio.plan.model import ACTION_HELP, ActionType, Plan
from duplio.plan.planner import decide
from duplio.plan.rules import parse_rules
from duplio.plan.undo import UndoReport
from duplio.util.fmt import format_ns, human_duration, human_size, plural
from duplio.util.text import printable

ACCENT = "red3"

def t(value: object, style: str | None = None) -> Text:
    """Text for any value; file names are made safe to display."""
    return Text(printable(str(value)), style=style or "")


def banner(console: Console) -> None:
    console.print(logo_text())


def root_label(result: ScanResult, index: int) -> str:
    root = result.inventory.roots[index]
    return root.label or root.location.display


def scan_summary(console: Console, result: ScanResult) -> None:
    inv = result.inventory
    stats = result.stats
    roots = Table(box=None, show_header=False, padding=(0, 1))
    roots.add_column(style="bold")
    roots.add_column()
    roots.add_column(style="dim")
    roots.add_column(style="magenta")
    for r in inv.roots:
        roots.add_row(str(r.index + 1), t(r.location.display), t(r.caps.label), "reference" if r.is_reference else "")

    lines = [
        Text.assemble(
            (f"{len(inv.entries):,}", "bold"), " files · ", (human_size(inv.total_bytes), "bold"),
            f" scanned in {human_duration(result.elapsed)}",
        )
    ]
    if result.options.mode is Mode.UNIQUE:
        missing_bytes = sum(e.size for e in result.unique)
        lines.append(
            Text.assemble(
                (f"{len(result.unique):,}", f"bold {ACCENT}"), " file" if len(result.unique) == 1 else " files",
                " (", (human_size(missing_bytes), "bold"), ") NOT in the reference locations · ",
                (f"{sum(1 for g in result.groups for m in g.members if not result.inventory.roots[m.root].is_reference):,}", "bold green"),
                " already have copies there",
            )
        )
    else:
        noun = "group" if len(result.groups) == 1 else "groups"
        label = f"possible duplicate {noun} (name + size only)" if result.options.verify.value == "quick" else f"duplicate {noun}"
        copies = "redundant copy" if result.duplicate_files == 1 else "redundant copies"
        lines.append(
            Text.assemble(
                (f"{len(result.groups):,}", "bold"), f" {label} · ",
                (f"{result.duplicate_files:,}", "bold"), f" {copies} · ",
                (human_size(result.wasted), f"bold {ACCENT}"), " reclaimable",
            )
        )
        if result.similar:
            lines.append(Text(f"{len(result.similar):,} groups of similar (not identical) images", style="yellow"))
    evidence = (
        f"evidence: {plural(stats.size_groups, 'size bucket')} → {stats.sampled:,} sampled → "
        f"{stats.full_hashed:,} hashed"
    )
    if stats.remote_hashed:
        evidence += f" (+{stats.remote_hashed:,} on device)"
    if stats.byte_compared:
        evidence += f" → {stats.byte_compared:,} byte-compared"
    evidence += f" · {human_size(stats.bytes_read)} read · {stats.cache_hits:,} cache hits"
    if stats.hardlink_aliases:
        evidence += f" · {stats.hardlink_aliases:,} hard links recognised"
    lines.append(Text(evidence, style="dim"))
    problems = len(inv.errors) + len(stats.errors)
    if problems:
        lines.append(Text(f"{plural(problems, 'item')} could not be read (see --export for details)", style="yellow"))
    if result.unverified:
        lines.append(Text(f"{plural(len(result.unverified), 'file')} could not be verified and were left out", style="yellow"))
    console.print(
        Panel(
            Group(roots, Text(""), *lines),
            title=Text(f"Duplio scan {result.session_id}", style=f"bold {ACCENT}"),
            border_style=ACCENT,
            box=box.ROUNDED,
        )
    )


def groups_table(console: Console, result: ScanResult, *, top: int, rules: Sequence[str] | None) -> None:
    if not result.groups or top <= 0:
        return
    parsed = parse_rules(rules)
    table = Table(box=box.SIMPLE_HEAD, expand=True, title="Largest duplicate groups", title_style="bold")
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Wasted", justify="right", style=ACCENT, no_wrap=True)
    table.add_column("Copies (★ keep · ✗ remove · 🔒 locked)", ratio=1)
    for group in result.groups[:top]:
        decision = decide(group, result.inventory.roots, parsed, ActionType.QUARANTINE)
        keep_ids = {m.id for m in decision.keep}
        lines = Text()
        for i, m in enumerate(group.members):
            if i:
                lines.append("\n")
            if m.id in decision.reasons:
                lines.append("🔒 ", style="green")
            elif m.id in keep_ids:
                lines.append("★ ", style="bold green")
            else:
                lines.append("✗ ", style=ACCENT)
            lines.append(f"{m.root + 1}:", style="dim")
            lines.append(printable(m.rel))
            aliases = group.aliases.get(m.id)
            if aliases:
                lines.append(f"  (+{len(aliases)} hard link{'s' if len(aliases) > 1 else ''})", style="dim")
        table.add_row(str(group.id), human_size(group.size), human_size(group.wasted), lines)
    console.print(table)
    if len(result.groups) > top:
        console.print(Text(f"… {len(result.groups) - top:,} more groups (use --top, or --export report.html)", style="dim"))


def unique_table(console: Console, result: ScanResult, *, top: int) -> None:
    if not result.unique or top <= 0:
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True, title="Not in the reference locations", title_style="bold")
    table.add_column("Location", style="dim", no_wrap=True)
    table.add_column("File", ratio=1)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Modified", style="dim", no_wrap=True)
    for e in result.unique[:top]:
        table.add_row(str(e.root + 1), t(e.rel), human_size(e.size), format_ns(e.mtime_ns))
    console.print(table)
    if len(result.unique) > top:
        console.print(Text(f"… {len(result.unique) - top:,} more (use --top or --export)", style="dim"))


def similar_table(console: Console, result: ScanResult, *, top: int) -> None:
    if not result.similar or top <= 0:
        return
    table = Table(box=box.SIMPLE_HEAD, expand=True, title="Similar images (review only)", title_style="bold yellow")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Images (distance, pixels, size)", ratio=1)
    for g in result.similar[:top]:
        lines = Text()
        for i, (m, dist, dim) in enumerate(zip(g.members, g.distances, g.dimensions, strict=True)):
            if i:
                lines.append("\n")
            lines.append("◆ " if i == 0 else "  ", style="yellow")
            lines.append(f"{m.root + 1}:", style="dim")
            lines.append(printable(m.rel))
            lines.append(f"  d={dist} · {dim[0]}×{dim[1]} · {human_size(m.size)}", style="dim")
        table.add_row(f"S{g.id}", lines)
    console.print(table)


def plan_summary(console: Console, plan: Plan, *, top: int = 10) -> None:
    files = len(plan.items)
    action = plan.action
    style = "bold red" if action is ActionType.DELETE else "bold"
    body = [
        Text.assemble(("Action: ", "dim"), (action.value, style), "  ", (ACTION_HELP[action], "dim")),
        Text.assemble(
            ("Changes: ", "dim"), (plural(files, "file"), "bold"), " in ", plural(len(plan.group_ids), "group"),
            " · frees ", (human_size(plan.reclaim), f"bold {ACCENT}"),
        ),
        Text.assemble(("Keep rules: ", "dim"), ", ".join(plan.rules) or "manual selection"),
        Text.assemble(
            ("Safety: ", "dim"),
            f"every file is re-checked and re-verified ({plan.verify}) right before it is touched; "
            "every change is journaled.",
        ),
    ]
    if plan.skipped_groups:
        body.append(Text(f"{plural(plan.skipped_groups, 'group')} need no action (all copies locked or kept)", style="dim"))
    for note in plan.notes[:5]:
        body.append(Text(f"note: {note}", style="yellow"))
    if len(plan.notes) > 5:
        body.append(Text(f"… {len(plan.notes) - 5} more notes", style="yellow"))
    if plan.items and top > 0:
        table = Table(box=box.SIMPLE, show_header=True, expand=True)
        table.add_column("Will " + action.value, ratio=1)
        table.add_column("Kept copy", ratio=1, style="green")
        table.add_column("Size", justify="right", no_wrap=True)
        for item in plan.items[:top]:
            table.add_row(t(f"{item.victim.root + 1}:{item.victim.rel}"), t(f"{item.keeper.root + 1}:{item.keeper.rel}"),
                          human_size(item.victim.size))
        body.append(table)
        if files > top:
            body.append(Text(f"… and {files - top:,} more", style="dim"))
    console.print(Panel(Group(*body), title=Text("Plan", style="bold"), border_style="yellow" if action is not ActionType.DELETE else "red"))


def apply_summary(console: Console, report: ApplyReport) -> None:
    done, skipped, failed = report.count("done"), report.count("skipped"), report.count("failed")
    would = report.count("would-do")
    if report.dry_run:
        head = Text.assemble(("Dry run: ", "bold"), f"{would:,} files verified and ready; {skipped:,} would be skipped; {failed:,} problems")
    else:
        head = Text.assemble(
            (f"{done:,} done", "bold green"), " · ", (f"{skipped:,} skipped", "yellow"), " · ",
            (f"{failed:,} failed", "bold red" if failed else "dim"), " · freed ", (human_size(report.reclaimed), f"bold {ACCENT}"),
        )
    body: list[object] = [head]
    issues = [o for o in report.outcomes if o.status in ("skipped", "failed")]
    if issues:
        table = Table(box=box.SIMPLE, expand=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("File", ratio=1)
        table.add_column("Why", ratio=1, style="dim")
        for o in issues[:15]:
            table.add_row(t(o.status, "yellow" if o.status == "skipped" else "red"), t(o.item.victim.rel), t(o.message))
        body.append(table)
        if len(issues) > 15:
            body.append(Text(f"… {len(issues) - 15} more", style="dim"))
    if report.cancelled:
        body.append(Text("Stopped early at your request; finished items are journaled.", style="yellow"))
    if not report.dry_run and done:
        if report.action in (ActionType.QUARANTINE, ActionType.HARDLINK, ActionType.REFLINK):
            body.append(Text.assemble(("Undo any time: ", "dim"), (f"duplio undo {report.apply_id}", "bold")))
        if report.action is ActionType.QUARANTINE:
            body.append(Text("Quarantined files still use space until you run 'duplio quarantine purge'.", style="dim"))
    console.print(Panel(Group(*body), title=Text(f"Applied {report.apply_id}" if not report.dry_run else "Verification", style="bold"), border_style="green" if not failed else "red"))  # type: ignore[arg-type]


def undo_summary(console: Console, report: UndoReport) -> None:
    table = Table(box=box.SIMPLE, expand=True, title=f"Undo {report.apply_id}" + (" (dry run)" if report.dry_run else ""))
    table.add_column("Result", no_wrap=True)
    table.add_column("File", ratio=2, overflow="fold")
    table.add_column("Note", style="dim", ratio=1)
    colours = {"restored": "green", "would-restore": "cyan", "conflict": "yellow", "failed": "red", "not-undoable": "red", "already-undone": "dim"}
    for line in report.lines[:200]:
        table.add_row(t(line.status, colours.get(line.status, "")), t(line.path), t(line.message))
    console.print(table)
    console.print(Text(f"{report.count('restored'):,} restored · {report.count('conflict'):,} conflicts · {report.count('failed'):,} failed"))


def confidence_style(c: Confidence) -> str:
    return {Confidence.CERTAIN: "green", Confidence.PROBABLE: "yellow", Confidence.SIMILAR: "magenta"}[c]


def version_line() -> Text:
    return Text(f"Duplio {__version__}", style="dim")
