"""FTP and FTP over TLS: web hosts, routers with a USB disk, cameras, old NAS boxes, many scanners and printers.

Location syntax::

    ftps://[user@]host[:port]/path     encrypted (explicit TLS, "AUTH TLS"); use this whenever the server allows it
    ftp://[user@]host[:port]/path      NOT encrypted: the password and every file cross the network in the clear

The path is taken from the top of what the server shows you (``/photos/2024``). Built on the standard library
(``ftplib``). Passwords are never stored: they come from ``DUPLIO_FTP_PASSWORD``, the optional ``keyring`` package, or
a prompt supplied by the UI. Without a user name the connection is anonymous.

How it behaves:

* Listings use ``MLSD`` where the server has it (names, sizes and exact UTC times in one answer). Otherwise ``LIST`` is
  read and each file's time is asked for with ``MDTM``. A server with neither cannot tell Duplio when a file changed,
  and is refused rather than guessed at.
* Reading restarts a transfer at any offset (``REST``), so a thumbnail or a sample of a big file costs kilobytes.
* Writing uploads to a hidden temporary name, checks the size, then renames it into place. FTP has no "rename unless
  it exists", so the check happens just before the rename; do not let another program write the same names at the
  same moment. Overwriting keeps the old file until the new one is in place.
* Deleting works on single files only (FTP's ``DELE``); folders are never removed.
* Implicit FTPS (the old port 990 style) is not supported.
"""

from __future__ import annotations

import contextlib
import errno
import ftplib
import posixpath
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO

from duplio.errors import LocationError
from duplio.util.text import temp_name
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

SPOOL_MEMORY = 8 * 1024 * 1024
MAX_CONNECTIONS = 4
POOL_WAIT = 30.0
STALE_AFTER = 10.0  # a connection idle for longer than this is checked before it is trusted
ABORT_WAIT = 5.0
READ_BLOCK = 64 * 1024


@dataclass(frozen=True)
class FtpTarget:
    host: str
    port: int
    tls: bool
    path: str
    username: str | None = None
    password: str | None = None


def parse_ftp(text: str) -> FtpTarget:
    from urllib.parse import unquote

    lowered = text.lower()
    if lowered.startswith("ftps://"):
        rest, tls = text[7:], True
    elif lowered.startswith("ftp://"):
        rest, tls = text[6:], False
    else:
        raise LocationError(f"not an FTP location: {text!r}")
    authority, slash, path = rest.partition("/")
    parts = split_authority(authority, "FTP")
    decoded = posixpath.normpath("/" + unquote(path, errors="surrogateescape")) if slash else "/"
    return FtpTarget(parts.host, parts.port or 21, tls, decoded, parts.username, parts.password)


# --------------------------------------------------------------------------- reading what the server sends
def parse_ftp_time(text: str) -> int | None:
    """``YYYYMMDDHHMMSS[.fff]`` (UTC, as in MDTM and MLSD) to nanoseconds since the epoch."""
    text = text.strip()
    m = re.fullmatch(r"(\d{14})(?:\.(\d{1,9}))?", text)
    if m is None:
        return None
    try:
        moment = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    fraction = int((m.group(2) or "0").ljust(9, "0"))
    return int(moment.timestamp()) * SECOND_NS + fraction


_UNIX = re.compile(
    r"^(?P<kind>[-dlbcpsD])(?P<perm>[-rwxsStTlL?]{9})[+@.]?\s+\d+\s+(?:\S+\s+){1,2}?(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year_or_time>\d{4}|\d{1,2}:\d{2})\s(?P<name>.+)$"
)
_DOS = re.compile(r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}(?:AM|PM)?)\s+(?P<size><DIR>|\d+)\s+(?P<name>.+)$")


