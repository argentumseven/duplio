"""Persist scan results so review and apply can happen later (or in the TUI).

Only what planning needs is stored: the roots, the options, every group with
its members and aliases, unique/unverified files and summary statistics.
Sessions are gzip-compressed JSON under the state directory.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duplio.compare.model import (
    Confidence,
    DuplicateGroup,
    EngineStats,
    ScanOptions,
    ScanResult,
    SimilarGroup,
)
from duplio.errors import ConfigError
from duplio.scan.inventory import FileEntry, Inventory, Root, RootRole, make_roots
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.atomic import atomic_write_bytes
from duplio.util.text import json_dumps
from duplio.vfs.location import BackendRegistry

SESSION_FORMAT = "duplio-session"


def _entry(e: FileEntry) -> list[Any]:
    return [e.id, e.root, e.path, e.rel, e.size, e.mtime_ns, list(e.inode) if e.inode else None, e.nlink, e.protected,
            e.ctime_ns, e.offline]


def _unentry(row: list[Any]) -> FileEntry:
    return FileEntry(
        id=int(row[0]),
        root=int(row[1]),
        path=str(row[2]),
        rel=str(row[3]),
        size=int(row[4]),
        mtime_ns=int(row[5]),
        inode=tuple(row[6]) if row[6] else None,  # type: ignore[arg-type]
        nlink=int(row[7]),
        protected=bool(row[8]),
        ctime_ns=row[9] if len(row) > 9 else None,
        offline=bool(row[10]) if len(row) > 10 else False,
    )


def result_to_dict(result: ScanResult) -> dict[str, Any]:
    inv = result.inventory
    return {
        "format": SESSION_FORMAT,
        "version": 1,
        "session_id": result.session_id,
        "created": result.created,
        "elapsed": result.elapsed,
        "options": result.options.to_dict(),
        "roots": [
            {"index": r.index, "uri": r.location.uri, "role": r.role.value, "label": r.label, "kind": r.caps.label}
            for r in inv.roots
        ],
        "totals": {"files": len(inv.entries), "bytes": inv.total_bytes, "errors": len(inv.errors)},
        "groups": [
            {
                "id": g.id,
                "size": g.size,
                "digest": g.digest,
                "algorithm": g.algorithm,
                "confidence": int(g.confidence),
                "method": g.method,
                "members": [_entry(m) for m in g.members],
                "aliases": {str(k): [_entry(a) for a in v] for k, v in g.aliases.items()},
            }
            for g in result.groups
        ],
        "similar": [
            {
                "id": s.id,
                "members": [_entry(m) for m in s.members],
                "distances": s.distances,
                "dimensions": [list(d) for d in s.dimensions],
            }
            for s in result.similar
        ],
        "unique": [_entry(e) for e in result.unique],
        "unverified": [_entry(e) for e in result.unverified],
        "stats": {
            "files_considered": result.stats.files_considered,
            "size_groups": result.stats.size_groups,
            "sampled": result.stats.sampled,
            "full_hashed": result.stats.full_hashed,
            "remote_hashed": result.stats.remote_hashed,
            "byte_compared": result.stats.byte_compared,
            "bytes_read": result.stats.bytes_read,
            "cache_hits": result.stats.cache_hits,
            "hardlink_aliases": result.stats.hardlink_aliases,
            "errors": result.stats.errors[:1000],
            "stage_seconds": result.stats.stage_seconds,
        },
        "scan_errors": inv.errors[:1000],
    }


@dataclass
class SessionInfo:
    session_id: str
    created: str
    path: Path
    roots: list[str]
    groups: int
    wasted: int
    mode: str


def save_session(result: ScanResult, dirs: AppDirs | None = None, path: str | Path | None = None) -> Path:
    dirs = (dirs or get_app_dirs()).ensure()
    target = Path(path) if path else dirs.sessions / f"{result.session_id}.json.gz"
    payload = json_dumps(result_to_dict(result)).encode()
    atomic_write_bytes(target, gzip.compress(payload, compresslevel=6) if target.suffix == ".gz" else payload)
    return target


def _read(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    if data.get("format") != SESSION_FORMAT:
        raise ConfigError(f"{path} is not a Duplio scan session")
    return data  # type: ignore[no-any-return]


def find_session(ref: str | None, dirs: AppDirs | None = None) -> Path:
    """Resolve a session id prefix, ``latest``/``None``, or a file path."""
    dirs = (dirs or get_app_dirs()).ensure()
    if ref and Path(ref).expanduser().is_file():
        return Path(ref).expanduser()
    sessions = sorted(dirs.sessions.glob("*.json.gz"), reverse=True)
    if not sessions:
        raise ConfigError("no saved scans yet; run 'duplio scan' first")
    if ref in (None, "", "latest"):
        return sessions[0]
    matches = [p for p in sessions if p.name.startswith(ref)]  # type: ignore[arg-type]
    if not matches:
        raise ConfigError(f"no saved scan matches {ref!r} (see 'duplio sessions')")
    if len(matches) > 1:
        raise ConfigError(f"{ref!r} matches several scans; use more characters")
    return matches[0]


def list_sessions(dirs: AppDirs | None = None) -> list[SessionInfo]:
    dirs = (dirs or get_app_dirs()).ensure()
    out = []
    for path in sorted(dirs.sessions.glob("*.json.gz"), reverse=True):
        try:
            data = _read(path)
        except (OSError, ValueError, ConfigError):
            continue
        wasted = sum(g["size"] * (len(g["members"]) - 1) for g in data["groups"] if g["confidence"] == 3)
        out.append(
            SessionInfo(
                session_id=data["session_id"],
                created=data["created"],
                path=path,
                roots=[r["uri"] for r in data["roots"]],
                groups=len(data["groups"]),
                wasted=wasted,
                mode=data["options"].get("mode", "all"),
            )
        )
    return out


def load_session(path: str | Path, registry: BackendRegistry) -> ScanResult:
    """Rebuild a :class:`ScanResult`, reopening every location (they must be reachable)."""
    data = _read(Path(path))
    locations = [(registry.open(r["uri"]), RootRole(r["role"])) for r in data["roots"]]
    roots: list[Root] = make_roots(locations, [r.get("label", "") for r in data["roots"]])
    inventory = Inventory(roots=roots)
    groups = []
    for g in data["groups"]:
        members = [_unentry(m) for m in g["members"]]
        groups.append(
            DuplicateGroup(
                id=int(g["id"]),
                size=int(g["size"]),
                members=members,
                digest=g.get("digest"),
                algorithm=g.get("algorithm"),
                confidence=Confidence(int(g["confidence"])),
                method=str(g.get("method", "")),
                aliases={int(k): [_unentry(a) for a in v] for k, v in g.get("aliases", {}).items()},
            )
        )
    similar = [
        SimilarGroup(
            id=int(s["id"]),
            members=[_unentry(m) for m in s["members"]],
            distances=[int(d) for d in s["distances"]],
            dimensions=[(int(d[0]), int(d[1])) for d in s["dimensions"]],
        )
        for s in data.get("similar", [])
    ]
    # Rebuild a minimal inventory so totals and lookups keep working.
    seen: dict[int, FileEntry] = {}
    for g in groups:
        for m in g.members:
            seen[m.id] = m
        for aliases in g.aliases.values():
            for a in aliases:
                seen[a.id] = a
    for s in similar:
        for m in s.members:
            seen.setdefault(m.id, m)
    unique = [_unentry(e) for e in data.get("unique", [])]
    unverified = [_unentry(e) for e in data.get("unverified", [])]
    for e in unique + unverified:
        seen.setdefault(e.id, e)
    inventory.entries = sorted(seen.values(), key=lambda e: e.id)
    stats = EngineStats()
    for key, value in data.get("stats", {}).items():
        if hasattr(stats, key):
            setattr(stats, key, [tuple(x) for x in value] if key == "errors" else value)
    result = ScanResult(
        inventory=inventory,
        options=ScanOptions.from_dict(data["options"]),
        groups=groups,
        similar=similar,
        unique=unique,
        unverified=unverified,
        stats=stats,
        session_id=data["session_id"],
        created=data["created"],
        elapsed=float(data.get("elapsed", 0.0)),
    )
    return result


def session_totals(path: str | Path) -> dict[str, Any]:
    return dict(_read(Path(path)).get("totals", {}))
