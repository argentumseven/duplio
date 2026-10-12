"""A small SFTP client (protocol version 3, the one every SFTP server speaks) over any pair of byte streams.

SFTP is a request/answer protocol: every request carries a number and the answer repeats it, so many requests can be
in flight at once. That is what makes it fast over a slow link, and it is why reading here keeps a window of reads
outstanding instead of asking for one block at a time.

This module knows nothing about how the bytes travel; :mod:`duplio.vfs.sftp` runs it over the ``ssh`` program.
"""

from __future__ import annotations

import errno
import stat as stat_mod
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# ---- packet types
INIT, VERSION, OPEN, CLOSE, READ, WRITE, LSTAT, FSTAT, SETSTAT, FSETSTAT = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
OPENDIR, READDIR, REMOVE, MKDIR, RMDIR, REALPATH, STAT, RENAME, READLINK, SYMLINK = 11, 12, 13, 14, 15, 16, 17, 18, 19, 20
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
EXTENDED, EXTENDED_REPLY = 200, 201

# ---- status codes
FX_OK, FX_EOF, FX_NO_SUCH_FILE, FX_PERMISSION_DENIED, FX_FAILURE = 0, 1, 2, 3, 4
FX_BAD_MESSAGE, FX_NO_CONNECTION, FX_CONNECTION_LOST, FX_OP_UNSUPPORTED = 5, 6, 7, 8

# ---- open flags and attribute flags
O_READ, O_WRITE, O_APPEND, O_CREAT, O_TRUNC, O_EXCL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20
A_SIZE, A_UIDGID, A_PERMISSIONS, A_ACMODTIME, A_EXTENDED = 0x1, 0x2, 0x4, 0x8, 0x80000000

MAX_PACKET = 8 * 1024 * 1024  # a server announcing more than this is broken or hostile
PROTOCOL_VERSION = 3


class ProtocolError(OSError):
    """The server sent something that is not valid SFTP."""

    def __init__(self, message: str) -> None:
        super().__init__(errno.EPROTO, message)


@dataclass(frozen=True, slots=True)
class Attrs:
    size: int | None = None
    uid: int | None = None
    gid: int | None = None
    permissions: int | None = None
    atime: int | None = None
    mtime: int | None = None

    @property
    def is_dir(self) -> bool:
        return self.permissions is not None and stat_mod.S_ISDIR(self.permissions)

    @property
    def is_file(self) -> bool:
        return self.permissions is not None and stat_mod.S_ISREG(self.permissions)

    @property
    def is_symlink(self) -> bool:
        return self.permissions is not None and stat_mod.S_ISLNK(self.permissions)


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: bytes
    attrs: Attrs


# --------------------------------------------------------------------------- encoding
def u32(value: int) -> bytes:
    return struct.pack(">I", value)


def u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def string(value: bytes) -> bytes:
    return u32(len(value)) + value


def encode_attrs(attrs: Attrs | None) -> bytes:
    if attrs is None:
        return u32(0)
    flags = 0
    body = b""
    if attrs.size is not None:
        flags |= A_SIZE
        body += u64(attrs.size)
    if attrs.uid is not None and attrs.gid is not None:
        flags |= A_UIDGID
        body += u32(attrs.uid) + u32(attrs.gid)
    if attrs.permissions is not None:
        flags |= A_PERMISSIONS
        body += u32(attrs.permissions)
    if attrs.atime is not None and attrs.mtime is not None:
        flags |= A_ACMODTIME
        body += u32(attrs.atime & 0xFFFFFFFF) + u32(attrs.mtime & 0xFFFFFFFF)
    return u32(flags) + body