def parse_list_line(line: str) -> tuple[str, str, bool, bool, int] | None:
    """``(name, kind, is_dir, is_file, size)`` from one line of a ``LIST`` answer (Unix or Windows style), else ``None``.

    ``kind`` is ``"dir"``, ``"file"``, ``"link"`` or ``"other"``. Times in these lines are not used: they are local time,
    have no seconds, and often no year.
    """
    line = line.rstrip("\r\n")
    m = _UNIX.match(line)
    if m is not None:
        name = m.group("name")
        flag = m.group("kind")
        if flag == "l":
            return name.split(" -> ", 1)[0], "link", False, False, 0
        if flag == "d":
            return name, "dir", True, False, 0
        if flag == "-":
            return name, "file", False, True, int(m.group("size"))
        return name, "other", False, False, 0
    m = _DOS.match(line)
    if m is not None:
        if m.group("size") == "<DIR>":
            return m.group("name"), "dir", True, False, 0
        return m.group("name"), "file", False, True, int(m.group("size"))
    return None


def _facts(line: str) -> dict[str, str]:
    out = {}
    for fact in line.split(";"):
        key, eq, value = fact.partition("=")
        if eq:
            out[key.strip().lower()] = value
    return out


_MISSING_WORDS = ("no such", "not found", "does not exist", "doesn't exist", "cannot find", "not a directory")
_REFUSED_WORDS = ("permission", "denied", "not allowed", "access", "privilege", "forbidden", "read-only", "read only",
                  "not authorized", "unauthorized", "not authorised")  # fmt: skip


