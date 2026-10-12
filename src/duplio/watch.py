"""Run a job whenever a folder changes, by polling.

Polling is deliberately simple and works the same on every storage there is — local disks, SMB shares,
NFS, phones — where file-change notifications are unreliable or unavailable. A cheap fingerprint of the
folder (file count, total size and an order-independent hash of names, sizes and times) is compared every
``interval`` seconds. When it changes, Duplio waits until it stops changing for ``settle`` seconds (so a
file that is still being written is not copied half-finished) and then runs the job once.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from duplio.events import CancelToken
from duplio.scan.filters import ScanFilter
from duplio.vfs.location import Location


def tree_fingerprint(location: Location, scan_filter: ScanFilter, *, one_file_system: bool = True) -> tuple[int, int, int]:
    count = total = acc = 0
    for raw in location.backend.walk(location.path, walk_filter=scan_filter, one_file_system=one_file_system):
        if scan_filter.uses_time and scan_filter.skip_by_time(raw.mtime_ns):
            continue
        count += 1
        total += raw.size
        digest = hashlib.blake2b(f"{raw.rel}\0{raw.size}\0{raw.mtime_ns}".encode("utf-8", "surrogateescape"), digest_size=8)
        acc ^= int.from_bytes(digest.digest(), "big")
    return count, total, acc


@dataclass
class WatchStats:
    runs: int = 0
    failures: int = 0
    checks: int = 0


def watch(
    fingerprint: Callable[[], Hashable],
    action: Callable[[], bool],
    *,
    interval: float,
    settle: float,
    cancel: CancelToken | None = None,
    run_first: bool = True,
    max_runs: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_change: Callable[[], None] | None = None,
) -> WatchStats:
    """Poll ``fingerprint``; call ``action`` (returns True on success) after each settled change.

    The fingerprint is taken again after the action, so changes the action itself makes (the other half of a
    sync) do not trigger another run.
    """
    cancel = cancel or CancelToken()
    stats = WatchStats()
    last: Hashable | None = None if run_first else fingerprint()
    while not cancel.cancelled:
        current = fingerprint()
        stats.checks += 1
        if current != last:
            if on_change is not None:
                on_change()
            while not cancel.cancelled:  # wait until the folder has been quiet for one settle period
                sleep(settle)
                again = fingerprint()
                if again == current:
                    break
                current = again
            if cancel.cancelled:
                break
            ok = action()
            stats.runs += 1
            if not ok:
                stats.failures += 1
            last = fingerprint()
            if max_runs is not None and stats.runs >= max_runs:
                break
        _sleep_interruptibly(sleep, interval, cancel)
    return stats


def _sleep_interruptibly(sleep: Callable[[float], None], seconds: float, cancel: CancelToken) -> None:
    remaining = seconds
    while remaining > 0 and not cancel.cancelled:
        step = min(remaining, 0.5)
        sleep(step)
        remaining -= step
