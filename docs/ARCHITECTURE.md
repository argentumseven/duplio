# Duplio architecture

Duplio is layered so that each layer only knows the one below it. Comparison,
planning and backup code never touch `os`, `smbclient` or `adb` directly. They
talk to a `Backend`, which is why a phone, a NAS and a local SSD can all take part
in one scan.

```
            ┌───────────────┐   ┌────────────────┐
            │  cli (click)  │   │  tui (textual) │       presentation: no business logic
            └──────┬────────┘   └───────┬────────┘
                   └─────────┬──────────┘
      ┌──────────────┬───────┴──────┬──────────────────┐
      │   compare    │     plan     │      backup      │  engines: pure logic + progress events
      │ engine       │ rules        │ repository       │
      │ scheduler    │ planner      │ retention        │
      │ similar      │ executor     │ mirror / ingest  │
      │ session      │ journal/undo │                  │
      └──────┬───────┴──────┬───────┴────────┬─────────┘
             │       scan (filters, roots, inventory)  │
             │     hashing (algorithms, core, cache)   │  shared services
             │     transfer.copy_verified              │
      ┌──────┴─────────────────────────────────────────┴───┐
      │ vfs: Backend ── LocalBackend (+ mount profiles)    │  storage
      │              ── SMBBackend (smbprotocol)           │
      │              ── ADBBackend (adb shell / exec-out)  │
      └────────────────────────────────────────────────────┘
```

## Storage: `duplio.vfs`

`Backend` (in `vfs/base.py`) is the whole storage contract:

- **identity**: `id`, `uri(path)`, `display(path)`, `device_key(path)`;
- **capabilities**: `capabilities(path) -> Capabilities`;
- **reading**: `walk`, `stat`, `open_read`, `read_range`, `remote_hash`, `listdir`, `free_space`;
- **writing**: `open_write` (atomic, no clobber), `rename` (no clobber), `remove`, `makedirs`, `set_mtime`, `hardlink`, `reflink`, `trash`;
- **path algebra**: `join`, `parent`, `basename`, `normalize`, `is_within`, `same_file`.

Backend-native paths are opaque strings: POSIX for local and ADB, UNC for SMB.
Paths relative to a scan root are always POSIX, so results compare across
backends.

`Capabilities` describes *behaviour*:

- the media kind;
- whether inodes can be trusted;
- timestamp resolution;
- sampling (random / sequential / none);
- the parallel-read budget;
- which hashes the far side can compute;
- whether rename is atomic;
- hard-link, reflink and trash support.

Engines decide everything from these properties, never from the backend type.
A new backend is therefore usually just a class plus a `Capabilities` profile.

`vfs/mounts.py` turns mounted storage into such profiles. It reads
`/proc/self/mountinfo`, the output of `mount` on macOS/BSD, or `GetDriveTypeW` on
Windows. A GVFS MTP path, a jmtpfs mount, a `cifs` share, a FAT card or a spinning
disk each get the right profile automatically.

### Adding a backend

1. Subclass `Backend` and implement `id`, `capabilities`, `uri`, `walk`, `stat` and `open_read`. Writing methods are optional; the defaults raise `UnsupportedOperation`.
2. Teach `vfs/location.py` the location syntax (`location_kind` and `BackendRegistry.open`).
3. If the far side can hash, implement `remote_hash` and list the algorithms in `Capabilities.remote_hash_algorithms`. The engine negotiates one algorithm per group.
4. Add a fake to `tests/fakes.py` and run the engine/executor tests against it, as `test_remote_backends.py` does for ADB and SMB.

## Discovery: `duplio.scan`

- `ScanFilter` implements the backend `WalkFilter` protocol (exclude / include, size limits, hidden files) and decides *protection*.
- `make_roots` validates locations, resolves symlinked roots, and refuses overlaps: nested paths, symlinks, bind mounts, and one phone reached through ADB and MTP at once.
- `scan` walks all roots concurrently into an `Inventory` of `FileEntry` records.

## Comparison: `duplio.compare`

`Engine` (in `compare/engine.py`) runs these stages: size buckets → alias collapse
→ sample → full digest → byte verification. After each stage, `_split` partitions
the buckets and `_settle` applies the mode predicate (`_keep` / `_drop`). Unique
mode is implemented entirely through `_drop`: a bucket that loses its last
reference member hands its source files to `unique`, or to `unverified` if a
reference member failed to read.

`scheduler.run_jobs` runs I/O jobs with **per-device** concurrency. The device key
comes from the backend, for example `local:/mount/point` or `adb:SERIAL`.

`session.py` serialises results, so review and apply can happen later or in
another process.

## Planning and applying: `duplio.plan`

- `rules.py`: ordered keep rules, used as a lexicographic sort.
- `planner.py`: turns groups into `PlanItem`s. It applies the hard constraints first (reference, protected, read-only, capability), then the rules or the user's own choices, then runs an independent invariant check.
- `model.py`: `Plan` / `PlanItem` / `FileRef`, versioned JSON.
- `executor.py`: the last-moment verification and action protocol (see `docs/SAFETY.md`), with a write-ahead `journal.py`.
- `undo.py`: reverses journals, lists and purges quarantines, and inspects interrupted operations.

## Backups: `duplio.backup`

- `repository.py`: a content-addressed chunk store on any backend, with a snapshot/tree split for cheap listing, a lock file, check, prune, diff and restore.
- `retention.py`: restic-style keep policies with extra safety rules.
- `mirror.py`: a plan/run split, so the mass-change brake and previews work.
- `ingest.py` and `media.py`: phone import with a local SQLite ledger.

## Presentation

The CLI (`cli/`) and the TUI (`tui/`) only gather input, call engines and render
results. Engines report progress through the `ProgressSink` protocol
(`events.py`):

- the CLI uses `RichProgress`;
- the TUI uses `PolledProgress`: worker threads update counters, and the UI polls them ten times a second, so a fast scan never floods the event loop;
- tests use `RecordingProgress`.

Cancellation is cooperative (`CancelToken`): workers check it between files and
inside read loops, and journaled work is always left consistent.
