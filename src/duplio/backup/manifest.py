"""Checksum manifests: prove that an archive is still what it was.

``create_manifest`` records every file's size, modification time and content hash.
``verify_manifest`` later reads everything again and sorts each file into:

``ok``         unchanged
``corrupt``    same size and same modification time, **different content**: the
               signature of silent corruption (bit rot, a failing disk, a bad copy)
``modified``   size or time changed and the content differs: someone edited it
``touched``    only the modification time changed, the content is the same
``missing``    in the manifest, not in the folder
``new``        in the folder, not in the manifest
``unreadable`` could not be read

A manifest is a plain JSON file, so it can live next to the archive, on another
drive, or in a cloud folder — anywhere you would trust it more than the archive.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from duplio import __version__
from duplio.compare.scheduler import Job, run_jobs
from duplio.errors import ConfigError
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import default_full_algorithm, get_algorithm
from duplio.hashing.core import hash_file
from duplio.scan.filters import ScanFilter
from duplio.util.atomic import atomic_write_text
from duplio.util.fmt import iso_now
from duplio.util.text import json_dumps, nfc
from duplio.vfs.base import RawEntry, WalkStats
from duplio.vfs.location import Location

MANIFEST_FORMAT = "duplio-manifest"


@dataclass
class ManifestEntry:
    rel: str
    size: int
    mtime_ns: int
    digest: str


@dataclass
class Manifest:
    root: str
    algorithm: str
    created: str
    entries: list[ManifestEntry] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def to_json(self) -> str:
        return json_dumps(
            {
                "format": MANIFEST_FORMAT, "version": 1, "created": self.created, "duplio": __version__,
                "root": self.root, "algorithm": self.algorithm,
                "files": [[e.rel, e.size, e.mtime_ns, e.digest] for e in self.entries],
            },
            indent=0,
        )  # fmt: skip

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        atomic_write_text(path, self.to_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> Manifest:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read the manifest {path}: {exc}") from exc
        if data.get("format") != MANIFEST_FORMAT:
            raise ConfigError(f"{path} is not a Duplio manifest")
        if int(data.get("version", 0)) > 1:
            raise ConfigError("this manifest was written by a newer Duplio; please upgrade")
        get_algorithm(str(data["algorithm"]))  # fails clearly if the hash is unavailable here
        entries = [ManifestEntry(str(r[0]), int(r[1]), int(r[2]), str(r[3])) for r in data["files"]]
        return cls(str(data.get("root", "")), str(data["algorithm"]), str(data.get("created", "")), entries)


def _hash_entries(
    location: Location, files: list[RawEntry], algorithm: str, progress: ProgressSink, cancel: CancelToken,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    backend = location.backend
    caps = location.caps
    device = backend.device_key(location.path)
    digests: dict[str, str] = {}
    errors: list[tuple[str, str]] = []
    lock = threading.Lock()

    def make(raw: RawEntry):  # type: ignore[no-untyped-def]
        def job() -> None:
            try:
                digest = hash_file(backend, raw.path, algorithm, expected_size=raw.size, cancel=cancel,
                                   on_bytes=lambda n: progress.advance(nbytes=n))
            except Exception as exc:
                from duplio.errors import CancelledError

                if isinstance(exc, CancelledError):
                    raise
                with lock:
                    errors.append((raw.rel, str(exc)))
            else:
                with lock:
                    digests[raw.rel] = digest
            progress.advance(items=1)

        return job

    run_jobs([Job(device, caps.max_parallel_reads, make(r)) for r in files], cancel=cancel)
    return digests, errors


def _list(location: Location, scan_filter: ScanFilter, progress: ProgressSink, cancel: CancelToken,
          one_file_system: bool = True) -> tuple[list[RawEntry], list[tuple[str, str]]]:
    progress.stage("Listing files")
    stats = WalkStats()
    files = []
    for raw in location.backend.walk(location.path, walk_filter=scan_filter, stats=stats,
                                     one_file_system=one_file_system, should_stop=lambda: cancel.cancelled):
        if scan_filter.uses_time and scan_filter.skip_by_time(raw.mtime_ns):
            continue
        files.append(raw)
    cancel.check()
    return files, list(stats.errors)


def create_manifest(
    location: Location,
    *,
    scan_filter: ScanFilter | None = None,
    algorithm: str | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> Manifest:
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    algorithm = algorithm or default_full_algorithm()
    get_algorithm(algorithm)
    files, walk_errors = _list(location, scan_filter or ScanFilter.for_backup(), progress, cancel)
    progress.stage("Hashing", total_items=len(files), total_bytes=sum(f.size for f in files))
    digests, errors = _hash_entries(location, files, algorithm, progress, cancel)
    manifest = Manifest(location.uri, algorithm, iso_now(), errors=walk_errors + errors)
    for raw in sorted(files, key=lambda r: r.rel):
        if raw.rel in digests:
            manifest.entries.append(ManifestEntry(raw.rel, raw.size, raw.mtime_ns, digests[raw.rel]))
    return manifest


@dataclass
class VerifyItem:
    rel: str
    status: str  # ok | corrupt | modified | touched | missing | new | unreadable
    detail: str = ""


@dataclass
class VerifyReport:
    items: list[VerifyItem] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    def select(self, status: str) -> list[VerifyItem]:
        return [i for i in self.items if i.status == status]

    @property
    def clean(self) -> bool:
        """Nothing lost, damaged or edited (new files do not make an archive unclean)."""
        return not any(i.status in ("corrupt", "modified", "missing", "unreadable") for i in self.items)


def verify_manifest(
    location: Location,
    manifest: Manifest,
    *,
    scan_filter: ScanFilter | None = None,
    quick: bool = False,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> VerifyReport:
    """Compare a folder with a manifest. ``quick`` trusts size and time instead of reading the data."""
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    files, walk_errors = _list(location, scan_filter or ScanFilter.for_backup(), progress, cancel)
    report = VerifyReport()
    for rel, _err in walk_errors:
        report.items.append(VerifyItem(rel, "unreadable", _err))
    # Pair by exact name first. Names that differ only in Unicode form (café composed / decomposed) are two
    # different files here, so collapsing them would let one hide the loss or damage of the other.
    unmatched = {f.rel: f for f in files}
    paired: list[tuple[ManifestEntry, RawEntry]] = []
    leftovers: list[ManifestEntry] = []
    for entry in manifest.entries:
        raw = unmatched.pop(entry.rel, None)
        if raw is None:
            leftovers.append(entry)
        else:
            paired.append((entry, raw))
    by_form: dict[str, list[RawEntry]] = {}
    for raw in unmatched.values():
        by_form.setdefault(nfc(raw.rel), []).append(raw)
    for entry in leftovers:  # a file moved between file systems that store names differently
        candidates = by_form.get(nfc(entry.rel))
        if candidates:
            raw = candidates.pop(0)
            unmatched.pop(raw.rel, None)
            paired.append((entry, raw))
        else:
            report.items.append(VerifyItem(entry.rel, "missing"))
    to_read: list[RawEntry] = []
    for entry, raw in paired:
        if quick:
            same = raw.size == entry.size and raw.mtime_ns == entry.mtime_ns
            report.items.append(VerifyItem(entry.rel, "ok" if same else "modified",
                                           "" if same else "size or time differs (not read: --quick)"))
        else:
            to_read.append(raw)
    for raw in unmatched.values():
        report.items.append(VerifyItem(raw.rel, "new"))
    expected = {raw.rel: entry for entry, raw in paired}
    if to_read:
        progress.stage("Verifying", total_items=len(to_read), total_bytes=sum(r.size for r in to_read))
        digests, errors = _hash_entries(location, to_read, manifest.algorithm, progress, cancel)
        for rel, message in errors:
            report.items.append(VerifyItem(rel, "unreadable", message))
        for raw in to_read:
            entry = expected[raw.rel]
            digest = digests.get(raw.rel)
            if digest is None:
                continue
            meta_same = raw.size == entry.size and raw.mtime_ns == entry.mtime_ns
            if digest == entry.digest:
                report.items.append(VerifyItem(raw.rel, "ok" if meta_same else "touched",
                                               "" if meta_same else "only the modification time differs"))
            elif meta_same:
                report.items.append(VerifyItem(raw.rel, "corrupt", "content differs although size and time are unchanged"))
            else:
                report.items.append(VerifyItem(raw.rel, "modified", "content changed"))
    report.items.sort(key=lambda i: (i.status, i.rel))
    return report
