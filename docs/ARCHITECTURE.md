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
      │ session      │ journal/undo │ merge / sync     │
      │ diff         │              │ snapshots        │
      └──────┬───────┴──────┬───────┴────────┬─────────┘
             │       scan (filters, roots, inventory)  │
             │     hashing (algorithms, core, cache)   │  shared services
             │     transfer.copy_verified              │
      ┌──────┴─────────────────────────────────────────┴───┐
      │ vfs: Backend ── LocalBackend (+ mount profiles)    │  storage
      │              ── SMBBackend (smbprotocol)           │
      │              ── SftpBackend (ssh -s sftp)          │
      │              ── DavBackend (http.client)           │
      │              ── FtpBackend (ftplib)                │
      │              ── ADBBackend (adb shell / exec-out)  │
      └────────────────────────────────────────────────────┘
      preview: embedded thumbnails, decoders, cache        pictures for the UI, never for decisions
```

## Storage: `duplio.vfs`

`Backend` (in `vfs/base.py`) is the whole storage contract:

- **identity**: `id`, `uri(path)`, `display(path)`, `device_key(path)`;
- **capabilities**: `capabilities(path) -> Capabilities`;
- **reading**: `walk`, `stat`, `open_read`, `read_range`, `remote_hash`, `listdir`, `free_space`;
- **writing**: `open_write` (atomic, no clobber), `rename` (no clobber), `remove`, `makedirs`, `set_mtime`, `hardlink`, `reflink`, `trash`;
- **path algebra**: `join`, `parent`, `basename`, `normalize`, `is_within`, `same_file`.

Backend-native paths are opaque strings: POSIX for local, ADB, SFTP, WebDAV and FTP, UNC for SMB.
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
- hard-link, reflink and trash support;
- `virtual` (a cloud or virtual drive whose files may download when read) and the derived `read_cost` (0 internal disk, 1 USB or
  spinning disk, 2 network, phone or cloud). The picture cache and the similar-image fingerprinting use `read_cost`, not the
  backend type, to decide what is worth saving.

Engines decide everything from these properties, never from the backend type.
A new backend is therefore usually just a class plus a `Capabilities` profile.

`vfs/mounts.py` turns mounted storage into such profiles. It reads
`/proc/self/mountinfo`, the output of `mount` on macOS/BSD, or `GetDriveTypeW` on
Windows. A GVFS MTP path, a jmtpfs mount, a `cifs` share, a FAT card or a spinning
disk each get the right profile automatically.

### Network protocols: `vfs/remote.py`

SFTP, WebDAV and FTP share `ListingBackend`: a backend that can list one folder (`_list`) gets the walker, `listdir`, the
POSIX path algebra, the depth limit and the check that refuses names a server must not send (`..`, a path separator). It
also provides `RangedReader`, a seekable file built from "open a stream at this offset", which is how a thumbnail or a
sample of a big file costs one small request on every protocol, and `climb_removing` for emptying folders upward.

- `vfs/sftp_protocol.py` is a small SFTP version 3 client over any pair of byte streams. Requests carry numbers and answers
  repeat them, so reads keep a window of requests in flight. `vfs/sftp.py` runs it over the system `ssh` program
  (`ssh -s host sftp`), which brings the user's keys, agent, `~/.ssh/config`, jump hosts and `known_hosts` for free, and
  keeps the whole backend free of compiled dependencies.
- `vfs/webdav.py` uses `http.client` with a small pool of connections, `PROPFIND` for listings (parsed as it arrives),
  ranged `GET`, and `PUT` + `MOVE` for atomic writes; Basic and Digest authentication.
- `vfs/ftp.py` uses `ftplib` with a bounded pool of logged-in connections, `MLSD` where there is one and `LIST` + `MDTM`
  otherwise, `REST` for ranged reads, and an explicit binary-mode step after every listing.

All three use one source of size and time for both listing and `stat`, because the executor compares a scan's facts with a
fresh look.

### Adding a backend

1. Subclass `Backend` (or `ListingBackend` for a protocol that lists one folder at a time) and implement `id`, `capabilities`, `uri`, `walk` (or `_list`), `stat` and `open_read`. Writing methods are optional; the defaults raise `UnsupportedOperation`.
2. Teach `vfs/location.py` the location syntax (`location_kind` and `BackendRegistry.open`).
3. If the far side can hash, implement `remote_hash` and list the algorithms in `Capabilities.remote_hash_algorithms`. The engine negotiates one algorithm per group.
4. Test it against a real server where one can run in the test process (`tests/remotekit.py` starts WsgiDAV, pyftpdlib and paramiko's SFTP server) and add it to the `remote` fixture of `tests/test_remote_contract.py`, which runs the same walk, read, write, delete, rename, probe and backup-repository checks on every protocol. Otherwise add a fake to `tests/fakes.py`, as `test_remote_backends.py` does for ADB and SMB.

### Cross-cutting services for network storage

- `vfs/guards.py`: checks run before anything is written to a destination: a really mounted file system (not the empty mount point), a sentinel marker file, a hung server (the probe runs in a daemon thread with a timeout), and free space with a reserve.
- `vfs/retry.py` and `transfer.copy_verified`: transient errors (stale handle, dropped session, timeout) are retried with backoff and `Backend.recover()` (SMB drops its connection cache); each retry restarts the file, so partial copies are never kept.
- `backup/lockfile.py`: the lock used by repositories and snapshot folders, held as a lease: exclusive create with an owner token, a heartbeat that rewrites it every minute, staleness judged by the share's own clock, `Lease.check()` while working and `Lease.verify()` right before an irreversible step (a lost lease raises `LockLostError`), and a stale lock is renamed away and checked rather than deleted.
- `util/faults.py`: crash and error injection points. Durable steps call `crashpoint(name)`; a test can kill the process or raise `EIO` at the n-th one. Free when unused.
- `util/throttle.py`: one token bucket shared by all workers (copies, read-back verification, repository uploads).
- `util/names.py`: Windows/SMB/exFAT name rules (illegal characters, trailing dots, reserved names, 255-unit limit) as `rel_issues` / `fix_rel`; `Capabilities.windows_names` says when they apply.
- `util/timecmp.py`: modification-time equality within the coarser drive's resolution, plus the single exact one-hour DST offset.
- `vfs/probe.py`: measures a drive's real behaviour instead of trusting its file-system name.

## Pictures: `duplio.preview`

The review screens and `duplio preview` show pictures without paying for them twice.

- `exif.py` finds the cheapest picture inside a file by reading ranges: the thumbnail in a JPEG's EXIF block (the first
  ~64 KiB), or the thumbnail and larger previews of a TIFF-based RAW file (DNG, CR2, NEF, ARW…). It works through a
  byte-budgeted reader, so a hostile file cannot make it read without limit.
- `jpeg.py` and `png.py` decode and encode thumbnails in pure Python (baseline JPEG matches Pillow within rounding), so the
  picture pane needs nothing installed. `render.py` turns a picture into half-block characters, applying EXIF orientation.
- `store.py` keeps pictures: a session store in a temporary folder (default) or a persistent one in the cache folder, indexed in
  SQLite by a hash of drive, path, size and modification time, with no file names, trimmed least-recently-used to a size cap.
- `service.py` is the cheapest-first ladder: cache, then a ranged read of the photo's own thumbnail, then a bounded whole read
  only when the file is small or the caller asked, otherwise `Unavailable` with a reason. It also chooses the cheapest member of
  a group of identical files (`pick_source`) and prefetches the next groups in the background (two workers, a per-drive
  limit, newest request first).
- `vfs/mounts.py` decides whether a drive is slow or virtual (Windows placeholder attributes, macOS dataless files, volume
  labels such as Tresorit/OneDrive, WebDAV redirector paths), and `duplio drives` explains each verdict.

`vfs/listing.py` is a short-lived memory of folder listings for the folder browser (`tui/picker.py`); scans never use it.

## Launchers and bundled libraries

`Duplio.bat`, `Duplio.command` and `duplio.sh` find a Python 3.11 or newer, put `src/` and `vendor/` on its path and run
`python -m duplio`. `vendor/` holds the pure-Python libraries (click, rich, textual and their dependencies) as unpacked
wheels with their licences; `tools/refresh_vendor.py` rebuilds it and refuses anything compiled. A bare `duplio` at a
terminal opens the guided screen.

## File names: `duplio.util.text`

File names are untrusted bytes, not text. Every persistence path goes through
`json_dumps` (ASCII-escaped, so lone surrogates round-trip) or `fs_bytes`
(SQLite keys). Every display path goes through `printable` (the CLI's
`SafeConsole` applies it to all output; the TUI uses `ptext`). Comparisons that
use names use `name_key` (NFC, optionally case-folded) and always try the exact
spelling first.

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

`diff.py` classifies every file of two locations (same / moved / changed / only / unreadable) by running the
engine in `all` mode over the pair, after a free pass that recognises hard links between the two sides. The
non-content bases decide by path, size and time first and only hand the leftovers to the engine, to find
moves. `merge` and `sync` are built on it.

## Planning and applying: `duplio.plan`

- `rules.py`: ordered keep rules, used as a lexicographic sort.
- `planner.py`: turns groups into `PlanItem`s. It applies the hard constraints first (reference, protected, read-only, capability), then the rules or the user's own choices, then runs an independent invariant check.
- `model.py`: `Plan` / `PlanItem` / `FileRef`, versioned JSON.
- `executor.py`: the last-moment verification and action protocol (see `docs/SAFETY.md`), with a write-ahead `journal.py`.
- `undo.py`: reverses journals and lists and purges quarantines.
- `reconcile.py`: settles operations that a crash left without an outcome, from the files their journal intent names (finished, not started, or rolled back by a rename); `record_failure` journals an error as done when the change had in fact taken effect.

## Backups: `duplio.backup`

- `repository.py`: a content-addressed chunk store on any backend, with a snapshot/tree split for cheap listing (every file list embeds a copy of its summary), a leased lock, check, repair, diff and restore.
- `repo_gc.py`: pruning. References are read from every file list; unused data goes to `trash/` and is deleted by a later run; data younger than a safety margin is never touched.
- `chunker.py`: fixed blocks, or keyed FastCDC-style content-defined chunks (numpy, optional), chosen per repository.
- `retention.py`: restic-style keep policies with extra safety rules.
- `mirror.py`: a plan/run split, so the mass-change brake and previews work.
- `ingest.py` and `media.py`: phone import with a local SQLite ledger.
- `merge.py`: plan from a `DiffResult` (conflict policy, name rewriting, layout, copy-once for identical sources), then run with the shared `execute_action` (verify, archive, no-clobber). `--move` builds a normal quarantine `Plan` with the destination as a reference root and runs the existing `Executor` under the same journal, so one `undo` reverses everything.
- `sync.py` and `syncstate.py`: a per-pair SQLite history of the last identical state; the decision table, safety brakes, and the journaled steps (copy, replace, quarantine, conflict rename).
- `snapshots.py`: hard-link snapshot folders with sidecar records, built in a hidden folder and renamed into place.
- `crypto.py`: AES-256-GCM sealing with the object identity as associated data, HMAC object ids, scrypt-wrapped master key; `repository.py` seals objects, file lists and summaries through it.
- `manifest.py`, `versions.py`, `export.py`: checksum manifests, `.duplio-versions` housekeeping, snapshot export to archives.

Journal operations understood by `undo`: `quarantine`, `trash`, `delete`, `hardlink`, `reflink`, `merge-copy`,
`merge-replace`, `sync-rename`. Undoing a sync also deletes the pair's history.

## Automation

`notify.py` delivers webhook, command and e-mail notifications and never raises; `watch.py` is a polling loop over a
folder fingerprint with a settle period; `cli/hooks.py` runs a profile's `pre`/`post`/`on_failure` commands and applies
its notification policy around `duplio run`.

## Presentation

The CLI (`cli/`) and the TUI (`tui/`) only gather input, call engines and render
results. Engines report progress through the `ProgressSink` protocol
(`events.py`):

- the CLI uses `RichProgress`;
- the TUI uses `PolledProgress`: worker threads update counters, and the UI polls them ten times a second, so a fast scan never floods the event loop;
- tests use `RecordingProgress`.

Cancellation is cooperative (`CancelToken`): workers check it between files and
inside read loops, and journaled work is always left consistent.
