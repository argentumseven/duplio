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

It uses **fixed-size chunks** instead of content-defined chunking. CDC in pure
Python costs tens of MB/s per core, which would make phone and NAS backups
CPU-bound. Fixed chunks still deduplicate whole files across machines and in-place
edits of large files. The repository format is versioned so a chunker can be added
later. Encryption is not in v1 (see `SAFETY.md`, "Known limits").

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
