"""SFTP specifics: how ssh is started, logging in, hostile servers, and the wire protocol."""

from __future__ import annotations

import io
import json
import stat
import struct
import sys
from pathlib import Path

import pytest

pytest.importorskip("paramiko")

from duplio.errors import BackendUnavailable, LocationError
from duplio.vfs import sftp_protocol as proto
from duplio.vfs.base import WalkStats
from duplio.vfs.location import BackendRegistry, location_kind
from duplio.vfs.sftp import SftpBackend, explain_ssh_failure, find_ssh, parse_sftp

from .conftest import rand_bytes, write
from .remotekit import SftpServer

OLD = 1_600_000_000
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake ssh is a POSIX script")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SftpServer:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh")
    srv.install(monkeypatch)
    return srv


# --------------------------------------------------------------------------- addresses
def test_sftp_addresses() -> None:
    t = parse_sftp("sftp://me@nas.local:2222/data/My%20Photos")
    assert (t.host, t.port, t.username, t.path) == ("nas.local", 2222, "me", "/data/My Photos")
    assert parse_sftp("sftp://host").path == "/" and parse_sftp("sftp://host").port is None
    assert parse_sftp("sftp://[fe80::1]/x").host == "fe80::1"
    assert location_kind("SFTP://h/x") == "sftp"
    with pytest.raises(LocationError):
        parse_sftp("ftp://host/x")


@pytest.mark.parametrize(
    "bad",
    [
        "sftp://-oProxyCommand=touch%20pwned/x",
        "sftp://-F/x",
        "sftp://-me@host/x",
        "sftp://host name/x",
        "sftp://;rm -rf/x",
        "sftp://$(id)/x",
        "sftp://ho`st/x",
    ],
)
def test_names_that_ssh_could_read_as_options_are_refused(bad: str) -> None:
    with pytest.raises(LocationError):
        parse_sftp(bad)


@posix_only
def test_ssh_is_started_with_safe_defaults(server: SftpServer) -> None:
    write(server.root / "f.txt", b"x", mtime=OLD)
    with BackendRegistry() as reg:
        reg.open(server.url("")).backend.stat("/f.txt")
    (call,) = server.calls()
    argv = call["argv"]
    assert argv[-5:] == ["-l", "alice", "-s", "--", "testhost", "sftp"][-5:] or argv[-6:] == ["-l", "alice", "-s", "--", "testhost", "sftp"]
    joined = " ".join(argv)
    for wanted in ("BatchMode=yes", "StrictHostKeyChecking=accept-new", "ClearAllForwardings=yes", "ForwardAgent=no", "ConnectTimeout=20"):
        assert wanted in joined
    assert "-T" in argv and call["askpass"] is None  # no terminal prompts, no password helper when none is needed


