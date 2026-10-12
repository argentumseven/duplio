"""WebDAV: Nextcloud, ownCloud, Synology and QNAP "WebDAV", Apache/nginx DAV, many cloud drives' DAV gateways.

Location syntax::

    davs://[user@]host[:port]/path     encrypted (https); the usual choice
    dav://[user@]host[:port]/path      not encrypted (http); only for a trusted home network
    https://host/path                  the same as davs://   (http:// is the same as dav://)

Built on the standard library only (``http.client``), so nothing has to be installed. Passwords are never stored:
they come from ``DUPLIO_DAV_PASSWORD``, the optional ``keyring`` package, or a prompt supplied by the UI. Basic and
Digest authentication are supported.

How it behaves:

* Listing is one ``PROPFIND`` per folder. A file that the server gives no size or no modification time for is
  reported as unreadable rather than guessed at, because both are part of how Duplio tells files apart.
* Reading uses ranged ``GET`` requests, so a thumbnail or a sample of a big file costs kilobytes.
* Writing uploads to a hidden temporary name, checks the size the server holds, then ``MOVE`` s it into place without
  overwriting anything unless asked. Deleting refuses folders (``DELETE`` on a folder would remove everything in it).
* A server cannot be told to keep a file's original modification time, so copies there carry the upload time.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import http.client
import posixpath
import re
import secrets
import ssl
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import IO, Protocol, cast
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

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

DAV = "{DAV:}"
MAX_REDIRECTS = 5
SPOOL_MEMORY = 8 * 1024 * 1024
IDLE_CONNECTIONS = 4

_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>"
)
_QUOTA_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:quota-available-bytes/></d:prop></d:propfind>"
)


@dataclass(frozen=True)
class DavTarget:
    host: str
    port: int
    tls: bool
    path: str  # absolute path on the server, decoded
    username: str | None = None
    password: str | None = None


def parse_dav(text: str) -> DavTarget:
    lowered = text.lower()
    for prefix, secure in (("davs://", True), ("dav://", False), ("https://", True), ("http://", False)):
        if lowered.startswith(prefix):
            rest, tls = text[len(prefix) :], secure
            break
    else:
        raise LocationError(f"not a WebDAV location: {text!r}")
    authority, slash, path = rest.partition("/")
    parts = split_authority(authority, "WebDAV")
    # A pasted web address may carry ?query or #fragment parts that are not part of a folder name.
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    decoded = posixpath.normpath("/" + unquote(path, errors="surrogateescape")) if slash else "/"
    return DavTarget(
        host=parts.host,
        port=parts.port or (443 if tls else 80),
        tls=tls,
        path=decoded,
        username=parts.username,
        password=parts.password,
    )


# --------------------------------------------------------------------------- authentication
_PARAM = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|([^,\s]*))')


def _params(text: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM.finditer(text)}


class _Auth:
    """Basic or Digest credentials, chosen from what the server asks for."""

    def __init__(self, host: str, username: str | None, password: str | None, provider) -> None:  # type: ignore[no-untyped-def]
        self._host = host
        self._username = username
        self._password = password
        self._provider = provider
        self._lock = threading.Lock()
        self.mode: str | None = None
        self._digest: dict[str, str] = {}
        self._nc = 0
        self._failed_with: tuple[str, str] | None = None

    def _credentials(self) -> tuple[str, str] | None:
        if not self._username:
            return None
        if self._password is None:
            self._password = resolve_password("dav", self._host, self._username, None, self._provider)
        if self._password is None:
            return None
        return self._username, self._password

    def accept_challenge(self, challenges: list[str]) -> bool:
        """Take note of a 401's ``WWW-Authenticate``; ``True`` when the request is worth repeating with credentials."""
        creds = self._credentials()
        if creds is None:
            return False
        digest = next((c for c in challenges if c.lstrip().lower().startswith("digest")), None)
        basic = next((c for c in challenges if c.lstrip().lower().startswith("basic")), None)
        with self._lock:
            if digest is not None:
                params = _params(digest.lstrip()[6:])
                stale = params.get("stale", "").lower() == "true"
                if self.mode == "digest" and not stale and self._digest.get("nonce") == params.get("nonce"):
                    return False  # the same challenge again: the credentials were refused
                self.mode = "digest"
                self._digest = params
                self._nc = 0
                return "nonce" in params
            if basic is not None:
                if self.mode == "basic":
                    return False  # already sent: refused
                self.mode = "basic"
                return True
        return False

    def header(self, method: str, uri: str) -> str | None:
        creds = self._credentials() if self.mode else None
        if creds is None:
            return None
        user, password = creds
        if self.mode == "basic":
            return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        with self._lock:
            d = dict(self._digest)
            self._nc += 1
            nc = f"{self._nc:08x}"
        algorithm = d.get("algorithm", "MD5")
        base = algorithm.upper().removesuffix("-SESS")
        name = {"MD5": "md5", "SHA-256": "sha256", "SHA-512-256": "sha512_256"}.get(base)
        if name is None:
            return None

        def h(text: str) -> str:
            return hashlib.new(name, text.encode("utf-8"), usedforsecurity=False).hexdigest()

        cnonce = secrets.token_hex(8)
        realm, nonce = d.get("realm", ""), d.get("nonce", "")
        ha1 = h(f"{user}:{realm}:{password}")
        if algorithm.upper().endswith("-SESS"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        qops = [q.strip() for q in d.get("qop", "").split(",") if q.strip()]
        if "auth" in qops:
            response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        elif not qops:
            response = h(f"{ha1}:{nonce}:{ha2}")
        else:
            return None  # only auth-int is offered; not supported
        fields = [
            f'username="{_q(user)}"', f'realm="{_q(realm)}"', f'nonce="{_q(nonce)}"', f'uri="{_q(uri)}"',
            f'response="{response}"', f"algorithm={algorithm}",
        ]  # fmt: skip
        if qops:
            fields += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
        if d.get("opaque"):
            fields.append(f'opaque="{_q(d["opaque"])}"')
        return "Digest " + ", ".join(fields)


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- one response body
class _Body:
    """The body of a response as a file; the connection goes back to the pool once it has been read to the end."""

    def __init__(self, backend: DavBackend, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse) -> None:
        self._backend = backend
        self._conn = conn
        self._resp = resp
        self._done = False

    def read(self, n: int = -1) -> bytes:
        if self._done:
            return b""
        try:
            data = self._resp.read(n if n is not None and n >= 0 else None)
        except (OSError, http.client.HTTPException) as exc:
            self._discard()
            raise _as_oserror(exc) from exc
        if (not data and n != 0) or self._resp.isclosed():
            self._release()
        return data

    def _release(self) -> None:
        if self._done:
            return
        self._done = True
        if self._resp.isclosed() and not self._resp.will_close:
            self._backend._checkin(self._conn)
        else:
            self._conn.close()

    def _discard(self) -> None:
        self._done = True
        self._conn.close()

    def close(self) -> None:
        if not self._done:
            self._discard()  # not fully read: the connection is in an unknown state

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _as_oserror(exc: BaseException) -> OSError:
    if isinstance(exc, OSError):
        return exc
    return ConnectionResetError(errno.ECONNRESET, f"the server closed the connection ({type(exc).__name__}: {exc})")


def _check_status(status: int, reason: str, what: str) -> None:
    """Raise the ``OSError`` that fits an unexpected HTTP status."""
    detail = f"{what}: {status} {reason}".strip()
    if status == 404 or status == 410:
        raise FileNotFoundError(errno.ENOENT, detail)
    if status in (401, 403):
        raise PermissionError(errno.EACCES, detail + (" (check the user name and password)" if status == 401 else ""))
    if status == 412:
        raise FileExistsError(errno.EEXIST, detail)
    if status in (507, 413):
        raise OSError(errno.ENOSPC, detail)
    if status == 423:
        raise OSError(errno.EBUSY, detail + " (the file is locked on the server)")
    if status in (408, 429, 502, 503, 504):
        raise ConnectionError(errno.ECONNRESET, detail + " (the server is busy or unreachable)")
    if status == 405 or status == 501:
        raise OSError(errno.ENOTSUP, detail + " (the server does not allow this)")
    raise OSError(errno.EIO, detail)


def parse_http_date(text: str | None) -> int | None:
    """Nanoseconds since the epoch from an HTTP date (or an ISO 8601 one, which some servers send), whole seconds."""
    if not text:
        return None
    text = text.strip()
    try:
        return int(parsedate_to_datetime(text).timestamp()) * SECOND_NS
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) * SECOND_NS
    except ValueError:
        return None


