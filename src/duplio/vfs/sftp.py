"""SFTP, over the ``ssh`` program that comes with Windows 10+, macOS and Linux.

Location syntax::

    sftp://[user@]host[:port]/path        the path is taken from the top of what the server shows you

Nothing has to be installed: Duplio starts the system ``ssh`` program and speaks SFTP through it, so logging in works
exactly as it does for ``ssh`` and ``sftp`` on the command line. Your ``~/.ssh/config``, ssh-agent, key files, jump hosts
and ``known_hosts`` all apply, and the server's identity is checked by ssh itself.

Logging in:

* Keys and the ssh agent work as usual. Duplio never asks ssh to prompt in the terminal.
* A password can be given as ``DUPLIO_SFTP_PASSWORD``, in the system keyring, or typed when the interface asks for it.
  (Passwords are handed to ssh through a private helper file that is removed afterwards. Not available on Windows: use a
  key or the ssh agent there.)
* The first time a server is seen its key is trusted and remembered in ``known_hosts`` (``StrictHostKeyChecking=accept-new``);
  a key that later *changes* is refused. For stricter handling, set ``DUPLIO_SSH_OPTIONS="-o StrictHostKeyChecking=yes"``.
  ``DUPLIO_SSH_OPTIONS`` takes any other ssh options too (``-i keyfile``, ``-J jumphost``...). ``DUPLIO_SSH`` names the
  ssh program to use.

How it behaves:

* Reads keep a window of requests in flight, so a fast link is used well and a thumbnail costs one small round trip.
* Writing goes to a hidden temporary name and is moved into place only if it is complete; nothing is replaced unless
  asked, and a replaced file is swapped atomically where the server supports it.
* Deleting works on single files only.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO

from duplio.errors import BackendUnavailable, LocationError
from duplio.util.text import temp_name
from duplio.vfs import sftp_protocol as proto
from duplio.vfs.base import Capabilities, MediaKind, Sampling, StatInfo
from duplio.vfs.mounts import SECOND_NS
from duplio.vfs.remote import (
    Child,
    ListingBackend,
    RangedReader,
    climb_removing,
    host_for_url,
    quote_path,
    resolve_password,
    split_authority,
)

CHUNK = 32 * 1024
WINDOW = 32  # requests kept in flight
START_TIMEOUT = 60.0
_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%\-]*$")


@dataclass(frozen=True)
class SftpTarget:
    host: str
    port: int | None
    path: str
    username: str | None = None
    password: str | None = None


def parse_sftp(text: str) -> SftpTarget:
    from urllib.parse import unquote

    if not text.lower().startswith("sftp://"):
        raise LocationError(f"not an SFTP location: {text!r}")
    authority, slash, path = text[7:].partition("/")
    parts = split_authority(authority, "SFTP")
    # These end up as arguments to the ssh program; a name that starts with '-' would be read as an option.
    if not _SAFE_HOST.match(parts.host):
        raise LocationError(f"not a valid server name for SFTP: {parts.host!r}")
    if parts.username is not None and (parts.username.startswith("-") or any(c.isspace() or ord(c) < 32 for c in parts.username)):
        raise LocationError(f"not a valid user name for SFTP: {parts.username!r}")
    decoded = posixpath.normpath("/" + unquote(path, errors="surrogateescape")) if slash else "/"
    return SftpTarget(parts.host, parts.port, decoded, parts.username, parts.password)


def _enc(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


def _dec(name: bytes) -> str:
    return name.decode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------- running ssh
def find_ssh() -> str:
    override = os.environ.get("DUPLIO_SSH")
    if override:
        return override
    found = shutil.which("ssh")
    if found:
        return found
    if sys.platform == "win32":
        hint = "Open Settings > Apps > Optional features > Add a feature, and add 'OpenSSH Client'."
    elif sys.platform == "darwin":
        hint = "ssh comes with macOS; if it was removed, reinstall the Xcode command line tools."
    else:
        hint = "Install the 'openssh-client' package (for example: sudo apt install openssh-client)."
    raise BackendUnavailable(f"SFTP needs the 'ssh' program, which was not found. {hint}")


_FRIENDLY = (
    ("host key verification failed", "the server's identity could not be confirmed"),
    ("remote host identification has changed", "the server's identity has CHANGED since it was last seen (this can mean someone is "
     "intercepting the connection; if the server was reinstalled, remove its old line from ~/.ssh/known_hosts and try again)"),
)  # fmt: skip


def explain_ssh_failure(host: str, user: str | None, stderr: str) -> OSError:
    """Turn what ssh printed into an error that says what to do."""
    text = " ".join(stderr.split())
    lowered = text.lower()
    who = f"{user}@{host}" if user else host
    for key, meaning in _FRIENDLY:
        if key in lowered:
            return PermissionError(errno.EACCES, f"SFTP to {who}: {meaning}. ({text[:300]})")
    if "permission denied" in lowered:
        return PermissionError(
            errno.EACCES,
            f"SFTP to {who}: login refused. Check the user name, and use a key (ssh-agent) or the password "
            f"(DUPLIO_SFTP_PASSWORD). ({text[:300]})",
        )
    if "could not resolve hostname" in lowered or "name or service not known" in lowered:
        return OSError(errno.EHOSTUNREACH, f"SFTP to {who}: the server name was not found. ({text[:300]})")
    if "connection refused" in lowered:
        return ConnectionRefusedError(errno.ECONNREFUSED, f"SFTP to {who}: connection refused (is SFTP/SSH running there?)")
    if "timed out" in lowered or "no route to host" in lowered:
        return TimeoutError(errno.ETIMEDOUT, f"SFTP to {who}: no answer from the server. ({text[:300]})")
    if "subsystem request failed" in lowered or "request for subsystem" in lowered:
        return OSError(errno.ENOTSUP, f"SFTP to {who}: the server has no SFTP service ({text[:300]})")
    return ConnectionResetError(errno.ECONNRESET, f"SFTP to {who}: the connection failed. {text[:400]}")


class _Ssh:
    """The ``ssh`` child process and its pipes."""

    def __init__(self, args: list[str], env: dict[str, str], *, new_session: bool) -> None:
        kwargs: dict = {}  # type: ignore[type-arg]
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        elif new_session:
            kwargs["start_new_session"] = True  # no terminal: ssh uses the askpass helper instead of prompting
        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0, **kwargs
            )
        except OSError as exc:
            raise BackendUnavailable(f"could not start ssh ({args[0]}): {exc}") from exc
        assert self.proc.stdin is not None and self.proc.stdout is not None and self.proc.stderr is not None
        self._tail: collections.deque[str] = collections.deque(maxlen=30)
        self._drain = threading.Thread(target=self._drain_stderr, name="duplio-ssh-stderr", daemon=True)
        self._drain.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                self._tail.append(raw.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def stderr_text(self) -> str:
        self._drain.join(timeout=0.5)
        return "\n".join(line for line in self._tail if line.strip())

    def read_exact(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        chunks: list[bytes] = []
        remaining = n
        while remaining:
            data = self.proc.stdout.read(remaining)
            if not data:
                raise EOFError("ssh closed the connection")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def write_all(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        try:
            view = memoryview(data)
            while view:
                written = self.proc.stdin.write(view)
                view = view[written:]
        except (BrokenPipeError, ValueError) as exc:
            raise ConnectionResetError(errno.ECONNRESET, "ssh closed the connection") from exc

    def close(self) -> None:
        for stream in (self.proc.stdin,):
            with contextlib.suppress(Exception):
                if stream:
                    stream.close()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                self.proc.kill()
                self.proc.wait(timeout=2)
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                if stream:
                    stream.close()


def _askpass_script(directory: str) -> str:
    """A tiny program ssh runs to get the password. It answers only password prompts, never yes/no questions."""
    path = os.path.join(directory, "askpass.sh")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w") as handle:
        handle.write('#!/bin/sh\ncase "$1" in\n  *assword*) printf \'%s\\n\' "$DUPLIO_ASKPASS_VALUE" ;;\n  *) exit 1 ;;\nesac\n')
    return path


# --------------------------------------------------------------------------- reading and writing files
class _Download:
    """Reads a byte range with a window of requests in flight."""

    def __init__(self, session: proto.SftpSession, path: bytes, start: int, end: int) -> None:
        self._s = session
        self._handle = session.open(path, proto.O_READ)
        self._end = end
        self._next = start  # next offset to ask for
        self._queue: collections.deque[tuple[int, int, proto.Pending]] = collections.deque()
        self._buf = b""
        self._closed = False

    def _fill(self) -> None:
        while len(self._queue) < WINDOW and self._next < self._end:
            length = min(CHUNK, self._end - self._next)
            self._queue.append((self._next, length, self._s.read_async(self._handle, self._next, length)))
            self._next += length

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            return b""
        want = (self._end - self._next + sum(length for _, length, _ in self._queue) + len(self._buf)) if n is None or n < 0 else n
        chunks: list[bytes] = []
        have = 0
        if self._buf:
            take, self._buf = self._buf[:want], self._buf[want:]
            chunks.append(take)
            have += len(take)
        while have < want:
            self._fill()
            if not self._queue:
                break
            offset, length, pending = self._queue.popleft()
            try:
                data = self._s.read_result(pending)
            except BaseException:
                self._abandon()
                raise
            if not data:
                self._abandon()
                raise ConnectionResetError(errno.ECONNRESET, f"the server ended the file early at byte {offset}")
            if len(data) > length:
                self._abandon()
                raise proto.ProtocolError("the server sent more data than was asked for")
            if len(data) < length:  # the server gave less than asked: ask again for the rest, ahead of the others
                rest = length - len(data)
                self._queue.appendleft((offset + len(data), rest, self._s.read_async(self._handle, offset + len(data), rest)))
            take = data[: want - have]
            self._buf = data[len(take) :]
            chunks.append(take)
            have += len(take)
        return b"".join(chunks)

    def _abandon(self) -> None:
        self._closed = True
        self._queue.clear()
        with contextlib.suppress(Exception):
            self._s.close_handle(self._handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.clear()  # answers still on their way are dropped when they arrive
        with contextlib.suppress(Exception):
            self._s.close_handle(self._handle)


class _Upload:
    """A file being written: data goes out in blocks with a window of unanswered writes, and every answer is checked."""

    def __init__(self, session: proto.SftpSession, path: bytes) -> None:
        self._s = session
        self._handle = session.open(path, proto.O_WRITE | proto.O_CREAT | proto.O_EXCL)
        self._pos = 0
        self._outstanding: collections.deque[proto.Pending] = collections.deque()
        self._open = True

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    @property
    def size(self) -> int:
        return self._pos

    def write(self, data: bytes | memoryview) -> int:
        view = memoryview(data)
        total = len(view)
        for start in range(0, total, CHUNK):
            piece = view[start : start + CHUNK]
            self._outstanding.append(self._s.write_async(self._handle, self._pos, piece))
            self._pos += len(piece)
            while len(self._outstanding) >= WINDOW:
                self._s.write_result(self._outstanding.popleft())
        return total

    def flush(self) -> None:
        while self._outstanding:
            self._s.write_result(self._outstanding.popleft())

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        try:
            self.flush()
        finally:
            self._outstanding.clear()
            self._s.close_handle(self._handle)

    def abort(self) -> None:
        if self._open:
            self._open = False
            self._outstanding.clear()
            with contextlib.suppress(Exception):
                self._s.close_handle(self._handle)


# --------------------------------------------------------------------------- the backend
class SftpBackend(ListingBackend):
    scheme = "sftp"

    def __init__(self, target: SftpTarget, *, credential_provider=None, timeout: float = 120.0) -> None:  # type: ignore[no-untyped-def]
        self.target = target
        self._credentials = credential_provider
        self._timeout = timeout
        self._password = target.password
        self._lock = threading.RLock()
        self._current: proto.SftpSession | None = None
        self._ssh: _Ssh | None = None
        self._askpass_dir: str | None = None
        self._asked_for_password = False
        self._looked_up = False

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        port = f":{self.target.port}" if self.target.port and self.target.port != 22 else ""
        return f"sftp://{user}{host_for_url(self.target.host.lower())}{port}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(
            kind=MediaKind.NETWORK,
            label=f"SFTP on {self.target.host}",
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=True,  # the cautious assumption; `duplio probe` measures the real behaviour
            mtime_resolution_ns=SECOND_NS,
            sampling=Sampling.RANDOM,
            max_parallel_reads=4,
            fstype="sftp",
            windows_names=True,
        )

    def uri(self, path: str) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        port = f":{self.target.port}" if self.target.port and self.target.port != 22 else ""
        return f"sftp://{user}{host_for_url(self.target.host)}{port}{quote_path(self.normalize(path))}"

    def display(self, path: str) -> str:
        return self.uri(path)

    # ---- the connection -------------------------------------------------
    def ssh_command(self, *, with_password: bool) -> list[str]:
        t = self.target
        extra = shlex.split(os.environ.get("DUPLIO_SSH_OPTIONS", ""), posix=sys.platform != "win32")
        # ssh keeps the FIRST value it is given for an option, so the user's own options go before ours.
        options = [
            "-T", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
        ]  # fmt: skip
        options += ["-o", "NumberOfPasswordPrompts=1"] if with_password else ["-o", "BatchMode=yes"]
        args = [find_ssh(), *extra, *options]
        if t.port:
            args += ["-p", str(t.port)]
        if t.username:
            args += ["-l", t.username]
        args += ["-s", "--", t.host, "sftp"]
        return args

    def _start(self, *, with_password: bool) -> tuple[proto.SftpSession, _Ssh]:
        args = self.ssh_command(with_password=with_password)
        env = dict(os.environ)
        if with_password:
            if sys.platform == "win32":
                raise PermissionError(
                    errno.EACCES,
                    f"SFTP to {self.target.host}: password login is not available on Windows. Use a key or the ssh agent.",
                )
            if self._askpass_dir is None:
                self._askpass_dir = tempfile.mkdtemp(prefix="duplio-askpass-")
                os.chmod(self._askpass_dir, 0o700)
                _askpass_script(self._askpass_dir)
            env["SSH_ASKPASS"] = os.path.join(self._askpass_dir, "askpass.sh")
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", "duplio:0")  # older ssh versions only use the helper when a display is set
            env["DUPLIO_ASKPASS_VALUE"] = self._password or ""
        ssh = _Ssh(args, env, new_session=with_password)
        session = proto.SftpSession(
            ssh.read_exact, ssh.write_all, ssh.close, timeout=self._timeout, describe_failure=ssh.stderr_text
        )
        watchdog = threading.Timer(START_TIMEOUT, lambda: ssh.proc.kill())
        watchdog.start()
        try:
            session.start()
        except OSError as exc:
            raise explain_ssh_failure(self.target.host, self.target.username, ssh.stderr_text() or str(exc)) from exc
        finally:
            watchdog.cancel()
        return session, ssh

    def _open_session(self) -> proto.SftpSession:
        if self._password is None and self.target.username and not self._looked_up:
            self._looked_up = True  # a stored password (environment, keyring) is used straight away; only prompting waits
            self._password = resolve_password("sftp", self.target.host, self.target.username, None, None)
        if self._password is not None:
            session, ssh = self._start(with_password=True)
        else:
            try:
                session, ssh = self._start(with_password=False)
            except PermissionError:
                # No key worked. Only now ask for a password (never when a key is enough).
                if self._asked_for_password or not self.target.username:
                    raise
                self._asked_for_password = True
                self._password = resolve_password("sftp", self.target.host, self.target.username, None, self._credentials)
                if self._password is None:
                    raise
                session, ssh = self._start(with_password=True)
        self._ssh = ssh
        return session

    def _session(self) -> proto.SftpSession:
        with self._lock:
            current = self._current
            if current is not None and current.alive:
                return current
            self._current = self._open_session()
            return self._current

    def recover(self) -> None:
        with self._lock:
            session, self._current = self._current, None
        if session is not None:
            session.close()

    def close(self) -> None:
        self.recover()
        directory, self._askpass_dir = self._askpass_dir, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    # ---- listing and metadata -------------------------------------------
    @staticmethod
    def _child(entry: proto.DirEntry) -> Child:
        a = entry.attrs
        name = _dec(entry.name)
        if a.is_dir:
            return Child(name=name, is_dir=True, is_file=False, mode=a.permissions)
        if a.is_symlink:
            return Child(name=name, is_dir=False, is_file=False, is_symlink=True)
        if not a.is_file:
            return Child(name=name, is_dir=False, is_file=False)
        problem = None
        if a.size is None:
            problem = "the server did not report this file's size"
        elif a.mtime is None:
            problem = "the server did not report this file's modification time, which is needed to tell files apart safely"
        return Child(name=name, is_dir=False, is_file=True, size=a.size or 0, mtime_ns=(a.mtime or 0) * SECOND_NS,
                     mode=a.permissions, problem=problem)  # fmt: skip

    def _list(self, path: str) -> list[Child]:
        entries = self._session().listdir(_enc(self.normalize(path)))
        return [self._child(e) for e in entries if e.name not in (b".", b"..")]

    def _info(self, attrs: proto.Attrs, path: str) -> StatInfo:
        if attrs.is_file and (attrs.size is None or attrs.mtime is None):
            raise OSError(errno.EIO, f"{path}: the server did not report the file's size or modification time")
        return StatInfo(
            size=attrs.size or 0,
            mtime_ns=(attrs.mtime or 0) * SECOND_NS,
            is_dir=attrs.is_dir,
            is_file=attrs.is_file,
            is_symlink=attrs.is_symlink,
            mode=attrs.permissions,
        )

    def stat(self, path: str) -> StatInfo | None:
        path = self.normalize(path)
        try:
            attrs = self._session().lstat(_enc(path))
        except FileNotFoundError:
            return None
        return self._info(attrs, path)

    def free_space(self, path: str) -> int | None:
        try:
            return self._session().free_space(_enc(self.normalize(path)))
        except OSError:
            return None

    # ---- reading --------------------------------------------------------
    def open_read(self, path: str) -> IO[bytes]:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder")
        session = self._session()
        size = info.size
        return RangedReader(size, lambda pos: _Download(session, _enc(path), pos, size), name=path)  # type: ignore[return-value]

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder")
        end = min(info.size, offset + length)
        if offset >= end:
            return b""
        stream = _Download(self._session(), _enc(path), offset, end)
        try:
            return stream.read(end - offset)
        finally:
            stream.close()

    # ---- writing --------------------------------------------------------
    def makedirs(self, path: str) -> None:
        path = self.normalize(path)
        missing: list[str] = []
        current = path
        while current != "/":
            info = self.stat(current)
            if info is not None:
                if not info.is_dir:
                    raise NotADirectoryError(errno.ENOTDIR, f"{current} exists and is not a folder")
                break
            missing.append(current)
            current = self.parent(current)
        for folder in reversed(missing):
            try:
                self._session().mkdir(_enc(folder))
            except OSError:
                info = self.stat(folder)
                if info is None or not info.is_dir:
                    raise

    def remove(self, path: str) -> None:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder; Duplio only deletes single files on an SFTP server")
        self._session().remove(_enc(path))

    def rename(self, src: str, dst: str) -> None:
        dst = self.normalize(dst)
        if self.stat(dst) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        self.makedirs(self.parent(dst))
        self._move_no_replace(self.normalize(src), dst)

    def _move_no_replace(self, src: str, dst: str) -> None:
        try:
            self._session().rename(_enc(src), _enc(dst))
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError | PermissionError | ConnectionError) and self.stat(dst) is not None:
                raise FileExistsError(errno.EEXIST, "destination exists", dst) from exc
            raise

    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        path = self.normalize(path)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", path)
        parent = self.parent(path)
        self.makedirs(parent)
        token = secrets.token_hex(4)
        tmp = posixpath.join(parent, temp_name(self.basename(path), f".duplio-tmp-{token}"))
        upload = _Upload(self._session(), _enc(tmp))
        try:
            yield upload  # type: ignore[misc]
            upload.close()
            held = self.stat(tmp)
            if held is None or held.size != upload.size:
                raise OSError(errno.EIO, f"the server holds {held.size if held else 'no'} bytes of {path}, expected {upload.size}")
            self._commit(tmp, path, overwrite=overwrite, token=token)
            stored = self.stat(path)
            if stored is None or not stored.is_file or stored.size != upload.size:
                raise OSError(errno.EIO, f"the server did not keep the file under the name {path} (it may not allow that name)")
        except BaseException:
            upload.abort()
            with contextlib.suppress(Exception):
                if self.stat(tmp) is not None:
                    self._session().remove(_enc(tmp))
            raise

    def _commit(self, tmp: str, dst: str, *, overwrite: bool, token: str) -> None:
        session = self._session()
        if not overwrite or self.stat(dst) is None:
            self._move_no_replace(tmp, dst)  # atomic: the server refuses if the name appeared in the meantime
            return
        if "posix-rename@openssh.com" in session.extensions:
            session.posix_rename(_enc(tmp), _enc(dst))
            return
        # No atomic replace: keep the old file aside until the new one is in place, and put it back on any failure.
        aside = posixpath.join(self.parent(dst), temp_name(self.basename(dst), f".duplio-old-{token}"))
        session.rename(_enc(dst), _enc(aside))
        try:
            session.rename(_enc(tmp), _enc(dst))
        except BaseException:
            with contextlib.suppress(Exception):
                session.rename(_enc(aside), _enc(dst))
            raise
        with contextlib.suppress(Exception):
            session.remove(_enc(aside))

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        seconds = mtime_ns // SECOND_NS
        with contextlib.suppress(Exception):
            self._session().setstat(_enc(self.normalize(path)), proto.Attrs(atime=seconds, mtime=seconds))

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        """SFTP's folder removal only works on an empty folder, so it cannot take anything with it."""

        def remove(folder: str) -> bool:
            try:
                self._session().rmdir(_enc(folder))
            except OSError:
                return False  # not empty, or not allowed: stop climbing
            return True

        climb_removing(self, path, stop_at, remove)


__all__ = ["SftpBackend", "SftpTarget", "explain_ssh_failure", "find_ssh", "parse_sftp"]
