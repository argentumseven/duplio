"""Android devices over ADB — fast, scriptable and able to hash on the phone.

MTP is slow and fragile; ADB is neither. With USB debugging enabled Duplio can
list a phone's storage in one round trip and, crucially, compute SHA-256 on
the phone itself, so comparing a phone against a computer moves digests over
USB instead of gigabytes of video.

Location syntax::

    adb:/sdcard/DCIM               the only connected device
    adb://SERIAL/sdcard/DCIM       a specific device (see ``adb devices``)
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import IO

from duplio.errors import BackendUnavailable, LocationError, UnsupportedOperation
from duplio.vfs.base import (
    Backend,
    Capabilities,
    MediaKind,
    RawEntry,
    RemoteDigest,
    Sampling,
    StatInfo,
    WalkFilter,
    WalkStats,
)
from duplio.vfs.mounts import SECOND_NS

_MAX_ARGS_BYTES = 64 * 1024  # stay well below Android's command-line limits


@dataclass(frozen=True)
class ADBTarget:
    serial: str | None
    path: str


def parse_adb(text: str) -> ADBTarget:
    if text.startswith("adb://"):
        rest = text[6:]
        serial, slash, path = rest.partition("/")
        if not serial:
            raise LocationError(f"adb:// location needs a device serial: {text!r}")
        return ADBTarget(serial=serial, path="/" + path if slash else "/sdcard")
    if text.startswith("adb:"):
        path = text[4:] or "/sdcard"
        if not path.startswith("/"):
            raise LocationError(f"ADB paths must be absolute, e.g. adb:/sdcard/DCIM (got {text!r})")
        return ADBTarget(serial=None, path=path)
    raise LocationError(f"not an ADB location: {text!r}")


def find_adb() -> str | None:
    explicit = os.environ.get("DUPLIO_ADB")
    if explicit:
        return explicit
    return shutil.which("adb")


def list_devices(adb: str | None = None) -> list[tuple[str, str, str]]:
    """``(serial, state, description)`` for each device ``adb devices -l`` reports."""
    adb = adb or find_adb()
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    devices = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            desc = " ".join(p for p in parts[2:] if p.startswith(("model:", "device:")))
            devices.append((parts[0], parts[1], desc.replace("model:", "").replace("device:", "")))
    return devices


class _StreamReader:
    """File-like wrapper around ``adb exec-out cat`` that validates completion."""

    def __init__(self, proc: subprocess.Popen[bytes], path: str) -> None:
        self._proc = proc
        self._path = path
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        assert self._proc.stdout is not None
        data = self._proc.stdout.read(size) if size >= 0 else self._proc.stdout.read()
        if not data and size != 0:
            self._finish()
        return data

    def readinto(self, buffer: bytearray | memoryview) -> int:
        assert self._proc.stdout is not None
        n = self._proc.stdout.readinto(buffer)  # type: ignore[attr-defined]
        if not n:
            self._finish()
        return n or 0

    def _finish(self) -> None:
        if self._eof:
            return
        self._eof = True
        code = self._proc.wait(timeout=60)
        if code != 0:
            err = self._proc.stderr.read().decode(errors="replace") if self._proc.stderr else ""
            raise OSError(f"adb read of {self._path} failed ({code}): {err.strip()}")

    def seek(self, offset: int, whence: int = 0) -> int:
        raise OSError("ADB streams are not seekable")

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=10)
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> _StreamReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ADBBackend(Backend):
    scheme = "adb"

    def __init__(self, serial: str | None = None, *, adb_path: str | None = None) -> None:
        adb = adb_path or find_adb()
        if not adb:
            raise BackendUnavailable(
                "the 'adb' tool was not found. Install Android platform-tools and enable USB debugging, "
                "or mount the phone with MTP and scan the mount point."
            )
        self.adb = adb
        if serial is None:
            ready = [d for d in list_devices(adb) if d[1] == "device"]
            if not ready:
                raise LocationError("no Android device is connected and authorised (check 'adb devices')")
            if len(ready) > 1:
                serials = ", ".join(d[0] for d in ready)
                raise LocationError(f"several devices are connected; choose one with adb://SERIAL/... ({serials})")
            serial = ready[0][0]
        self.serial = serial
        self._hash_tools: dict[str, bool] = {}
        self._lock = threading.Lock()

    # ---- plumbing -------------------------------------------------------
    def _cmd(self, *args: str) -> list[str]:
        return [self.adb, "-s", self.serial, *args]

    def shell(self, command: str, *, timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        proc = subprocess.run(self._cmd("shell", command), capture_output=True, timeout=timeout, check=False)
        if check and proc.returncode != 0:
            raise OSError(
                f"adb shell failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
            )
        return proc

    @staticmethod
    def q(path: str) -> str:
        return shlex.quote(path)

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        return f"adb:{self.serial}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(
            kind=MediaKind.DEVICE,
            label=f"Android device {self.serial} (ADB)",
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=path.startswith(("/sdcard", "/storage/emulated")),
            mtime_resolution_ns=SECOND_NS,
            sampling=Sampling.NONE,
            max_parallel_reads=2,
            remote_hash_algorithms=("sha256", "sha1", "md5"),
            fstype="adb",
            windows_names=True,
        )

    def uri(self, path: str) -> str:
        return f"adb://{self.serial}{posixpath.normpath(path)}"

    # ---- reading --------------------------------------------------------
    def walk(
        self,
        root: str,
        *,
        walk_filter: WalkFilter | None = None,
        stats: WalkStats | None = None,
        one_file_system: bool = True,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[RawEntry]:
        stats = stats if stats is not None else WalkStats()
        root = posixpath.normpath(root)
        # The trailing slash makes find descend into /sdcard, which is a symlink.
        cmd = (
            f"find {self.q(root + '/')} -mindepth 1 \\( -type f -o -type d \\) "
            f"-exec stat -c '%F|%s|%Y|%n' {{}} + 2>/dev/null"
        )
        proc = self.shell(cmd, timeout=3600, check=False)
        if proc.returncode not in (0, 1) and not proc.stdout:
            stats.errors.append((root, proc.stderr.decode(errors="replace").strip() or "listing failed"))
            return
        prefix = root.rstrip("/") + "/"
        skipped_dirs: list[str] = []
        lines = proc.stdout.decode("utf-8", errors="surrogateescape").splitlines()
        # Directories first so excluded subtrees prune their files.
        records = []
        for line in lines:
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            kind, size_text, mtime_text, path = parts
            path = posixpath.normpath(path)
            if not path.startswith(prefix):
                continue
            records.append((kind, size_text, mtime_text, path))
        records.sort(key=lambda r: r[3])
        for kind, size_text, mtime_text, path in records:
            if should_stop is not None and should_stop():
                return
            rel = path[len(prefix) :]
            if any(rel == d or rel.startswith(d + "/") for d in skipped_dirs):
                continue
            name = posixpath.basename(path)
            if kind == "directory":
                stats.dirs += 1
                if walk_filter is not None and walk_filter.skip_dir(rel, name):
                    stats.filtered += 1
                    skipped_dirs.append(rel)
                continue
            if kind not in ("regular file", "regular empty file"):
                stats.special_skipped += 1
                continue
            try:
                size, mtime = int(size_text), int(mtime_text)
            except ValueError:
                continue
            if walk_filter is not None and walk_filter.skip_file(rel, name, size):
                stats.filtered += 1
                continue
            stats.files += 1
            stats.bytes += size
            yield RawEntry(path=path, rel=rel, size=size, mtime_ns=mtime * SECOND_NS)

    def stat(self, path: str) -> StatInfo | None:
        proc = self.shell(f"stat -c '%F|%s|%Y' -- {self.q(path)}", check=False, timeout=60)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").lower()
            if "no such file" in err or "not a directory" in err or not err:
                return None
            raise OSError(f"adb stat {path}: {err.strip()}")
        parts = proc.stdout.decode(errors="replace").strip().split("|")
        if len(parts) != 3:
            raise OSError(f"unexpected stat output for {path}")
        kind = parts[0]
        return StatInfo(
            size=int(parts[1]),
            mtime_ns=int(parts[2]) * SECOND_NS,
            is_dir=kind == "directory",
            is_file=kind in ("regular file", "regular empty file"),
            is_symlink=kind == "symbolic link",
        )

    def open_read(self, path: str) -> IO[bytes]:
        proc = subprocess.Popen(
            self._cmd("exec-out", f"cat {self.q(path)}"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return _StreamReader(proc, path)  # type: ignore[return-value]

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        raise UnsupportedOperation("partial reads over ADB are slower than remote hashing")

    def _has_tool(self, tool: str) -> bool:
        with self._lock:
            if tool not in self._hash_tools:
                proc = self.shell(f"command -v {tool}", check=False, timeout=30)
                self._hash_tools[tool] = proc.returncode == 0 and bool(proc.stdout.strip())
            return self._hash_tools[tool]

    def remote_hash(self, paths: Sequence[str], algorithm: str) -> dict[str, RemoteDigest]:
        tool = {"sha256": "sha256sum", "sha1": "sha1sum", "md5": "md5sum"}.get(algorithm)
        if tool is None or not self._has_tool(tool):
            raise UnsupportedOperation(f"device cannot compute {algorithm}")
        results: dict[str, RemoteDigest] = {}
        batch: list[str] = []
        size = 0
        for path in paths:
            quoted = self.q(path)
            if batch and size + len(quoted) > _MAX_ARGS_BYTES:
                results.update(self._hash_batch(tool, batch))
                batch, size = [], 0
            batch.append(path)
            size += len(quoted) + 1
        if batch:
            results.update(self._hash_batch(tool, batch))
        return results

    def _hash_batch(self, tool: str, paths: list[str]) -> dict[str, RemoteDigest]:
        """Hash a batch on the device, then stat it, all in one round trip.

        The post-hash ``stat`` lets the caller detect files that changed while
        they were being hashed (the device cannot report a byte count itself).
        """
        quoted = " ".join(self.q(p) for p in paths)
        marker = "@@duplio-stat@@"
        cmd = f"{tool} -- {quoted} 2>/dev/null; echo {marker}; stat -c '%s|%Y|%n' -- {quoted} 2>/dev/null"
        proc = self.shell(cmd, check=False, timeout=24 * 3600)
        text = proc.stdout.decode("utf-8", errors="surrogateescape")
        hash_part, _, stat_part = text.partition(marker)
        wanted = set(paths)
        digests: dict[str, str] = {}
        for line in hash_part.splitlines():
            digest, sep, name = line.partition("  ")
            if not sep:
                continue
            digest = digest.lstrip("\\").lower()
            if name in wanted and digest and all(c in "0123456789abcdef" for c in digest):
                digests[name] = digest
        meta: dict[str, tuple[int, int]] = {}
        for line in stat_part.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                meta[parts[2]] = (int(parts[0]), int(parts[1]) * SECOND_NS)
        out: dict[str, RemoteDigest] = {}
        for name, digest in digests.items():
            size, mtime = meta.get(name, (None, None))
            out[name] = RemoteDigest(digest, size, mtime)
        return out

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        proc = self.shell(f"ls -1Ap -- {self.q(path)}", timeout=120)
        items = []
        for line in proc.stdout.decode("utf-8", errors="surrogateescape").splitlines():
            if not line:
                continue
            items.append((line.rstrip("/"), line.endswith("/")))
        return items

    def free_space(self, path: str) -> int | None:
        proc = self.shell(f"df -k {self.q(path)}", check=False, timeout=60)
        lines = proc.stdout.decode(errors="replace").splitlines()
        if len(lines) >= 2:
            fields = lines[-1].split()
            if len(fields) >= 4 and fields[3].isdigit():
                return int(fields[3]) * 1024
        return None

    # ---- writing --------------------------------------------------------
    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(path)
        fd, local_tmp = tempfile.mkstemp(prefix="duplio-adb-")
        remote_tmp = posixpath.join(posixpath.dirname(path), f".duplio-tmp-{os.path.basename(local_tmp)}")
        try:
            with os.fdopen(fd, "wb") as handle:
                yield handle
            self.makedirs(posixpath.dirname(path))
            push = subprocess.run(self._cmd("push", local_tmp, remote_tmp), capture_output=True, check=False)
            if push.returncode != 0:
                raise OSError(f"adb push failed: {push.stderr.decode(errors='replace').strip()}")
            flag = "-f" if overwrite else "-n"
            self.shell(f"mv {flag} -- {self.q(remote_tmp)} {self.q(path)}")
            if self.stat(remote_tmp) is not None:
                self.shell(f"rm -f -- {self.q(remote_tmp)}", check=False)
                raise FileExistsError(path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(local_tmp)

    def makedirs(self, path: str) -> None:
        self.shell(f"mkdir -p -- {self.q(path)}")

    def remove(self, path: str) -> None:
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(path)
        if info.is_dir:
            raise IsADirectoryError(path)
        self.shell(f"rm -f -- {self.q(path)}")
        if self.stat(path) is not None:
            raise OSError(f"could not delete {path} on the device")

    def rename(self, src: str, dst: str) -> None:
        if self.stat(dst) is not None:
            raise FileExistsError(dst)
        self.makedirs(posixpath.dirname(dst))
        self.shell(f"mv -n -- {self.q(src)} {self.q(dst)}")
        if self.stat(src) is not None or self.stat(dst) is None:
            raise OSError(f"could not move {src} to {dst} on the device")

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        stop = posixpath.normpath(stop_at)
        current = posixpath.normpath(path)
        while current != stop and current.startswith(stop + "/"):
            if self.shell(f"rmdir -- {self.q(current)}", check=False).returncode != 0:
                return
            current = posixpath.dirname(current)

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        self.shell(f"touch -m -d @{mtime_ns // SECOND_NS} -- {self.q(path)}", check=False)
