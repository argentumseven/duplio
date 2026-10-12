"""Find out how a drive really behaves, by trying it.

Mount tables and file-system names only say so much. A NAS share might be
case-insensitive, keep two-second timestamps, reject ``:`` in names, or be slow
enough that the defaults are wrong. ``probe_location`` writes a few tiny temporary
files into a throw-away folder (removed afterwards, even on failure), measures
what it can, and turns the results into plain recommendations.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
import unicodedata
from dataclasses import dataclass, field

from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.vfs.location import Location

_TEST_MTIME_NS = 1_600_000_001_123_456_789  # odd second + sub-second digits reveal the stored resolution
_RESOLUTIONS_NS = (1, 100, 1_000, 1_000_000, 1_000_000_000, 2_000_000_000)
_SUSPECT_CHARS = '<>:"|?*'


@dataclass
class ProbeReport:
    location: str
    label: str
    kind: str
    fstype: str
    writable: bool = False
    free_bytes: int | None = None
    latency_ms: float | None = None
    case_sensitive: bool | None = None
    normalization_insensitive: bool | None = None
    mtime_resolution_ns: int | None = None
    rejected_chars: list[str] = field(default_factory=list)
    strips_trailing_dots: bool | None = None
    max_name_ok: int | None = None
    hardlinks: bool | None = None
    write_mib_s: float | None = None
    read_mib_s: float | None = None
    notes: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def _try(report: ProbeReport, what: str, fn):  # type: ignore[no-untyped-def]
    try:
        return fn()
    except Exception as exc:
        report.notes.append(f"{what}: {type(exc).__name__}: {exc}")
        return None


def probe_location(
    location: Location,
    *,
    throughput_mib: int = 8,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
) -> ProbeReport:
    progress = progress or NullProgress()
    cancel = cancel or CancelToken()
    b = location.backend
    caps = location.caps
    report = ProbeReport(location=location.display, label=caps.label, kind=caps.kind.value, fstype=caps.fstype)
    base = b.join(location.path, f".duplio-probe-{secrets.token_hex(4)}")
    created: list[str] = []

    def put(name: str, data: bytes = b"duplio-probe") -> str:
        path = b.join(base, name)
        with b.open_write(path) as handle:
            handle.write(data)
        created.append(path)
        return path

    progress.stage("Probing drive", total_items=9)
    try:
        b.makedirs(base)
        # 1. basic read/write and latency
        probe = _try(report, "write test", lambda: put("basic.tmp", b"hello duplio"))
        if probe is not None:
            with b.open_read(probe) as handle:
                report.writable = handle.read() == b"hello duplio"
            start = time.perf_counter()
            for _ in range(10):
                b.stat(probe)
            report.latency_ms = (time.perf_counter() - start) / 10 * 1000
        progress.advance(items=1)
        cancel.check()
        if not report.writable:
            report.recommendations.append("The location is not writable; only read-only operations will work.")
            return report
        report.free_bytes = b.free_space(location.path)

        # 2. case sensitivity
        def case() -> bool:
            put("CaseProbe.tmp")
            return b.stat(b.join(base, "caseprobe.tmp")) is None

        report.case_sensitive = _try(report, "case test", case)
        progress.advance(items=1)

        # 3. Unicode normalisation (macOS/APFS-style storage treats é and e+◌́ as one name)
        def normalisation() -> bool:
            composed = unicodedata.normalize("NFC", "norm-é.tmp")
            put(composed)
            return b.stat(b.join(base, unicodedata.normalize("NFD", composed))) is not None

        report.normalization_insensitive = _try(report, "normalisation test", normalisation)
        progress.advance(items=1)

        # 4. timestamp resolution
        def resolution() -> int | None:
            path = put("mtime.tmp")
            b.set_mtime(path, _TEST_MTIME_NS)
            info = b.stat(path)
            if info is None:
                return None
            for res in _RESOLUTIONS_NS:
                if info.mtime_ns == (_TEST_MTIME_NS // res) * res:
                    return res
            return None

        report.mtime_resolution_ns = _try(report, "timestamp test", resolution)
        progress.advance(items=1)

        # 5. characters the drive refuses
        for ch in _SUSPECT_CHARS:
            try:
                put(f"chr-a{ch}b.tmp")
            except Exception:  # any refusal means the character cannot be stored here
                report.rejected_chars.append(ch)
        progress.advance(items=1)

        # 6. trailing dots are silently dropped by Windows-style stores
        def trailing() -> bool:
            put("trail.")
            names = {name for name, _ in b.listdir(base)}
            return "trail." not in names

        try:
            report.strips_trailing_dots = trailing()
        except Exception:  # refused outright: same consequence as being stripped
            report.strips_trailing_dots = True
        progress.advance(items=1)

        # 7. longest name that works
        for length in (255, 200, 128):
            try:
                put("n" * (length - 4) + ".tmp")
                report.max_name_ok = length
                break
            except Exception:
                continue
        progress.advance(items=1)

        # 8. hard links (only some local file systems)
        def links() -> bool:
            src = put("link-src.tmp")
            dst = b.join(base, "link-dst.tmp")
            b.hardlink(src, dst)
            created.append(dst)
            return True

        with contextlib.suppress(Exception):
            report.hardlinks = bool(links())
        if report.hardlinks is None:
            report.hardlinks = False
        progress.advance(items=1)

        # 9. throughput
        if throughput_mib > 0:
            cancel.check()

            def speed() -> tuple[float, float]:
                block = os.urandom(1024 * 1024)
                path = b.join(base, "speed.tmp")
                start = time.perf_counter()
                with b.open_write(path) as handle:
                    for _ in range(throughput_mib):
                        handle.write(block)
                write_s = time.perf_counter() - start
                created.append(path)
                start = time.perf_counter()
                with b.open_read(path) as handle:
                    while handle.read(1024 * 1024):
                        pass
                read_s = time.perf_counter() - start
                return throughput_mib / max(write_s, 1e-6), throughput_mib / max(read_s, 1e-6)

            result = _try(report, "speed test", speed)
            if result is not None:
                report.write_mib_s, report.read_mib_s = result
        progress.advance(items=1)
    finally:
        for path in reversed(created):
            with contextlib.suppress(Exception):
                b.remove(path)
        with contextlib.suppress(Exception):
            # ``remove_empty_dirs`` removes the folder itself when asked to climb out of it
            b.remove_empty_dirs(base, location.path)
    _recommend(report)
    return report


def _recommend(r: ProbeReport) -> None:
    if r.mtime_resolution_ns and r.mtime_resolution_ns >= 1_000_000_000:
        secs = r.mtime_resolution_ns // 1_000_000_000
        r.recommendations.append(
            f"Timestamps are stored in {secs}-second steps: Duplio allows that tolerance when comparing "
            "automatically. If the other side is a Windows/FAT disk, add --ignore-dst to forgive one-hour shifts."
        )
    if r.rejected_chars or r.strips_trailing_dots:
        shown = " ".join(r.rejected_chars) or "(trailing dots)"
        r.recommendations.append(
            f"This drive rejects some characters in names ({shown}). Use --names fix with merge/sync/mirror so "
            "such files are copied under a safe name instead of failing."
        )
    if r.case_sensitive is False:
        r.recommendations.append(
            "Names are case-insensitive: 'A.txt' and 'a.txt' cannot coexist here. Duplio reports collisions and "
            "never merges them silently."
        )
    if r.normalization_insensitive:
        r.recommendations.append("Accented names are matched regardless of Unicode form (macOS-style storage).")
    if r.hardlinks is False:
        r.recommendations.append(
            "No hard links: 'duplio snapshot' (browsable hard-link snapshots) is unavailable here; use "
            "'duplio backup' (deduplicated repository) instead."
        )
    if r.write_mib_s is not None and r.write_mib_s < 5:
        r.recommendations.append(
            f"Writes are slow ({r.write_mib_s:.1f} MiB/s). Prefer --verify hash over --verify bytes and consider "
            "running large jobs overnight (duplio schedule)."
        )
    if r.latency_ms is not None and r.latency_ms > 20:
        r.recommendations.append(
            f"High latency ({r.latency_ms:.0f} ms per operation): many small files will be slow. "
            "Duplio fingerprints a few KiB per file before reading whole files to keep traffic down."
        )
    if not r.recommendations:
        r.recommendations.append("Nothing unusual: this drive behaves like a normal local disk.")
