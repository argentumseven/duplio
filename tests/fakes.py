"""Test doubles: a fake ``adb`` executable and a fake ``smbclient`` module.

The fake adb runs the device-side shell commands Duplio issues (find, stat,
sha256sum, mv, rm…) against the local filesystem, so the real ADB backend
code path is exercised end to end. The fake smbclient maps UNC paths onto a
local folder using the same function names and error semantics as
``smbprotocol``'s ``smbclient``.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as stat_mod
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

FAKE_ADB = textwrap.dedent(
    """\
    #!{python}
    import shutil, subprocess, sys
    args = sys.argv[1:]
    if args[:1] == ["devices"]:
        print("List of devices attached")
        print("FAKE123        device usb:1-1 product:x model:Fake_Phone device:fake")
        sys.exit(0)
    if args[:1] == ["-s"]:
        args = args[2:]
    cmd = args[0]
    if cmd in ("shell", "exec-out"):
        proc = subprocess.run(["sh", "-c", " ".join(args[1:])])
        sys.exit(proc.returncode)
    if cmd == "push":
        shutil.copyfile(args[1], args[2])
        print("1 file pushed")
        sys.exit(0)
    print("unsupported: " + " ".join(args), file=sys.stderr)
    sys.exit(1)
    """
)


def install_fake_adb(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "adb"
    path.write_text(FAKE_ADB.format(python=sys.executable))
    path.chmod(0o755)
    return path


class FakeSMB:
    """Implements the subset of ``smbclient`` Duplio uses, backed by ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions: list[tuple[str, str | None, str | None, int]] = []

    def _local(self, unc: str) -> str:
        parts = [p for p in unc.replace("/", "\\").split("\\") if p]
        if len(parts) < 2:
            raise OSError(errno.EINVAL, "bad UNC path", unc)
        return str(self.root.joinpath(*parts[1:]))  # parts[0] is the server

    # session management
    def register_session(self, server, username=None, password=None, port=445, **kw):  # type: ignore[no-untyped-def]
        self.sessions.append((server, username, password, port))

    def delete_session(self, server, port=445, **kw):  # type: ignore[no-untyped-def]
        pass

    # metadata
    def lstat(self, path):  # type: ignore[no-untyped-def]
        st = os.lstat(self._local(path))
        return SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
                               st_atime_ns=st.st_atime_ns, st_ino=st.st_ino, st_dev=st.st_dev, st_nlink=st.st_nlink)

    stat = lstat

    def scandir(self, path, search_pattern="*", **kw):  # type: ignore[no-untyped-def]
        local = self._local(path)
        out = []
        for entry in os.scandir(local):
            st = entry.stat(follow_symlinks=False)
            unc = path.rstrip("\\") + "\\" + entry.name
            info = self.lstat(unc)
            out.append(SimpleNamespace(
                name=entry.name,
                path=unc,
                is_dir=lambda st=st: stat_mod.S_ISDIR(st.st_mode),
                is_file=lambda st=st: stat_mod.S_ISREG(st.st_mode),
                is_symlink=lambda st=st: stat_mod.S_ISLNK(st.st_mode),
                stat=lambda info=info: info,
            ))
        return iter(out)

    def stat_volume(self, path, **kw):  # type: ignore[no-untyped-def]
        usage = shutil.disk_usage(self._local(path))
        return SimpleNamespace(total_size=usage.total, caller_available_size=usage.free, actual_available_size=usage.free)

    # data
    def open_file(self, path, mode="r", share_access=None, **kw):  # type: ignore[no-untyped-def]
        return open(self._local(path), mode)

    def remove(self, path, **kw):  # type: ignore[no-untyped-def]
        os.unlink(self._local(path))

    def rename(self, src, dst, **kw):  # type: ignore[no-untyped-def]
        target = self._local(dst)
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "exists", dst)
        os.rename(self._local(src), target)

    def replace(self, src, dst, **kw):  # type: ignore[no-untyped-def]
        os.replace(self._local(src), self._local(dst))

    def makedirs(self, path, exist_ok=False, **kw):  # type: ignore[no-untyped-def]
        os.makedirs(self._local(path), exist_ok=exist_ok)

    def rmdir(self, path, **kw):  # type: ignore[no-untyped-def]
        os.rmdir(self._local(path))

    def utime(self, path, times=None, ns=None, **kw):  # type: ignore[no-untyped-def]
        os.utime(self._local(path), ns=ns)
