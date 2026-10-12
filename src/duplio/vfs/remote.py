"""Pieces shared by the network backends (WebDAV, FTP, SFTP).

All three address files by POSIX paths on the server, list a folder to find what is in it, and read through a
connection that can drop. What differs is the protocol, so this module holds the rest: the folder walker, the checks
on names a server sends back, password lookup, and a seekable reader built on "open a stream at this offset".
"""

from __future__ import annotations

import errno
import io
import os
import posixpath
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from duplio.errors import LocationError
from duplio.vfs.base import Backend, RawEntry, WalkFilter, WalkStats

MAX_DEPTH = 128  # a server whose folders contain themselves would otherwise be walked forever
SKIP_FORWARD = 256 * 1024  # a forward seek this short is read through instead of reopening the stream


@dataclass(frozen=True, slots=True)
class Child:
    """One entry of a remote folder listing."""

    name: str
    is_dir: bool
    is_file: bool
    size: int = 0
    mtime_ns: int = 0
    is_symlink: bool = False
    mode: int | None = None
    problem: str | None = None  # why this entry cannot be used safely (for instance: the server gave no modification time)


def unsafe_name(name: str) -> str | None:
    """Why a name a server sent cannot be a file name, or ``None`` when it can.

    Names become paths on this computer when a folder is copied or merged, so a server must not be able to hand over
    ``../../x`` or a name with a path separator in it.
    """
    if not name:
        return "empty name"
    if name in (".", ".."):
        return "name is a folder reference"
    if "/" in name or "\x00" in name:
        return "name contains a path separator or NUL"
    return None


class ListingBackend(Backend):
    """A backend that can list one folder at a time (:meth:`_list`); the walk, :meth:`listdir` and path algebra follow."""

    sep = "/"

    def _list(self, path: str) -> list[Child]:  # pragma: no cover - interface
        raise NotImplementedError

    # ---- path algebra (absolute POSIX paths on the server) --------------
    def normalize(self, path: str) -> str:
        # A backslash is an ordinary character in a name on these servers: never turned into a separator, or the file
        # `a\b.txt` would be read as `a/b.txt`, a different file.
        return posixpath.normpath("/" + path.lstrip("/")) if path else "/"

    def parent(self, path: str) -> str:
        norm = self.normalize(path)
        return posixpath.dirname(norm) or "/"

    def basename(self, path: str) -> str:
        return posixpath.basename(self.normalize(path))

    def join(self, base: str, rel: str) -> str:
        if not rel:
            return base
        return posixpath.join(base, rel)

    # ---- reading --------------------------------------------------------
    def _list_names(self, path: str) -> list[Child]:
        """Like :meth:`_list` but the entries only need names and kinds (sizes and times may be left out)."""
        return self._list(path)

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        return [(c.name, c.is_dir) for c in self._list_names(self.normalize(path)) if unsafe_name(c.name) is None]

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
        stack: list[tuple[str, str, int]] = [(self.normalize(root), "", 0)]
        while stack:
            if should_stop is not None and should_stop():
                return
            dir_path, dir_rel, depth = stack.pop()
            if depth > MAX_DEPTH:
                stats.errors.append((dir_path, f"folders are nested more than {MAX_DEPTH} deep; not followed"))
                continue
            try:
                children = self._list(dir_path)
            except OSError as exc:
                stats.errors.append((dir_path, str(exc) or type(exc).__name__))
                continue
            stats.dirs += 1
            subdirs: list[tuple[str, str, int]] = []
            for child in children:
                name = child.name
                path = posixpath.join(dir_path, name) if name else dir_path
                bad = unsafe_name(name)
                if bad is not None:
                    if name not in (".", ".."):
                        stats.errors.append((path, f"skipped: {bad}"))
                    continue
                rel = f"{dir_rel}/{name}" if dir_rel else name
                if child.problem is not None and not child.is_dir:
                    stats.errors.append((path, child.problem))
                    continue
                if child.is_symlink:
                    stats.symlinks_skipped += 1
                    continue
                if child.is_dir:
                    if walk_filter is not None and walk_filter.skip_dir(rel, name):
                        stats.filtered += 1
                        continue
                    subdirs.append((path, rel, depth + 1))
                    continue
                if not child.is_file:
                    stats.special_skipped += 1
                    continue
                if walk_filter is not None and walk_filter.skip_file(rel, name, child.size):
                    stats.filtered += 1
                    continue
                stats.files += 1
                stats.bytes += child.size
                yield RawEntry(path=path, rel=rel, size=child.size, mtime_ns=child.mtime_ns)
            subdirs.sort(reverse=True)
            stack.extend(subdirs)


def climb_removing(backend: ListingBackend, path: str, stop_at: str, remove_one: Callable[[str], bool]) -> None:
    """Remove ``path`` and then each parent below ``stop_at`` for as long as ``remove_one`` says the folder was removed."""
    stop = backend.normalize(stop_at)
    current = backend.normalize(path)
    while current != stop and backend.is_within(current, stop):
        if not remove_one(current):
            return
        current = backend.parent(current)


