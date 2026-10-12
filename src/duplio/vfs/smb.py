"""Native SMB2/3 access via ``smbprotocol`` — no mount required.

Location syntax::

    smb://[domain;]user@server[:port]/share/optional/path
    \\\\server\\share\\path          (on non-Windows systems)

Passwords are never stored. They come from ``DUPLIO_SMB_PASSWORD``, the
optional ``keyring`` package, or an interactive prompt supplied by the UI.
Omitting the user name attempts Kerberos / integrated authentication.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Any

from duplio.errors import BackendUnavailable, LocationError
from duplio.vfs.base import (
    Backend,
    Capabilities,
    MediaKind,
    RawEntry,
    Sampling,
    StatInfo,
    WalkFilter,
    WalkStats,
)
from duplio.vfs.mounts import SECOND_NS

CredentialProvider = Callable[[str, str | None], str | None]
"""``(server, username) -> password`` used when no password is otherwise available."""


@dataclass(frozen=True)
class SMBTarget:
    server: str
    share: str
    path: str  # backslash-separated, no leading separator ("" for share root)
    port: int = 445
    username: str | None = None
    domain: str | None = None
    password: str | None = None

    @property
    def unc(self) -> str:
        base = f"\\\\{self.server}\\{self.share}"
        return f"{base}\\{self.path}" if self.path else base


def parse_smb(text: str) -> SMBTarget:
    """Parse ``smb://`` URLs and ``\\\\server\\share`` UNC paths."""
    from urllib.parse import unquote

    if text.startswith(("\\\\", "//")) and not text.lower().startswith("smb:"):
        parts = [p for p in text.replace("/", "\\").split("\\") if p]
        if len(parts) < 2:
            raise LocationError(f"UNC path needs a server and a share: {text!r}")
        return SMBTarget(server=parts[0], share=parts[1], path="\\".join(parts[2:]))

    if not text.lower().startswith("smb://"):
        raise LocationError(f"not an SMB location: {text!r}")
    rest = text[6:]
    authority, _, path = rest.partition("/")
    userinfo, at, hostport = authority.rpartition("@")
    if not at:
        hostport, userinfo = authority, ""
    username = domain = password = None
    if userinfo:
        user_part, colon, pw = userinfo.partition(":")
        if colon:
            password = unquote(pw)
        if ";" in user_part:
            domain, _, user_part = user_part.partition(";")
            domain = unquote(domain)
        username = unquote(user_part) or None
    host, port = hostport, 445
    if hostport.startswith("["):  # IPv6 literal
        end = hostport.find("]")
        host = hostport[1:end]
        if hostport[end + 1 :].startswith(":"):
            port = int(hostport[end + 2 :])
    elif ":" in hostport:
        host, _, port_text = hostport.partition(":")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise LocationError(f"invalid SMB port in {text!r}") from exc
    if not host:
        raise LocationError(f"SMB location needs a server: {text!r}")
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise LocationError(f"SMB location needs a share name: {text!r}")
    return SMBTarget(
        server=host,
        share=segments[0],
        path="\\".join(segments[1:]),
        port=port,
        username=username,
        domain=domain,
        password=password,
    )


def _smbclient() -> Any:
    try:
        import smbclient
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BackendUnavailable(
            "native SMB access needs the 'smbprotocol' package: pip install 'duplio[smb]' "
            "(or mount the share and scan the mount point instead)"
        ) from exc
    return smbclient


def _is_missing(exc: OSError) -> bool:
    return isinstance(exc, FileNotFoundError | NotADirectoryError) or exc.errno in (
        errno.ENOENT,
        errno.ENOTDIR,
    )


