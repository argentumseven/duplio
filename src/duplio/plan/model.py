"""Plans: a reviewable, serialisable list of exactly what will be done.

A plan is produced from scan results, shown to the user, optionally saved and
edited, and only then applied. It records every expectation the executor will
re-check at the last moment: sizes, modification times and digests.
"""

from __future__ import annotations

import gzip
import json
import secrets
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from duplio.errors import ConfigError
from duplio.util.atomic import atomic_write_bytes
from duplio.util.fmt import iso_now
from duplio.util.text import json_dumps

PLAN_FORMAT = "duplio-plan"
PLAN_VERSION = 1


class ActionType(StrEnum):
    QUARANTINE = "quarantine"  # move aside next to the data; fully reversible
    TRASH = "trash"  # system trash (local disks only)
    DELETE = "delete"  # permanent; requires explicit confirmation
    HARDLINK = "hardlink"  # replace with a hard link to the kept copy
    REFLINK = "reflink"  # replace with a copy-on-write clone (btrfs, XFS, APFS)


ACTION_HELP = {
    ActionType.QUARANTINE: "Move duplicates into a .duplio-quarantine folder (instant, reversible with 'duplio undo').",
    ActionType.TRASH: "Send duplicates to the system trash (local disks only).",
    ActionType.DELETE: "Delete duplicates permanently. Cannot be undone.",
    ActionType.HARDLINK: "Replace duplicates with hard links to the kept copy (same disk only; edits become shared).",
    ActionType.REFLINK: "Replace duplicates with copy-on-write clones (btrfs/XFS/APFS); safest space saver.",
}

REVERSIBLE = frozenset({ActionType.QUARANTINE, ActionType.HARDLINK, ActionType.REFLINK})


@dataclass
class FileRef:
    root: int
    path: str
    rel: str
    size: int
    mtime_ns: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileRef:
        return cls(int(d["root"]), str(d["path"]), str(d["rel"]), int(d["size"]), int(d["mtime_ns"]))


@dataclass
class RootSpec:
    index: int
    uri: str
    role: str
    label: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RootSpec:
        return cls(int(d["index"]), str(d["uri"]), str(d["role"]), str(d.get("label", "")))


@dataclass
class PlanItem:
    id: int
    group_id: int
    op: ActionType
    victim: FileRef
    keeper: FileRef
    digest: str | None
    algorithm: str | None
    confidence: int
    reclaim: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["op"] = self.op.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlanItem:
        return cls(
            id=int(d["id"]),
            group_id=int(d["group_id"]),
            op=ActionType(d["op"]),
            victim=FileRef.from_dict(d["victim"]),
            keeper=FileRef.from_dict(d["keeper"]),
            digest=d.get("digest"),
            algorithm=d.get("algorithm"),
            confidence=int(d["confidence"]),
            reclaim=int(d.get("reclaim", 0)),
            note=str(d.get("note", "")),
        )


@dataclass
class Plan:
    action: ActionType
    roots: list[RootSpec]
    items: list[PlanItem] = field(default_factory=list)
    session_id: str = ""
    plan_id: str = field(default_factory=lambda: secrets.token_hex(6))
    created: str = field(default_factory=iso_now)
    verify: str = "hash"
    rules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_groups: int = 0

    @property
    def reclaim(self) -> int:
        return sum(i.reclaim for i in self.items)

    @property
    def group_ids(self) -> set[int]:
        return {i.group_id for i in self.items}

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "version": PLAN_VERSION,
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "created": self.created,
            "action": self.action.value,
            "verify": self.verify,
            "rules": self.rules,
            "notes": self.notes,
            "skipped_groups": self.skipped_groups,
            "roots": [asdict(r) for r in self.roots],
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        if d.get("format") != PLAN_FORMAT:
            raise ConfigError("not a Duplio plan file")
        if int(d.get("version", 0)) > PLAN_VERSION:
            raise ConfigError("this plan was written by a newer Duplio; please upgrade")
        return cls(
            action=ActionType(d["action"]),
            roots=[RootSpec.from_dict(r) for r in d["roots"]],
            items=[PlanItem.from_dict(i) for i in d["items"]],
            session_id=str(d.get("session_id", "")),
            plan_id=str(d.get("plan_id", "")),
            created=str(d.get("created", "")),
            verify=str(d.get("verify", "hash")),
            rules=list(d.get("rules", [])),
            notes=list(d.get("notes", [])),
            skipped_groups=int(d.get("skipped_groups", 0)),
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        data = json_dumps(self.to_dict(), indent=1).encode()
        if path.suffix == ".gz":
            data = gzip.compress(data)
        atomic_write_bytes(path, data)
        return path

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        raw = Path(path).read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return cls.from_dict(json.loads(raw))
