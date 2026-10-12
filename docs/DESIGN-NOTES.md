# Design notes: what current tools do, and what Duplio took from them

These notes were written while designing Duplio (October 2026), from current
documentation of the leading open-source tools.

## Duplicate finders

**[Czkawka / Krokiet](https://github.com/qarmin/czkawka)** groups by size first,
then hashes a small fragment (2 KB) of each candidate, then hashes whole files; it
caches results for repeat scans. Its author reports that the partial-hash step can
roughly halve search time.

Duplio uses the same staged idea, with three changes:

- It samples the head, middle and tail (16 KiB each) instead of only the head. Container formats such as MP4 and disk images often have identical headers.
- It prunes by *mode* after every stage. In reference, cross and unique scans, whole buckets are dropped before any more data is read.
- It is device-aware: head-only sampling on MTP, and on-device SHA-256 on Android over ADB.

**Hash choice.** Current guidance is that XXH3 is the fastest per byte for trusted
inputs, while BLAKE3 closes the gap on large inputs with SIMD and is the right
choice when collisions must be practically impossible. At scale, 64-bit hashes are
not safe for deduplication. Duplio therefore uses:

- XXH3-128 to rule files *out*, during sampling;
- BLAKE3 (or BLAKE2b-256 from the standard library) to rule files *in*;
- an automatic byte-for-byte comparison whenever a user picks a weaker hash.

`duplio doctor` measured on the build machine: BLAKE3 4.2 GiB/s and XXH3-128
6.1 GiB/s, against BLAKE2b at 0.4 GiB/s on one core.

## Backup tools

**[restic](https://restic.net/blog/2025-03-27/restic-0.18.0-released/)**, rustic
and Kopia use content-defined chunking. Restic uses Rabin fingerprints with a
per-repository random polynomial, files under 512 KiB are not split, and blobs
average about 1 MiB. Restic 0.18 (March 2025) added a mitigation for an attack
that could reveal whether a known large file is present in a repository.

Duplio's snapshot store keeps the parts of that design that matter for safety:

- content addressing;
- incremental runs keyed by metadata;
- snapshots that only become visible once their data is durable;
- mark-and-sweep pruning;
- restic-style retention.

It uses **fixed-size chunks by default** and offers content-defined chunking as an opt-in (`backup init --chunker cdc`;
see "Chunking" below). Fixed chunks deduplicate whole files across machines and in-place edits of large files; they do not
survive bytes being *inserted* into a big file. Encryption is opt-in (AES-256-GCM, keyed ids), added in the spirit of restic and Kopia: a random master key wrapped by a password-derived key, so a password change is instant.

## Phones on the desktop

**MTP on Linux** ([Arch wiki](https://wiki.archlinux.org/title/MTP),
[Debian wiki](https://wiki.debian.org/mtp)) runs through GVFS (`gvfs-backends`) or
FUSE tools such as jmtpfs, simple-mtpfs and go-mtpfs. Two caveats matter here:

- Phones must be unlocked and switched to *File transfer*.
- Some file managers hang on `DCIM/Camera`.

Duplio:

- recognises these mounts from the mount table and gives them a cautious profile: one request at a time, head-only sampling, no reliance on inode numbers, 1 s timestamp tolerance, copy-verify-delete where rename is unreliable;
- warns when a phone is visible but locked;
- has the mirror brake refuse to treat an empty, locked phone as "everything was deleted".

**ADB** avoids MTP entirely. It is the only practical direct route on macOS and
Windows, and it allows hashing on the device.

## Terminal UI

The TUI is built on **[Textual](https://pypi.org/project/textual/)** (8.2.x at the
time of writing). Long jobs run in worker threads that update a polled progress
model; the UI never blocks on I/O and never floods its event loop.

## Comparing and syncing folders

**[FreeFileSync](https://freefilesync.org/manual.php?topic=comparison-settings)** compares by time and size
(two-second tolerance for FAT, an exact one-hour shift for daylight saving) or by content, and keeps a database of the
last synchronised state so it can tell a new file from a deleted one. **[rclone bisync](https://rclone.org/bisync/)**
makes the same distinction from prior listings, aborts when too much would be deleted (`--max-delete`, 50 % by default),
refuses to run when a side looks empty, and offers conflict policies that keep the loser under a new name.
**Carbon Copy Cloner**'s SafetyNet and **SyncBack**'s versioning move replaced and deleted files aside instead of
removing them.

Duplio's `diff` / `merge` / `sync` take those ideas and add the one most of them lack: the question people actually ask is
usually *"is everything in A also in B, whatever it is called there?"*, so content, not path, decides first, and a renamed
photo is a move rather than a deletion plus a new file.

## Network storage pitfalls

The recurring failures in NAS and cloud-mount backups are well documented: a share that is not mounted so the backup fills
the local disk (the reason rclone has `--check-access` and rsnapshot a `no_create_root` option), NFS stale handles after a
server reboot, servers that hang forever instead of failing, and Windows-style name rules (`: ? *`, trailing dots, `CON`) on
SMB and exFAT. Duplio checks the mount, an optional marker file, responsiveness and free space before writing; retries
transient errors with a reconnect; rewrites names deliberately instead of failing mid-copy; and compares times within what each
drive can store. `duplio probe` measures all of this on the actual drive.

## Pictures on slow and cloud drives

The complaint that started this: reviewing duplicates on a cloud drive was slow because every thumbnail meant pulling the whole
photo across. The options considered, and why the cheapest-first ladder won:

- **Download and keep copies of the photos** (a mirror): fastest afterwards, but it moves the cost to the first run, uses as
  much disk as the library, and for a zero-knowledge drive writes decrypted copies to disk.
- **Cache thumbnails made from whole downloads**: saves the second look, not the first, which is the slow one.
- **Read the thumbnail the photo already carries.** Cameras and phones write a JPEG thumbnail into the EXIF block near the start
  of the file, and RAW files carry previews at known offsets in their TIFF structure. A ranged read of the first ~64 KiB gets it:
  kilobytes instead of megabytes, with no cache needed for the first look. It is a trick image browsers commonly use on local
  files; the addition here is doing it with ranged reads over each protocol, and falling back (small file read whole; bigger
  pictures only when asked) in a defined order.
- Cache what was fetched, in a temporary folder that is deleted when Duplio closes by default, with an opt-in persistent cache.
  The default is deliberately the cautious one for zero-knowledge drives.

Cloud drives report files that are not on disk with placeholder attributes (Windows: `FILE_ATTRIBUTE_OFFLINE`,
`RECALL_ON_OPEN`, `RECALL_ON_DATA_ACCESS`; macOS: the `SF_DATALESS` flag). Reading such a file downloads it, so Duplio
treats the attribute as "online only" and fetches nothing until asked.

**Tresorit.** The question was whether a Tresorit drive is an SMB share. Searches (the vendor's pages could not be opened from
the environment this was written in, so this rests on search results, not on the documents) describe it as a client-side virtual
drive (a drive letter on Windows, a location in Finder on macOS) that exists only while the Tresorit app runs, keeps file
metadata locally and decrypts contents on demand; an older macOS version used FUSE, and some reports tie the older Windows
drive's behaviour to the Windows WebDAV redirector. None of them says SMB, and none settles what a given version reports.
So Duplio does not assume: `duplio drives PATH` prints the drive type, file-system name, volume label and, for mapped drives,
the server name Windows reports, and says whether it recognised a WebDAV redirector. Any drive that looks virtual or networked
gets the same treatment.

## More than SMB

Common remote kinds and how each was added without new installs:

- **SFTP** is the most requested after SMB. Instead of an SSH library (which brings compiled code that cannot be bundled for
  every computer), Duplio speaks the SFTP protocol itself over the system `ssh`. That keeps keys, agent, `~/.ssh/config` and
  `known_hosts` behaviour identical to what the user already has. The cost is that password login needs a helper file for ssh's
  `SSH_ASKPASS` mechanism, which is done on Unix-like systems only.
- **WebDAV** covers Nextcloud, ownCloud, and the "WebDAV" switch of Synology and QNAP boxes, and is also what the Windows
  WebDAV redirector talks. Its `DELETE` removes a folder with everything inside, so Duplio never issues one for a folder it did
  not just create, and it cannot set modification times, so content decides identity.
- **FTP/FTPS** is old but is still what routers with a USB disk, cameras and shared hosting offer. `MLSD` gives exact
  times; servers without it (vsftpd, for one) are handled with `LIST` plus `MDTM`; a server with neither is refused, because
  Duplio's cache and change checks need a modification time it can trust.

## Run it without installing

The launchers exist for people who are not going to type `pip install`. The constraint is "no compile, no download": the
libraries Duplio needs at run time (click, rich, textual and their dependencies) are pure Python, so they are committed
unpacked in `vendor/` (importing from wheel files would break Textual, which reads its own style files from disk). Features
that need compiled code (Pillow, cryptography, smbprotocol, numpy) stay optional, and the picture pane has its own small
decoders precisely so the common path does not need Pillow.

## Paid features, and where they landed

| Feature of commercial tools | In Duplio |
|---|---|
| Client-side encryption (Arq, SyncBack Pro) | `backup init --encrypt` |
| Time-Machine style browsable snapshots (CCC, ChronoSync, Time Machine) | `duplio snapshot` (hard links) |
| Two-way sync with conflict handling (ChronoSync, Beyond Compare, SyncBack) | `duplio sync` |
| Bandwidth limits and schedules (Arq, Backblaze) | `--bwlimit`, `duplio schedule` |
| E-mail / push notifications, before/after commands (SyncBack Pro, ChronoSync) | profile `notify`, `pre`, `post`, `on_failure` |
| Real-time monitoring (SyncBack, FreeFileSync RealTimeSync) | `duplio watch` (polling) |
| Corruption checks (CCC) | `duplio manifest verify`, `backup check --read-data` |
| Version history and file search across backups (Arq, Time Machine) | `backup find`, `backup history`, `backup export` |
| Filters by type and age | `--type`, `--newer-than`, `--older-than` |


## A second look at repositories, after an independent review

An independent review of the first version raised five problems in the backup repository and three design questions.
Before changing anything each was checked against the code, and the fix was designed from how the established tools
solve the same problem rather than from the review's suggestion alone.

**Garbage collection.** Kopia's maintainers explain why its garbage collection does not simply delete whatever nothing
references: several computers can write to one repository, a backup can run for days, and its snapshot record only exists at
the end, so data that looks unreferenced may be in use ([discussion](https://kopia.discourse.group/t/how-to-immediately-gc-unused-blobs/295),
[another](https://kopia.discourse.group/t/unreferenced-content-does-nor-get-deleted/84)); it applies a minimum age and keeps
`--safety=none` for people who accept the risk. Duplio does the same (default one day) and adds what restic and Kopia do not
have for an offline, single-writer repository: a trash with a grace period, so that even a wrong decision is reversible.
References are read from the file list of every snapshot (including those whose summary is missing or damaged), and any file list
that cannot be read stops the run. The summary of a snapshot is embedded in its file list, so a lost summary can be rebuilt exactly.

**Locks.** restic refreshes its lock about every five minutes and treats one that is 30 minutes old (or whose process is gone) as stale
([forum](https://forum.restic.net/t/restic-unlock-automation/5511)); since 0.15 it also cancels a command that cannot refresh its lock in time, because a
backup that slept could otherwise carry on while a prune removed its data
([changelog](https://git.0l.de/Mirrors/restic/src/branch/master/changelog/0.15.0_2023-01-12/issue-2715)). Duplio's lock is a lease on the same model
(refresh every minute, abandoned after twenty), with two differences. Age is measured on the share (the lock file against a probe file
written moments ago on the same share), because the time written inside the lock comes from another computer's clock. And the owner fences
itself: it re-reads the lock right before publishing a snapshot or moving data, and a machine that woke up after sleeping re-confirms the lock
instead of assuming it.

**Change detection.** Borg's default is to treat a file as unchanged only if its change time (ctime), size and inode match
([docs](https://borgbackup.readthedocs.io/en/latest/usage/create.html)): the change time cannot be set by users, whereas the modification
time can be set back after an edit. The cost is that a metadata change alone looks like a change, which in a deduplicating backup
costs a re-read and no storage. Duplio now does the same for backups and sync on file systems that keep these reliably, with `--ignore-ctime` for jobs that change
attributes in bulk (restic has the same switch).

**Files that change during a backup.** Borg reports "file changed while we backed it up" as a warning and exits with status 1
([discussion](https://www.python.org/pipermail/borgbackup/2023q2/002217.html)), and the recommended answer to live data is to back up a
file-system snapshot. Duplio flags the file in the snapshot rather than only in that run's output, so the information is still there when
the backup is restored or thinned months later, and refuses to treat a snapshot of a moving target as the last good one.

**Chunking.** Content-defined chunking cuts where a rolling hash of the last few dozen bytes meets a condition, so after an insertion
the cut points re-synchronise. Duplio's is FastCDC-style (Xia et al., 2016): a gear hash, a minimum size, and "normalised" cutting that
keeps sizes near the average. Python packages exist ([fastcdc-py](https://github.com/titusz/fastcdc-py), optional Cython;
[PyFastCDC](https://pypi.org/project/pyfastcdc/), Cython); this one is written against numpy instead so that the gear table can be **keyed** by
the repository (and, when encrypted, by its secret key), which stops chunk boundaries from being used to recognise a known file, and so that
it has no compiler requirement. It evaluates the 64-byte window in six vectorised doubling passes instead of one Python step per byte.

Measured on one core of the benchmark machine below (128 MiB of random data, chunking plus BLAKE2b hashing): fixed 4 MiB blocks 406 MB/s
(hash-bound), content-defined 51 MB/s for an average chunk of 1 MiB or 4 MiB alike, so it costs roughly an eighth of the speed. With a 3 MiB file and 100 bytes
inserted near its start, the second backup stored over 2.5 MiB of new data in a fixed-block repository and under 0.3 MiB in a content-defined one
(`test_an_insertion_into_a_big_file_costs_little_with_cdc_and_everything_with_fixed`). Pick content-defined chunking for files that are edited by
insertion (databases, mailboxes, documents, disk images); leave the default for photos, video and archives, which are written once.

**Testing for crashes.** ALICE ([summary](https://blog.acolyer.org/2016/02/11/fs-not-equal/)) found 60 crash-consistency vulnerabilities in 11 widely used programs
(LevelDB, SQLite, Git among them) by replaying the file-system calls of a workload and checking an invariant after every possible interruption.
CrashMonkey and ACE ([paper](https://arxiv.org/pdf/1810.02904)) found that most bugs need only a few operations, so exhaustive tests of small workloads are
effective. Duplio applies the same idea one level up, to its own steps (`tests/test_crash_sweep.py`): each operation is run on a small tree, every durable step is
numbered, and the operation is replayed once per step in a process that is killed there (or made to fail with an I/O error, including "the change happened but the answer
was lost"). It found real defects that the earlier hand-written tests had not: an interrupted run whose changes `undo` and `recover` could not see, a failure recorded as
failed although the change had happened, an ambiguous commit that left a duplicate on retry, a torn journal line that hid everything written after it, and a phone import that
did not free space after being interrupted.

**Scale.** `benchmarks/scale.py` generates a tree and times the real command line, in a clean process each, with the child's peak memory. Run on 300,000
files (4.33 GB, median file about 4 KiB, a long tail to 4 MiB, 10 % duplicates; a second tree of hard links with 1 % of the files changed), on a 4-core Linux virtual machine
with a local disk, Python 3.13:

| Command | Time | Peak memory |
|---|---|---|
| `scan` one tree for duplicates | 62 s | 553 MiB |
| `backup run`, first run | 486 s (about 620 files/s, 8.9 MB/s) | 526 MiB |
| `backup run`, second run, nothing changed | 19 s (about 16,000 files/s) | 656 MiB |
| `backup check` | 8 s | 534 MiB |
| `diff` of two trees, 1 % changed | 105 s | 1,141 MiB |
| `sync --dry-run`, first run | 56 s | 554 MiB |

The repository held 3.93 GB for the 4.33 GB tree. What this shows, and what it does not:

- Memory grows with the number of files (roughly 2 to 4 KiB per file for these commands). Extrapolated linearly, a few million files fit in 16 GB and tens of millions do not; that is an
  inference from one measurement, not a tested limit. Past it, an on-disk index would be needed.
- The first backup of many small files is limited by the file system, not the CPU. Every chunk is its own file, written, flushed and renamed; on a 20,000-file sample the same backup ran at
  577 files/s with flushing and 1,447 files/s without it, so durability accounts for about 60 % of the time. restic and Kopia bundle small blobs into pack files to avoid this. Duplio does not, and a first
  backup of a photo library (few, large files) is far less affected than a source tree.
- An unchanged tree is cheap to back up again (19 s for 300,000 files), which is what scheduled runs mostly do.
- Not measured: network shares (round trips dominate there), spinning disks, trees beyond 300,000 files, and concurrent writers (a repository has one writer at a time by design).

