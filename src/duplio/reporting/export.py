"""Export scan results as JSON, CSV or a self-contained HTML report."""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path

from duplio import __version__
from duplio.compare.model import ScanResult
from duplio.compare.session import result_to_dict
from duplio.util.atomic import atomic_write_text
from duplio.util.fmt import format_ns, human_size


def to_json(result: ScanResult) -> str:
    return json.dumps(result_to_dict(result), indent=1, ensure_ascii=False)


def to_csv(result: ScanResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group", "kind", "size", "digest", "location", "path", "relative_path", "modified", "protected"])
    roots = result.inventory.roots
    for g in result.groups:
        for m in g.members:
            writer.writerow([g.id, g.confidence.label, g.size, g.digest or "", roots[m.root].location.uri,
                             m.path, m.rel, format_ns(m.mtime_ns), m.protected])
    for s in result.similar:
        for m, dist in zip(s.members, s.distances, strict=True):
            writer.writerow([f"S{s.id}", f"similar (distance {dist})", m.size, "", roots[m.root].location.uri,
                             m.path, m.rel, format_ns(m.mtime_ns), m.protected])
    for u in result.unique:
        writer.writerow(["", "unique", u.size, "", roots[u.root].location.uri, u.path, u.rel,
                         format_ns(u.mtime_ns), u.protected])
    return buf.getvalue()


_CSS = """
:root{--bg:#f7f7f8;--fg:#18181b;--muted:#6b6b76;--card:#fff;--line:#e4e4e7;--accent:#b4232c;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#0e0e11;--fg:#ececf1;--muted:#9a9aa6;--card:#17171c;--line:#2a2a33;--accent:#ef4444;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:28px 24px 8px;max-width:1100px;margin:auto}h1{margin:0;font-size:26px;letter-spacing:.5px}
h1 span{color:var(--accent)}.sub{color:var(--muted)}main{max-width:1100px;margin:auto;padding:0 24px 48px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:22px}.stat small{color:var(--muted)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;padding:8px 14px}
summary{cursor:pointer;font-weight:600}table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
td,th{text-align:left;padding:4px 6px;border-top:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:500}.tag{display:inline-block;padding:0 6px;border-radius:6px;border:1px solid var(--line);color:var(--muted);font-size:12px}
.prot{color:var(--ok)}code{font-size:12px}
"""


def to_html(result: ScanResult, *, max_groups: int = 2000) -> str:
    roots = result.inventory.roots
    esc = html.escape
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Duplio report</title><style>{_CSS}</style></head><body>",
        "<header><h1>DUPL<span>IO</span> report</h1>",
        f"<div class='sub'>Scan {esc(result.session_id)} · {esc(result.created)} · mode {esc(result.options.mode.value)} · Duplio {__version__}</div></header><main>",
        "<div class='stats'>",
        f"<div class='stat'><b>{len(result.inventory.entries):,}</b><small>files scanned</small></div>",
        f"<div class='stat'><b>{len(result.groups):,}</b><small>duplicate groups</small></div>",
        f"<div class='stat'><b>{human_size(result.wasted)}</b><small>reclaimable</small></div>",
        f"<div class='stat'><b>{len(result.similar):,}</b><small>similar-image groups</small></div>",
        f"<div class='stat'><b>{len(result.unique):,}</b><small>unique files</small></div>",
        "</div><h2>Locations</h2><table><tr><th>#</th><th>Location</th><th>Role</th><th>Storage</th></tr>",
    ]
    for r in roots:
        parts.append(f"<tr><td>{r.index + 1}</td><td><code>{esc(r.location.uri)}</code></td><td>{esc(r.role.value)}</td><td>{esc(r.caps.label)}</td></tr>")
    parts.append("</table>")
    if result.groups:
        parts.append("<h2>Duplicate groups</h2>")
        for g in result.groups[:max_groups]:
            parts.append(
                f"<details><summary>#{g.id} · {g.count} copies × {human_size(g.size)} "
                f"<span class='tag'>{esc(g.confidence.label)}</span> <span class='tag'>{esc(human_size(g.wasted))} wasted</span></summary>"
                f"<div class='sub'>{esc(g.method)}{' · ' + esc(g.digest[:16]) + '…' if g.digest else ''}</div>"
                "<table><tr><th>Location</th><th>Path</th><th>Modified</th></tr>"
            )
            for m in g.members:
                prot = " <span class='prot'>protected</span>" if m.protected else ""
                parts.append(
                    f"<tr><td>{r_label(roots, m.root)}</td><td><code>{esc(m.rel)}</code>{prot}</td><td>{esc(format_ns(m.mtime_ns))}</td></tr>"
                )
            parts.append("</table></details>")
        if len(result.groups) > max_groups:
            parts.append(f"<p class='sub'>… and {len(result.groups) - max_groups:,} more groups (see the JSON export).</p>")
    if result.similar:
        parts.append("<h2>Similar images</h2>")
        for s in result.similar[:max_groups]:
            parts.append(f"<details><summary>S{s.id} · {len(s.members)} images</summary><table><tr><th>Location</th><th>Path</th><th>Size</th><th>Pixels</th><th>Distance</th></tr>")
            for m, dist, dim in zip(s.members, s.distances, s.dimensions, strict=True):
                parts.append(f"<tr><td>{r_label(roots, m.root)}</td><td><code>{esc(m.rel)}</code></td><td>{human_size(m.size)}</td><td>{dim[0]}×{dim[1]}</td><td>{dist}</td></tr>")
            parts.append("</table></details>")
    if result.unique:
        parts.append("<h2>Files not found in the reference locations</h2><table><tr><th>Location</th><th>Path</th><th>Size</th></tr>")
        for u in result.unique[: max_groups * 5]:
            parts.append(f"<tr><td>{r_label(roots, u.root)}</td><td><code>{esc(u.rel)}</code></td><td>{human_size(u.size)}</td></tr>")
        parts.append("</table>")
    parts.append("</main></body></html>")
    return "".join(parts)


def r_label(roots, index: int) -> str:  # type: ignore[no-untyped-def]
    root = roots[index]
    return html.escape(f"{index + 1}: {root.label or root.location.display}")


def export(result: ScanResult, path: str | Path) -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        atomic_write_text(path, to_json(result))
    elif suffix == ".csv":
        atomic_write_text(path, to_csv(result))
    elif suffix in (".html", ".htm"):
        atomic_write_text(path, to_html(result))
    else:
        from duplio.errors import ConfigError

        raise ConfigError("export format is chosen by extension: .json, .csv or .html")
    return path
