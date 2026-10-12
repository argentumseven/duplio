"""Data model for comparison results."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from duplio.hashing.algorithms import default_full_algorithm, default_sample_algorithm
from duplio.scan.inventory import FileEntry, Inventory
from duplio.util.fmt import iso_now, utc_stamp_ms


class Mode(StrEnum):
    ALL = "all"  # duplicates anywhere, within and across roots
    CROSS = "cross"  # only files that exist in two or more roots
    WITHIN = "within"  # duplicates inside each root separately
    REFERENCE = "reference"  # copies in normal roots of files in reference roots
    UNIQUE = "unique"  # files in normal roots with no copy in reference roots


MODE_HELP = {
    Mode.ALL: "Find every set of identical files across all locations.",
    Mode.CROSS: "Only report files that exist in two or more of the locations.",
    Mode.WITHIN: "Find duplicates inside each location separately.",
    Mode.REFERENCE: "Find copies of files that already exist in reference locations.",
    Mode.UNIQUE: "Find files that are NOT present in the reference locations (what is not backed up yet).",
}


class Verify(StrEnum):
    QUICK = "quick"  # size + name only: fast, report-only
    HASH = "hash"  # cryptographic content hash (default)
    BYTES = "bytes"  # hash, then byte-for-byte confirmation


class Confidence(IntEnum):
    SIMILAR = 1  # perceptually similar images; never acted on automatically
    PROBABLE = 2  # metadata match (size + name); report-only
    CERTAIN = 3  # identical content proven by cryptographic hash or bytes

    @property
    def label(self) -> str:
        return {1: "similar", 2: "probable", 3: "identical"}[int(self)]


@dataclass
class ScanOptions:
    mode: Mode = Mode.ALL
    verify: Verify = Verify.HASH
    full_algorithm: str = field(default_factory=default_full_algorithm)
    sample_algorithm: str = field(default_factory=default_sample_algorithm)
    use_sampling: bool = True
    within_targets: bool = False
    similar_images: bool = False
    similar_threshold: int = 6
    one_file_system: bool = True
    local_workers: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "verify": self.verify.value,
            "full_algorithm": self.full_algorithm,
            "sample_algorithm": self.sample_algorithm,
            "use_sampling": self.use_sampling,
            "within_targets": self.within_targets,
            "similar_images": self.similar_images,
            "similar_threshold": self.similar_threshold,
            "one_file_system": self.one_file_system,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ScanOptions:
        opts = cls()
        opts.mode = Mode(str(data.get("mode", "all")))
        opts.verify = Verify(str(data.get("verify", "hash")))
        opts.full_algorithm = str(data.get("full_algorithm", opts.full_algorithm))
        opts.sample_algorithm = str(data.get("sample_algorithm", opts.sample_algorithm))
        opts.use_sampling = bool(data.get("use_sampling", True))
        opts.within_targets = bool(data.get("within_targets", False))
        opts.similar_images = bool(data.get("similar_images", False))
        opts.similar_threshold = int(data.get("similar_threshold", 6))  # type: ignore[call-overload]
        opts.one_file_system = bool(data.get("one_file_system", True))
        return opts


@dataclass
class DuplicateGroup:
    id: int
    size: int
    members: list[FileEntry]
    digest: str | None
    algorithm: str | None
    confidence: Confidence
    method: str
    aliases: dict[int, list[FileEntry]] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def wasted(self) -> int:
        return self.size * (len(self.members) - 1)

    def roots(self) -> set[int]:
        out = {m.root for m in self.members}
        for extra in self.aliases.values():
            out.update(a.root for a in extra)
        return out


@dataclass
class SimilarGroup:
    id: int
    members: list[FileEntry]
    distances: list[int]
    dimensions: list[tuple[int, int]]

    @property
    def confidence(self) -> Confidence:
        return Confidence.SIMILAR


@dataclass
class EngineStats:
    files_considered: int = 0
    size_groups: int = 0
    sampled: int = 0
    full_hashed: int = 0
    remote_hashed: int = 0
    byte_compared: int = 0
    bytes_read: int = 0
    cache_hits: int = 0
    hardlink_aliases: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    stage_seconds: dict[str, float] = field(default_factory=dict)


def new_session_id() -> str:
    return f"{utc_stamp_ms()}-{secrets.token_hex(2)}"


@dataclass
class ScanResult:
    inventory: Inventory
    options: ScanOptions
    groups: list[DuplicateGroup] = field(default_factory=list)
    similar: list[SimilarGroup] = field(default_factory=list)
    unique: list[FileEntry] = field(default_factory=list)
    unverified: list[FileEntry] = field(default_factory=list)
    stats: EngineStats = field(default_factory=EngineStats)
    session_id: str = field(default_factory=new_session_id)
    created: str = field(default_factory=iso_now)
    elapsed: float = 0.0

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups if g.confidence is Confidence.CERTAIN)

    @property
    def duplicate_files(self) -> int:
        return sum(g.count - 1 for g in self.groups)

    def group(self, group_id: int) -> DuplicateGroup:
        for g in self.groups:
            if g.id == group_id:
                return g
        raise KeyError(group_id)
