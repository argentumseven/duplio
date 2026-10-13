<p align="center">
  <img src="assets/duplio-icon-256.png" width="160" alt="Duplio icon: a blue and a red featureless figure pulling apart, joined by a stretching web">
</p>

<h1 align="center">Duplio</h1>
<p align="center"><em>Duplicates in. One survivor out.</em></p>

Duplio finds identical files across **local folders, USB drives, phones (MTP, PTP, iOS AFC, or
Android over ADB) and network drives (SMB, SFTP, WebDAV, FTP)**, helps you decide which copy survives, and cleans
up in a way you can always undo. It also **compares two folders by content** (a renamed
photo is not a difference), **merges** whatever one lacks into the other, **syncs** in both
directions, and does **deduplicated, encrypted, versioned backups**, hard-link **snapshots**,
**verified mirrors** and **phone photo imports** that skip what you already have. Every one of
them works on local folders, mounted network drives, native network connections and phones alike. On a slow
drive it fetches only the small preview built into each photo, once, so reviewing pictures does not wait on the link.

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

- [Install](#install) (or just double-click a launcher)
- [Quick start](#quick-start)
- [The window](#the-window)
- [The text screen](#the-text-screen)
- [Phones, cameras and network shares](#phones-cameras-and-network-shares)
- [Pictures on slow drives](#pictures-on-slow-drives)
- [How duplicates are detected](#how-duplicates-are-detected)
- [Performance](#performance)
- [What counts as "the same file"](#what-counts-as-the-same-file)
- [How much file attributes are trusted](#how-much-file-attributes-are-trusted)
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

### The easy way: double-click, nothing to install

1. Download Duplio (**Code → Download ZIP** on GitHub) and unpack it. Right-click → *Extract All* on Windows.
2. Double-click the launcher for your computer. [`START-HERE.txt`](START-HERE.txt) says the same in three short steps.

| Computer | Launcher | Notes |
|---|---|---|
| Windows | `Duplio.bat` | |
| Mac | `Duplio.command` | the first time, right-click → *Open* (macOS asks once because the file came from the internet) |
| Linux, or any terminal | `./duplio.sh` | |

The launcher runs Duplio straight from its folder. **Nothing is installed, compiled or downloaded**: the libraries Duplio
needs are bundled in [`vendor/`](vendor/README.md) (all pure Python, so the same files work everywhere). The one thing the
computer must have is Python 3.11 or newer, which is free; if it is missing the launcher says how to get it (on Windows
it offers to install it through the built-in `winget`, after asking). With no arguments the launcher opens the window
(see [The window](#the-window)); with arguments it runs that command: `./duplio.sh scan ~/Pictures`.

What works with nothing else installed: every command, the window and the text screen, duplicate finding, compare/merge/sync, plain
backups, picture previews (Duplio decodes the thumbnails itself), and network drives over SFTP, WebDAV and FTP.
What needs an optional package, because it contains compiled code that cannot be bundled for every computer:
similar-image search (Pillow), encrypted backups (cryptography), native `smb://` (smbprotocol; a mounted share works
without it), faster hashing (blake3, xxhash) and content-defined backup chunking (numpy). `duplio doctor` lists them, and a
feature that needs one says so instead of failing.

> The Linux launcher is exercised by the automated tests, including on a Python that has no packages at all. The Windows
> `Duplio.bat` and macOS `Duplio.command` follow the same steps but have not been run on those systems by their author.

### With pip

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
| `cdc`    | content-defined chunking for backups   | numpy                  |

Run `duplio doctor` to see what is installed, which phone tools (`adb`, `jmtpfs`,
`gio`) are available, and how fast each hash runs on your machine.

## Quick start

```bash
duplio                                             # the window: two folders side by side, with pictures
duplio ui                                          # the same things as a text screen, for a terminal

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
duplio scan sftp://me@nas.local/photos davs://cloud.example.com/remote.php/dav/files/me/Photos
duplio drives                                      # which of my drives are slow, virtual, cloud-only?
duplio preview "T:\Photos\IMG_0042.jpg"            # a picture in the terminal, fetched cheaply
```

A typical clean-up has three steps, and you can stop after any of them:

1. **Scan** (`duplio scan`): read-only. Every scan is saved as a session.
2. **Plan** (`duplio plan [SESSION]`): decide what to keep and remove. The plan is saved as a JSON file you can read or edit.
3. **Apply** (`duplio apply PLAN`): carry out the plan, re-checking every file at the last moment.

`duplio dedupe` does all three in one go and asks for confirmation before it changes anything.

## The window

`duplio` (or a double-click on the launcher) opens a window. **Every screen puts two folders next to each other:
folder A on the left, folder B on the right, one row per file, with the real thumbnail of each picture.** Folder A is
blue and folder B is red everywhere, the colours of the two figures in the icon, so a glance at a picture's border says
which folder it is in. The look is the text screen's, in a window: one monospace typeface throughout, near-black background, red
screen titles, amber headings, flat buttons with a thin rule above and below (red for the main action, green for a safe copy,
bright red for anything that removes), a numbered list of screens on the left, and a bar of keys along the bottom
(**Ctrl+1** to **Ctrl+8** jump to a screen, **Ctrl+Q** quits). *Settings → Colours* switches to a light version for the next
start. Button labels are short and toolbars wrap onto a second line in a narrow window, so no text is cut off; a test
checks every screen at three window widths. Every list can be put in the order you want (name, date, size, type or
folder, ascending or descending; duplicates also by number of copies), and the choice is remembered. Each folder box takes a typed path, a recent folder, **Choose folder…**, or **Phone or server…**
(a plugged-in phone, a drive, an SMB share, SFTP, WebDAV or FTP). The **⇄** button swaps A and B.

| Screen | What A and B are | What you see | What you can do |
|---|---|---|---|
| **Browse a folder** | one folder, on any drive | a wall of thumbnails or a list; folders to enter, Up and Back, a filter | open a file, copy its address; sort by name, date, size or type ([Browsing a folder](#browsing-a-folder-and-the-picture-buffer)) |
| **Compare two folders** | any two folders | every file of A with what B has for it: identical, renamed or moved, different content, only in A, only in B | copy the missing files A → B or B → A (all of them, or just the pictures you clicked) |
| **Is it backed up?** | A: what you want to protect. B: the backup | a verdict ("Everything in A is safe in B" or "12 of 340 files in A are NOT in B"), and the files that are missing | copy what is missing into the backup |
| **Find duplicates** | A alone, or A and B (B can be a *reference* that is never changed) | every set of identical files as a row of pictures, each marked KEEP, REMOVE or LOCKED | click a picture to change its mark, choose a rule ("keep the oldest"…), move the REMOVE copies to the quarantine, undo |
| **Keep in sync** | the two sides of a two-way sync | what a sync would do, with arrows; a file changed on both sides is kept as two versions | preview, then **Sync now** |
| **Import from phone** | A: a phone or card. B: your library | what is new on the phone and what the library already has (even under another name) | import only the new files, sorted into folders by date |
| **History & undo** | | every change Duplio has made | undo one, or free the space by emptying its quarantine |
| **Settings** | | colours (dark or light), picture buffer and memory, hidden files, patterns Duplio must never touch | |

Looking never changes anything. A comparison reads both folders; only a button that says what it will do (**Copy 5 files**,
**Remove 3 copies**, **Sync now**, **Import new files**) changes files, and it first shows the full list of
what will happen and asks. The rules are the ones the commands follow ([SAFETY.md](docs/SAFETY.md)): a copy never replaces a
file (a clash is kept under a new name), every copy is read back and compared, removed files go to a quarantine and not into
the void, and *History & undo* reverses a whole action.

**Pictures.** The thumbnail is the small preview already inside each photo (or Duplio's own decoder of it), so a phone or a
NAS is not made to send whole photos. Pictures from slow drives are remembered while the window is open (or longer, in
*Settings*); a file that exists only in the cloud shows a cloud symbol and is not downloaded unless you right-click it and
ask. Double-click a picture to open the file; right-click for *Show in its folder*, *Copy its address* and more. Pillow
makes the thumbnails faster when it is installed, but is not required.

**What the window needs.** Tk, which comes with Python on Windows and macOS (python.org's installer) and is a separate
package on most Linux systems: `sudo apt install python3-tk` (Debian, Ubuntu), `sudo dnf install python3-tkinter`
(Fedora). The launchers pick a Python that has Tk when the computer has several. On a computer without Tk, or without a
screen, `duplio` says why in one line and opens the text screen instead; `duplio gui` says what is missing and exits.

> The window was exercised on Linux under a virtual screen (Xvfb), with automated tests that drive every screen against
> real folders: comparing, copying both ways, the backup verdict, duplicates with removal and undo, sync, import, history.
> It has **not** been run on Windows or macOS by its author. Tk looks and behaves slightly differently there (fonts, the
> folder chooser, high-resolution scaling); if something looks wrong, `duplio ui` (text) and every command still work.

## The text screen

`duplio ui` opens a keyboard- and mouse-driven interface in the terminal (no pictures except through the
picture pane below; the window above is the one that shows two folders against each other):

- **Home** has eight workflows and a live list of connected phones, shares and drives.
- **Find duplicates**: add locations, either typed in, picked from the detected devices, or chosen with the folder browser (**Browse…**, or `Ctrl+O` in any folder box; it walks local folders, USB drives, phones and network servers, and remembers a remote folder's listing for two minutes so going up and down a NAS does not ask it again; `F5` asks again). Mark any location as *reference* (used for matching, never changed). Then choose the mode and verification level.
- **Review**: duplicate groups on the left, sorted by wasted space, and their copies on the right: ★ keep, ✗ remove, 🔒 locked (reference, protected or read-only).
  - `space` toggles keep/remove. You can never unmark the last kept copy.
  - `k` keeps only the highlighted copy.
  - `x` skips the whole group.
  - `r` re-applies the keep rules.
  - `p` shows the plan.

  The action can be switched (quarantine, trash, delete, hardlink, reflink) and the summary updates live.
  A **picture pane** shows the highlighted copy. It is fetched once per group from the cheapest copy, the next groups are fetched in the background while you read, and nothing stored only in the cloud is downloaded until you press `f` ([details](#pictures-on-slow-drives)). `o` opens it in your image viewer.
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
| **SFTP** (SSH) | `sftp://user@host[:port]/path` | Uses the `ssh` program that comes with Windows 10+, macOS and Linux: your keys, ssh-agent, `~/.ssh/config` and `known_hosts` apply. Nothing to install. |
| **WebDAV** (Nextcloud, ownCloud, Synology/QNAP, Apache/nginx) | `davs://user@host/path` (or `https://…`); `dav://` for plain http | Standard library only. Basic and Digest login. |
| **FTP / FTPS** | `ftps://user@host/path` (encrypted); `ftp://…` is **not encrypted** | Standard library only. The server must be able to report file times (MLSD or MDTM). |
| Cloud drives (Tresorit, OneDrive, iCloud, Dropbox, Google Drive) | their drive letter or folder | Recognised as virtual; pictures are cached and nothing is downloaded unasked. See [Pictures on slow drives](#pictures-on-slow-drives). |
| USB sticks, SD cards | their mount point | FAT/exFAT get 2-second timestamp tolerance and no hard links. Reading is treated as slow, so pictures are cached. |

On **macOS** Android phones are not mounted. Use ADB, or an MTP FUSE tool. On
**Windows** phones are only visible in Explorer, not as drive letters, so use ADB.
Duplio refuses to scan the same phone through ADB and MTP at once, because each
file would look like its own duplicate.

## Pictures on slow drives

Reviewing duplicates means looking at pictures, and on a USB stick, a NAS, a phone or a cloud drive that is exactly the
slow part: a 6 MB photo has to cross the link just to draw a thumbnail. Duplio asks the cheapest question first:

1. **Seen already?** Every picture it has fetched is kept, so going back and forth costs nothing.
2. **The photo's own thumbnail.** Cameras and phones store a small JPEG inside each photo (in its EXIF block), and RAW files
   (DNG, CR2, NEF, ARW and the like) carry larger previews. Duplio reads only the first ~64 KiB of the file (a *ranged*
   read, a few kilobytes on the wire) and takes that.
3. **A small file, read whole**: up to 1 MiB on a slow drive.
4. **Only when you ask**: a larger picture with no preview inside is not downloaded unless you press `f` in the review
   screen (or pass `--download`). Until then the pane says why there is no picture.
5. **Identical copies are one picture.** For a group of identical files, the picture comes from the cheapest copy: one
   already cached, then one that is not cloud-only, then the faster drive, then the smaller file.

Which drives count as slow? `duplio drives` shows how every drive was recognised. In the default `auto` mode this is
network and SMB/SFTP/WebDAV/FTP drives, phones and cameras, USB sticks and spinning disks, and cloud or virtual drives
(Windows placeholder files, macOS "dataless" files, OneDrive/iCloud/Dropbox/Google Drive/Tresorit-style drives). An
internal SSD is read directly.

**Cloud-only files are never fetched unasked.** A file that exists only in the cloud (the drive lists it but has not
downloaded it) is detected from its file attributes, and previewing it would download it. Duplio waits: `f` in the review
screen, `--online-only`, or `duplio previews settings --online-only` allow it.

**What is kept, and for how long.** By default pictures are kept only until Duplio closes, in a private temporary folder
that is deleted on exit. That is deliberate: a zero-knowledge drive such as Tresorit keeps your files encrypted so that
only your devices see them, and a thumbnail cache on disk would be a plain-text copy of part of them. If you would rather
keep the pictures between runs, switch it on knowingly:

```bash
duplio previews settings --keep persistent --max-size 2GB   # stored UNENCRYPTED in Duplio's cache folder
duplio previews stats                                       # how big is it?
duplio previews clear                                       # delete every saved picture
duplio previews warm sftp://me@nas/photos/trip              # fetch a folder's pictures in advance (or overnight)
```

The saved index holds no file names (entries are looked up by a hash of the drive, path, size and modification time), and a
file that changes gets a new key, so an edited photo is never shown as its old self.

### Browsing a folder, and the picture buffer

**Browse a folder** in the window is a plain folder viewer for any drive, with the picture handling above behind it: a wall
of thumbnails (or a list), folders to go into, Up and Back, a filter box and sorting by name, date, size or type. Open it on
a Tresorit, NAS, phone or SFTP folder and move around freely:

- The folder is read in one trip (a WebDAV PROPFIND, an FTP MLSD, an SFTP readdir carry every size and time), and listings
  of slow drives are remembered for five minutes, so Back and Up are instant. *Refresh* asks the drive again.
- Its pictures are fetched in the background into a buffer on disk, **nearest the part you are looking at first**, ahead of
  your scrolling. A folder of three thousand photos fills in a few seconds from a local disk; from a remote one it takes as
  long as the drive needs, and each picture costs the few kilobytes of its embedded preview, once.
- What is on screen is shown from the buffer. If memory runs short the pictures held in memory shrink (never below 48 MB),
  and any that were let go are made again from the buffer, **never fetched from the drive again**.
- **Failsafes.** The buffer has a size limit (Settings, 1 GB by default, roughly 1 to 4 MB per hundred pictures) and the picture
  memory has its own (Automatic: an eighth of the computer's memory, between 192 MB and 1 GB). When the buffer is full,
  fetching **stops and says so** instead of evicting pictures of the folder you are looking at only to fetch them again; the
  pictures of the open folder are also the last the buffer lets go. Fetching also stops, saying why, when the disk the buffer
  is on is almost full (under 1 GB free) or when the drive stops answering (25 reads in a row fail). The queues are bounded,
  so scrolling through a huge folder cannot pile up work. A full disk or damaged cache index never loses a picture that was
  just fetched or stops the window: the index is simply started again.
- Cloud-only files are still not downloaded unless you tick *Also fetch pictures of cloud-only files*. Some cloud drives
  download the whole file even for a small read; Duplio reads each file once and keeps the picture, so it happens once.
- To keep the pictures **between runs** (so opening the same folder tomorrow costs nothing), choose *between runs* under
  *Remember pictures* in Settings. They are then stored unencrypted in Duplio's cache folder, which is why it is off by default.

**A picture is only a picture.** Previews are for your eyes. They are never used to decide that files are identical, which
files to remove, or whether a copy is safe: those decisions always come from re-reading the real files (see
[docs/SAFETY.md](docs/SAFETY.md)).

**Similar-image search** (`--similar`) is the other thing that reads whole photos. On slow drives it fingerprints each photo
from the same small embedded thumbnail instead, a few kilobytes per photo. Photos with no small preview inside are
skipped with a note saying how many; `--similar-full-read` reads them whole, as it used to.

**Pictures without Pillow.** The terminal picture pane works with nothing installed: Duplio carries its own small JPEG and
PNG decoders for thumbnails. (Pillow is used when present, and for similar images.)

**A Tresorit drive.** Tresorit describes its drive as a virtual drive that exists while its app runs, lists files without
storing them locally and decrypts them as they are opened. Whether Windows reports it to programs as a network (SMB or
WebDAV) drive is not something public documentation settles, so Duplio does not guess: run `duplio drives T:\` (or the
folder path on macOS) and it prints the drive type, the file-system name and, for a mapped network drive, the server your
computer says it is connected to, and how it decided the drive is slow. Whatever the answer, the drive is treated as
virtual: pictures are cached, and nothing is downloaded unless you say so.

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

**Cache.** Digests are cached in SQLite, keyed by path, size and modification time, so a rescan of an unchanged
archive takes seconds. Files modified in the last few seconds are not cached, because their timestamps cannot yet be
trusted. A remembered digest cannot notice content that changed **without its size or time changing** (bit rot, a
failing disk, a tool that restores timestamps), so the cache is only allowed to *propose*:

- `scan`, `dedupe` and `similar` use it to find candidates; before anything is changed, the executor re-reads both files.
- Questions whose answer is "this is already safe somewhere else" read fresh: `compare`, `diff`, `merge`, `sync`,
  `mirror` and `ingest`, in the CLI and the TUI. A backup copy that rotted since the last scan is reported as such, not
  believed. `--use-cache` opts back in for speed, where you accept that blind spot.

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

At larger scale (300,000 files, 4.3 GB, on a 4-core virtual machine with a local disk): `scan` took 62 s using 553 MiB; `diff` of two such
trees 105 s using 1.1 GiB; an unchanged `backup run` 19 s; the *first* backup 8 minutes (about 620 files/s, limited by flushing each small
chunk file to disk). The full table, what it does and does not show, and `python benchmarks/scale.py` to repeat it are in
[docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md#a-second-look-at-repositories-after-an-independent-review).

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

## How much file attributes are trusted

Modification time, size, permissions and inode numbers are easy to get wrong (clocks drift, drives round times to 1–2 s,
tools restore old timestamps, bit rot changes data and leaves the metadata alone, network file systems reuse inode numbers).
Duplio therefore never lets them decide anything that could cost data:

| Decision | Decided by | What the attributes do |
|---|---|---|
| Is this file a duplicate / already in the destination / backed up? (`scan`, `dedupe`, `compare`, `diff`, `merge`, `sync`, `ingest`) | **content** (size is only a prefilter: different size means different content) | nothing; digests are read fresh, never taken from the cache, for "safe elsewhere" answers |
| May this file be removed or replaced? (`dedupe`, `merge --move`, `--clean-source`, sync deletions) | **content, re-read right before the action** (hash, or byte-for-byte) | a changed size or time only makes Duplio skip the file |
| Are these two hard links the same file? | inode and device, only on file systems where those are stable (not FAT, SMB, MTP, most network mounts) | |
| Which copy is "newer" in a conflict | modification time, but a time in the future is never believed and a tie keeps both versions | a wrong clock cannot make a file win |
| Which copy to keep among *identical* files, or the `{year}` in a merge layout | modification time (`oldest`, `newest`, `--layout`); photo imports read EXIF, then the file name, before the file time | only a preference; nothing is lost if it is wrong |

Four tools must skip unchanged files cheaply, so they do trust size and time (like rsync, restic and FreeFileSync): the
incremental `backup`, the default `mirror`, `snapshot` hard-link reuse and `sync` change detection. They add the "racy
timestamp" rule: a file modified within a few seconds of the moment it was recorded (or within two timestamp steps of the
drive, whichever is longer) is compared by content, because an edit in the same step keeps the same size and time. Times are
compared within what both drives can store, and `--ignore-dst` forgives an exact one-hour shift.

`backup` and `sync` also compare the file's **change time and inode** wherever the file system keeps them reliably (local
POSIX file systems; not SMB, MTP or most network mounts). Users and tools can set the modification time but not the change time, so
an edit that restored the old size and modification time (`touch -r`, `rsync -t`, a restore tool, a deliberate disguise) is still
noticed, as is a file replaced by another with the same size and time. Borg's default works the same way. A permission change
moves the change time too; in a backup that costs one re-read (the chunks are already stored), in a sync one comparison and
no copy. `--ignore-ctime` turns this off for jobs that touch attributes in bulk.

What a size-and-time tool cannot see, where those are unavailable, is a change that kept both on a file that had long settled
(silent corruption, or a tool that restores timestamps). For that, ask for content: `backup run --rehash`, `mirror --checksum`, `snapshot create
--verify-links`, `sync --basis content` (which avoids needless copies; it does not make sync read everything), and the
read-everything checkers `manifest verify` and `backup check --read-data`.

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
- Only one apply, undo or purge runs at a time. Stale locks from crashed processes are reclaimed safely.
- Every journal entry names the files the step will create. After a crash or power loss, `duplio recover` settles each interrupted step from what is actually on disk (finished, not started, or rolled back by putting a file back; nothing is deleted), and `duplio undo` does the same before it reverses a run.
- A kept copy is verified again for every irreversible step; a verdict from earlier in the run is reused only for undoable steps, and only while the file's size, times and identity are unchanged.
- It is tested by killing it: `tests/test_crash_sweep.py` stops each operation at every durable step (and makes each step fail with an I/O error) and checks that nothing is lost and that a rerun or `undo` puts things right. See [docs/SAFETY.md](docs/SAFETY.md#interrupted-anywhere).

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

Duplio talks to these directly, with no mount needed, and every command that takes a folder accepts them:

| Kind | Address | Needs | Notes |
|---|---|---|---|
| SMB | `smb://[domain;]user@server/share/path`, `\\server\share\path` | `pip install duplio[smb]` | Or mount the share and use the mount point. Password: `DUPLIO_SMB_PASSWORD`, the keyring, or a prompt. |
| **SFTP** | `sftp://user@host[:port]/path` | the `ssh` program (built into Windows 10+, macOS, Linux) | Login works as it does for `ssh`: keys and ssh-agent, `~/.ssh/config`, jump hosts. Password (`DUPLIO_SFTP_PASSWORD`, keyring or prompt) is handed to ssh through a private helper file that is removed afterwards; not available on Windows, where a key or the agent is used. The first time a server is seen its key is trusted and remembered in `known_hosts`; a key that later **changes** is refused with an explanation. For stricter handling set `DUPLIO_SSH_OPTIONS="-o StrictHostKeyChecking=yes"` (it also takes `-i key`, `-J jump`…). |
| **WebDAV** | `davs://user@host[:port]/path`, `https://…` (`dav://`, `http://` are not encrypted) | nothing | Nextcloud, ownCloud, Synology and QNAP WebDAV, Apache and nginx DAV. Basic and Digest login; `DUPLIO_DAV_PASSWORD`, the keyring or a prompt. WebDAV cannot set a file's modification time, so copies there carry their upload time (content, not time, decides what is identical). |
| **FTP / FTPS** | `ftps://user@host[:port]/path` (explicit TLS), `ftp://…` | nothing | `ftp://` sends the password and every file **unencrypted**; the interface and `duplio drives` say so. The server must report file times (MLSD, or MDTM); one that cannot is refused rather than guessed at. Implicit FTPS (port 990) is not supported. `DUPLIO_FTP_PASSWORD`, the keyring or a prompt. |
| Android | `adb:/sdcard/DCIM` | `adb` | Hashes are computed on the phone. |

Paths are taken from the top of what the server shows (`sftp://host/home/me/photos`). Passwords are never written to
disk. On all three new kinds, a file is written under a hidden temporary name, its size is checked, and only then is it
moved into place, without replacing anything unless asked; only single files are ever deleted (never a folder, which on
WebDAV would take everything inside with it); and every copy is read back and compared before it counts. They are tested
against real servers (WsgiDAV, pyftpdlib, and paramiko's SFTP server behind a stand-in for `ssh`), not against every
vendor's: a server with unusual behaviour may need `duplio probe` first.

Mounted shares (`cifs`, `smbfs`, NFS, SSHFS, cloud FUSE mounts) and all of the above get the same treatment, and
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

**Scope.** Duplio's core is finding duplicates and comparing, merging and syncing folders safely; those paths get the most
scrutiny. The backup features exist so that the same tool can keep a verified, versioned copy of the folders, phones and
shares it already understands. They are deliberately simpler than a dedicated system such as restic, Borg or Kopia: no
symbolic links, ownership or extended attributes are stored, repositories are single-writer, and the numbers under
[Performance](#performance) and in [docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md) say how far they have been measured. For
a fleet of machines, or data where those properties matter, use one of those tools; for a personal archive, a phone or a
NAS share, this is built to be safe first.

### Snapshots: `duplio backup …`

These are content-addressed, deduplicated and versioned, and the repository can
live on any writable location: a USB drive, another disk, or `smb://` storage.

- **Deduplicated** across files, snapshots and machines: a photo backed up from the phone and from the laptop is stored once. Data is split into fixed 4 MiB chunks by default, so in-place edits of large files are deduplicated too. `backup init --chunker cdc` (needs `pip install "duplio[cdc]"`) cuts chunks where the content says instead, so data *inserted* into a big file (a database, a disk image, a document that grows at the top) is shared as well; it is slower, see [docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md).
- **Incremental**: files whose size, times and inode match the previous snapshot are not re-read.
- **Compressed** with zlib, except formats that are already compressed (JPEG, video, archives…).
- **Self-verifying**: every chunk is re-hashed whenever it is read. `backup check --read-data [--subset 0.1]` detects bit rot.
- **Crash-safe**: a snapshot only exists once all its data is durable.
- **Retention**: `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 [--keep-within 14d] --apply --prune`. Forget is a dry run until you pass `--apply`. An empty policy is refused, and the newest snapshot of each series is always kept, together with the newest one without problems.
- **Pruning is cautious.** What is in use is read from every snapshot's file list, and a missing or unreadable one stops the run. Unused data goes to the repository's `trash/` and is deleted by a later `backup prune` once it has sat there for a day (`--grace`); data written in the last day is never touched (`--min-age`). `backup trash list/restore/purge` manages the trash, `backup repair` rebuilds a lost snapshot summary from its file list, and `backup prune --immediate` frees space at once without any of this protection.
- **Locks are leases.** A running backup refreshes its lock every minute; another machine treats it as abandoned only after twenty minutes, judged by the share's own clock. A backup that finds its lock taken over (or cannot confirm it) stops before publishing anything.
- **Restore** a whole snapshot or `--include` globs. Restore never overwrites a different existing file unless you ask; `--conflict rename` writes the restored file next to it.
- **Portable**: BLAKE2b ids come from the standard library, so restoring never depends on optional packages.

#### Files that change while they are backed up

Duplio looks at each file before and after reading it (size, modification time, change time, inode) and reads it again if
it moved. A file that never holds still (a log being appended, a database in use) is stored as read but **flagged**: the
run lists it and exits with status 1, the snapshot counts it as a problem in `backup snapshots` and in retention, `restore`
warns that the file may be internally inconsistent, and the next run reads it again. A set of files that belong together (a database and its log, a VM
disk) cannot be made consistent by any tool that copies it while it is running. Back it up from a file-system snapshot
instead (LVM, ZFS, btrfs, APFS, VSS) with a profile's `pre` and `post` commands:

```toml
[profiles.home]
type = "backup"
source = "/tank/home/.zfs/snapshot/duplio"       # the frozen copy, not the live folder
repo = "smb://me@nas/backup"
pre  = ["zfs snapshot tank/home@duplio"]
post = ["zfs destroy tank/home@duplio"]
```

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
| Network drives | `drives`, `probe`, `sentinel create/check`; `--bwlimit`, `--expect-mounted`, `--sentinel` on every command that writes |
| Pictures | `preview`, `previews warm/stats/clear/settings` |
| Backups | `backup init/run/snapshots/ls/restore/check/forget/prune/trash/repair/diff/stats/unlock/find/history/export/passwd`, `snapshot create/list/forget`, `mirror`, `ingest` |
| Integrity | `manifest create/verify`, `versions list/prune` |
| Automation | `profile list/add/remove`, `run`, `schedule`, `notify`, `watch` |
| Setup | `ui` (or just `duplio`), `devices`, `doctor`, `rules`, `config [init]`, `cache stats/clear/prune` |

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

[previews]               # pictures from slow, remote and cloud drives (see "Pictures on slow drives")
mode = "auto"            # auto: slow/remote/cloud drives · always · off
keep = "session"         # session: gone when Duplio closes · persistent: kept (UNENCRYPTED) between runs
max_size = "1GB"         # limit of the picture buffer on disk (persistent or not)
size_px = 256            # longest side of a stored picture
prefetch = 12            # pictures fetched ahead of the one on screen
small_download = "1MB"   # a picture this small may be read whole on a slow drive without asking
max_download = "64MB"    # never read more than this of one file to make a picture
online_only = false      # fetch cloud-only files' pictures without asking (this downloads them)
memory = 0               # memory the window may use for pictures on screen, in bytes (0: automatic)
prefetch_folder = true   # Browse: fetch a whole folder's pictures in the background
```

## Project layout

```
src/duplio/
  vfs/          storage backends: base contract, local (+ mount and cloud-drive classification), SMB, SFTP (+ its
                protocol client), WebDAV, FTP/FTPS, ADB, locations, destination guards, retry, drive probe, listing cache
  preview/      pictures from slow drives: embedded-thumbnail extraction, JPEG/PNG decoders, cache, cheapest-first service
  hashing/      algorithm registry, streaming & sampled hashing, SQLite digest cache
  scan/         filters (exclude / include / protect), roots, inventory, overlap detection
  compare/      staged engine, device-aware scheduler, similar images, result model, sessions, folder diff
  plan/         keep rules, planner + invariants, plan files, journal, executor, undo/quarantine, crash reconciliation
  backup/       snapshot repository (+ encryption, chunkers, leased locks, garbage collection and trash),
                retention, mirror, phone ingest, media dates, merge, sync (+ history), hard-link snapshots,
                manifests, version housekeeping
  devices/      phone / share / drive discovery
  reporting/    JSON / CSV / HTML exports
  cli/          click commands and Rich rendering
  gui/          the window (Tk): A-against-B pages, folder browser, thumbnails, jobs on background threads; model.py,
                sorting.py, listing.py, prefetch.py, memory.py and palette.py hold the logic that needs no screen
  assets/       the pixel mark (logo_data.py is generated by tools/make_logo.py from the icon, keeping its transparency)
  tui/          Textual application (incl. the folder browser and the picture pane)
  transfer.py   the single verified-copy primitive (retry + bandwidth limit)
  notify.py     webhook / command / e-mail notifications
  watch.py      polling change detection
  util/faults.py crash and error injection points used by the crash sweeps
benchmarks/     scale.py: time and memory on a large synthetic tree
Duplio.bat, Duplio.command, duplio.sh, START-HERE.txt   double-click launchers (no installation)
vendor/         the pure-Python libraries the launchers use (rebuild: tools/refresh_vendor.py)
  config.py     TOML settings and profiles
  schedule.py   systemd / launchd / Task Scheduler / cron generation
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the parts fit together and how to add a backend, [docs/SAFETY.md](docs/SAFETY.md) for the full list of safety guarantees and how each is tested, and
[docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md) for how current tools (Czkawka, restic, GVFS/jmtpfs, Textual) shaped the design.

## Development

```bash
pip install -e ".[all,dev]"
pytest              # 800+ tests: engine, safety, odd file names, backends (fake ADB/SMB; real WebDAV, FTP and SFTP servers
                    # run inside the tests), pictures, launchers, backups, merge, sync, CLI, TUI, the window, crash sweeps
DUPLIO_SWEEP=full pytest tests/test_crash_sweep.py   # kill every operation at every durable step (the release gate)
python benchmarks/scale.py --files 300000 --dir /big/disk   # time and memory on a large tree
ruff check src tests && mypy src
python tools/refresh_vendor.py                               # rebuild vendor/ (needs internet access)
```

The window's logic is tested everywhere; the tests that drive the real window need Tk and a screen, and are skipped
without them. On a machine with no screen: `sudo apt install python3-tk xvfb` and `xvfb-run pytest tests/test_gui_flows.py`.

The network-drive tests start WsgiDAV, pyftpdlib and paramiko's SFTP server inside the test process; without those
packages (they are in the `dev` extra) those tests are skipped.

The icon (`assets/duplio-icon-original.png`, with resized copies and a multi-size
`duplio.ico`) shows a blue and a red featureless figure pulling apart, still joined by a
stretching web. The terminal logo (`duplio.assets.logo`) is the same picture sampled into
colour half-block characters.

## License

MIT