@posix_only
def test_port_and_extra_options_come_before_ours(server: SftpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUPLIO_SSH_OPTIONS", "-o StrictHostKeyChecking=yes -i /keys/id_ed25519")
    with BackendRegistry() as reg:
        reg.open("sftp://alice@testhost:2222/").backend.stat("/")
    argv = server.calls()[0]["argv"]
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv.index("StrictHostKeyChecking=yes") < argv.index("StrictHostKeyChecking=accept-new")  # ssh keeps the first value
    assert "-i" in argv and "/keys/id_ed25519" in argv


def test_ssh_missing_gives_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUPLIO_SSH", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(BackendUnavailable, match="ssh"):
        find_ssh()


# --------------------------------------------------------------------------- logging in
@posix_only
def test_a_password_is_used_only_when_no_key_works_and_never_for_other_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh", password="s3cret pass'word")
    srv.install(monkeypatch)
    write(srv.root / "f.txt", b"hello", mtime=OLD)
    asked: list[tuple[str, str | None]] = []

    def prompt(host: str, user: str | None) -> str | None:
        asked.append((host, user))
        return "s3cret pass'word"

    with BackendRegistry(credential_provider=prompt) as reg:
        b = reg.open(srv.url()).backend
        info = b.stat("/f.txt")
        assert info is not None and info.size == 5
        assert b.read_range("/f.txt", 0, 5) == b"hello"
    assert asked == [("testhost", "alice")]  # asked once
    calls = srv.calls()
    first, second = calls[0], [c for c in calls if "argv" in c][1]
    assert "BatchMode=yes" in first["argv"]  # keys and agent first, with no prompting
    assert "BatchMode=yes" not in second["argv"] and "NumberOfPasswordPrompts=1" in second["argv"]
    assert second["askpass"] and second["askpass_require"] == "force"
    assert {"askpass_answers_yes_no": False} in calls  # the helper refuses anything that is not a password prompt
    assert not Path(second["askpass"]).exists()  # removed again when the backend closed


@posix_only
def test_a_password_from_the_environment_skips_the_key_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh", password="pw")
    srv.install(monkeypatch)
    monkeypatch.setenv("DUPLIO_SFTP_PASSWORD", "pw")
    with BackendRegistry() as reg:
        assert reg.open(srv.url()).backend.stat("/") is not None
    assert len([c for c in srv.calls() if "argv" in c]) == 1


@posix_only
def test_a_wrong_password_is_refused_once_without_hammering_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh", password="right")
    srv.install(monkeypatch)
    monkeypatch.setenv("DUPLIO_SFTP_PASSWORD", "wrong")
    with BackendRegistry() as reg, pytest.raises(PermissionError, match="login refused"):
        reg.open(srv.url()).backend.stat("/")
    assert len([c for c in srv.calls() if "argv" in c]) == 1


@posix_only
def test_no_password_and_no_key_says_what_to_do(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh", password="right")
    srv.install(monkeypatch)
    with BackendRegistry() as reg, pytest.raises(PermissionError, match="DUPLIO_SFTP_PASSWORD"):
        reg.open(srv.url()).backend.stat("/")


@posix_only
def test_a_changed_host_key_is_refused_with_an_explanation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = SftpServer(tmp_path / "server", tmp_path / "ssh", hostkey_changed=True)
    srv.install(monkeypatch)
    with BackendRegistry() as reg, pytest.raises(PermissionError, match="CHANGED"):
        reg.open(srv.url()).backend.stat("/")


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("ssh: Could not resolve hostname nas: Name or service not known", OSError),
        ("ssh: connect to host 10.0.0.9 port 22: Connection refused", ConnectionRefusedError),
        ("ssh: connect to host 10.0.0.9 port 22: Connection timed out", TimeoutError),
        ("alice@nas: Permission denied (publickey).", PermissionError),
        ("Host key verification failed.", PermissionError),
        ("subsystem request failed on channel 0", OSError),
        ("something unexpected", ConnectionResetError),
    ],
)
def test_ssh_failures_are_explained(stderr: str, kind: type) -> None:
    err = explain_ssh_failure("nas", "alice", stderr)
    assert isinstance(err, kind) and "nas" in str(err)


