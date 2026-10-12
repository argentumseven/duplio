"""Turn user-supplied location strings into ``(backend, path)`` pairs.

Accepted forms:

* ``/home/me/Pictures``, ``~/Pictures``, ``C:\\Users\\me``  — local (incl. mounts)
* ``smb://user@nas/photos/2024`` or ``\\\\nas\\photos``   — native SMB
* ``adb:/sdcard/DCIM`` or ``adb://SERIAL/sdcard/DCIM``     — Android over ADB
* ``davs://user@host/path`` (or ``https://…``)            — WebDAV (Nextcloud, ownCloud, NAS web folders)
* ``ftp://user@host/path``, ``ftps://user@host/path``      — FTP, and FTP with TLS
* ``sftp://user@host/path``                                — SFTP (needs the ``ssh`` program that comes with the system)

A :class:`BackendRegistry` shares one backend instance per server/device, so a
scan over two folders of the same NAS uses one authenticated session.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

from duplio.errors import LocationError
from duplio.vfs.base import Backend, Capabilities
from duplio.vfs.local import LocalBackend


@dataclass(frozen=True)
class Location:
    backend: Backend
    path: str

    @property
    def uri(self) -> str:
        return self.backend.uri(self.path)

    @property
    def display(self) -> str:
        return self.backend.display(self.path)

    @property
    def caps(self) -> Capabilities:
        return self.backend.capabilities(self.path)

    def child(self, rel: str) -> Location:
        return Location(self.backend, self.backend.join(self.path, rel))


def location_kind(text: str) -> str:
    """Classify a location string without opening it: local, smb, adb, dav, ftp or sftp."""
    lowered = text.lower()
    if lowered.startswith("smb://"):
        return "smb"
    if lowered.startswith("adb:"):
        return "adb"
    if lowered.startswith(("dav://", "davs://", "http://", "https://")):
        return "dav"
    if lowered.startswith(("ftp://", "ftps://")):
        return "ftp"
    if lowered.startswith("sftp://"):
        return "sftp"
    if sys.platform != "win32" and text.startswith(("\\\\", "//")) and len(text) > 2:
        return "smb"
    return "local"


class BackendRegistry:
    """Creates and caches backends; close it to release network sessions."""

    def __init__(self, *, credential_provider=None) -> None:  # type: ignore[no-untyped-def]
        self._lock = threading.Lock()
        self._backends: dict[str, Backend] = {}
        self.credential_provider = credential_provider

    def local(self) -> LocalBackend:
        with self._lock:
            backend = self._backends.get("local")
            if backend is None:
                backend = LocalBackend()
                self._backends["local"] = backend
            assert isinstance(backend, LocalBackend)
            return backend

    def register(self, key: str, backend: Backend) -> Backend:
        with self._lock:
            return self._backends.setdefault(key, backend)

    def open(self, text: str) -> Location:
        text = text.strip()
        if not text:
            raise LocationError("empty location")
        kind = location_kind(text)
        if kind == "smb":
            from duplio.vfs.smb import SMBBackend, parse_smb

            target = parse_smb(text)
            key = f"smb://{target.server.lower()}:{target.port}/{(target.username or '').lower()}"
            with self._lock:
                backend = self._backends.get(key)
            if backend is None:
                backend = self.register(
                    key, SMBBackend(target, credential_provider=self.credential_provider)
                )
            return Location(backend, target.unc)
        if kind == "dav":
            from duplio.vfs.webdav import DavBackend, parse_dav

            dav_target = parse_dav(text)
            key = (
                f"{'davs' if dav_target.tls else 'dav'}://{(dav_target.username or '')}@"
                f"{dav_target.host.lower()}:{dav_target.port}"
            )
            with self._lock:
                backend = self._backends.get(key)
            if backend is None:
                backend = self.register(key, DavBackend(dav_target, credential_provider=self.credential_provider))
            return Location(backend, dav_target.path)
        if kind == "ftp":
            from duplio.vfs.ftp import FtpBackend, parse_ftp

            ftp_target = parse_ftp(text)
            key = (
                f"{'ftps' if ftp_target.tls else 'ftp'}://{(ftp_target.username or '')}@"
                f"{ftp_target.host.lower()}:{ftp_target.port}"
            )
            with self._lock:
                backend = self._backends.get(key)
            if backend is None:
                backend = self.register(key, FtpBackend(ftp_target, credential_provider=self.credential_provider))
            return Location(backend, ftp_target.path)
        if kind == "sftp":
            from duplio.vfs.sftp import SftpBackend, parse_sftp

            sftp_target = parse_sftp(text)
            key = f"sftp://{(sftp_target.username or '')}@{sftp_target.host.lower()}:{sftp_target.port or 22}"
            with self._lock:
                backend = self._backends.get(key)
            if backend is None:
                backend = self.register(key, SftpBackend(sftp_target, credential_provider=self.credential_provider))
            return Location(backend, sftp_target.path)
        if kind == "adb":
            from duplio.vfs.adb import ADBBackend, parse_adb

            adb_target = parse_adb(text)
            key = f"adb:{adb_target.serial or '*'}"
            with self._lock:
                backend = self._backends.get(key)
            if backend is None:
                adb_backend = ADBBackend(adb_target.serial)
                backend = self.register(f"adb:{adb_backend.serial}", adb_backend)
                if adb_target.serial is None:
                    self.register(key, backend)
            return Location(backend, adb_target.path)
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(text)))
        return Location(self.local(), path)

    def close(self) -> None:
        with self._lock:
            backends = list({id(b): b for b in self._backends.values()}.values())
            self._backends.clear()
        for backend in backends:
            try:
                backend.close()
            except Exception:
                pass

    def __enter__(self) -> BackendRegistry:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
