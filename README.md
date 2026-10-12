<p align="center">
  <img src="assets/duplio-icon-256.png" width="160" alt="Duplio icon: a blue and a red featureless figure pulling apart, joined by a stretching web">
</p>

<h1 align="center">Duplio</h1>
<p align="center"><em>Duplicates in. One survivor out.</em></p>

Duplio finds identical files across **local folders, phones (MTP, PTP, iOS AFC, or
Android over ADB) and SMB shares**, helps you decide which copy survives, and cleans
up in a way you can always undo. It also **compares two folders by content** (a renamed
photo is not a difference), **merges** whatever one lacks into the other, **syncs** in both
directions, and does **deduplicated, encrypted, versioned backups**, hard-link **snapshots**,
**verified mirrors** and **phone photo imports** that skip what you already have. Every one of
them works on local folders, mounted network drives, native SMB shares and phones alike.

It is built around one rule: **never lose data.** Nothing is removed without a plan
you review. Right before anything is touched, the file and the copy you keep are
re-read and re-hashed. Every change is written to a journal first. The default
action moves files into a quarantine and can be undone instantly.

```
$ duplio scan ~/Archive          # real output from the benchmark under "Performance"
╭──────────────────────────── Duplio scan 20261012T134014570Z-5a25 ────────────────────────────╮
│  1  ~/Archive  Hard disk (ext4)                                                              │
│                                                                                              │
│ 20,000 files · 1.8 GiB scanned in 2.2s                                                       │
│ 389 duplicate groups · 1,601 redundant copies · 727.5 MiB reclaimable                        │
│ evidence: 8 size buckets → 5,549 sampled → 15,489 hashed · 1.2 GiB read · 0 cache hits       │
╰──────────────────────────────────────────────────────────────────────────────────────────────╯
```

