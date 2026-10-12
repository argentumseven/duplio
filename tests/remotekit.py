"""Real servers for the network backends, run inside the test process.

WebDAV is served by WsgiDAV, FTP by pyftpdlib and SFTP by paramiko's server code: independent implementations of the
protocols, so the backends are tested against software other people wrote rather than against their own idea of it.
Each helper serves a folder on disk, so a test can put files there and look at what the backend wrote.
"""

from __future__ import annotations

import threading
from pathlib import Path


class CountingApp:
    """WSGI wrapper that counts the bytes sent back to clients."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app
        self.sent = 0
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):  # type: ignore[no-untyped-def]
        with self._lock:
            self.requests.append((environ.get("REQUEST_METHOD", ""), environ.get("PATH_INFO", "")))
        for chunk in self.app(environ, start_response):
            with self._lock:
                self.sent += len(chunk)
            yield chunk


class DavServer:
    """A WebDAV server on 127.0.0.1 serving ``root``. ``auth`` is "none", "basic" or "digest" (user alice / secret)."""

    def __init__(self, root: Path, auth: str = "none") -> None:
        from cheroot import wsgi
        from wsgidav.wsgidav_app import WsgiDAVApp

        self.root = root
        self.auth = auth
        root.mkdir(parents=True, exist_ok=True)
        config: dict = {
            "host": "127.0.0.1",
            "port": 0,
            "provider_mapping": {"/": str(root)},
            "verbose": 0,
            "logging": {"enable": False},
        }  # type: ignore[type-arg]
        if auth == "none":
            config["simple_dc"] = {"user_mapping": {"*": True}}
        else:
            config["simple_dc"] = {"user_mapping": {"*": {"alice": {"password": "secret"}}}}
            config["http_authenticator"] = {
                "domain_controller": None,
                "accept_basic": auth == "basic",
                "accept_digest": auth == "digest",
                "default_to_digest": auth == "digest",
            }
        self.app = CountingApp(WsgiDAVApp(config))
        self._server = wsgi.Server(("127.0.0.1", 0), self.app)
        self._server.prepare()
        self.port = self._server.bind_addr[1]
        self._thread = threading.Thread(target=self._server.serve, daemon=True)
        self._thread.start()

    def url(self, sub: str = "", *, user: str | None = None) -> str:
        who = f"{user}@" if user else ""
        return f"dav://{who}127.0.0.1:{self.port}/{sub.lstrip('/')}"

    def close(self) -> None:
        self._server.stop()
        self._thread.join(timeout=5)

    def __enter__(self) -> DavServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def self_signed_certificate(directory: Path) -> Path:
    """A certificate for 127.0.0.1, written as one PEM file (key and certificate), for the TLS tests."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = directory / "cert.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    return path


class FtpServer:
    """An FTP server on 127.0.0.1 serving ``root`` (user alice / secret, or anonymous read-only).

    ``without`` removes commands from the server, to behave like servers that lack them (``MLSD`` and ``MLST``, ``MDTM``,
    ``REST``). ``tls`` is a certificate file from :func:`self_signed_certificate`.
    """

    def __init__(self, root: Path, *, without: tuple[str, ...] = (), tls: Path | None = None) -> None:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
        from pyftpdlib.servers import ThreadedFTPServer

        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        authorizer = DummyAuthorizer()
        authorizer.add_user("alice", "secret", str(root), perm="elradfmwMT")
        authorizer.add_anonymous(str(root), perm="elr")
        base = TLS_FTPHandler if tls else FTPHandler
        attrs: dict = {  # type: ignore[type-arg]
            "authorizer": authorizer,
            "proto_cmds": {k: v for k, v in base.proto_cmds.items() if k not in without},
            "banner": "test server",
            "log_prefix": "",
        }
        if tls:
            attrs.update(certfile=str(tls), tls_control_required=True, tls_data_required=True)
        self.handler = type("Handler", (base,), attrs)
        self.commands: list[str] = []
        outer = self

        original = self.handler.pre_process_command

        def record(handler, line, cmd, arg):  # type: ignore[no-untyped-def]
            outer.commands.append(cmd)
            return original(handler, line, cmd, arg)

        self.handler.pre_process_command = record  # type: ignore[assignment]
        self._server = ThreadedFTPServer(("127.0.0.1", 0), self.handler)
        self.port = self._server.address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"timeout": 0.2, "handle_exit": False}, daemon=True)
        self._thread.start()

    def url(self, sub: str = "", *, user: str | None = "alice", scheme: str = "ftp") -> str:
        who = f"{user}@" if user else ""
        return f"{scheme}://{who}127.0.0.1:{self.port}/{sub.lstrip('/')}"

    def close(self) -> None:
        self._server.close_all()
        self._thread.join(timeout=5)

    def __enter__(self) -> FtpServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SftpServer:
    """A fake ``ssh`` that serves SFTP from ``root`` using paramiko's server code.

    ``install`` points ``DUPLIO_SSH`` at it. ``password`` makes it refuse logins without that password (keys are not
    simulated: a login without the password helper is refused, like a server that only offers password login).
    """

    def __init__(self, root: Path, workdir: Path, *, posix_rename: bool = False, password: str | None = None,
                 hostkey_changed: bool = False) -> None:  # fmt: skip
        import os
        import stat
        import sys

        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.log = workdir / "ssh-calls.log"
        self.script = workdir / "fake-ssh"
        repo = Path(__file__).resolve().parent.parent
        self.script.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(repo)!r})\nfrom tests.sftp_stub import main\nmain()\n")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        self.env = {"STUB_ROOT": str(root), "STUB_LOG": str(self.log)}
        if posix_rename:
            self.env["STUB_POSIX_RENAME"] = "1"
        if password is not None:
            self.env["STUB_PASSWORD"] = password
        if hostkey_changed:
            self.env["STUB_HOSTKEY_CHANGED"] = "1"
        _ = os

    def install(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("DUPLIO_SSH", str(self.script))
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)

    def url(self, sub: str = "", *, user: str | None = "alice") -> str:
        who = f"{user}@" if user else ""
        return f"sftp://{who}testhost/{sub.lstrip('/')}"

    def calls(self) -> list[dict]:  # type: ignore[type-arg]
        import json

        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def close(self) -> None:
        return None

    def __enter__(self) -> SftpServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
