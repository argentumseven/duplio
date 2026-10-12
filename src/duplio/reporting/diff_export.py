"""Export a folder comparison as JSON, CSV or a self-contained HTML page."""

from __future__ import annotations

import csv
import html
import io
from pathlib import Path
from typing import Any

from duplio import __version__
from duplio.compare.diff import KIND_ORDER, KIND_TITLE, DiffItem, DiffResult, Kind
from duplio.errors import ConfigError
from duplio.reporting.export import _CSS
from duplio.util.atomic import atomic_write_text
from duplio.util.fmt import format_ns, human_size, iso_now
from duplio.util.text import json_dumps, printable

SIDES = ("A", "B")


def summary_dict(result: DiffResult) -> dict[str, Any]:
    return {
        kind.value: {
            SIDES[s]: {"files": result.count(kind, s), "bytes": result.size(kind, s)} for s in (0, 1)
        }
        for kind in KIND_ORDER
    }


def _item_dict(item: DiffItem) -> dict[str, Any]:
    return {
        "side": SIDES[item.side],
        "kind": item.kind.value,
        "path": item.entry.rel,
        "size": item.entry.size,
        "mtime_ns": item.entry.mtime_ns,
        "counterparts": [o.rel for o in item.others],
        "newer": {1: "this side", -1: "other side", 0: ""}[item.newer] if item.kind is Kind.CHANGED else "",
        "extra_copies": item.extra_copies,
        "group": item.group,
        "note": item.note,
    }


def diff_to_dict(result: DiffResult) -> dict[str, Any]:
    roots = result.inventory.roots
    return {
        "format": "duplio-diff",
        "version": 1,
        "created": iso_now(),
        "basis": result.options.basis.value,
        "locations": {SIDES[r.index]: r.location.uri for r in roots},
        "time_tolerance_ns": result.tolerance_ns,
        "case_insensitive_paths": result.case_folded,
        "identical_content": result.identical_content,
        "identical_layout": result.identical_layout,
        "summary": summary_dict(result),
        "duplicates_within": {
            SIDES[s]: dict(zip(("files", "bytes"), result.duplicates_within(s), strict=True)) for s in (0, 1)
        },
        "errors": [list(e) for e in result.errors[:1000]],
        "items": [_item_dict(i) for i in result.items],
    }


def diff_to_json(result: DiffResult) -> str:
    return json_dumps(diff_to_dict(result), indent=1)


def diff_to_csv(result: DiffResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["location", "category", "relative_path", "size", "modified", "counterparts", "newer", "note"])
    for i in result.items:
        newer = {1: "this side", -1: "other side", 0: ""}[i.newer] if i.kind is Kind.CHANGED else ""
        writer.writerow([SIDES[i.side], i.kind.value, printable(i.entry.rel), i.entry.size, format_ns(i.entry.mtime_ns),
                         " | ".join(printable(o.rel) for o in i.others), newer, i.note])
    return buf.getvalue()


def diff_to_html(result: DiffResult, *, max_rows: int = 5000) -> str:
    def esc(text: str) -> str:
        return html.escape(printable(text))

    roots = result.inventory.roots
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Duplio comparison</title><style>{_CSS}</style></head><body>",
        "<header><h1>DUPL<span>IO</span> comparison</h1>",
        f"<div class='sub'>{esc(iso_now())} · basis {esc(result.options.basis.value)} · Duplio {__version__}</div></header><main>",
        "<table><tr><th></th><th>Location</th></tr>",
    ]
    for r in roots:
        parts.append(f"<tr><td>{SIDES[r.index]}</td><td><code>{esc(r.location.uri)}</code></td></tr>")
    parts.append("</table><div class='stats'>")
    for kind in KIND_ORDER:
        a, b = result.count(kind, 0), result.count(kind, 1)
        if a or b:
            parts.append(f"<div class='stat'><b>{a:,} / {b:,}</b><small>{esc(KIND_TITLE[kind])} (A / B)</small></div>")
    parts.append("</div>")
    for kind in (Kind.ONLY, Kind.CHANGED, Kind.MOVED, Kind.UNREADABLE, Kind.SAME):
        rows = [i for i in result.select(kind) if kind not in (Kind.CHANGED, Kind.MOVED, Kind.SAME) or i.side == 0]
        if not rows:
            continue
        parts.append(
            f"<details{' open' if kind in (Kind.ONLY, Kind.CHANGED) else ''}><summary>{esc(KIND_TITLE[kind])} · {len(rows):,}</summary>"
            "<table><tr><th>Side</th><th>Path</th><th>Size</th><th>Modified</th><th>Other side</th></tr>"
        )
        for i in rows[:max_rows]:
            other = "<br>".join(f"<code>{esc(o.rel)}</code>" for o in i.others[:5])
            parts.append(
                f"<tr><td>{SIDES[i.side]}</td><td><code>{esc(i.entry.rel)}</code></td><td>{human_size(i.entry.size)}</td>"
                f"<td>{esc(format_ns(i.entry.mtime_ns))}</td><td>{other}</td></tr>"
            )
        if len(rows) > max_rows:
            parts.append(f"<tr><td colspan='5' class='sub'>… {len(rows) - max_rows:,} more (see the JSON export)</td></tr>")
        parts.append("</table></details>")
    parts.append("</main></body></html>")
    return "".join(parts)


def export_diff(result: DiffResult, path: str | Path) -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        atomic_write_text(path, diff_to_json(result))
    elif suffix == ".csv":
        atomic_write_text(path, diff_to_csv(result))
    elif suffix in (".html", ".htm"):
        atomic_write_text(path, diff_to_html(result))
    else:
        raise ConfigError("export format is chosen by extension: .json, .csv or .html")
    return path