# --------------------------------------------------------------------------- addresses and passwords
@dataclass(frozen=True)
class Authority:
    username: str | None
    password: str | None
    host: str
    port: int | None


def split_authority(text: str, what: str) -> Authority:
    """``[user[:password]@]host[:port]`` (IPv6 literals in brackets); percent-escapes in user and password are decoded."""
    from urllib.parse import unquote

    userinfo, at, hostport = text.rpartition("@")
    if not at:
        userinfo, hostport = "", text
    username = password = None
    if userinfo:
        user_part, colon, pw = userinfo.partition(":")
        username = unquote(user_part) or None
        if colon:
            password = unquote(pw)
    host, port = hostport, None
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0:
            raise LocationError(f"{what} address has an unclosed '[': {text!r}")
        host = hostport[1:end]
        rest = hostport[end + 1 :]
        if rest.startswith(":") and rest[1:]:
            port = _port(rest[1:], what, text)
    elif ":" in hostport:
        host, _, port_text = hostport.rpartition(":")
        port = _port(port_text, what, text)
    if not host:
        raise LocationError(f"{what} location needs a server name: {text!r}")
    return Authority(username, password, host, port)


def _port(text: str, what: str, whole: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise LocationError(f"invalid {what} port in {whole!r}") from exc
    if not 0 < port < 65536:
        raise LocationError(f"invalid {what} port in {whole!r}")
    return port


def host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def resolve_password(
    scheme: str, host: str, username: str | None, explicit: str | None, provider: Callable[[str, str | None], str | None] | None
) -> str | None:
    """The password, from the location itself, ``DUPLIO_<SCHEME>_PASSWORD``, the system keyring, or a prompt callback.

    Passwords are never written to disk by Duplio.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get(f"DUPLIO_{scheme.upper()}_PASSWORD")
    if env:
        return env
    if username:
        try:
            import keyring

            stored = keyring.get_password(f"duplio-{scheme}:{host}", username)
            if stored:
                return stored
        except Exception:  # keyring is optional and may be misconfigured
            pass
        if provider is not None:
            return provider(host, username)
    return None


def quote_path(path: str) -> str:
    from urllib.parse import quote

    return quote(path, safe="/", errors="surrogateescape")  # names that are not valid UTF-8 survive the round trip


# --------------------------------------------------------------------------- a seekable reader
class Stream(Protocol):
    """What :class:`RangedReader` needs from the thing that does the transfer."""

    def read(self, n: int = -1) -> bytes: ...

    def close(self) -> None: ...


class RangedReader(io.RawIOBase):
    """A read-only, seekable file over ``opener(offset) -> stream``: the stream is opened where it is needed.

    A short forward seek is read through; any other seek closes the stream and opens a new one at the new place, so
    sampling the head, middle and tail of a big file costs three small requests instead of a download. A stream that
    ends before the size it was opened for is an error: a connection that dropped must not look like a short file.
    """

    def __init__(self, size: int, opener: Callable[[int], Stream], *, name: str = "") -> None:
        super().__init__()
        self._size = size
        self._opener = opener
        self._pos = 0
        self._stream: Stream | None = None
        self._stream_pos = 0
        self.name = name

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if target < 0:
            raise OSError(errno.EINVAL, "negative seek position")
        self._pos = target
        return target

    def _stream_at(self, pos: int) -> Stream:
        stream = self._stream
        if stream is not None:
            if self._stream_pos == pos:
                return stream
            if 0 < pos - self._stream_pos <= SKIP_FORWARD:
                skip = pos - self._stream_pos
                while skip:
                    got = stream.read(min(skip, 65536))
                    if not got:
                        break
                    skip -= len(got)
                if not skip:
                    self._stream_pos = pos
                    return stream
            self._drop_stream()
        self._stream = self._opener(pos)
        self._stream_pos = pos
        return self._stream

    def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def readinto(self, b) -> int:  # type: ignore[no-untyped-def]
        if self.closed:
            raise ValueError("I/O operation on closed file")
        want = min(len(b), self._size - self._pos)
        if want <= 0:
            return 0
        stream = self._stream_at(self._pos)
        data = stream.read(want)
        if not data:
            self._drop_stream()
            raise ConnectionResetError(errno.ECONNRESET, f"the server ended the transfer early at byte {self._pos}")
        b[: len(data)] = data
        self._pos += len(data)
        self._stream_pos = self._pos
        return len(data)

    def read(self, size: int = -1) -> bytes:
        """Read ``size`` bytes (everything left when negative); fewer only at the end of the file."""
        if size is None or size < 0:
            size = max(self._size - self._pos, 0)
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            buf = bytearray(min(remaining, 1024 * 1024))
            got = self.readinto(buf)
            if not got:
                break
            chunks.append(bytes(buf[:got]))
            remaining -= got
        return b"".join(chunks)

    def close(self) -> None:
        self._drop_stream()
        super().close()
