"""Snapshot retention ("keep 7 daily, 4 weekly, 12 monthly...").

Semantics follow the widely used restic/borg model: snapshots are grouped by
source and label; within a group, newest first, a ``keep-daily N`` rule keeps
the newest snapshot of each of the last N distinct days that have snapshots,
and so on for each rule. A snapshot is kept if *any* rule keeps it.

Safety additions: an empty policy is refused (it would delete everything),
and the newest snapshot of every group is always kept, together with the newest
one that had no read errors.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from duplio.backup.repository import SnapshotMeta
from duplio.errors import ConfigError


@dataclass
class RetentionPolicy:
    keep_last: int = 0
    keep_hourly: int = 0
    keep_daily: int = 0
    keep_weekly: int = 0
    keep_monthly: int = 0
    keep_yearly: int = 0
    keep_within_s: float = 0.0
    keep_tags: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(
            (
                self.keep_last,
                self.keep_hourly,
                self.keep_daily,
                self.keep_weekly,
                self.keep_monthly,
                self.keep_yearly,
                self.keep_within_s,
                self.keep_tags,
            )
        )


_BUCKETS: list[tuple[str, Callable[[datetime], str]]] = [
    ("hourly", lambda d: d.strftime("%Y-%m-%d %H")),
    ("daily", lambda d: d.strftime("%Y-%m-%d")),
    ("weekly", lambda d: "%04d-W%02d" % d.isocalendar()[:2]),
    ("monthly", lambda d: d.strftime("%Y-%m")),
    ("yearly", lambda d: d.strftime("%Y")),
]


@dataclass
class RetentionDecision:
    keep: list[tuple[SnapshotMeta, list[str]]]
    remove: list[SnapshotMeta]


def apply_policy(
    snapshots: list[SnapshotMeta], policy: RetentionPolicy, *, now: float | None = None
) -> RetentionDecision:
    if policy.empty:
        raise ConfigError("the retention policy keeps nothing; add at least one --keep-* option")
    now = time.time() if now is None else now
    groups: dict[tuple[str, str], list[SnapshotMeta]] = defaultdict(list)
    for snap in snapshots:
        groups[(snap.source, snap.label)].append(snap)

    keep: list[tuple[SnapshotMeta, list[str]]] = []
    remove: list[SnapshotMeta] = []
    for members in groups.values():
        members = sorted(members, key=lambda s: (s.time, s.id), reverse=True)
        reasons: dict[str, list[str]] = defaultdict(list)
        for i, snap in enumerate(members):
            if i < policy.keep_last:
                reasons[snap.id].append("last")
            if policy.keep_within_s and now - snap.timestamp <= policy.keep_within_s:
                reasons[snap.id].append("within")
            if policy.keep_tags and set(snap.tags) & set(policy.keep_tags):
                reasons[snap.id].append("tag")
        for name, bucket_of in _BUCKETS:
            limit = getattr(policy, f"keep_{name}")
            if not limit:
                continue
            seen: set[str] = set()
            for snap in members:
                key = bucket_of(datetime.fromtimestamp(snap.timestamp))
                if key in seen:
                    continue
                if len(seen) >= limit:
                    break
                seen.add(key)
                reasons[snap.id].append(name)
        if members and not reasons[members[0].id]:
            reasons[members[0].id].append("newest (always kept)")
        # A snapshot that had read errors, or files that kept changing, is not trustworthy; the newest one that had none must survive as well, or
        # thinning could leave only incomplete ones.
        complete = next((snap for snap in members if not snap.problems), None)
        if complete is not None and not reasons[complete.id]:
            reasons[complete.id].append("newest without problems (always kept)")
        for snap in members:
            if reasons.get(snap.id):
                keep.append((snap, reasons[snap.id]))
            else:
                remove.append(snap)
    keep.sort(key=lambda kr: kr[0].time)
    remove.sort(key=lambda s: s.time)
    return RetentionDecision(keep=keep, remove=remove)