class Reader:
    """Cursor over a packet's bytes; running past the end is a protocol error, never an IndexError."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ProtocolError("the server's answer was cut short")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack(">Q", self.take(8))[0])

    def string(self) -> bytes:
        return self.take(self.u32())

    def attrs(self) -> Attrs:
        flags = self.u32()
        size = uid = gid = permissions = atime = mtime = None
        if flags & A_SIZE:
            size = self.u64()
        if flags & A_UIDGID:
            uid, gid = self.u32(), self.u32()
        if flags & A_PERMISSIONS:
            permissions = self.u32()
        if flags & A_ACMODTIME:
            atime, mtime = self.u32(), self.u32()
        if flags & A_EXTENDED:
            for _ in range(self.u32()):
                self.string()
                self.string()
        return Attrs(size, uid, gid, permissions, atime, mtime)

    @property
    def left(self) -> int:
        return len(self.data) - self.pos


def status_error(code: int, message: str, what: str) -> OSError:
    """The ``OSError`` that fits an SFTP status."""
    detail = f"{what}: {message}" if message else what
    if code == FX_NO_SUCH_FILE:
        return FileNotFoundError(errno.ENOENT, detail)
    if code == FX_PERMISSION_DENIED:
        return PermissionError(errno.EACCES, detail)
    if code in (FX_NO_CONNECTION, FX_CONNECTION_LOST):
        return ConnectionResetError(errno.ECONNRESET, detail)
    if code == FX_OP_UNSUPPORTED:
        return OSError(errno.ENOTSUP, detail + " (the server does not support this)")
    lowered = message.lower()
    if "no space" in lowered or "quota" in lowered or "disk full" in lowered:
        return OSError(errno.ENOSPC, detail)
    if "exist" in lowered and ("already" in lowered or "file exists" in lowered):
        return FileExistsError(errno.EEXIST, detail)
    return OSError(errno.EIO, detail)


# --------------------------------------------------------------------------- the session
class Pending:
    """An answer that has not arrived yet."""

    __slots__ = ("_event", "error", "payload", "ptype", "what")

    def __init__(self, what: str) -> None:
        self._event = threading.Event()
        self.error: BaseException | None = None
        self.ptype = 0
        self.payload = b""
        self.what = what

    def _set(self, ptype: int, payload: bytes) -> None:
        self.ptype, self.payload = ptype, payload
        self._event.set()

    def _fail(self, exc: BaseException) -> None:
        self.error = exc
        self._event.set()

    def wait(self, timeout: float) -> tuple[int, bytes]:
        if not self._event.wait(timeout):
            raise TimeoutError(errno.ETIMEDOUT, f"{self.what}: no answer from the server within {timeout:.0f} s")
        if self.error is not None:
            raise self.error
        return self.ptype, self.payload


class SftpSession:
    """One SFTP conversation. ``read_exact(n)`` must return exactly ``n`` bytes or raise ``EOFError``;
    ``write_all(data)`` sends everything; ``close()`` ends the connection."""

    def __init__(
        self,
        read_exact: Callable[[int], bytes],
        write_all: Callable[[bytes], None],
        close: Callable[[], None],
        *,
        timeout: float = 120.0,
        describe_failure: Callable[[], str] | None = None,
    ) -> None:
        self._read_exact = read_exact
        self._write_all = write_all
        self._close = close
        self.timeout = timeout
        self._describe_failure = describe_failure or (lambda: "")
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending: dict[int, Pending] = {}
        self._next_id = 1
        self._dead: BaseException | None = None
        self.extensions: dict[str, bytes] = {}
        self._thread: threading.Thread | None = None

    # ---- connection ------------------------------------------------------
    def start(self) -> None:
        """Say hello and read the server's version. Raises ``OSError`` when the server does not speak SFTP."""
        try:
            self._send_packet(INIT, u32(PROTOCOL_VERSION))
            ptype, data = self._read_packet()
        except ProtocolError:
            self._close_quietly()
            raise
        except (EOFError, OSError) as exc:
            self._close_quietly()
            extra = self._describe_failure()
            raise ConnectionResetError(errno.ECONNRESET, extra or "the connection closed before the SFTP greeting") from exc
        if ptype != VERSION:
            self._close_quietly()
            raise ProtocolError(f"expected an SFTP greeting, got packet type {ptype}")
        r = Reader(data)
        version = r.u32()
        if version < 3:
            self._close_quietly()
            raise ProtocolError(f"the server speaks SFTP version {version}; version 3 or newer is needed")
        while r.left:
            name = r.string()
            self.extensions[name.decode("ascii", "replace")] = r.string()
        self._thread = threading.Thread(target=self._reader, name="duplio-sftp-reader", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._dead is None

    def _close_quietly(self) -> None:
        try:
            self._close()
        except Exception:
            pass

    def close(self) -> None:
        self._fail(ConnectionResetError(errno.ECONNRESET, "the SFTP connection was closed"))

    def _fail(self, exc: BaseException) -> None:
        with self._plock:
            if self._dead is None:
                self._dead = exc
            pending, self._pending = list(self._pending.values()), {}
        self._close_quietly()
        for p in pending:
            p._fail(self._dead)

    # ---- packets ---------------------------------------------------------
    def _send_packet(self, ptype: int, body: bytes) -> None:
        packet = u32(len(body) + 1) + bytes([ptype]) + body
        with self._wlock:
            self._write_all(packet)

    def _read_packet(self) -> tuple[int, bytes]:
        (length,) = struct.unpack(">I", self._read_exact(4))
        if length < 1 or length > MAX_PACKET:
            raise ProtocolError(f"the server announced a packet of {length} bytes")
        data = self._read_exact(length)
        return data[0], data[1:]

    def _reader(self) -> None:
        try:
            while True:
                ptype, data = self._read_packet()
                if len(data) < 4:
                    raise ProtocolError("a packet without a request number")
                (request_id,) = struct.unpack(">I", data[:4])
                with self._plock:
                    pending = self._pending.pop(request_id, None)
                if pending is not None:
                    pending._set(ptype, data[4:])
                # An answer nobody waits for any more (the reader was closed early) is dropped.
        except BaseException as exc:
            if self._dead is None:
                extra = self._describe_failure()
                if isinstance(exc, EOFError):
                    exc = ConnectionResetError(errno.ECONNRESET, extra or "the SFTP connection ended")
                elif extra and isinstance(exc, OSError) and not isinstance(exc, ProtocolError):
                    exc = ConnectionResetError(errno.ECONNRESET, f"{exc} ({extra})")
            self._fail(exc)

    def submit(self, ptype: int, body: bytes, what: str) -> Pending:
        pending = Pending(what)
        with self._plock:
            if self._dead is not None:
                raise self._dead if isinstance(self._dead, OSError) else ConnectionResetError(errno.ECONNRESET, str(self._dead))
            request_id = self._next_id
            self._next_id = (self._next_id % 0xFFFFFFFF) + 1
            self._pending[request_id] = pending
        try:
            self._send_packet(ptype, u32(request_id) + body)
        except BaseException as exc:
            with self._plock:
                self._pending.pop(request_id, None)
            self._fail(exc if isinstance(exc, OSError) else ConnectionResetError(errno.ECONNRESET, str(exc)))
            raise
        return pending

    def call(self, ptype: int, body: bytes, what: str) -> tuple[int, bytes]:
        return self.submit(ptype, body, what).wait(self.timeout)

    # ---- answers ---------------------------------------------------------
    @staticmethod
    def raise_status(payload: bytes, what: str) -> None:
        r = Reader(payload)
        code = r.u32()
        message = r.string().decode("utf-8", "replace") if r.left >= 4 else ""
        if code != FX_OK:
            raise status_error(code, message, what)

    def expect_ok(self, ptype: int, payload: bytes, what: str) -> None:
        if ptype != STATUS:
            raise ProtocolError(f"{what}: unexpected answer type {ptype}")
        self.raise_status(payload, what)

    def _expect(self, ptype: int, payload: bytes, wanted: int, what: str) -> bytes:
        if ptype == STATUS:
            self.raise_status(payload, what)
            raise ProtocolError(f"{what}: the server answered 'ok' with no data")
        if ptype != wanted:
            raise ProtocolError(f"{what}: unexpected answer type {ptype}")
        return payload

    # ---- operations ------------------------------------------------------
    def open(self, path: bytes, flags: int, attrs: Attrs | None = None) -> bytes:
        what = f"opening {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(OPEN, string(path) + u32(flags) + encode_attrs(attrs), what)
        return Reader(self._expect(ptype, payload, HANDLE, what)).string()

    def close_handle(self, handle: bytes) -> None:
        ptype, payload = self.call(CLOSE, string(handle), "closing a file")
        self.expect_ok(ptype, payload, "closing a file")

    def read_async(self, handle: bytes, offset: int, length: int) -> Pending:
        return self.submit(READ, string(handle) + u64(offset) + u32(length), "reading")

    def read_result(self, pending: Pending) -> bytes:
        """The bytes of a read; empty at the end of the file."""
        ptype, payload = pending.wait(self.timeout)
        if ptype == STATUS:
            r = Reader(payload)
            code = r.u32()
            if code == FX_EOF:
                return b""
            message = r.string().decode("utf-8", "replace") if r.left >= 4 else ""
            raise status_error(code, message, "reading")
        if ptype != DATA:
            raise ProtocolError(f"reading: unexpected answer type {ptype}")
        return Reader(payload).string()

    def write_async(self, handle: bytes, offset: int, data: bytes | memoryview) -> Pending:
        return self.submit(WRITE, string(handle) + u64(offset) + u32(len(data)) + bytes(data), "writing")

    def write_result(self, pending: Pending) -> None:
        ptype, payload = pending.wait(self.timeout)
        self.expect_ok(ptype, payload, "writing")

    def _attrs_call(self, ptype: int, body: bytes, what: str) -> Attrs:
        answer, payload = self.call(ptype, body, what)
        return Reader(self._expect(answer, payload, ATTRS, what)).attrs()

    def lstat(self, path: bytes) -> Attrs:
        return self._attrs_call(LSTAT, string(path), f"looking up {path.decode('utf-8', 'replace')}")

    def stat(self, path: bytes) -> Attrs:
        return self._attrs_call(STAT, string(path), f"looking up {path.decode('utf-8', 'replace')}")

    def fstat(self, handle: bytes) -> Attrs:
        return self._attrs_call(FSTAT, string(handle), "looking up an open file")

    def setstat(self, path: bytes, attrs: Attrs) -> None:
        what = f"changing {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(SETSTAT, string(path) + encode_attrs(attrs), what)
        self.expect_ok(ptype, payload, what)

    def listdir(self, path: bytes) -> list[DirEntry]:
        what = f"listing {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(OPENDIR, string(path), what)
        handle = Reader(self._expect(ptype, payload, HANDLE, what)).string()
        entries: list[DirEntry] = []
        try:
            while True:
                ptype, payload = self.call(READDIR, string(handle), what)
                if ptype == STATUS:
                    r = Reader(payload)
                    code = r.u32()
                    if code == FX_EOF:
                        break
                    message = r.string().decode("utf-8", "replace") if r.left >= 4 else ""
                    raise status_error(code, message, what)
                if ptype != NAME:
                    raise ProtocolError(f"{what}: unexpected answer type {ptype}")
                r = Reader(payload)
                for _ in range(r.u32()):
                    name = r.string()
                    r.string()  # the "long name" (an ls -l line): free text, not trusted or used
                    entries.append(DirEntry(name, r.attrs()))
        finally:
            try:
                self.close_handle(handle)
            except OSError:
                pass
        return entries

    def remove(self, path: bytes) -> None:
        what = f"deleting {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(REMOVE, string(path), what)
        self.expect_ok(ptype, payload, what)

    def mkdir(self, path: bytes) -> None:
        what = f"creating the folder {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(MKDIR, string(path) + encode_attrs(None), what)
        self.expect_ok(ptype, payload, what)

    def rmdir(self, path: bytes) -> None:
        """Remove an EMPTY folder; the server refuses when anything is inside."""
        what = f"removing the folder {path.decode('utf-8', 'replace')}"
        ptype, payload = self.call(RMDIR, string(path), what)
        self.expect_ok(ptype, payload, what)

    def rename(self, src: bytes, dst: bytes) -> None:
        """Move without replacing: protocol version 3 says renaming onto an existing name fails."""
        what = f"moving {src.decode('utf-8', 'replace')}"
        ptype, payload = self.call(RENAME, string(src) + string(dst), what)
        self.expect_ok(ptype, payload, what)

    def posix_rename(self, src: bytes, dst: bytes) -> None:
        """Move and replace atomically (the ``posix-rename@openssh.com`` extension)."""
        what = f"replacing {dst.decode('utf-8', 'replace')}"
        body = string(b"posix-rename@openssh.com") + string(src) + string(dst)
        ptype, payload = self.call(EXTENDED, body, what)
        self.expect_ok(ptype, payload, what)

    def free_space(self, path: bytes) -> int | None:
        """Bytes available to this user (the ``statvfs@openssh.com`` extension), or ``None``."""
        if "statvfs@openssh.com" not in self.extensions:
            return None
        ptype, payload = self.call(EXTENDED, string(b"statvfs@openssh.com") + string(path), "asking for free space")
        if ptype != EXTENDED_REPLY:
            return None
        r = Reader(payload)
        values = [r.u64() for _ in range(11)]
        frsize, bavail = values[1], values[4]
        return bavail * (frsize or values[0])

    def wait_all(self, pendings: list[Pending]) -> None:
        deadline = time.monotonic() + self.timeout
        for p in pendings:
            p.wait(max(deadline - time.monotonic(), 1.0))
