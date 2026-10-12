"""An SFTP server on stdin/stdout, built from paramiko's server code, serving one folder.

It plays the part of ``ssh -s host sftp``: the tests point ``DUPLIO_SSH`` at a small script that runs :func:`main`, so
Duplio starts a real child process and talks the real protocol to an implementation it did not write itself.
Behaviour follows OpenSSH's sftp-server where it matters: a plain rename refuses to replace an existing file.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import sys

import paramiko
from paramiko import (
    SFTP_FAILURE,
    SFTP_OK,
    SFTPAttributes,
    SFTPHandle,
    SFTPServer,
    SFTPServerInterface,
)


class _Handle(SFTPHandle):
    def stat(self):  # type: ignore[no-untyped-def]
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):  # type: ignore[no-untyped-def]
        try:
            SFTPServer.set_file_attr(self.filename, attr)
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)


class Interface(SFTPServerInterface):
    ROOT = "/"

    def _real(self, path: str) -> str:
        rel = posixpath.normpath("/" + path).lstrip("/")
        return os.path.join(self.ROOT, rel)

    def list_folder(self, path):  # type: ignore[no-untyped-def]
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attr = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def stat(self, path):  # type: ignore[no-untyped-def]
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):  # type: ignore[no-untyped-def]
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):  # type: ignore[no-untyped-def]
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o666)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        handle = _Handle(flags)
        handle.filename = real
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def remove(self, path):  # type: ignore[no-untyped-def]
        try:
            os.remove(self._real(path))
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def rename(self, oldpath, newpath):  # type: ignore[no-untyped-def]
        new = self._real(newpath)
        if os.path.lexists(new):
            return SFTP_FAILURE  # sftp-server: "it is an error if the new name already exists"
        try:
            os.rename(self._real(oldpath), new)
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def posix_rename(self, oldpath, newpath):  # type: ignore[no-untyped-def]
        try:
            os.replace(self._real(oldpath), self._real(newpath))
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):  # type: ignore[no-untyped-def]
        try:
            os.mkdir(self._real(path))
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def rmdir(self, path):  # type: ignore[no-untyped-def]
        try:
            os.rmdir(self._real(path))
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def chattr(self, path, attr):  # type: ignore[no-untyped-def]
        try:
            SFTPServer.set_file_attr(self._real(path), attr)
            return SFTP_OK
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def canonicalize(self, path):  # type: ignore[no-untyped-def]
        return posixpath.normpath("/" + path)


class _Transport:
    def get_log_channel(self) -> str:
        return "stub"

    def get_hexdump(self) -> bool:
        return False


class _Stdio:
    def recv(self, n: int) -> bytes:
        return os.read(0, n)

    def send(self, data: bytes) -> int:
        return os.write(1, data)

    def get_transport(self) -> _Transport:
        return _Transport()

    def get_name(self) -> str:
        return "stdio"


class _Server(SFTPServer):
    advertise_posix_rename = False

    def _send_server_version(self):  # type: ignore[no-untyped-def]
        version = super()._send_server_version()
        return version


def _hello_with_extensions(server: SFTPServer, extra: list[tuple[str, str]]) -> None:
    """Replace the greeting so the server can advertise extensions the way OpenSSH does."""
    from paramiko.message import Message
    from paramiko.sftp import CMD_INIT, CMD_VERSION

    original = server._send_server_version

    def send() -> int:
        t, _data = server._read_packet()
        if t != CMD_INIT:
            raise paramiko.SFTPError("Incompatible sftp protocol")
        msg = Message()
        msg.add_int(3)
        for name, value in extra:
            msg.add(name, value)
        server._send_packet(CMD_VERSION, msg)
        return 3

    server._send_server_version = send  # type: ignore[method-assign]
    _ = original


def main() -> None:
    """Entry point of the fake ``ssh``: log how it was called, check the password, then serve SFTP on stdin/stdout."""
    log = os.environ.get("STUB_LOG")
    if log:
        with open(log, "a") as handle:
            record = {
                "argv": sys.argv[1:],
                "askpass": os.environ.get("SSH_ASKPASS"),
                "askpass_require": os.environ.get("SSH_ASKPASS_REQUIRE"),
                "new_session": os.getsid(0) == os.getpid() if hasattr(os, "getsid") else None,
                "has_tty_stdin": os.isatty(0),
            }
            handle.write(json.dumps(record) + "\n")
    if os.environ.get("STUB_HOSTKEY_CHANGED"):
        sys.stderr.write("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n")
        sys.stderr.write("@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n")
        sys.stderr.write("Host key verification failed.\n")
        sys.exit(255)
    expected = os.environ.get("STUB_PASSWORD")
    if expected is not None:
        askpass = os.environ.get("SSH_ASKPASS")
        given = None
        if askpass and "BatchMode=yes" not in sys.argv:
            result = subprocess.run([askpass, "alice@testhost's password: "], capture_output=True, text=True, check=False)
            given = result.stdout.rstrip("\n") if result.returncode == 0 else None
            # ssh must never get the password for a question that is not about a password
            refused = subprocess.run([askpass, "Are you sure you want to continue connecting (yes/no)? "], capture_output=True, check=False)
            if log:
                with open(log, "a") as handle:
                    handle.write(json.dumps({"askpass_answers_yes_no": refused.returncode == 0}) + "\n")
        if given != expected:
            sys.stderr.write("alice@testhost: Permission denied (publickey,password).\n")
            sys.exit(255)
    Interface.ROOT = os.environ["STUB_ROOT"]
    channel = _Stdio()
    server = _Server(channel, "sftp", paramiko.ServerInterface(), sftp_si=Interface)
    extensions = []
    if os.environ.get("STUB_POSIX_RENAME"):
        extensions.append(("posix-rename@openssh.com", "1"))
    _hello_with_extensions(server, extensions)
    try:
        server.start_subsystem("sftp", None, channel)  # type: ignore[arg-type]
    except (EOFError, OSError):
        pass