def _translate(exc: BaseException, what: str, *, writing: bool = False) -> OSError:
    """Turn what ftplib raises into the ``OSError`` the rest of Duplio understands.

    FTP answers ``550`` both for "no such file" and for "you may not"; the words in the answer decide, and when they
    do not, a refused write is reported as a refusal and a refused read as a missing file.
    """
    if isinstance(exc, OSError):
        return exc
    text = str(exc).strip()
    detail = f"{what}: {text}" if text else what
    if isinstance(exc, EOFError):
        return ConnectionResetError(errno.ECONNRESET, f"{what}: the server closed the connection")
    if isinstance(exc, ftplib.error_temp):
        code = text[:3]
        if code == "452":
            return OSError(errno.ENOSPC, detail)
        if code == "421":
            return ConnectionResetError(errno.ECONNRESET, detail)
        return ConnectionError(errno.ECONNRESET, detail + " (the server could not do this right now)")
    if isinstance(exc, ftplib.error_perm):
        code = text[:3]
        lowered = text.lower()
        if code == "552" or "disk full" in lowered or "quota" in lowered or "no space" in lowered:
            return OSError(errno.ENOSPC, detail)
        if code == "530":
            return PermissionError(errno.EACCES, detail + " (check the user name and password)")
        if "already exist" in lowered or "file exists" in lowered:
            return FileExistsError(errno.EEXIST, detail)
        if any(word in lowered for word in _MISSING_WORDS):
            return FileNotFoundError(errno.ENOENT, detail)
        if any(word in lowered for word in _REFUSED_WORDS):
            return PermissionError(errno.EACCES, detail)
        if code in ("550", "551", "553", "450"):
            return PermissionError(errno.EACCES, detail) if writing else FileNotFoundError(errno.ENOENT, detail)
        if code in ("502", "504", "500", "501"):
            return OSError(errno.ENOTSUP, detail + " (the server does not support this)")
        return OSError(errno.EIO, detail)
    if isinstance(exc, ftplib.Error):
        return OSError(errno.EIO, detail)
    if isinstance(exc, ValueError):
        return OSError(errno.EINVAL, detail)
    return OSError(errno.EIO, f"{what}: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- connections
class _Pool:
    """A small bounded set of logged-in connections. A connection is kept busy for as long as a transfer uses it."""

    def __init__(self, factory: Callable[[], ftplib.FTP], limit: int = MAX_CONNECTIONS) -> None:
        self._factory = factory
        self._limit = limit
        self._idle: list[ftplib.FTP] = []
        self._count = 0
        self._cond = threading.Condition()
        self._closed = False

    def acquire(self) -> tuple[ftplib.FTP, bool]:
        """``(connection, reused)``; waits for one if all are busy."""
        deadline = time.monotonic() + POOL_WAIT
        with self._cond:
            while True:
                if self._closed:
                    raise OSError(errno.EBADF, "the FTP connection was closed")
                if self._idle:
                    return self._idle.pop(), True
                if self._count < self._limit:
                    self._count += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError(errno.EBUSY, "all FTP connections are busy")
                self._cond.wait(remaining)
        try:
            return self._factory(), False
        except BaseException:
            with self._cond:
                self._count -= 1
                self._cond.notify()
            raise

    def release(self, conn: ftplib.FTP) -> None:
        conn.idle_since = time.monotonic()  # type: ignore[attr-defined]
        with self._cond:
            if not self._closed:
                self._idle.append(conn)
                self._cond.notify()
                return
            self._count -= 1
        _quit(conn)

    def discard(self, conn: ftplib.FTP) -> None:
        with self._cond:
            self._count -= 1
            self._cond.notify()
        with contextlib.suppress(Exception):
            conn.close()

    def drop_idle(self) -> None:
        with self._cond:
            idle, self._idle = self._idle, []
            self._count -= len(idle)
            self._cond.notify_all()
        for conn in idle:
            _quit(conn)

    def close(self) -> None:
        with self._cond:
            self._closed = True
        self.drop_idle()


def _binary(conn: ftplib.FTP) -> None:
    """Make sure the next transfer or SIZE is in binary mode. ``ftplib`` switches a connection to text mode for every
    listing and leaves it there; a file fetched in text mode can be silently altered (line ends rewritten)."""
    if not getattr(conn, "binary", False):
        conn.voidcmd("TYPE I")
        conn.binary = True  # type: ignore[attr-defined]


def _text_mode_used(conn: ftplib.FTP) -> None:
    conn.binary = False  # type: ignore[attr-defined]


def _quit(conn: ftplib.FTP) -> None:
    try:
        conn.sock.settimeout(2)  # type: ignore[union-attr]
        conn.quit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()


class _Download:
    """The data connection of one ``RETR``; the control connection returns to the pool when it is finished."""

    def __init__(self, backend: FtpBackend, conn: ftplib.FTP, data: socket.socket) -> None:
        self._backend = backend
        self._conn = conn
        self._sock = data
        self._file = data.makefile("rb")
        self._done = False

    def read(self, n: int = -1) -> bytes:
        if self._done:
            return b""
        try:
            data = self._file.read(n if n is not None and n >= 0 else None)
        except (OSError, ssl.SSLError) as exc:
            self._fail()
            raise ConnectionResetError(errno.ECONNRESET, f"the FTP data connection failed: {exc}") from exc
        if not data and n != 0:
            self._finish()
        return data

    def _close_data(self) -> None:
        with contextlib.suppress(Exception):
            self._file.close()
        with contextlib.suppress(Exception):
            self._sock.close()

    def _finish(self) -> None:
        """The server has sent everything: read its closing answer and return the connection."""
        self._done = True
        self._close_data()
        try:
            self._conn.voidresp()
        except Exception:
            self._backend._pool.discard(self._conn)
            return
        self._backend._pool.release(self._conn)

    def _fail(self) -> None:
        self._done = True
        self._close_data()
        self._backend._pool.discard(self._conn)

    def close(self) -> None:
        """Stop early: the server answers an abandoned transfer with 426 (or 226); anything else drops the connection."""
        if self._done:
            return
        self._done = True
        self._close_data()
        conn = self._conn
        try:
            assert conn.sock is not None
            conn.sock.settimeout(ABORT_WAIT)
            try:
                conn.getresp()
            except ftplib.error_temp:
                pass  # 426/450/451: "transfer aborted", which is what was asked for
            conn.sock.settimeout(self._backend._timeout)
        except Exception:
            self._backend._pool.discard(conn)
            return
        self._backend._pool.release(conn)

    def __enter__(self) -> _Download:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- the backend
class FtpBackend(ListingBackend):
    scheme = "ftp"

    def __init__(self, target: FtpTarget, *, credential_provider=None, timeout: float = 60.0, context=None) -> None:  # type: ignore[no-untyped-def]
        self.target = target
        self._credentials = credential_provider
        self._timeout = timeout
        self._context = context
        self._password = target.password
        self._pool = _Pool(self._connect)
        self._features: set[str] | None = None
        self._rest_ok = True
        self._mode = ""  # "mlsd" or "list"
        self._lock = threading.Lock()

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        port = "" if self.target.port == 21 else f":{self.target.port}"
        return f"{'ftps' if self.target.tls else 'ftp'}://{user}{host_for_url(self.target.host.lower())}{port}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(
            kind=MediaKind.NETWORK,
            label=f"FTP on {self.target.host}" + ("" if self.target.tls else " (NOT encrypted)"),
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=True,  # the cautious assumption
            mtime_resolution_ns=SECOND_NS,
            sampling=Sampling.RANDOM if self._rest_ok else Sampling.SEQUENTIAL,
            max_parallel_reads=2,
            fstype="ftps" if self.target.tls else "ftp",
            windows_names=True,
        )

    def uri(self, path: str) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        port = "" if self.target.port == 21 else f":{self.target.port}"
        scheme = "ftps" if self.target.tls else "ftp"
        return f"{scheme}://{user}{host_for_url(self.target.host)}{port}{quote_path(self.normalize(path))}"

    def display(self, path: str) -> str:
        return self.uri(path)

    # ---- connecting -----------------------------------------------------
    def _connect(self) -> ftplib.FTP:
        t = self.target
        try:
            if t.tls:
                conn: ftplib.FTP = ftplib.FTP_TLS(context=self._context or ssl.create_default_context(), encoding="utf-8")
            else:
                conn = ftplib.FTP(encoding="utf-8")
            conn.connect(t.host, t.port, timeout=self._timeout)
            user = t.username or "anonymous"
            if t.username and self._password is None:
                self._password = resolve_password("ftp", t.host, t.username, None, self._credentials)
            password = self._password if t.username else "anonymous@"
            if t.tls:
                assert isinstance(conn, ftplib.FTP_TLS)
                conn.auth()
            conn.login(user, password or "")
            if t.tls:
                assert isinstance(conn, ftplib.FTP_TLS)
                conn.prot_p()
            conn.set_pasv(True)
            _binary(conn)
            self._learn(conn)
            if "UTF8" in (self._features or set()):
                with contextlib.suppress(ftplib.Error):
                    conn.sendcmd("OPTS UTF8 ON")
            return conn
        except ftplib.error_perm as exc:
            if str(exc).startswith("530"):
                raise PermissionError(errno.EACCES, f"{t.host} refused the user name or password") from exc
            raise _translate(exc, f"connecting to {t.host}") from exc
        except (ftplib.Error, EOFError, ssl.SSLError) as exc:
            raise _translate(exc, f"connecting to {t.host}") from exc

    def _learn(self, conn: ftplib.FTP) -> None:
        with self._lock:
            if self._features is not None:
                return
            features: set[str] = set()
            try:
                answer = conn.sendcmd("FEAT")
                for line in answer.splitlines()[1:-1]:
                    word = line.strip().split(" ", 1)[0].upper()
                    if word:
                        features.add(word)
            except ftplib.Error:
                features = {"MDTM", "SIZE", "REST"}  # no FEAT: older server; assume the basics and find out by trying
            self._features = features
            if "MLST" in features:
                self._mode = "mlsd"
            elif "MDTM" in features:
                self._mode = "list"
            else:
                raise LocationError(
                    f"The FTP server {self.target.host} cannot report when files were changed (it has neither MLSD nor "
                    "MDTM), so Duplio cannot tell files apart safely there. Use SFTP or WebDAV on the same device, or "
                    "mount the drive and scan the mount point."
                )
            if "REST" not in features:
                self._rest_ok = False

    def recover(self) -> None:
        self._pool.drop_idle()

    def close(self) -> None:
        self._pool.close()

    def _checkout(self) -> ftplib.FTP:
        """A logged-in connection. One that sat idle for a while may have been closed by the server; that is checked
        first so the operation does not fail on it."""
        while True:
            conn, reused = self._pool.acquire()
            if reused and time.monotonic() - getattr(conn, "idle_since", 0.0) > STALE_AFTER:
                try:
                    conn.voidcmd("NOOP")
                except (ftplib.Error, OSError, EOFError):
                    self._pool.discard(conn)
                    continue
            return conn

    @contextlib.contextmanager
    def _use(self, what: str, *, writing: bool = False) -> Iterator[ftplib.FTP]:
        conn = self._checkout()
        healthy = False
        try:
            yield conn
            healthy = True
        except ftplib.error_perm as exc:
            healthy = True  # an answer like "550 no such file" leaves the connection fine
            raise _translate(exc, what, writing=writing) from exc
        except (ftplib.Error, EOFError, ValueError, ssl.SSLError) as exc:
            raise _translate(exc, what, writing=writing) from exc
        finally:
            if healthy:
                self._pool.release(conn)
            else:
                self._pool.discard(conn)

    # ---- listing and metadata -------------------------------------------
    def _list(self, path: str) -> list[Child]:
        return self._listing(path, times=True)

    def _list_names(self, path: str) -> list[Child]:
        return self._listing(path, times=False)

    def _listing(self, path: str, *, times: bool) -> list[Child]:
        path = self.normalize(path)
        with self._use(f"listing {path}") as conn:
            if self._mode == "mlsd":
                return self._mlsd(conn, path)
            return self._list_command(conn, path, times)

    def _mlsd(self, conn: ftplib.FTP, path: str) -> list[Child]:
        out: list[Child] = []
        _text_mode_used(conn)
        for name, facts in conn.mlsd(path, ["type", "size", "modify"]):
            kind = facts.get("type", "").lower()
            if kind in ("cdir", "pdir"):
                continue
            out.append(self._child_from_facts(name, facts))
        return out

    @staticmethod
    def _child_from_facts(name: str, facts: dict[str, str]) -> Child:
        kind = facts.get("type", "").lower()
        if kind == "dir":
            return Child(name=name, is_dir=True, is_file=False)
        if kind.startswith("os.unix=slink") or kind.startswith("os.unix=symlink"):
            return Child(name=name, is_dir=False, is_file=False, is_symlink=True)
        if kind != "file":
            return Child(name=name, is_dir=False, is_file=False)
        size = facts.get("size", "")
        mtime = parse_ftp_time(facts["modify"]) if "modify" in facts else None
        problem = None
        if not size.isdigit():
            problem = "the server did not report this file's size"
        elif mtime is None:
            problem = "the server did not report this file's modification time, which is needed to tell files apart safely"
        return Child(name=name, is_dir=False, is_file=True, size=int(size) if size.isdigit() else 0,
                     mtime_ns=mtime or 0, problem=problem)  # fmt: skip

    def _list_command(self, conn: ftplib.FTP, path: str, times: bool) -> list[Child]:
        lines: list[str] = []
        _text_mode_used(conn)
        conn.retrlines(f"LIST {path}", lines.append)
        out: list[Child] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("total "):
                continue
            parsed = parse_list_line(line)
            if parsed is None:
                out.append(Child(name="(unreadable listing line)", is_dir=False, is_file=True,
                                 problem=f"could not understand the server's listing line: {stripped[:80]!r}"))  # fmt: skip
                continue
            name, kind, is_dir, is_file, size = parsed
            if name in (".", ".."):
                continue
            if kind == "link":
                out.append(Child(name=name, is_dir=False, is_file=False, is_symlink=True))
            elif is_dir:
                out.append(Child(name=name, is_dir=True, is_file=False))
            elif is_file:
                mtime = None
                problem = None
                if times:
                    mtime = self._mdtm(conn, posixpath.join(path, name))
                    if mtime is None:
                        problem = "the server did not report this file's modification time"
                out.append(Child(name=name, is_dir=False, is_file=True, size=size, mtime_ns=mtime or 0, problem=problem))
            else:
                out.append(Child(name=name, is_dir=False, is_file=False))
        return out

    @staticmethod
    def _mdtm(conn: ftplib.FTP, path: str) -> int | None:
        try:
            answer = conn.voidcmd(f"MDTM {path}")
        except ftplib.error_perm:
            return None
        return parse_ftp_time(answer[4:])

    def stat(self, path: str) -> StatInfo | None:
        path = self.normalize(path)
        with self._use(f"looking up {path}") as conn:
            if path == "/":
                return StatInfo(size=0, mtime_ns=0, is_dir=True, is_file=False)
            if self._mode == "mlsd":
                try:
                    answer = conn.sendcmd(f"MLST {path}")
                except ftplib.error_perm as exc:
                    if str(exc).startswith(("550", "553", "450")):
                        return None
                    raise
                lines = answer.splitlines()
                fact_line = next((ln for ln in lines[1:] if ln.startswith(" ")), "")
                facts = _facts(fact_line.strip().rpartition(" ")[0] or fact_line.strip())
                child = self._child_from_facts(posixpath.basename(path), facts)
                if child.problem:
                    raise OSError(errno.EIO, f"{path}: {child.problem}")
                return StatInfo(size=child.size, mtime_ns=child.mtime_ns, is_dir=child.is_dir, is_file=child.is_file,
                                is_symlink=child.is_symlink)  # fmt: skip
            try:  # a folder can be entered; a file cannot
                conn.voidcmd(f"CWD {path}")
                return StatInfo(size=0, mtime_ns=0, is_dir=True, is_file=False)
            except ftplib.error_perm:
                pass
            try:
                _binary(conn)
                size = int(conn.voidcmd(f"SIZE {path}")[4:].strip())
            except ftplib.error_perm:
                return None
            mtime = self._mdtm(conn, path)
            if mtime is None:
                raise OSError(errno.EIO, f"{path}: the server did not report the file's modification time")
            return StatInfo(size=size, mtime_ns=mtime, is_dir=False, is_file=True)

    # ---- reading --------------------------------------------------------
    def _open_stream(self, path: str, pos: int) -> _Download:
        conn = self._checkout()
        try:
            use_rest = bool(pos) and self._rest_ok
            _binary(conn)
            try:
                data, _size = conn.ntransfercmd(f"RETR {path}", rest=pos if use_rest else None)
            except ftplib.error_perm as exc:
                if not (use_rest and str(exc)[:3] in ("500", "501", "502", "504")):
                    raise
                self._rest_ok = False  # no restart support: begin again and read through to the offset
                data, _size = conn.ntransfercmd(f"RETR {path}")
        except ftplib.error_perm as exc:
            self._pool.release(conn)
            raise _translate(exc, f"reading {path}") from exc
        except BaseException as exc:
            self._pool.discard(conn)
            if isinstance(exc, ftplib.Error | EOFError | ssl.SSLError | ValueError):
                raise _translate(exc, f"reading {path}") from exc
            raise
        stream = _Download(self, conn, data)
        if pos and not self._rest_ok:
            skipped = 0
            while skipped < pos:
                chunk = stream.read(min(1024 * 1024, pos - skipped))
                if not chunk:
                    stream.close()
                    raise ConnectionResetError(errno.ECONNRESET, "the server ended the transfer early")
                skipped += len(chunk)
        return stream

    def open_read(self, path: str) -> IO[bytes]:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder")
        return RangedReader(info.size, lambda pos: self._open_stream(path, pos), name=path)  # type: ignore[return-value]

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        path = self.normalize(path)
        try:
            stream = self._open_stream(path, offset)
        except OSError:
            info = self.stat(path)  # a restart point at or past the end of the file is refused by some servers
            if info is not None and info.is_file and offset >= info.size:
                return b""
            raise
        try:
            return stream.read(length)
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
            with self._use(f"creating the folder {folder}", writing=True) as conn:
                try:
                    conn.voidcmd(f"MKD {folder}")
                except ftplib.error_perm:
                    info = self.stat(folder)
                    if info is None or not info.is_dir:
                        raise

    def remove(self, path: str) -> None:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder; Duplio only deletes single files on an FTP server")
        with self._use(f"deleting {path}", writing=True) as conn:
            conn.voidcmd(f"DELE {path}")

    def _rename_raw(self, src: str, dst: str) -> None:
        with self._use(f"moving {src}", writing=True) as conn:
            conn.rename(src, dst)

    def rename(self, src: str, dst: str) -> None:
        dst = self.normalize(dst)
        if self.stat(dst) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        self.makedirs(self.parent(dst))
        self._rename_raw(self.normalize(src), dst)

    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        path = self.normalize(path)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", path)
        parent = self.parent(path)
        self.makedirs(parent)
        token = secrets.token_hex(4)
        tmp = posixpath.join(parent, temp_name(self.basename(path), f".duplio-tmp-{token}"))
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY) as spool:
            try:
                yield spool  # type: ignore[misc]
                size = spool.tell()
                spool.seek(0)
                with self._use(f"uploading {path}", writing=True) as conn:
                    conn.storbinary(f"STOR {tmp}", spool, blocksize=READ_BLOCK)
                    conn.binary = True  # type: ignore[attr-defined]
                held = self.stat(tmp)
                if held is None or held.size != size:
                    raise OSError(errno.EIO, f"the server holds {held.size if held else 'no'} bytes of {path}, expected {size}")
                self._put_in_place(tmp, path, overwrite=overwrite, token=token)
                stored = self.stat(path)
                if stored is None or not stored.is_file or stored.size != size:
                    raise OSError(errno.EIO, f"the server did not keep the file under the name {path} (it may not allow that name)")
            except BaseException:
                with contextlib.suppress(Exception):
                    if self.stat(tmp) is not None:
                        with self._use("removing a half-written file", writing=True) as conn:
                            conn.voidcmd(f"DELE {tmp}")
                raise

    def _put_in_place(self, tmp: str, dst: str, *, overwrite: bool, token: str) -> None:
        if not overwrite:
            if self.stat(dst) is not None:  # FTP cannot say "rename unless it exists": look just before
                raise FileExistsError(errno.EEXIST, "destination exists", dst)
            self._rename_raw(tmp, dst)
            return
        existing = self.stat(dst)
        if existing is None:
            self._rename_raw(tmp, dst)
            return
        # Replace by renaming over it where the server allows that; otherwise keep the old file aside until the new one
        # is in place, and put it back if anything goes wrong.
        try:
            self._rename_raw(tmp, dst)
            if self.stat(tmp) is None:
                return
        except OSError:
            pass
        aside = posixpath.join(self.parent(dst), temp_name(self.basename(dst), f".duplio-old-{token}"))
        self._rename_raw(dst, aside)
        try:
            self._rename_raw(tmp, dst)
        except BaseException:
            with contextlib.suppress(Exception):
                self._rename_raw(aside, dst)
            raise
        with contextlib.suppress(Exception), self._use("removing the replaced file", writing=True) as conn:
            conn.voidcmd(f"DELE {aside}")

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        if "MFMT" not in (self._features or set()):
            return
        stamp = datetime.fromtimestamp(mtime_ns // SECOND_NS, UTC).strftime("%Y%m%d%H%M%S")
        with contextlib.suppress(Exception), self._use("setting the time", writing=True) as conn:
            conn.voidcmd(f"MFMT {stamp} {self.normalize(path)}")

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        """Remove folders that are empty, checked first: FTP servers refuse to remove a folder with files in it, and the
        listing is looked at anyway so that no server is ever asked to remove something that has contents."""

        def remove(folder: str) -> bool:
            try:
                if self._list_names(folder):
                    return False
                with self._use(f"removing the folder {folder}", writing=True) as conn:
                    conn.voidcmd(f"RMD {folder}")
            except (OSError, LocationError):
                return False
            return True

        climb_removing(self, path, stop_at, remove)


__all__ = ["FtpBackend", "FtpTarget", "parse_ftp", "parse_ftp_time", "parse_list_line"]