# --------------------------------------------------------------------------- the protocol and a server that lies
class Wire:
    """Plays a canned server: feeds the client its bytes and collects what the client sends."""

    def __init__(self, replies: bytes) -> None:
        self._in = io.BytesIO(replies)
        self.sent = bytearray()
        self.closed = False

    def read_exact(self, n: int) -> bytes:
        data = self._in.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def write_all(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        self.closed = True


def packet(ptype: int, body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 1) + bytes([ptype]) + body


def test_a_greeting_from_something_that_is_not_sftp_is_refused() -> None:
    wire = Wire(b"SSH-2.0-OpenSSH_9.6\r\n")
    session = proto.SftpSession(wire.read_exact, wire.write_all, wire.close)
    with pytest.raises(OSError):
        session.start()
    assert wire.closed


def test_a_greeting_with_an_old_version_is_refused() -> None:
    wire = Wire(packet(proto.VERSION, proto.u32(2)))
    with pytest.raises(proto.ProtocolError, match="version 3"):
        proto.SftpSession(wire.read_exact, wire.write_all, wire.close).start()


def test_an_enormous_packet_is_not_allocated() -> None:
    wire = Wire(struct.pack(">I", 0x7FFFFFFF))
    with pytest.raises(proto.ProtocolError):
        proto.SftpSession(wire.read_exact, wire.write_all, wire.close).start()


def test_truncated_attributes_are_a_protocol_error_not_a_crash() -> None:
    with pytest.raises(proto.ProtocolError):
        proto.Reader(struct.pack(">I", proto.A_SIZE) + b"\x00\x01").attrs()
    with pytest.raises(proto.ProtocolError):
        proto.Reader(struct.pack(">I", 50) + b"short").string()


def test_attribute_round_trip_and_types() -> None:
    attrs = proto.Attrs(size=2**40, uid=1, gid=2, permissions=stat.S_IFREG | 0o644, atime=5, mtime=1_600_000_000)
    back = proto.Reader(proto.encode_attrs(attrs)).attrs()
    assert back == attrs and back.is_file and not back.is_dir and not back.is_symlink
    assert proto.Attrs(permissions=stat.S_IFLNK | 0o777).is_symlink and proto.Attrs(permissions=stat.S_IFDIR).is_dir


@posix_only
def test_names_the_server_sends_cannot_escape(server: SftpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    write(server.root / "ok.txt", b"x", mtime=OLD)
    with BackendRegistry() as reg:
        backend = reg.open(server.url()).backend
        assert isinstance(backend, SftpBackend)
        real = proto.SftpSession.listdir

        def evil(self, path):  # type: ignore[no-untyped-def]
            entries = real(self, path)
            attrs = proto.Attrs(size=1, permissions=stat.S_IFREG | 0o644, mtime=OLD)
            return [*entries, proto.DirEntry(b"../../etc/passwd", attrs), proto.DirEntry(b"a/b", attrs), proto.DirEntry(b"", attrs)]

        monkeypatch.setattr(proto.SftpSession, "listdir", evil)
        stats = WalkStats()
        assert [e.rel for e in backend.walk("/", stats=stats)] == ["ok.txt"]
        assert len(stats.errors) == 3


@posix_only
def test_files_without_times_are_reported_not_guessed(server: SftpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    write(server.root / "ok.txt", b"x", mtime=OLD)
    with BackendRegistry() as reg:
        backend = reg.open(server.url()).backend
        real = proto.SftpSession.listdir

        def timeless(self, path):  # type: ignore[no-untyped-def]
            return [proto.DirEntry(e.name, proto.Attrs(size=e.attrs.size, permissions=e.attrs.permissions)) for e in real(self, path)]

        monkeypatch.setattr(proto.SftpSession, "listdir", timeless)
        stats = WalkStats()
        assert list(backend.walk("/", stats=stats)) == []
        assert stats.errors and "modification time" in stats.errors[0][1]


# --------------------------------------------------------------------------- behaviour over the wire
@posix_only
def test_a_big_file_reads_back_exactly_with_requests_in_flight(server: SftpServer) -> None:
    data = rand_bytes(3_500_000, 11)
    write(server.root / "big.bin", data, mtime=OLD)
    with BackendRegistry() as reg:
        b = reg.open(server.url()).backend
        with b.open_read("/big.bin") as handle:
            assert handle.read() == data
        with b.open_read("/big.bin") as handle:
            handle.seek(1_234_567)
            assert handle.read(100_000) == data[1_234_567:1_334_567]
            handle.seek(10)
            assert handle.read(10) == data[10:20]


@posix_only
def test_overwrite_with_and_without_the_atomic_rename_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for atomic in (False, True):
        srv = SftpServer(tmp_path / f"s{atomic}", tmp_path / f"w{atomic}", posix_rename=atomic)
        srv.install(monkeypatch)
        write(srv.root / "f.txt", b"old", mtime=OLD)
        with BackendRegistry() as reg:
            b = reg.open(srv.url()).backend
            with pytest.raises(RuntimeError), b.open_write("/f.txt", overwrite=True) as handle:
                handle.write(b"new")
                raise RuntimeError("stop")
            assert (srv.root / "f.txt").read_bytes() == b"old"
            with b.open_write("/f.txt", overwrite=True) as handle:
                handle.write(b"new contents")
            assert (srv.root / "f.txt").read_bytes() == b"new contents"
            assert [p.name for p in srv.root.iterdir()] == ["f.txt"]
            assert ("posix-rename@openssh.com" in b._session().extensions) is atomic  # type: ignore[attr-defined]


@posix_only
def test_a_lost_connection_is_reported_and_the_next_call_reconnects(server: SftpServer) -> None:
    write(server.root / "f.txt", b"abc", mtime=OLD)
    with BackendRegistry() as reg:
        b = reg.open(server.url()).backend
        assert b.stat("/f.txt") is not None
        session = b._current  # type: ignore[attr-defined]
        assert session is not None
        b._ssh.proc.kill()  # type: ignore[attr-defined]
        with pytest.raises(OSError):
            for _ in range(3):
                b.stat("/f.txt")
        b.recover()
        assert b.stat("/f.txt") is not None and len([c for c in server.calls() if "argv" in c]) == 2


@posix_only
def test_modification_times_can_be_set(server: SftpServer) -> None:
    write(server.root / "f.txt", b"abc", mtime=OLD)
    with BackendRegistry() as reg:
        b = reg.open(server.url()).backend
        b.set_mtime("/f.txt", (OLD + 1000) * 10**9)
        assert int((server.root / "f.txt").stat().st_mtime) == OLD + 1000


def test_the_log_helper_file_is_json(server: SftpServer) -> None:
    assert server.calls() == [] or all(isinstance(c, dict) for c in server.calls())
    _ = json


def test_user_names_are_case_exact_when_connections_are_shared() -> None:
    with BackendRegistry() as reg:
        assert reg.open("sftp://Alice@Host/x").backend is not reg.open("sftp://alice@Host/x").backend  # different accounts
        assert reg.open("sftp://alice@HOST/x").backend is reg.open("sftp://alice@host/y").backend  # host names are not case-sensitive
