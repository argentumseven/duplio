"""Local filesystems — including phones, cameras and shares mounted into the tree.

The same backend serves ``/home/me/Pictures``, a GVFS MTP mount such as
``/run/user/1000/gvfs/mtp:host=Google_Pixel_8/Internal shared storage``, a
jmtpfs mount, a ``cifs`` share or a USB stick. The mount table decides the
capabilities of each path, so a phone automatically gets single-threaded,
sequential I/O and FAT gets two-second timestamp tolerance.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
import secrets
import shutil
import stat as stat_mod
import sys
import threading
from collections.abc import Callable, Iterator
from typing import IO

from duplio.errors import UnsupportedOperation
from duplio.util.faults import crashpoint
from duplio.util.text import temp_name
from duplio.vfs.base import (
    Backend,
    Capabilities,
    RawEntry,
    StatInfo,
    WalkFilter,
    WalkStats,
)
from duplio.vfs.mounts import MOUNTS, MountTable, capabilities_for_local_path

try:  # optional dependency
    import send2trash as _send2trash
except ImportError:  # pragma: no cover - depends on environment
    _send2trash = None

_FICLONE = 0x40049409  # Linux ioctl: share extents copy-on-write
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _load_libc() -> ctypes.CDLL | None:
    if sys.platform == "win32":
        return None
    try:
        name = ctypes.util.find_library("c")
        return ctypes.CDLL(name, use_errno=True) if name else None
    except OSError:  # pragma: no cover
        return None


_LIBC = _load_libc()


def _change_time(st: os.stat_result) -> int | None:
    """The inode change time. On Windows ``st_ctime`` is the creation time, which says nothing about changes."""
    return None if os.name == "nt" else st.st_ctime_ns


def rename_noreplace(src: str, dst: str) -> None:
    """Rename ``src`` to ``dst``, failing with ``FileExistsError`` if ``dst`` exists.

    ``os.rename`` silently overwrites on POSIX, which is exactly the kind of
    surprise a data-safety tool cannot afford. We use ``renameat2`` with
    ``RENAME_NOREPLACE`` on Linux, fall back to the atomic link-then-unlink
    trick, and only as a last resort to check-then-rename.
    """
    if sys.platform == "win32":  # pragma: no cover - os.rename never clobbers on Windows
        os.rename(src, dst)
        return
    if _LIBC is not None and sys.platform.startswith("linux") and hasattr(_LIBC, "renameat2"):
        res = _LIBC.renameat2(
            _AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE
        )
        if res == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        if err not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
            raise OSError(err, os.strerror(err), src)
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", dst) from None
        os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        # Both names now point at the same data; drop the new one so the
        # operation is a clean failure rather than a silent duplicate.
        with contextlib.suppress(OSError):
            os.unlink(dst)
        raise


def _drop_page_cache(fd: int) -> None:
    """Ask the kernel to forget cached pages so a verify-read hits the medium."""
    if hasattr(os, "posix_fadvise"):
        with contextlib.suppress(OSError):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


class LocalBackend(Backend):
    scheme = "local"
    sep = os.sep

    def __init__(self, mount_table: MountTable | None = None) -> None:
        self._mounts = mount_table or MOUNTS
        self._caps_cache: dict[str, tuple[Capabilities, str]] = {}
        self._lock = threading.Lock()

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        return "local"

    def _profile(self, path: str) -> tuple[Capabilities, str]:
        path = os.path.abspath(path)
        entry = self._mounts.find(path) if sys.platform != "win32" else None
        key = entry.mount_point if entry else os.path.splitdrive(path)[0] or os.sep
        if entry is not None and entry.fstype == "fuse.gvfsd-fuse":
            # One GVFS mount hosts every device; the next segment identifies it.
            rest = path[len(entry.mount_point) :].lstrip(os.sep).split(os.sep, 1)[0]
            key = os.path.join(entry.mount_point, rest)
        with self._lock:
            cached = self._caps_cache.get(key)
        if cached is not None:
            return cached
        caps, mount_point = capabilities_for_local_path(path, self._mounts)
        if caps.can_trash is False and _send2trash is not None and caps.kind in ("local", "removable", "rotational"):
            caps = caps.with_(can_trash=True)
        result = (caps, key if entry is not None else mount_point)
        with self._lock:
            self._caps_cache[key] = result
        return result

    def capabilities(self, path: str) -> Capabilities:
        return self._profile(path)[0]

    def device_key(self, path: str) -> str:
        return f"local:{self._profile(path)[1]}"

    def uri(self, path: str) -> str:
        return os.path.abspath(path)

    def display(self, path: str) -> str:
        home = os.path.expanduser("~")
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home) :]
        return path

    # ---- path algebra ---------------------------------------------------
    def join(self, base: str, rel: str) -> str:
        if not rel:
            return base
        return os.path.join(base, *[p for p in rel.split("/") if p])

    def parent(self, path: str) -> str:
        return os.path.dirname(os.path.abspath(path))

    def basename(self, path: str) -> str:
        return os.path.basename(path.rstrip(os.sep))

    def normalize(self, path: str) -> str:
        return os.path.normpath(os.path.abspath(path))

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
        root = os.path.abspath(root)
        try:
            root_stat = os.stat(root)
        except OSError as exc:
            stats.errors.append((root, str(exc)))
            return
        if not stat_mod.S_ISDIR(root_stat.st_mode):
            stats.errors.append((root, "not a directory"))
            return
        root_dev = root_stat.st_dev
        stack: list[tuple[str, str]] = [(root, "")]
        while stack:
            if should_stop is not None and should_stop():
                return
            dir_path, dir_rel = stack.pop()
            try:
                with os.scandir(dir_path) as it:
                    children = list(it)
            except OSError as exc:
                stats.errors.append((dir_path, exc.strerror or str(exc)))
                continue
            stats.dirs += 1
            subdirs: list[tuple[str, str]] = []
            for child in children:
                name = child.name
                rel = f"{dir_rel}/{name}" if dir_rel else name
                try:
                    if child.is_symlink():
                        stats.symlinks_skipped += 1
                        continue
                    if child.is_dir(follow_symlinks=False):
                        if walk_filter is not None and walk_filter.skip_dir(rel, name):
                            stats.filtered += 1
                            continue
                        if one_file_system:
                            if child.stat(follow_symlinks=False).st_dev != root_dev:
                                stats.other_fs_skipped += 1
                                continue
                        subdirs.append((child.path, rel))
                        continue
                    if not child.is_file(follow_symlinks=False):
                        stats.special_skipped += 1
                        continue
                    st = child.stat(follow_symlinks=False)
                except OSError as exc:
                    stats.errors.append((child.path, exc.strerror or str(exc)))
                    continue
                if walk_filter is not None and walk_filter.skip_file(rel, name, st.st_size):
                    stats.filtered += 1
                    continue
                stats.files += 1
                stats.bytes += st.st_size
                yield RawEntry(
                    path=child.path,
                    rel=rel,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    inode=(st.st_dev, st.st_ino) if st.st_ino else None,
                    nlink=st.st_nlink,
                    ctime_ns=_change_time(st),
                )
            # Reverse so traversal order is alphabetical-ish and deterministic.
            subdirs.sort(reverse=True)
            stack.extend(subdirs)

    def stat(self, path: str) -> StatInfo | None:
        try:
            st = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return StatInfo(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            is_dir=stat_mod.S_ISDIR(st.st_mode),
            is_file=stat_mod.S_ISREG(st.st_mode),
            is_symlink=stat_mod.S_ISLNK(st.st_mode),
            inode=(st.st_dev, st.st_ino) if st.st_ino else None,
            nlink=st.st_nlink,
            mode=stat_mod.S_IMODE(st.st_mode),
            ctime_ns=_change_time(st),
        )

    def open_read(self, path: str) -> IO[bytes]:
        return open(path, "rb")

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        with open(path, "rb", buffering=0) as handle:
            if hasattr(os, "pread"):
                out = bytearray()
                fd = handle.fileno()
                while len(out) < length:
                    chunk = os.pread(fd, length - len(out), offset + len(out))
                    if not chunk:
                        break
                    out += chunk
                return bytes(out)
            handle.seek(offset)
            return handle.read(length) or b""

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        with os.scandir(path) as it:
            return [(e.name, e.is_dir(follow_symlinks=False)) for e in it]

    def free_space(self, path: str) -> int | None:
        probe = os.path.abspath(path)
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                return None
            probe = parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    # ---- writing --------------------------------------------------------
    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        caps = self.capabilities(os.path.dirname(os.path.abspath(path)) or path)
        if not caps.can_write:
            raise UnsupportedOperation(f"{caps.label} is read-only")
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        if not overwrite and os.path.lexists(path):
            raise FileExistsError(errno.EEXIST, "destination exists", path)

        if caps.atomic_rename:
            tmp = os.path.join(directory, temp_name(os.path.basename(path), f".duplio-tmp-{secrets.token_hex(4)}"))
            handle = open(tmp, "xb")  # noqa: SIM115
        else:
            # MTP and similar FUSE layers cannot rename reliably: write in place,
            # but never on top of an existing file (a failed write would destroy it).
            if os.path.lexists(path):
                raise UnsupportedOperation(
                    f"{caps.label} cannot replace files safely; move the existing file aside first"
                )
            tmp = path
            handle = open(path, "xb")  # noqa: SIM115
        try:
            yield handle
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
            _drop_page_cache(handle.fileno())
            handle.close()
            crashpoint("fs.write.before-commit")
            if tmp != path:
                if overwrite:
                    os.replace(tmp, path)
                else:
                    rename_noreplace(tmp, path)
                from duplio.util.atomic import fsync_dir

                fsync_dir(directory)
            crashpoint("fs.write.committed")
        except BaseException:
            handle.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def remove(self, path: str) -> None:
        st = os.lstat(path)
        if stat_mod.S_ISDIR(st.st_mode):
            raise IsADirectoryError(errno.EISDIR, "refusing to remove a directory", path)
        crashpoint("fs.remove.before")
        os.unlink(path)
        crashpoint("fs.remove.after")

    def rename(self, src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        crashpoint("fs.rename.before")
        rename_noreplace(src, dst)
        crashpoint("fs.rename.after")

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        stop = os.path.normpath(os.path.abspath(stop_at))
        current = os.path.normpath(os.path.abspath(path))
        while current != stop and current.startswith(stop + os.sep):
            try:
                os.rmdir(current)
            except OSError:
                return
            current = os.path.dirname(current)

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        crashpoint("fs.set-mtime.before", can_fail=False)
        try:
            st = os.lstat(path)
            os.utime(path, ns=(st.st_atime_ns, mtime_ns))
        except OSError:
            pass

    def hardlink(self, src: str, dst: str) -> None:
        crashpoint("fs.hardlink.before")
        os.link(src, dst)
        crashpoint("fs.hardlink.after")

    def reflink(self, src: str, dst: str) -> None:
        crashpoint("fs.reflink.before")
        if sys.platform.startswith("linux"):
            import fcntl

            with open(src, "rb") as fsrc, open(dst, "xb") as fdst:
                try:
                    fcntl.ioctl(fdst.fileno(), _FICLONE, fsrc.fileno())
                except OSError as exc:
                    fdst.close()
                    with contextlib.suppress(OSError):
                        os.unlink(dst)
                    raise UnsupportedOperation(
                        f"this filesystem cannot clone files ({exc.strerror})"
                    ) from exc
            return
        if sys.platform == "darwin" and _LIBC is not None and hasattr(_LIBC, "clonefile"):
            if _LIBC.clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
                err = ctypes.get_errno()
                raise UnsupportedOperation(f"clonefile failed: {os.strerror(err)}")
            return
        raise UnsupportedOperation("copy-on-write clones are not supported on this platform")

    def trash(self, path: str) -> None:
        if _send2trash is None:
            raise UnsupportedOperation("install Send2Trash to use the system trash")
        crashpoint("fs.trash.before")
        _send2trash.send2trash(path)
        crashpoint("fs.trash.after")

    def same_file(self, a: str, b: str) -> bool | None:
        if self.normalize(a) == self.normalize(b):
            return True
        try:
            if os.path.samefile(a, b):
                return True
        except OSError:
            return None
        return False if self.capabilities(a).stable_inodes else None