class _Readable(Protocol):
    def read(self, n: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _Prop:
    path: str  # decoded, absolute, without trailing slash
    name: str  # the last part of the address, decoded on its own (so a %2F in a name stays inside the name)
    is_dir: bool
    size: int | None
    mtime_ns: int | None


def _href_path(href: str) -> tuple[str, str]:
    """``(decoded absolute path, decoded last name)`` of an address from a listing."""
    raw = urlsplit(href).path if "://" in href else href.split("?", 1)[0]
    decoded = unquote(raw, errors="surrogateescape")
    path = posixpath.normpath("/" + decoded.lstrip("/")) if decoded.strip("/") else "/"
    name = unquote(raw.rstrip("/").rsplit("/", 1)[-1], errors="surrogateescape")
    return path, name


def parse_multistatus(stream: _Readable) -> Iterator[_Prop]:
    """Entries of a ``207 Multi-Status`` reply, read as it arrives. Documents with a DOCTYPE are refused."""
    parser: ET.XMLPullParser = ET.XMLPullParser(events=("end",))
    first = True
    try:
        while True:
            chunk = stream.read(65536)
            if first and chunk:
                if b"<!doctype" in chunk[:8192].lower() or b"<!entity" in chunk[:8192].lower():
                    raise OSError(errno.EIO, "the server's reply contains a DTD, which is not accepted")
                first = False
            if chunk:
                parser.feed(chunk)
            else:
                parser.close()
            for event in parser.read_events():
                elem = cast(ET.Element, event[1])  # type: ignore[misc]
                if elem.tag == DAV + "response":
                    prop = _read_response(elem)
                    elem.clear()
                    if prop is not None:
                        yield prop
            if not chunk:
                return
    except ET.ParseError as exc:
        raise OSError(errno.EIO, f"the server sent an unreadable listing ({exc})") from exc


def _read_response(elem: ET.Element) -> _Prop | None:
    href = elem.findtext(DAV + "href")
    if href is None:
        return None
    is_dir, size, mtime = False, None, None
    seen = False
    for propstat in elem.findall(DAV + "propstat"):
        status = (propstat.findtext(DAV + "status") or "HTTP/1.1 200 OK").split()
        try:
            code = int(status[1])
        except (IndexError, ValueError):
            code = 200
        if not 200 <= code < 300:
            continue
        prop = propstat.find(DAV + "prop")
        if prop is None:
            continue
        seen = True
        rtype = prop.find(DAV + "resourcetype")
        if rtype is not None and rtype.find(DAV + "collection") is not None:
            is_dir = True
        length = (prop.findtext(DAV + "getcontentlength") or "").strip()
        if length.isdigit():
            size = int(length)
        modified = parse_http_date(prop.findtext(DAV + "getlastmodified"))
        if modified is not None:
            mtime = modified
    if not seen:
        return None
    path, name = _href_path(href)
    return _Prop(path, name, is_dir, size, mtime)


# --------------------------------------------------------------------------- the backend
class DavBackend(ListingBackend):
    scheme = "dav"

    def __init__(self, target: DavTarget, *, credential_provider=None, timeout: float = 60.0, context=None) -> None:  # type: ignore[no-untyped-def]
        self.target = target
        self._timeout = timeout
        self._context = context
        self._auth = _Auth(target.host, target.username, target.password, credential_provider)
        self._idle: list[http.client.HTTPConnection] = []
        self._pool_lock = threading.Lock()
        self._ranges_ok = True
        self._closed = False

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        default = 443 if self.target.tls else 80
        port = "" if self.target.port == default else f":{self.target.port}"
        return f"{'davs' if self.target.tls else 'dav'}://{user}{host_for_url(self.target.host.lower())}{port}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(
            kind=MediaKind.NETWORK,
            label=f"WebDAV on {self.target.host}" + ("" if self.target.tls else " (not encrypted)"),
            can_hardlink=False,
            stable_inodes=False,
            case_insensitive=True,  # the cautious assumption; `duplio probe` measures the real behaviour
            atomic_rename=True,
            mtime_resolution_ns=SECOND_NS,
            sampling=Sampling.RANDOM if self._ranges_ok else Sampling.SEQUENTIAL,
            max_parallel_reads=4,
            fstype="webdav",
            windows_names=True,
        )

    def uri(self, path: str) -> str:
        user = f"{self.target.username}@" if self.target.username else ""
        default = 443 if self.target.tls else 80
        port = "" if self.target.port == default else f":{self.target.port}"
        scheme = "davs" if self.target.tls else "dav"
        return f"{scheme}://{user}{host_for_url(self.target.host)}{port}{quote_path(self.normalize(path))}"

    def display(self, path: str) -> str:
        return self.uri(path)

    # ---- connections ----------------------------------------------------
    def _new_connection(self) -> http.client.HTTPConnection:
        if self.target.tls:
            ctx = self._context or ssl.create_default_context()
            return http.client.HTTPSConnection(self.target.host, self.target.port, timeout=self._timeout, context=ctx)
        return http.client.HTTPConnection(self.target.host, self.target.port, timeout=self._timeout)

    def _checkout(self) -> tuple[http.client.HTTPConnection, bool]:
        with self._pool_lock:
            if self._idle:
                return self._idle.pop(), True
        return self._new_connection(), False

    def _checkin(self, conn: http.client.HTTPConnection) -> None:
        with self._pool_lock:
            if not self._closed and len(self._idle) < IDLE_CONNECTIONS:
                self._idle.append(conn)
                return
        conn.close()

    def recover(self) -> None:
        with self._pool_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def close(self) -> None:
        self._closed = True
        self.recover()

    # ---- requests -------------------------------------------------------
    def _url(self, path: str) -> str:
        scheme = "https" if self.target.tls else "http"
        default = 443 if self.target.tls else 80
        port = "" if self.target.port == default else f":{self.target.port}"
        return f"{scheme}://{host_for_url(self.target.host)}{port}{path}"

    def _same_server_path(self, location: str) -> str | None:
        parts = urlsplit(location)
        if parts.scheme:
            if parts.scheme != ("https" if self.target.tls else "http"):
                return None
            default = 443 if self.target.tls else 80
            if (parts.hostname or "").lower() != self.target.host.lower() or (parts.port or default) != self.target.port:
                return None
        path = parts.path or "/"
        return path + (f"?{parts.query}" if parts.query else "")

    def _roundtrip(self, method, url_path, headers, body, idempotent):  # type: ignore[no-untyped-def]
        start = body.tell() if body is not None and hasattr(body, "tell") else None
        for attempt in (0, 1):
            conn, reused = self._checkout()
            try:
                conn.request(method, url_path, body=body, headers=headers)
                return conn, conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if attempt == 0 and reused and idempotent:
                    if start is not None:
                        body.seek(start)
                    continue  # a pooled connection the server had already closed: use a fresh one
                raise _as_oserror(exc) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(self, method, path, *, headers=None, body=None, idempotent=True, raw_path=False):  # type: ignore[no-untyped-def]
        """Send one request; returns ``(connection, response)``. Follows redirects on this server and answers 401."""
        url_path = path if raw_path else quote_path(path)
        auth_tries = 0
        hops = 0
        while True:
            hdrs = {"User-Agent": "duplio", "Accept-Encoding": "identity", "Connection": "keep-alive"}
            hdrs.update(headers or {})
            token = self._auth.header(method, url_path)
            if token:
                hdrs["Authorization"] = token
            start = body.tell() if body is not None and hasattr(body, "tell") else None
            conn, resp = self._roundtrip(method, url_path, hdrs, body, idempotent)
            if resp.status in (301, 302, 307, 308):
                location = resp.getheader("Location")
                self._drain(conn, resp)
                target = self._same_server_path(location) if location else None
                hops += 1
                if target is None or hops > MAX_REDIRECTS:
                    raise OSError(errno.EIO, f"{method} {path}: the server redirected to somewhere else ({location})")
                url_path = target
                raw_path = True
                if start is not None:
                    body.seek(start)
                continue
            if resp.status == 401:
                challenges = resp.msg.get_all("WWW-Authenticate") or []
                self._drain(conn, resp)
                auth_tries += 1
                if auth_tries <= 2 and self._auth.accept_challenge(challenges):
                    if start is not None:
                        body.seek(start)
                    continue
                raise PermissionError(
                    errno.EACCES,
                    f"{self.target.host} refused the user name or password"
                    if self._auth.mode
                    else f"{self.target.host} asks for a user name and password (use davs://USER@{self.target.host}/...)",
                )
            return conn, resp

    def _drain(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse) -> None:
        try:
            resp.read()
        except (OSError, http.client.HTTPException):
            conn.close()
            return
        if resp.will_close:
            conn.close()
        else:
            self._checkin(conn)

    def _simple(self, method, path, *, expect, what, **kw) -> http.client.HTTPResponse:  # type: ignore[no-untyped-def]
        """A request whose reply body is not needed; raises for any status outside ``expect``."""
        conn, resp = self._send(method, path, **kw)
        status, reason = resp.status, resp.reason
        self._drain(conn, resp)
        if status not in expect:
            _check_status(status, reason, what)
        return resp

    def _propfind(self, path: str, depth: int, body: bytes = _PROPFIND_BODY) -> list[_Prop]:
        send_path = path if depth == 0 or path.endswith("/") else path + "/"
        headers = {"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8", "Content-Length": str(len(body))}
        conn, resp = self._send("PROPFIND", send_path, headers=headers, body=body)
        if resp.status != 207:
            status, reason = resp.status, resp.reason
            self._drain(conn, resp)
            _check_status(status, reason, f"listing {path}")
        stream = _Body(self, conn, resp)
        try:
            return list(parse_multistatus(stream))
        finally:
            stream.close()

    # ---- listing and metadata -------------------------------------------
    def _list(self, path: str) -> list[Child]:
        path = self.normalize(path)
        props = self._propfind(path, 1)
        here = next((p for p in props if p.path == path), None)
        if here is None and props and props[0].is_dir:
            here = props[0]  # a server behind a proxy that rewrites the start of every address
        if here is not None and not here.is_dir:
            raise NotADirectoryError(errno.ENOTDIR, f"{path} is not a folder")
        out: list[Child] = []
        for prop in props:
            if prop is here:
                continue
            out.append(self._child(prop.name, prop))
        return out

    @staticmethod
    def _child(name: str, prop: _Prop) -> Child:
        if prop.is_dir:
            return Child(name=name, is_dir=True, is_file=False, mtime_ns=prop.mtime_ns or 0)
        problem = None
        if prop.size is None:
            problem = "the server did not report this file's size"
        elif prop.mtime_ns is None:
            problem = "the server did not report this file's modification time, which is needed to tell files apart safely"
        return Child(name=name, is_dir=False, is_file=True, size=prop.size or 0, mtime_ns=prop.mtime_ns or 0, problem=problem)

    def stat(self, path: str) -> StatInfo | None:
        path = self.normalize(path)
        try:
            props = self._propfind(path, 0)
        except FileNotFoundError:
            return None
        if not props:
            return None
        prop = props[0]
        if not prop.is_dir:
            if prop.size is None or prop.mtime_ns is None:
                raise OSError(errno.EIO, f"{path}: the server did not report the file's size or modification time")
        return StatInfo(
            size=prop.size or 0,
            mtime_ns=prop.mtime_ns or 0,
            is_dir=prop.is_dir,
            is_file=not prop.is_dir,
        )

    def free_space(self, path: str) -> int | None:
        try:
            conn, resp = self._send(
                "PROPFIND", self.normalize(path),
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8", "Content-Length": str(len(_QUOTA_BODY))},
                body=_QUOTA_BODY,
            )  # fmt: skip
            if resp.status != 207:
                self._drain(conn, resp)
                return None
            data = resp.read()
            self._drain(conn, resp)
            if b"<!doctype" in data[:8192].lower():
                return None
            root = ET.fromstring(data)
            text = root.findtext(f".//{DAV}quota-available-bytes")
            return int(text) if text and text.strip().isdigit() else None
        except Exception:
            return None

    # ---- reading --------------------------------------------------------
    def _get(self, path: str, start: int, end: int | None) -> tuple[_Body | _EmptyBody, int]:
        """Open a ranged ``GET``; returns the body and the offset it begins at (0 when the server ignored the range)."""
        headers = {}
        if start or end is not None:
            headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        conn, resp = self._send("GET", path, headers=headers)
        status = resp.status
        if status == 206:
            return _Body(self, conn, resp), start
        if status == 200:
            if start:
                self._ranges_ok = False  # the server sent everything: reading mid-file means reading the file up to there
            return _Body(self, conn, resp), 0
        reason = resp.reason
        self._drain(conn, resp)
        if status == 416:  # a range that starts at or past the end of the file
            return _EmptyBody(), start
        _check_status(status, reason, f"reading {path}")
        raise AssertionError("unreachable")  # pragma: no cover

    def open_read(self, path: str) -> IO[bytes]:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder")

        def opener(pos: int) -> _Body | _EmptyBody:
            body, begins = self._get(path, pos, None)
            if begins < pos:
                _skip(body, pos - begins)
            return body

        return RangedReader(info.size, opener, name=path)  # type: ignore[return-value]

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        path = self.normalize(path)
        body, begins = self._get(path, offset, offset + length - 1)
        try:
            if begins < offset:
                _skip(body, offset - begins)
            chunks: list[bytes] = []
            remaining = length
            while remaining > 0:
                data = body.read(remaining)
                if not data:
                    break
                chunks.append(data)
                remaining -= len(data)
            return b"".join(chunks)
        finally:
            body.close()

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
            conn, resp = self._send("MKCOL", folder.rstrip("/") + "/", idempotent=False)
            status, reason = resp.status, resp.reason
            self._drain(conn, resp)
            if status in (201, 405):  # 405: it exists now (another process, or created a moment ago)
                continue
            _check_status(status, reason, f"creating the folder {folder}")

    def remove(self, path: str) -> None:
        path = self.normalize(path)
        info = self.stat(path)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, f"{path}: no such file")
        if info.is_dir:
            raise IsADirectoryError(errno.EISDIR, f"{path} is a folder; Duplio only deletes single files on a WebDAV server")
        self._simple("DELETE", path, expect=(200, 202, 204), what=f"deleting {path}", idempotent=False)

    def _destination(self, path: str) -> str:
        return self._url(quote_path(self.normalize(path)))

    def rename(self, src: str, dst: str) -> None:
        dst = self.normalize(dst)
        self.makedirs(self.parent(dst))
        self._move(self.normalize(src), dst, overwrite=False)

    def _move(self, src: str, dst: str, *, overwrite: bool) -> None:
        headers = {"Destination": self._destination(dst), "Overwrite": "T" if overwrite else "F"}
        try:
            self._simple("MOVE", src, expect=(201, 204), what=f"moving {src}", headers=headers, idempotent=False)
        except ConnectionError:
            # The reply was lost; the move may have happened. Look before deciding.
            moved = self.stat(dst)
            if moved is not None and self.stat(src) is None:
                return
            raise

    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        path = self.normalize(path)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", path)
        parent = self.parent(path)
        self.makedirs(parent)
        tmp = posixpath.join(parent, temp_name(self.basename(path), f".duplio-tmp-{secrets.token_hex(4)}"))
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY) as spool:
            try:
                yield spool  # type: ignore[misc]
                size = spool.tell()
                spool.seek(0)
                self._put(tmp, spool, size)  # type: ignore[arg-type]
                self._move(tmp, path, overwrite=overwrite)
                self._check_stored(path, size)
            except BaseException:
                with contextlib.suppress(Exception):
                    if self.stat(tmp) is not None:
                        self._simple(
                            "DELETE", tmp, expect=(200, 202, 204), what="removing a half-written file", idempotent=False
                        )
                raise

    def _check_stored(self, path: str, size: int) -> None:
        """A server that mangles a name (some cut it at a ``?``) still answers 'created'; look for the file under its name."""
        held = self.stat(path)
        if held is None or not held.is_file or held.size != size:
            raise OSError(errno.EIO, f"the server did not keep the file under the name {path} (it may not allow that name)")

    def _put(self, path: str, spool: IO[bytes], size: int) -> None:
        headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
        self._simple("PUT", path, expect=(200, 201, 204), what=f"uploading {path}", headers=headers, body=spool)
        held = self.stat(path)
        if held is None or held.size != size:
            raise OSError(errno.EIO, f"the server holds {held.size if held else 'no'} bytes of {path}, expected {size}")

    # Times cannot be set through WebDAV.
    def set_mtime(self, path: str, mtime_ns: int) -> None:
        return None

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        """Remove empty folders that Duplio made for itself (names starting ``.duplio-``) and nothing else.

        WebDAV's DELETE removes a folder together with everything in it, with no "only if empty" form, so a folder is
        only deleted when it is Duplio's own and a listing taken just before shows it empty. Other empty folders are
        left alone: an empty folder does no harm, and a file added by someone else in the same instant would be lost.
        """

        def remove(folder: str) -> bool:
            if not self.basename(folder).startswith(".duplio-"):
                return False
            try:
                if self._list(folder):
                    return False
                self._simple("DELETE", folder.rstrip("/") + "/", expect=(200, 202, 204), what=f"removing {folder}", idempotent=False)
            except OSError:
                return False
            return True

        climb_removing(self, path, stop_at, remove)


class _EmptyBody:
    """A body with nothing in it (the reply to a range that starts at or past the end of the file)."""

    def read(self, n: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None


def _skip(body: _Body | _EmptyBody, count: int) -> None:
    while count > 0:
        data = body.read(min(count, 1024 * 1024))
        if not data:
            raise ConnectionResetError(errno.ECONNRESET, "the server ended the transfer early")
        count -= len(data)


__all__ = ["DavBackend", "DavTarget", "parse_dav", "parse_http_date"]