Rescanning the same tree takes 1.1 s: every digest comes from the cache and nothing
is read again.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [The terminal UI](#the-terminal-ui)
- [Phones, cameras and network shares](#phones-cameras-and-network-shares)
- [How duplicates are detected](#how-duplicates-are-detected)
- [Performance](#performance)
- [What counts as "the same file"](#what-counts-as-the-same-file)
- [Choosing which copy to keep](#choosing-which-copy-to-keep)
- [Actions](#actions)
- [Safety model](#safety-model)
- [Compare, merge and sync two folders](#compare-merge-and-sync-two-folders)
- [Network drives](#network-drives)
- [Backups](#backups)
- [Integrity: manifests and old versions](#integrity-manifests-and-old-versions)
- [Profiles, schedules, notifications and watch](#profiles-schedules-notifications-and-watch)
- [Filters](#filters)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Development](#development)

## Install

Duplio needs Python 3.11 or newer.

```bash
pip install "duplio[all]"        # everything: TUI, fast hashing, SMB, images, trash, encryption
pip install duplio               # core only (CLI; standard-library hashing)
```

| Extra    | Adds                                   | Packages               |
|----------|----------------------------------------|------------------------|
| `tui`    | the interactive terminal UI            | textual                |
| `fast`   | BLAKE3 content hashing, XXH3 sampling  | blake3, xxhash         |
| `smb`    | native `smb://` access without a mount | smbprotocol            |
| `media`  | similar-image search, EXIF dates       | Pillow                 |
| `trash`  | the system-trash action                | Send2Trash             |
| `encrypt`| encrypted backup repositories          | cryptography           |

Run `duplio doctor` to see what is installed, which phone tools (`adb`, `jmtpfs`,
`gio`) are available, and how fast each hash runs on your machine.

## Quick start

```bash
duplio ui                                          # guided interface (recommended)

duplio scan ~/Pictures ~/Downloads                 # read-only: what is duplicated?
duplio dedupe ~/Pictures ~/Downloads               # scan → plan → confirm → quarantine
duplio undo                                        # changed your mind? put it all back

duplio compare adb:/sdcard/DCIM --against ~/Pictures      # is my phone backed up?
duplio ingest  adb:/sdcard/DCIM ~/Pictures/Library        # import only new photos
duplio backup init /mnt/usb/duplio && duplio backup run ~/Documents /mnt/usb/duplio

duplio diff ~/Photos smb://me@nas/photos           # same files? renamed? only on one side?
duplio merge ~/Photos smb://me@nas/photos --dry-run  # copy over only what the NAS lacks
duplio sync ~/Documents /media/usb/Documents       # two-way, deletions never destructive
duplio probe smb://me@nas/backups                  # how does this drive really behave?
```

A typical clean-up has three steps, and you can stop after any of them:

1. **Scan** (`duplio scan`): read-only. Every scan is saved as a session.
2. **Plan** (`duplio plan [SESSION]`): decide what to keep and remove. The plan is saved as a JSON file you can read or edit.
3. **Apply** (`duplio apply PLAN`): carry out the plan, re-checking every file at the last moment.

`duplio dedupe` does all three in one go and asks for confirmation before it changes anything.

## The terminal UI

`duplio ui` opens a keyboard- and mouse-driven interface:

- **Home** has eight workflows and a live list of connected phones, shares and drives.
- **Find duplicates**: add locations, either typed in or picked from the detected devices. Mark any location as *reference* (used for matching, never changed). Then choose the mode and verification level.
- **Review**: duplicate groups on the left, sorted by wasted space, and their copies on the right: ★ keep, ✗ remove, 🔒 locked (reference, protected or read-only).
  - `space` toggles keep/remove. You can never unmark the last kept copy.
  - `k` keeps only the highlighted copy.
  - `x` skips the whole group.
  - `r` re-applies the keep rules.
  - `p` shows the plan.

  The action can be switched (quarantine, trash, delete, hardlink, reflink) and the summary updates live.
- **Similar images** tab: visually similar but not identical pictures. Nothing is selected unless you mark it, and marked images can only be quarantined or trashed.
- **Plan review**: every change, with a *Verify only (dry run)* button. Permanent deletion requires typing `DELETE`.
- **History & undo**: every applied plan. Undo it, or permanently empty its quarantine, which requires typing `PURGE`.
- **Is it backed up?**, **Import from phone**, **Mirror a folder** and **Snapshot backup**: guided forms with previews.
- **Compare & merge** and **Two-way sync**: compare two folders, preview exactly what a merge or sync would do, then confirm.

`F2` switches between the light and dark themes, `?` explains the safety model and the keys, and `Ctrl+Q` quits.

## Phones, cameras and network shares

Run `duplio devices` to list everything Duplio can see, each with a location string you can paste into any command.

| Storage | How to use it | Notes |
|---|---|---|
| Android over **MTP** (Linux) | GVFS: `/run/user/UID/gvfs/mtp:host=…/Internal shared storage` or a `jmtpfs`/`simple-mtpfs`/`go-mtpfs` mount point | Unlock the phone and pick *File transfer*. Duplio detects MTP mounts and uses one request at a time, head-only sampling and copy-verify-delete where MTP cannot rename. |
| Android over **ADB** (all OSes) | `adb:/sdcard/DCIM` or `adb://SERIAL/sdcard` | Needs USB debugging. **Hashes are computed on the phone** (`sha256sum`), so comparing a phone against a computer transfers digests, not gigabytes. |
| **iPhone/iPad** | `ifuse` mount or GVFS `afc:`/`gphoto2:` | Read access to the camera roll; PTP is treated as read-only. |
| **SMB**, mounted | any `cifs`/`smbfs` mount or GVFS `smb-share:` | Detected and treated as network storage (parallel reads, 1 s time tolerance, case-insensitive). |
| **SMB**, native | `smb://[domain;]user@server/share/path` or `\\server\share` | No mount needed (`pip install duplio[smb]`). The password comes from `DUPLIO_SMB_PASSWORD`, `keyring`, or a prompt. With no user name, Kerberos is used. |
| USB sticks, SD cards | their mount point | FAT/exFAT get 2-second timestamp tolerance and no hard links. |

On **macOS** Android phones are not mounted. Use ADB, or an MTP FUSE tool. On
**Windows** phones are only visible in Explorer, not as drive letters, so use ADB.
Duplio refuses to scan the same phone through ADB and MTP at once, because each
file would look like its own duplicate.

## How duplicates are detected

The cheapest evidence comes first; bytes are read only where they can still change the answer:

```
size buckets ─► hard-link collapse ─► sample fingerprint ─► full digest ─► byte compare
   free             free (inode)        3 × 16 KiB/file       whole file     optional
```

1. **Size.** Files of a unique size cannot have a duplicate.
2. **Aliases.** Hard links (and anything else with the same inode on storage where inodes are stable) are one file. They are never "deduplicated" against each other, and the space they share is not counted as wasted.
3. **Sample fingerprint.** For files over 128 KiB, the head, the middle (page-aligned) and the tail are sampled with a fast hash (XXH3-128). On MTP and slow links only the head is read. This rules out most same-size files quickly.
4. **Full digest.** The whole content is hashed with BLAKE3, falling back to BLAKE2b-256. When a group includes an ADB phone, the group agrees on SHA-256 and the phone computes it itself.
5. **Byte compare** (`--verify bytes`) confirms matches byte for byte. It always runs when you choose a non-cryptographic hash (`--hash md5`, `sha1`, `xxh3_128`).

After every stage each bucket is split and **pruned by the mode**. In
`reference`/`unique`/`cross` modes, a bucket that no longer spans the required
locations is dropped before more data is read. Checking a phone against a 2 TB
archive reads only the archive files that match a phone file's size *and*
fingerprint.

Every read is checked against the size recorded at scan time. A file that changes
mid-scan, or a truncated device stream, can therefore never pass as a match.
Unreadable files are reported, never guessed about. In `unique` mode, if a backup
copy cannot be read, the matching file is reported as *unverified*, never as
"missing" or "safe".

**Scheduling.** I/O is queued per physical device, with a worker count sized to
that device: one for MTP, few for spinning disks (read in inode order), more for
SSDs and network shares. A phone and a NAS are read at the same time and neither
slows the other.

**Cache.** Digests are cached in SQLite, keyed by path, size and modification time,
so a rescan of an unchanged archive takes seconds. Files modified in the last few
seconds are not cached, because their timestamps cannot yet be trusted. The cache
only speeds up discovery: actions always re-read the bytes.

**Modes.**

| Mode | Reports |
|---|---|
| `all` (default) | every set of identical files, within and across locations |
| `cross` | only files present in two or more locations |
| `within` | duplicates inside each location separately |
| `reference` (default with `--ref`) | copies, in normal locations, of files that exist in reference locations |
| `unique` (`duplio compare`) | files in normal locations with **no** copy in the reference locations |

`--verify quick` matches by size and name only and is report-only. `--similar-images`
adds perceptual (dHash + BK-tree) grouping of resized and re-encoded photos. JPEGs
are decoded at 1/8 scale, so this stays cheap.

### Performance

These numbers come from a synthetic tree of 20,000 files (1.8 GiB, 10 % duplicates,
deliberately few distinct sizes), measured on one ext4 volume with a warm OS page
cache:

| Run | Time | Data read |
|---|---|---|
| first scan | 2.2 s | 1.2 GiB (5,549 files sampled, 15,489 hashed) |
| rescan | 1.1 s | 0 B (21,038 cache hits) |

Measure on your own hardware with `duplio doctor`. It benchmarks every available
hash; BLAKE3 and XXH3 typically run at several GiB/s per core.

## What counts as "the same file"

**Names never decide.** Duplicates are found by content, so `holiday.jpg`,
`IMG_0042.jpeg` and `backup-of-something.bin` are one group if their bytes are
identical, and two files called `holiday.jpg` with different bytes are not. The same
goes for "is my phone backed up?": a photo you renamed on the computer still counts as
backed up. Names matter in exactly four places:

| Where | How names are used |
|---|---|
| keep rules | `not-copy-named`, `prefer-path=…` etc. only choose *which of several proven-identical copies* survives |
| `--verify quick` | size + name, report-only, compared after Unicode (NFC) normalisation and case folding |
| `mirror` | files are paired by relative path (exact spelling first; then NFC/case-folded on storage that needs it); with `--delete`, a file that was merely renamed is detected by content and moved, not re-copied |
| `ingest` | a name that is already taken by *different* content gets ` (2)`, ` (3)`… |
| `diff`, `merge`, `sync` | the relative path pairs "the same place" on both sides (NFC, case-folded when a drive needs it); `merge` names the kept-both copy and `--names` rewrites names a destination cannot hold |

**Odd file names are handled as data, not text.** Real names can be invalid UTF-8
(old archives, Windows-1252, some phones), contain terminal escape sequences or
newlines, use bidirectional overrides (a name containing U+202E can make `photo…gnp.exe` display as `photoexe.png`),
differ only in Unicode form (`é` composed or decomposed, as macOS and some SMB servers
store it) or be 255 bytes long. Duplio:

- stores every path losslessly (ASCII-escaped JSON, byte-exact SQLite keys), so such
  files are scanned, planned, quarantined, undone, backed up and restored byte for byte;
- shows undecodable bytes as `\xe9` and control or bidi characters as visible escapes
  (`\x1b[2J`, `\u202e`), so a hostile name can neither crash the screen, rewrite your
  terminal nor spoof another name; only `compare --list [-0]` prints raw bytes, for
  piping into `xargs`;
- never merges two different names: `café` (composed) and `café` (decomposed) are two
  files on Linux and stay two files, and a mirror reports, instead of silently dropping,
  names that differ only by case when the destination cannot store both;
- shortens its own temporary names so that even a 255-byte file name can be written.

**Not detected as duplicates (yet):** files that are *similar but not identical* other
than images (re-encoded video, audio with different tags, documents re-saved by an
editor), whole duplicate folders, a file that is a truncated prefix of another, and
related sets such as RAW+JPEG pairs, Live Photos or `.xmp` sidecars (Duplio treats each
file on its own, so a keep rule can keep one half of a pair from each location).

## Choosing which copy to keep

Keep rules work like a multi-column sort. The first rule that tells two copies
apart decides, and the path is the final, deterministic tie-breaker.

```
default: has-hardlinks, not-copy-named, root-order, oldest, shortest-path
```

| Rule | Prefers |
|---|---|
| `not-copy-named` | names without `copy`, `(1)`, `~`, `.bak`, `Kopie`, `コピー`… |
| `has-hardlinks` | the copy other hard links already point to |
| `root-order` | locations in the order you listed them |
| `prefer-root=N` / `avoid-root=N` | location number N |
| `prefer-path=GLOB` / `avoid-path=GLOB` | e.g. `prefer-path=**/Camera/**`, `avoid-path=**/Downloads/**` |
| `prefer-kind=KIND` | `local`, `rotational`, `removable`, `network`, `device` |
| `oldest` / `newest` | modification time |
| `shallowest` / `deepest`, `shortest-path` / `longest-path` | folder structure |

These are **always kept**, whatever the rules say:

- copies in reference locations;
- protected paths, including by default `*.photoslibrary`, Lightroom data, `*.app` bundles, `Android/data`, `Android/obb`, `node_modules`, `site-packages`, `.venv`, `Program Files` and `AppData`. Removing a "duplicate" inside these breaks them;
- read-only storage.

Version-control internals (`.git` and similar) are skipped when deduplicating.

## Actions

| Action | Effect | Undo |
|---|---|---|
| `quarantine` (default) | instant move into `.duplio-quarantine/<run>/` inside the scanned location, with the folder structure kept. Falls back to copy → verify → delete into Duplio's state folder on storage that cannot rename (MTP). | `duplio undo` |
| `trash` | system trash (local disks) | from the system trash |
| `delete` | permanent. Requires `--i-understand` with `--yes`, or typing `DELETE` in the UI | none |
| `hardlink` | replaces the copy with a hard link to the kept file (same filesystem) | `duplio undo` re-creates an independent file with its original timestamp and permissions |
| `reflink` | replaces the copy with a copy-on-write clone (btrfs, XFS, APFS): saves space without linking edits | `duplio undo` |

Quarantined files still use space until `duplio quarantine purge [ID | --older-than 30d]`.

## Safety model

For every file to remove or replace, the executor:

1. re-reads its metadata and skips it if the size or modification time changed after the scan;
2. checks that the **kept** copy still exists and still hashes to the group digest. This hash is computed fresh, never taken from the cache;
3. re-hashes the file to remove, or compares both byte for byte (`--verify-before bytes`, and always for weak hashes);
4. refuses to act if the two paths are provably the same file (hard link, symlinked folder, bind mount);
5. writes an `intent` record to the journal and fsyncs it;
6. acts with the safest primitive available: a no-clobber rename (`renameat2(RENAME_NOREPLACE)`), or an atomic `rename(2)` over the original for links and clones;
7. checks that the kept copy **still exists afterwards**. If removing one path made the other disappear, the two paths were secretly the same file (for example a share mounted twice), and the change is rolled back;
8. records `done`.

The rest of the system follows the same rules:

- Scan roots that overlap, directly or through symlinks and bind mounts, are refused.
- Plans are checked by an independent invariant pass: every group keeps at least one copy, no reference location is targeted, and no kept copy is also removed.
- Writes go to a temporary file plus a no-clobber rename. Copies are verified by re-reading the destination, with the page cache dropped where the OS allows. Existing files are never overwritten unless you explicitly ask.
- Only one apply, undo or purge runs at a time. Stale locks from crashed processes are reclaimed safely, and `duplio recover` explains any interrupted operation.

## Compare, merge and sync two folders

### Compare: `duplio diff A B`

Read-only. Every file ends up in exactly one category, seen from its own side:

| Category | Meaning |
|---|---|
| **identical** | the same content at the same relative path on the other side |
| **moved or renamed** | the same content exists on the other side, but under another name or in another folder |
| **different** | a file with the same path exists on the other side, with other content (the newer side is shown) |
| **only here** | nothing like it on the other side, at that path or anywhere |
| **unreadable** | could not be read; nothing is claimed about it |

Names never decide, so a photo that was renamed or filed into another folder is *moved*,
not missing. Paths are paired after Unicode normalisation, and case-insensitively when either
drive is case-insensitive.

`--basis` chooses how "the same" is judged:

| Basis | Judged by | Reads file contents? |
|---|---|---|
| `content` (default) | the staged engine: size → fingerprint → hash [→ bytes] | only files whose size matches a file on the other side |
| `time-size` | same path, size and modification time, within what both drives can store (`--time-tolerance`, `--ignore-dst` for the exact one-hour shift between FAT/NTFS and Unix) | only to find moves, `--no-moves` turns that off |
| `size` | same path and size | no |

A hard link between the two sides is recognised as the same file without reading it. For scripts:
`--list only-a|only-b|changed|moved|same [--print0]` prints exact relative paths, `--check` exits 1 when the
content differs (`--strict`: also when the layout does), and `--export report.{json,csv,html}` / `--json`
write the full result.

```bash
duplio diff ~/Photos smb://me@nas/photos
duplio diff /media/usb/backup ~/Documents --basis time-size --ignore-dst
duplio diff A B --list only-a --print0 | xargs -0 -I{} echo "missing in B: {}"
```

### Merge: `duplio merge SOURCE DEST`

Copies into `DEST` everything from `SOURCE` that `DEST` does not already have. The comparison is by
content, so a file that `DEST` already holds under another name is **not** copied again.

- **Identical files inside the source are copied once** (`--keep-dupes` copies all).
- **Nothing in `DEST` is overwritten silently.** A different file at the same path is handled by `--on-conflict`: `rename` (default: keep both; the new one becomes `name (from SOURCE 2026-10-12).ext`), `skip`, `newer` (take the newer; the old version goes to `.duplio-versions`) or `replace` (same, always).
- **Names the destination cannot store** (`: ? * " < > |`, trailing dots or spaces, reserved names such as `CON`, over-long names) are rewritten safely, skipped or refused: `--names auto|fix|skip|abort|keep`. Two names that fix to the same result both survive (`a_b`, `a_b (2)`).
- `--layout '{year}/{month}/{name}'` files by date and name (tokens: `rel dir name stem ext year month day date`).
- **Every copy is written to a temporary file, read back and verified, and renamed into place without clobbering.** The source is re-checked just before copying: a file edited since the comparison is skipped, not copied half-new.
- `--move` removes each source file **only after** its copy is verified, by moving it into the quarantine, the same reversible mechanism as `dedupe`. `--clean-source` additionally clears source files already present in `DEST` (verified by content).
- Several copies run in parallel on network drives; `--bwlimit 5MB/s` keeps the connection usable.
- One journal covers the run: `duplio undo` removes the copies (only while the source still holds the same content), puts replaced versions back and restores moved sources.

```bash
duplio merge ~/Pictures/Phone-dump smb://me@nas/photos --dry-run
duplio merge /media/usb/old-laptop ~/Documents --on-conflict newer
duplio merge adb:/sdcard/DCIM ~/Photos --layout '{year}/{month}/{name}' --move
```

### Two-way sync: `duplio sync A B`

Changes on either side are carried to the other. Sync keeps a small history of the last state in which the two
sides were identical, which is how it tells *new here* from *deleted there*:

| A | B | History | Result |
|---|---|---|---|
| present | absent | none | copied to B (new) |
| present | absent | in sync, A unchanged | **deleted on B** → A's copy is moved to the quarantine |
| present | absent | in sync, A edited | edit wins: copied back to B |
| changed | unchanged | any | A's version replaces B's (the old one is kept in `.duplio-versions`) |
| changed | changed differently | any | conflict: `--on-conflict keep-both` (default: the newer keeps the name, the other is kept next to it on both sides), `newer`, `a`, `b` or `skip` |

- **The first run never deletes anything** (there is no history yet).
- **Brakes**: a side that suddenly holds no files, a plan that would remove more than `--max-delete` (50 %) of a side, or a side that could not be fully read each stop the deletions (`--force` overrides the first two). This is the signature of an unmounted drive.
- Files are re-checked right before they are copied or moved aside; anything that changed is left for the next run. A failed file is retried next time and does not corrupt the history.
- `--basis content` also avoids re-copying files that were only touched. `--deletions restore|ignore` changes what a deletion means. `--reset` forgets the history.
- `duplio undo` reverses a whole sync, including conflict renames, and resets the pair's history (the next run is then a first run, which cannot delete).
- Sync does not offer age filters: a file near the cut-off could look "deleted" on a drive that rounds its times.

## Network drives

Mounted shares (`cifs`, `smbfs`, NFS, SSHFS, cloud FUSE mounts) and native `smb://` locations get the same treatment, and
Duplio checks what can go wrong with them before writing anything:

| Risk | What Duplio does |
|---|---|
| **The share is not mounted**, so a "backup" silently fills the local disk at the empty mount point | Destinations under `/mnt`, `/media`, `/run/media`, `/Volumes` must be a really mounted file system (`--no-expect-mounted` skips this; `--expect-mounted` forces it elsewhere) |
| **The wrong drive is mounted there** | `--sentinel` requires a marker file; create it once with `duplio sentinel create LOCATION` |
| **The server hangs** (dead NFS server, sleeping NAS) | the destination must answer within 15 s, otherwise the command stops with a clear message instead of freezing |
| **Not enough room** | the bytes to write plus a reserve are compared with the free space before the first byte is written |
| **A flaky connection** (stale NFS handle, dropped SMB session, timeout) | transient errors are retried with backoff and a reconnect; each retry starts the file over, so a partial copy is never kept. Permission and "file exists" errors are never retried |
| **Saturating the link** | `--bwlimit 5MB/s` caps copies, read-back verification and backup uploads |
| **File names the share cannot hold** | see `--names` above; Windows/SMB/exFAT rules are applied automatically |
| **Timestamps that do not match** (FAT: 2 s steps, SMB: whole seconds, DST shifts) | times are compared within what both drives can store; `--ignore-dst` forgives the exact one-hour offset |

`duplio probe LOCATION` finds out how a drive really behaves by writing a few tiny files to a temporary folder (removed
afterwards): case sensitivity, Unicode folding, timestamp resolution, rejected characters, the longest name, hard-link
support, latency and read/write speed, followed by plain recommendations.

## Backups

### Snapshots: `duplio backup …`

These are content-addressed, deduplicated and versioned, and the repository can
live on any writable location: a USB drive, another disk, or `smb://` storage.

- **Deduplicated** across files, snapshots and machines: a photo backed up from the phone and from the laptop is stored once. Data is split into fixed 4 MiB chunks, so in-place edits of large files are deduplicated too.
- **Incremental**: files whose size and time match the previous snapshot are not re-read.
- **Compressed** with zlib, except formats that are already compressed (JPEG, video, archives…).
- **Self-verifying**: every chunk is re-hashed whenever it is read. `backup check --read-data [--subset 0.1]` detects bit rot.
- **Crash-safe**: a snapshot only exists once all its data is durable.
- **Retention**: `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 [--keep-within 14d] --apply --prune`. Forget is a dry run until you pass `--apply`. An empty policy is refused, and the newest snapshot of each series is always kept.
- **Restore** a whole snapshot or `--include` globs. Restore never overwrites a different existing file unless you ask; `--conflict rename` writes the restored file next to it.
- **Portable**: BLAKE2b ids come from the standard library, so restoring never depends on optional packages.

```bash
duplio backup init /mnt/usb/duplio
duplio backup run adb:/sdcard /mnt/usb/duplio --label phone
duplio backup snapshots /mnt/usb/duplio
duplio backup diff /mnt/usb/duplio 2026 latest
duplio backup restore /mnt/usb/duplio latest ~/Restored --include "DCIM/**"
duplio backup find /mnt/usb/duplio '*tax*2023*.pdf'     # in every snapshot, even if deleted since
duplio backup history /mnt/usb/duplio Documents/report.docx
duplio backup export /mnt/usb/duplio latest ~/phone.zip   # also .tar, .tar.gz, .tar.xz
```

**Encrypted repositories.** `duplio backup init REPO --encrypt` encrypts everything before it is stored (needs
`pip install "duplio[encrypt]"`). Chunks, file lists and snapshot summaries are sealed with AES-256-GCM, bound to their
own id so objects cannot be swapped; object ids are keyed (HMAC), so nobody holding the repository can test whether a
known file is in it; and the password only unlocks a random master key (scrypt), so `duplio backup passwd` changes it
instantly without re-encrypting. The password comes from a prompt, `--password-file`, or `DUPLIO_REPO_PASSWORD`
(`DUPLIO_REPO_PASSWORD_FILE`) for scheduled runs. **If the password is lost the data cannot be recovered.** Tampering is
reported by `backup check --read-data`.

### Snapshots as plain folders: `duplio snapshot`

Time-Machine / rsnapshot style. Each snapshot is an ordinary folder with the full tree; **unchanged files are hard links**
to the previous snapshot, so they take no extra space, yet every snapshot opens in any file manager and any one can be
deleted without touching the others.

```bash
duplio snapshot create ~/Documents /media/usb/snapshots --label documents
duplio snapshot list /media/usb/snapshots
duplio snapshot forget /media/usb/snapshots --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --apply
```

A snapshot is built in a hidden folder and renamed into place only when complete; files are made read-only (links share
data, so editing one would change them all); hard-link support is *tested* on the destination, not assumed. Restoring is
copying a folder (or `duplio merge SNAPSHOT DEST`). Use `duplio backup` instead on drives without hard links (SMB, exFAT).

### Mirror: `duplio mirror SOURCE DEST`

Makes `DEST` an exact, verified copy of `SOURCE`.

- Changed files and (with `--delete`) files gone from the source are moved to `DEST/.duplio-versions/<time>/`, never deleted.
- Renames in the source are replayed as moves instead of re-copies.
- A **mass-change brake** refuses to run when the source looks empty or more than half the destination would change. A locked phone shows an empty storage over MTP, and the brake catches that.
- `--keep-versions 90d` prunes old versions.

### Phone import: `duplio ingest SOURCE LIBRARY`

- Streams each new photo or video **once**, hashing while copying, and verifies the copy.
- Files it into `{year}/{year}-{month}/{name}`, or any layout you choose: `{date}`, `{kind}`, `{device}`, `{stem}`, `{ext}`…
- Takes the capture date from EXIF, from the MP4/MOV `mvhd` atom (no ffmpeg), from camera and messenger file names (`IMG_20240131_…`, `PXL_…`, `Screenshot_2024-…`, `IMG-20240131-WA0007`…), or from the modification time, in that order.
- Skips anything already in the library, and anything imported before, even if you deleted it from the library since. `--reimport-deleted` turns that off.
- `--free-source` then deletes from the phone only files whose content was verified in the library *during this run*.

## Integrity: manifests and old versions

`duplio manifest create FOLDER -o archive.json` records the size, time and hash of every file. Later,
`duplio manifest verify FOLDER archive.json` reads everything again and tells silent corruption from honest changes:

| Result | Meaning |
|---|---|
| `corrupt` | same size **and** same modification time, different content: bit rot, a failing disk, a bad copy |
| `modified` | the content changed: someone edited it |
| `touched` | only the modification time changed |
| `missing` / `new` | in the manifest but gone / in the folder but not in the manifest |

`--quick` trusts size and time. Exit status 1 means something is missing, damaged or edited. Keep the manifest
somewhere you trust more than the archive.

Replaced files are kept in `.duplio-versions/<run>/` by mirror, merge and sync. `duplio versions list LOCATION` shows
them and `duplio versions prune LOCATION --older-than 90d --keep-last 3 --apply` deletes old runs. It is the only place
those versions are ever destroyed, it is a dry run unless `--apply` is given, and folders it does not recognise are never touched.

## Profiles, schedules, notifications and watch

```bash
duplio profile add phone --type ingest --set source=adb:/sdcard/DCIM --set dest=~/Pictures/Library
duplio profile add docs  --type backup --set source=~/Documents --set repo=smb://me@nas/backup \
                         --set keep_daily=7 --set keep_monthly=12
duplio run phone
duplio schedule docs --every daily          # prints systemd timer / launchd plist / schtasks / cron
```

Profile types: `dedupe`, `compare`, `backup`, `mirror`, `ingest`, `merge`, `sync`, `snapshot`. Any profile may also set
`pre`, `post` and `on_failure` (lists of shell commands), `notify` and `notify_on`, `bwlimit`, `sentinel` and `expect_mounted`.

```toml
[profiles.nas-backup]
type = "mirror"
source = "~/Documents"
dest = "/mnt/nas/documents"
pre = ["mount /mnt/nas"]                  # runs first; if it fails the job is not run (exit 4)
post = ["umount /mnt/nas"]                # always runs afterwards, with DUPLIO_STATUS and DUPLIO_EXIT_CODE set
notify = ["email:me@example.com", "webhook:https://ntfy.sh/my-topic"]
notify_on = "problems"                    # failure (default) | problems | always | never
bwlimit = "10MB/s"
sentinel = ".duplio-sentinel"

[notify.smtp]                             # only needed for e-mail
host = "mail.example.com"
user = "bot"
password_env = "DUPLIO_SMTP_PASSWORD"    # the password itself is never stored in the file
```

Scheduled runs are cautious:

- A source that is not connected (for example the phone is unplugged) is skipped with exit code 3, not treated as a failure, and does not notify. The `pre` command runs first, so it can be the thing that mounts the drive.
- `duplio run` exits 0 done · 1 done with problems · 2 failed · 3 skipped · 4 a `pre` command failed.
- A scheduled dedupe only ever quarantines, which can be undone.

**Notifications.** Targets are `webhook:URL` (a JSON POST that works with Slack, Discord, ntfy and Home Assistant),
`command:PROGRAM` (the message arrives in `DUPLIO_STATUS`, `DUPLIO_TITLE` and `DUPLIO_MESSAGE`, never spliced into the
command line) and `email:ADDRESS`. A notification that cannot be delivered is reported as a warning and never fails the job;
webhook addresses (which often contain a token) are never printed. Test a target with `duplio notify TARGET`.

**Watch.** `duplio watch SOURCE DEST --action mirror|merge|sync` polls for changes (so it works on SMB shares, NFS and
phones, where change notifications are unreliable), waits until the folder has been quiet for `--settle` seconds so a file
that is still being written is not copied half-finished, and then runs the job once.

## Filters

`--type images|video|audio|documents|archives|code|.ext` (repeatable) limits scan, dedupe, diff, merge and sync to a kind
of file; `--newer-than 30d` and `--older-than 1y` limit all but sync by age. They combine with `-i/-x` globs and size limits.


## Command reference

| Area | Commands |
|---|---|
| Duplicates | `scan`, `dedupe`, `compare`, `similar`, `plan`, `apply` |
| Safety net | `undo`, `history`, `quarantine list/purge`, `recover`, `sessions` |
| Two folders | `diff`, `merge`, `sync` |
| Network drives | `probe`, `sentinel create/check`; `--bwlimit`, `--expect-mounted`, `--sentinel` on every command that writes |
| Backups | `backup init/run/snapshots/ls/restore/check/forget/prune/diff/stats/unlock/find/history/export/passwd`, `snapshot create/list/forget`, `mirror`, `ingest` |
| Integrity | `manifest create/verify`, `versions list/prune` |
| Automation | `profile list/add/remove`, `run`, `schedule`, `notify`, `watch` |
| Setup | `ui`, `devices`, `doctor`, `rules`, `config [init]`, `cache stats/clear/prune` |

Every command has `--help` with examples. `scan` and `compare` accept `--json` and `--export report.{html,csv,json}`.

## Configuration

`duplio config init` writes a commented `config.toml`. It lives in `~/.config/duplio/` on Linux, `~/Library/Application Support/Duplio/` on macOS and `%APPDATA%\Duplio\` on Windows. `DUPLIO_HOME` overrides all locations.

```toml
[general]
action = "quarantine"
keep = ["has-hardlinks", "not-copy-named", "root-order", "oldest", "shortest-path"]
verify = "hash"       # or "bytes"
cache = true

[scan]
exclude = ["*.tmp"]
protect = ["**/Lightroom/**"]
skip_hidden = false
```

## Project layout

```
src/duplio/
  vfs/          storage backends: base contract, local (+ mount classification), SMB, ADB, locations,
                destination guards, retry, drive probe
  hashing/      algorithm registry, streaming & sampled hashing, SQLite digest cache
  scan/         filters (exclude / include / protect), roots, inventory, overlap detection
  compare/      staged engine, device-aware scheduler, similar images, result model, sessions, folder diff
  plan/         keep rules, planner + invariants, plan files, journal, executor, undo/quarantine
  backup/       snapshot repository (+ encryption), retention, mirror, phone ingest, media dates,
                merge, sync (+ history), hard-link snapshots, manifests, version housekeeping
  devices/      phone / share / drive discovery
  reporting/    JSON / CSV / HTML exports
  cli/          click commands and Rich rendering
  tui/          Textual application
  transfer.py   the single verified-copy primitive (retry + bandwidth limit)
  notify.py     webhook / command / e-mail notifications
  watch.py      polling change detection
  config.py     TOML settings and profiles
  schedule.py   systemd / launchd / Task Scheduler / cron generation
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the parts fit together and how to add a backend, [docs/SAFETY.md](docs/SAFETY.md) for the full list of safety guarantees and how each is tested, and
[docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md) for how current tools (Czkawka, restic, GVFS/jmtpfs, Textual) shaped the design.

## Development

```bash
pip install -e ".[all,dev]"
pytest              # 390+ tests: engine, safety, odd file names, backends (fake ADB/SMB), backups, merge, sync, CLI, TUI
ruff check src tests && mypy src
```

The icon (`assets/duplio-icon-original.png`, with resized copies and a multi-size
`duplio.ico`) shows a blue and a red featureless figure pulling apart, still joined by a
stretching web. The terminal logo (`duplio.assets.logo`) is the same picture sampled into
colour half-block characters.

## License

MIT