class SMBBackend(Backend):
    scheme = "smb"
    sep = "\\"

    def __init__(
        self,
        target: SMBTarget,
        *,
        credential_provider: CredentialProvider | None = None,
        client: Any = None,
    ) -> None:
        self.target = target
        self._client = client
        self._credentials = credential_provider
        self._session_lock = threading.Lock()
        self._registered = False

    # ---- session --------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _smbclient()
        self._ensure_session()
        return self._client

    def _resolve_password(self) -> str | None:
        if self.target.password is not None:
            return self.target.password
        env = os.environ.get("DUPLIO_SMB_PASSWORD")
        if env:
            return env
        if self.target.username:
            try:
                import keyring

                stored = keyring.get_password(f"duplio-smb:{self.target.server}", self.target.username)
                if stored:
                    return stored
            except Exception:  # keyring is optional and may be misconfigured
                pass
            if self._credentials is not None:
                return self._credentials(self.target.server, self.target.username)
        return None

    def _ensure_session(self) -> None:
        if self._registered:
            return
        with self._session_lock:
            if self._registered:
                return
            username = self.target.username
            if username and self.target.domain:
                username = f"{self.target.domain}\\{username}"
            try:
                self._client.register_session(
                    self.target.server,
                    username=username,
                    password=self._resolve_password(),
                    port=self.target.port,
                )
            except Exception as exc:
                raise LocationError(f"cannot connect to SMB server {self.target.server}: {exc}") from exc
            self._registered = True

    def close(self) -> None:
        if self._registered and self._client is not None:
            with contextlib.suppress(Exception):
                self._client.delete_session(self.target.server, port=self.target.port)
            self._registered = False

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        port = "" if self.target.port == 445 else f":{self.target.port}"
        return f"smb://{self.target.server.lower()}{port}"

    def capabilities(self, path: str) -> Capabilities:
        return Capabilities(
            kind=MediaKind.NETWORK,
            label=f"SMB share on {self.target.server}",
            can_hardlink=False,
            stable_inodes=True,  # SMB file IDs are unique per volume
            case_insensitive=True,
            mtime_resolution_ns=SECOND_NS,
            sampling=Sampling.RANDOM,
            max_parallel_reads=4,
            fstype="smb",
        )

    def uri(self, path: str) -> str:
        unc = self.normalize(path)
        parts = [p for p in unc.split("\\") if p]
        user = ""
        if self.target.username:
            user = (f"{self.target.domain};" if self.target.domain else "") + self.target.username + "@"
        port = "" if self.target.port == 445 else f":{self.target.port}"
        return f"smb://{user}{self.target.server}{port}/" + "/".join(parts[1:])

    def display(self, path: str) -> str:
        return self.normalize(path)

    # ---- path algebra ---------------------------------------------------
    def join(self, base: str, rel: str) -> str:
        if not rel:
            return base
        return base.rstrip("\\") + "\\" + "\\".join(p for p in rel.split("/") if p)

    def parent(self, path: str) -> str:
        norm = self.normalize(path)
        parts = norm.lstrip("\\").split("\\")
        if len(parts) <= 2:
            return norm
        return "\\\\" + "\\".join(parts[:-1])

    def basename(self, path: str) -> str:
        return self.normalize(path).rsplit("\\", 1)[-1]

    def normalize(self, path: str) -> str:
        parts = [p for p in path.replace("/", "\\").split("\\") if p and p != "."]
        out: list[str] = []
        for part in parts:
            if part == "..":
                if len(out) > 2:
                    out.pop()
            else:
                out.append(part)
        return "\\\\" + "\\".join(out)

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
        client = self.client
        stack: list[tuple[str, str]] = [(self.normalize(root), "")]
        while stack:
            if should_stop is not None and should_stop():
                return
            dir_path, dir_rel = stack.pop()
            try:
                children = list(client.scandir(dir_path))
            except OSError as exc:
                stats.errors.append((dir_path, str(exc)))
                continue
            stats.dirs += 1
            subdirs: list[tuple[str, str]] = []
            for child in children:
                name = child.name
                if name in (".", ".."):
                    continue
                rel = f"{dir_rel}/{name}" if dir_rel else name
                path = f"{dir_path}\\{name}"
                try:
                    if child.is_symlink():
                        stats.symlinks_skipped += 1
                        continue
                    if child.is_dir():
                        if walk_filter is not None and walk_filter.skip_dir(rel, name):
                            stats.filtered += 1
                            continue
                        subdirs.append((path, rel))
                        continue
                    if not child.is_file():
                        stats.special_skipped += 1
                        continue
                    st = child.stat()
                except OSError as exc:
                    stats.errors.append((path, str(exc)))
                    continue
                if walk_filter is not None and walk_filter.skip_file(rel, name, st.st_size):
                    stats.filtered += 1
                    continue
                stats.files += 1
                stats.bytes += st.st_size
                ino = getattr(st, "st_ino", 0)
                yield RawEntry(
                    path=path,
                    rel=rel,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    inode=(getattr(st, "st_dev", 0), ino) if ino else None,
                    nlink=getattr(st, "st_nlink", 1) or 1,
                )
            subdirs.sort(reverse=True)
            stack.extend(subdirs)

    def stat(self, path: str) -> StatInfo | None:
        import stat as stat_mod

        try:
            st = self.client.lstat(self.normalize(path))
        except OSError as exc:
            if _is_missing(exc):
                return None
            raise
        mode = st.st_mode
        ino = getattr(st, "st_ino", 0)
        return StatInfo(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            is_dir=stat_mod.S_ISDIR(mode),
            is_file=stat_mod.S_ISREG(mode),
            is_symlink=stat_mod.S_ISLNK(mode),
            inode=(getattr(st, "st_dev", 0), ino) if ino else None,
            nlink=getattr(st, "st_nlink", 1) or 1,
        )

    def open_read(self, path: str) -> IO[bytes]:
        return self.client.open_file(self.normalize(path), mode="rb", share_access="r")

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        return [
            (e.name, e.is_dir())
            for e in self.client.scandir(self.normalize(path))
            if e.name not in (".", "..")
        ]

    def free_space(self, path: str) -> int | None:
        try:
            return int(self.client.stat_volume(self.normalize(path)).caller_available_size)
        except Exception:
            return None

    # ---- writing --------------------------------------------------------
    @contextlib.contextmanager
    def open_write(self, path: str, *, overwrite: bool = False) -> Iterator[IO[bytes]]:
        client = self.client
        path = self.normalize(path)
        if not overwrite and self.stat(path) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", path)
        parent = self.parent(path)
        client.makedirs(parent, exist_ok=True)
        tmp = f"{parent}\\.{self.basename(path)}.duplio-tmp-{secrets.token_hex(4)}"
        handle = client.open_file(tmp, mode="wb")
        try:
            yield handle
            handle.flush()
            handle.close()
            if overwrite:
                client.replace(tmp, path)
            else:
                try:
                    client.rename(tmp, path)
                except OSError as exc:
                    if exc.errno == errno.EEXIST or isinstance(exc, FileExistsError):
                        raise FileExistsError(errno.EEXIST, "destination exists", path) from exc
                    raise
        except BaseException:
            with contextlib.suppress(Exception):
                handle.close()
            with contextlib.suppress(Exception):
                client.remove(tmp)
            raise

    def makedirs(self, path: str) -> None:
        self.client.makedirs(self.normalize(path), exist_ok=True)

    def remove(self, path: str) -> None:
        self.client.remove(self.normalize(path))

    def rename(self, src: str, dst: str) -> None:
        dst = self.normalize(dst)
        if self.stat(dst) is not None:
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        self.client.makedirs(self.parent(dst), exist_ok=True)
        self.client.rename(self.normalize(src), dst)

    def remove_empty_dirs(self, path: str, stop_at: str) -> None:
        stop = self.normalize(stop_at).casefold()
        current = self.normalize(path)
        while current.casefold() != stop and current.casefold().startswith(stop + "\\"):
            try:
                self.client.rmdir(current)
            except Exception:
                return
            current = self.parent(current)

    def set_mtime(self, path: str, mtime_ns: int) -> None:
        with contextlib.suppress(Exception):
            st = self.client.stat(self.normalize(path))
            self.client.utime(self.normalize(path), ns=(st.st_atime_ns, mtime_ns))
