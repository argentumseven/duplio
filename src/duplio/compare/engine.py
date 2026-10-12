"""The comparison pipeline.

Cheap evidence first, expensive evidence only where still needed::

    size buckets ─► alias collapse ─► sample fingerprint ─► full digest ─► (bytes)
       free           free (inode)      ~48 KiB per file      whole file     optional

After every stage each bucket is split by the new evidence and *pruned by the
mode's predicate*. In ``reference``/``unique``/``cross`` modes that pruning is
dramatic: a bucket that no longer spans the required roots is dropped before
any more bytes are read. Comparing a phone against a 2 TB archive therefore
reads only the handful of archive files whose size *and* fingerprint match
something on the phone.

Other details that matter:

* Hard links and other same-inode aliases are collapsed — they share storage,
  so they are not wasted space and must never be "deduplicated" against each
  other.
* Buckets mixing backends agree on one algorithm. When a member lives on an
  Android device reachable over ADB, SHA-256 is computed *on the phone* and
  the computer computes SHA-256 too; nothing is transferred but digests.
* A digest that is not cryptographic (xxh3, SHA-1, MD5) is always followed by
  a byte-for-byte comparison before a group is called identical.
* Every read checks the byte count against the scanned size, so a file that
  changes mid-scan, or a truncated device stream, can never masquerade as a
  match. Unreadable files are reported, never guessed about.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from duplio.compare.model import (
    Confidence,
    DuplicateGroup,
    EngineStats,
    Mode,
    ScanOptions,
    ScanResult,
    Verify,
)
from duplio.compare.scheduler import Job, run_jobs
from duplio.events import CancelToken, NullProgress, ProgressSink
from duplio.hashing.algorithms import get_algorithm
from duplio.hashing.cache import HashCache, NullCache
from duplio.hashing.core import SampleLayout, files_identical, hash_file, layout_for, sample_file
from duplio.preview.service import PreviewService
from duplio.scan.inventory import FileEntry, Inventory, Root
from duplio.util.text import name_key
from duplio.vfs.base import MediaKind, Sampling

REMOTE_BATCH = 64


@dataclass
class _Bucket:
    size: int
    members: list[FileEntry]
    aliases: dict[int, list[FileEntry]] = field(default_factory=dict)
    tainted: bool = False  # a reference member failed to read (unique mode)
    algorithm: str | None = None
    digest: str | None = None
    method: str = ""


class _SafeProgress:
    """Serialises progress calls coming from worker threads."""

    def __init__(self, inner: ProgressSink) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def stage(self, name: str, *, total_items: int | None = None, total_bytes: int | None = None) -> None:
        with self._lock:
            self._inner.stage(name, total_items=total_items, total_bytes=total_bytes)

    def advance(self, *, items: int = 0, nbytes: int = 0, detail: str | None = None) -> None:
        with self._lock:
            self._inner.advance(items=items, nbytes=nbytes, detail=detail)

    def message(self, text: str, *, level: str = "info") -> None:
        with self._lock:
            self._inner.message(text, level=level)


class Engine:
    def __init__(
        self,
        inventory: Inventory,
        options: ScanOptions,
        *,
        cache: HashCache | None = None,
        progress: ProgressSink | None = None,
        cancel: CancelToken | None = None,
        previews: PreviewService | None = None,
    ) -> None:
        self.inv = inventory
        self.opts = options
        self.previews = previews
        self.cache = cache or NullCache()
        self.progress = _SafeProgress(progress or NullProgress())
        self.cancel = cancel or CancelToken()
        self.stats = EngineStats()
        self._lock = threading.Lock()
        self._failed: set[int] = set()
        self.unique: list[FileEntry] = []
        self.unverified: list[FileEntry] = []
        if self.opts.mode in (Mode.REFERENCE, Mode.UNIQUE) and not any(r.is_reference for r in inventory.roots):
            from duplio.errors import ConfigError

            raise ConfigError(f"mode '{self.opts.mode}' needs at least one reference location")
        if self.opts.mode is Mode.UNIQUE and all(r.is_reference for r in inventory.roots):
            from duplio.errors import ConfigError

            raise ConfigError("mode 'unique' needs at least one non-reference location to check")

    # ------------------------------------------------------------------
    # Mode predicates
    # ------------------------------------------------------------------
    def _root(self, entry: FileEntry) -> Root:
        return self.inv.roots[entry.root]

    def _all_entries(self, bucket: _Bucket) -> Iterable[FileEntry]:
        for m in bucket.members:
            yield m
            yield from bucket.aliases.get(m.id, ())

    def _keep(self, bucket: _Bucket) -> bool:
        mode = self.opts.mode
        if mode in (Mode.ALL, Mode.WITHIN):
            return len(bucket.members) >= 2
        if mode is Mode.CROSS:
            return len(bucket.members) >= 2 and len({e.root for e in self._all_entries(bucket)}) >= 2
        has_ref = any(self._root(e).is_reference for e in self._all_entries(bucket))
        targets = [m for m in bucket.members if not self._root(m).is_reference]
        if mode is Mode.REFERENCE:
            if has_ref and targets and len(bucket.members) >= 2:
                return True
            return self.opts.within_targets and len(targets) >= 2
        # UNIQUE: we only need to keep investigating sources that might be present in a reference.
        has_source = any(not self._root(e).is_reference for e in self._all_entries(bucket))
        return has_ref and has_source

    def _drop(self, bucket: _Bucket) -> None:
        if self.opts.mode is not Mode.UNIQUE:
            return
        has_ref = any(self._root(e).is_reference for e in self._all_entries(bucket))
        if has_ref:
            return
        for entry in self._all_entries(bucket):
            if not self._root(entry).is_reference:
                (self.unverified if bucket.tainted else self.unique).append(entry)

    def _settle(self, buckets: Iterable[_Bucket]) -> list[_Bucket]:
        kept = []
        for bucket in buckets:
            if self._keep(bucket):
                kept.append(bucket)
            else:
                self._drop(bucket)
        return kept

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------
    def run(self) -> ScanResult:
        started = time.monotonic()
        result = ScanResult(inventory=self.inv, options=self.opts, stats=self.stats)
        entries = self.inv.entries
        self.stats.files_considered = len(entries)

        buckets = self._timed("group", self._size_buckets, entries)
        self.stats.size_groups = len(buckets)
        if self.opts.verify is Verify.QUICK:
            buckets = self._timed("quick", self._quick, buckets)
            confidence, final = Confidence.PROBABLE, buckets
        else:
            if self.opts.use_sampling:
                buckets = self._timed("sample", self._sample_stage, buckets)
            buckets = self._timed("hash", self._full_stage, buckets)
            buckets = self._timed("verify", self._verify_stage, buckets)
            confidence, final = Confidence.CERTAIN, buckets

        groups = []
        for bucket in final:
            members = sorted(bucket.members, key=lambda e: (e.root, e.path))
            groups.append(
                DuplicateGroup(
                    id=0,
                    size=bucket.size,
                    members=members,
                    digest=bucket.digest,
                    algorithm=bucket.algorithm,
                    confidence=confidence,
                    method=bucket.method,
                    aliases={k: v for k, v in bucket.aliases.items() if v},
                )
            )
        groups.sort(key=lambda g: (-g.wasted, -g.size, g.members[0].path))
        for i, group in enumerate(groups, 1):
            group.id = i
        result.groups = groups

        if self.opts.mode is Mode.UNIQUE:
            self.unique.sort(key=lambda e: (e.root, e.rel))
            result.unique = self.unique
        result.unverified = sorted(self.unverified, key=lambda e: (e.root, e.rel))
        result.failed = [e for e in entries if e.id in self._failed]

        if self.opts.similar_images and self.opts.mode is not Mode.UNIQUE:
            from duplio.compare.similar import find_similar_images

            result.similar = self._timed(
                "similar",
                lambda _: find_similar_images(
                    self.inv,
                    groups,
                    threshold=self.opts.similar_threshold,
                    cache=self.cache,
                    progress=self.progress,
                    cancel=self.cancel,
                    stats=self.stats,
                    previews=self.previews,
                    full_read=self.opts.similar_full_read,
                ),
                None,
            )
        self.cache.flush()
        self.stats.cache_hits = self.cache.hits
        result.elapsed = time.monotonic() - started
        return result

    def _timed(self, name: str, fn: Callable, arg):  # type: ignore[no-untyped-def,type-arg]
        t0 = time.monotonic()
        out = fn(arg)
        self.stats.stage_seconds[name] = self.stats.stage_seconds.get(name, 0.0) + time.monotonic() - t0
        self.cancel.check()
        return out

    def _size_buckets(self, entries: list[FileEntry]) -> list[_Bucket]:
        self.progress.stage("Grouping by size", total_items=len(entries))
        by_key: dict[tuple[int, int], list[FileEntry]] = defaultdict(list)
        within = self.opts.mode is Mode.WITHIN
        for e in entries:
            by_key[(e.root if within else -1, e.size)].append(e)
        buckets = []
        for (_, size), members in by_key.items():
            bucket = self._collapse_aliases(size, members)
            buckets.append(bucket)
        self.progress.advance(items=len(entries))
        return self._settle(buckets)

    def _collapse_aliases(self, size: int, members: list[FileEntry]) -> _Bucket:
        if len(members) == 1:
            return _Bucket(size=size, members=members)
        seen: dict[tuple[str, tuple[int, int]], FileEntry] = {}
        out: list[FileEntry] = []
        aliases: dict[int, list[FileEntry]] = {}
        for e in sorted(members, key=lambda x: (x.root, x.path)):
            root = self._root(e)
            if e.inode is not None and root.caps.stable_inodes:
                key = (root.backend.id, e.inode)
                first = seen.get(key)
                if first is not None:
                    aliases.setdefault(first.id, []).append(e)
                    self.stats.hardlink_aliases += 1
                    continue
                seen[key] = e
            out.append(e)
        return _Bucket(size=size, members=out, aliases=aliases)

    def _quick(self, buckets: list[_Bucket]) -> list[_Bucket]:
        self.progress.stage("Matching names", total_items=len(buckets))
        out = []
        for bucket in buckets:
            parts: dict[str, list[FileEntry]] = defaultdict(list)
            for m in bucket.members:
                parts[name_key(m.rel.rsplit("/", 1)[-1], fold=True)].append(m)
            for members in parts.values():
                out.append(
                    _Bucket(
                        size=bucket.size,
                        members=members,
                        aliases={m.id: bucket.aliases.get(m.id, []) for m in members},
                        method="size + name (not content-verified)",
                    )
                )
        self.progress.advance(items=len(buckets))
        return self._settle(out)

    # ---- helpers -------------------------------------------------------
    def _fail(self, entry: FileEntry, exc: BaseException) -> None:
        with self._lock:
            self._failed.add(entry.id)
            self.stats.errors.append((self._root(entry).backend.display(entry.path), str(exc)))

    def _split(self, buckets: list[_Bucket], keys: dict[int, str], *, set_digest: bool = False) -> list[_Bucket]:
        """Partition each bucket by ``keys[entry.id]``; failed entries leave their bucket."""
        out: list[_Bucket] = []
        for bucket in buckets:
            tainted = bucket.tainted
            parts: dict[str, list[FileEntry]] = defaultdict(list)
            for m in bucket.members:
                if m.id in self._failed or m.id not in keys:
                    if self.opts.mode is Mode.UNIQUE:
                        if self._root(m).is_reference:
                            tainted = True
                        else:
                            self.unverified.append(m)
                            self.unverified.extend(
                                a for a in bucket.aliases.get(m.id, ()) if not self._root(a).is_reference
                            )
                    continue
                parts[keys[m.id]].append(m)
            for key, members in parts.items():
                out.append(
                    _Bucket(
                        size=bucket.size,
                        members=members,
                        aliases={m.id: bucket.aliases[m.id] for m in members if m.id in bucket.aliases},
                        tainted=tainted,
                        algorithm=bucket.algorithm,
                        digest=key if set_digest else bucket.digest,
                        method=bucket.method,
                    )
                )
        return self._settle(out)

    def _device_and_limit(self, entry: FileEntry) -> tuple[str, int]:
        root = self._root(entry)
        limit = root.caps.max_parallel_reads
        if self.opts.local_workers and root.caps.kind is MediaKind.LOCAL:
            limit = self.opts.local_workers
        return root.backend.device_key(entry.path), limit

    def _ordered(self, entries: list[FileEntry]) -> list[FileEntry]:
        """Spinning disks: read in inode order, a cheap proxy for on-disk order."""
        return sorted(
            entries,
            key=lambda e: (
                self._root(e).caps.kind is not MediaKind.ROTATIONAL,
                e.inode[1] if e.inode and self._root(e).caps.kind is MediaKind.ROTATIONAL else 0,
                e.path,
            ),
        )

    def _cache_get(self, entry: FileEntry, kind: str) -> str | None:
        return self.cache.get(self._root(entry).backend.id, entry.path, kind, entry.size, entry.mtime_ns)

    def _cache_put(self, entry: FileEntry, kind: str, digest: str) -> None:
        root = self._root(entry)
        self.cache.put(
            root.backend.id,
            entry.path,
            kind,
            entry.size,
            entry.mtime_ns,
            digest,
            mtime_resolution_ns=root.caps.mtime_resolution_ns,
        )

    # ---- sampling -----------------------------------------------------
    def _layout(self, bucket: _Bucket) -> SampleLayout | None:
        modes = {self._root(m).caps.sampling for m in bucket.members}
        if Sampling.NONE in modes:
            return None
        layout = layout_for(Sampling.SEQUENTIAL if Sampling.SEQUENTIAL in modes else Sampling.RANDOM)
        if layout is None or bucket.size <= layout.full_hash_threshold:
            return None
        return layout

    def _sample_stage(self, buckets: list[_Bucket]) -> list[_Bucket]:
        todo: list[tuple[FileEntry, SampleLayout]] = []
        sampled_buckets: list[_Bucket] = []
        passthrough: list[_Bucket] = []
        for bucket in buckets:
            layout = self._layout(bucket)
            if layout is None:
                passthrough.append(bucket)
                continue
            sampled_buckets.append(bucket)
            todo.extend((m, layout) for m in bucket.members)
        if not todo:
            return buckets
        algo = self.opts.sample_algorithm
        self.progress.stage(
            "Fingerprinting samples",
            total_items=len(todo),
            total_bytes=sum(layout.sampled_bytes for _, layout in todo),
        )
        keys: dict[int, str] = {}
        jobs: list[Job] = []
        layouts = {e.id: layout for e, layout in todo}
        for entry in self._ordered([e for e, _ in todo]):
            layout = layouts[entry.id]
            kind = f"{layout.cache_kind}:{algo}"
            cached = self._cache_get(entry, kind)
            if cached is not None:
                keys[entry.id] = cached
                self.progress.advance(items=1, nbytes=layout.sampled_bytes)
                continue
            device, limit = self._device_and_limit(entry)
            jobs.append(Job(device, limit, self._make_sample_job(entry, layout, kind, algo, keys)))
        run_jobs(jobs, cancel=self.cancel)
        return passthrough + self._split(sampled_buckets, keys)

    def _make_sample_job(
        self, entry: FileEntry, layout: SampleLayout, kind: str, algo: str, keys: dict[int, str]
    ) -> Callable[[], None]:
        backend = self._root(entry).backend

        def job() -> None:
            read = 0

            def count(n: int) -> None:
                nonlocal read
                read += n

            try:
                digest = sample_file(backend, entry.path, entry.size, layout, algo, on_bytes=count)
            except Exception as exc:
                self._fail(entry, exc)
                self.progress.advance(items=1)
                return
            with self._lock:
                keys[entry.id] = digest
                self.stats.sampled += 1
                self.stats.bytes_read += read
            self._cache_put(entry, kind, digest)
            self.progress.advance(items=1, nbytes=read)

        return job

    # ---- full digests ----------------------------------------------------
    def _negotiate(self, bucket: _Bucket) -> str:
        """Choose one algorithm all members can use efficiently."""
        remote_sets = []
        for m in bucket.members:
            caps = self._root(m).caps
            if caps.remote_hash_algorithms and caps.sampling is Sampling.NONE:
                remote_sets.append(caps.remote_hash_algorithms)
        if remote_sets:
            for name in remote_sets[0]:
                if all(name in s for s in remote_sets) and get_algorithm(name).cryptographic:
                    return name
            for name in remote_sets[0]:
                if all(name in s for s in remote_sets):
                    return name
        return self.opts.full_algorithm

    def _full_stage(self, buckets: list[_Bucket]) -> list[_Bucket]:
        keys: dict[int, str] = {}
        stream: list[tuple[FileEntry, str]] = []
        remote: dict[tuple[int, str], list[FileEntry]] = defaultdict(list)
        for bucket in buckets:
            bucket.algorithm = self._negotiate(bucket)
            alg = get_algorithm(bucket.algorithm)
            bucket.method = f"{alg.name} content hash"
            for m in bucket.members:
                kind = f"full:{bucket.algorithm}"
                cached = self._cache_get(m, kind)
                if cached is not None:
                    keys[m.id] = cached
                    continue
                caps = self._root(m).caps
                if bucket.algorithm in caps.remote_hash_algorithms:
                    remote[(m.root, bucket.algorithm)].append(m)
                else:
                    stream.append((m, bucket.algorithm))
        total_items = len(stream) + sum(len(v) for v in remote.values())
        self.progress.stage(
            "Hashing contents", total_items=total_items, total_bytes=sum(e.size for e, _ in stream)
        )
        jobs: list[Job] = []
        algos = {e.id: a for e, a in stream}
        for entry in self._ordered([e for e, _ in stream]):
            device, limit = self._device_and_limit(entry)
            jobs.append(Job(device, limit, self._make_hash_job(entry, algos[entry.id], keys)))
        for (root_idx, algo), entries in remote.items():
            root = self.inv.roots[root_idx]
            device = root.backend.device_key(root.path)
            for i in range(0, len(entries), REMOTE_BATCH):
                batch = entries[i : i + REMOTE_BATCH]
                jobs.append(Job(device, root.caps.max_parallel_reads, self._make_remote_job(root, batch, algo, keys)))
        run_jobs(jobs, cancel=self.cancel)
        return self._split(buckets, keys, set_digest=True)

    def _make_hash_job(self, entry: FileEntry, algo: str, keys: dict[int, str]) -> Callable[[], None]:
        backend = self._root(entry).backend

        def job() -> None:
            try:
                digest = hash_file(
                    backend,
                    entry.path,
                    algo,
                    expected_size=entry.size,
                    cancel=self.cancel,
                    on_bytes=lambda n: self.progress.advance(nbytes=n),
                )
            except Exception as exc:
                from duplio.errors import CancelledError

                if isinstance(exc, CancelledError):
                    raise
                self._fail(entry, exc)
                self.progress.advance(items=1)
                return
            with self._lock:
                keys[entry.id] = digest
                self.stats.full_hashed += 1
                self.stats.bytes_read += entry.size
            self._cache_put(entry, f"full:{algo}", digest)
            self.progress.advance(items=1)

        return job

    def _make_remote_job(
        self, root: Root, batch: list[FileEntry], algo: str, keys: dict[int, str]
    ) -> Callable[[], None]:
        def job() -> None:
            try:
                digests = root.backend.remote_hash([e.path for e in batch], algo)
            except Exception as exc:
                for entry in batch:
                    self._fail(entry, exc)
                self.progress.advance(items=len(batch))
                return
            for entry in batch:
                remote = digests.get(entry.path)
                if remote is None:
                    self._fail(entry, OSError("device did not return a digest"))
                    continue
                changed = (remote.size is not None and remote.size != entry.size) or (
                    remote.mtime_ns is not None and remote.mtime_ns != entry.mtime_ns
                )
                if changed:
                    self._fail(entry, OSError("file changed during scan"))
                    continue
                with self._lock:
                    keys[entry.id] = remote.digest
                    self.stats.remote_hashed += 1
                self._cache_put(entry, f"full:{algo}", remote.digest)
            self.progress.advance(items=len(batch))

        return job

    # ---- byte verification -----------------------------------------------
    def _verify_stage(self, buckets: list[_Bucket]) -> list[_Bucket]:
        needs = [
            b
            for b in buckets
            if self.opts.verify is Verify.BYTES or not get_algorithm(b.algorithm or "blake2b").cryptographic
        ]
        if not needs:
            return buckets
        need_ids = {id(b) for b in needs}
        others = [b for b in buckets if id(b) not in need_ids]
        self.progress.stage(
            "Comparing bytes",
            total_items=sum(len(b.members) - 1 for b in needs),
            total_bytes=sum(b.size * len(b.members) for b in needs),
        )
        results: dict[int, list[_Bucket]] = {}
        jobs = []
        for idx, bucket in enumerate(needs):
            device, limit = self._device_and_limit(bucket.members[0])
            jobs.append(Job(device, limit, self._make_verify_job(idx, bucket, results)))
        run_jobs(jobs, cancel=self.cancel)
        verified: list[_Bucket] = []
        for idx in sorted(results):
            verified.extend(results[idx])
        return others + self._settle(verified)

    def _make_verify_job(self, idx: int, bucket: _Bucket, results: dict[int, list[_Bucket]]) -> Callable[[], None]:
        def job() -> None:
            remaining = list(bucket.members)
            parts: list[list[FileEntry]] = []
            tainted = bucket.tainted
            while remaining:
                base = remaining[0]
                same: list[FileEntry] = [base]
                diff: list[FileEntry] = []
                base_loc = (self._root(base).backend, base.path)
                for m in remaining[1:]:
                    try:
                        equal = files_identical(
                            base_loc,
                            (self._root(m).backend, m.path),
                            cancel=self.cancel,
                            on_bytes=lambda n: self.progress.advance(nbytes=n),
                        )
                    except Exception as exc:
                        from duplio.errors import CancelledError

                        if isinstance(exc, CancelledError):
                            raise
                        self._fail(m, exc)
                        if self.opts.mode is Mode.UNIQUE:
                            if self._root(m).is_reference:
                                tainted = True
                            else:
                                with self._lock:
                                    self.unverified.append(m)
                        continue
                    finally:
                        self.progress.advance(items=1)
                    (same if equal else diff).append(m)
                    with self._lock:
                        self.stats.byte_compared += 1
                parts.append(same)
                remaining = diff
            alg = bucket.algorithm or ""
            with self._lock:
                results[idx] = [
                    _Bucket(
                        size=bucket.size,
                        members=p,
                        aliases={m.id: bucket.aliases[m.id] for m in p if m.id in bucket.aliases},
                        tainted=tainted,
                        algorithm=bucket.algorithm,
                        digest=bucket.digest,
                        method=f"{alg} hash + byte-for-byte",
                    )
                    for p in parts
                ]

        return job


def compare(
    inventory: Inventory,
    options: ScanOptions,
    *,
    cache: HashCache | None = None,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    previews: PreviewService | None = None,
) -> ScanResult:
    return Engine(inventory, options, cache=cache, progress=progress, cancel=cancel, previews=previews).run()
