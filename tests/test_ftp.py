"""FTP specifics: listing formats, TLS, servers that lack features, and connection handling."""

from __future__ import annotations

import ssl
import threading
from pathlib import Path

import pytest

pytest.importorskip("pyftpdlib")

from duplio.errors import LocationError
from duplio.vfs import ftp as ftpmod
from duplio.vfs.base import Sampling, WalkStats
from duplio.vfs.ftp import FtpBackend, parse_ftp, parse_ftp_time, parse_list_line
from duplio.vfs.location import BackendRegistry, location_kind

from .conftest import rand_bytes, write
from .remotekit import FtpServer, self_signed_certificate

OLD = 1_600_000_000


@pytest.fixture(autouse=True)
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUPLIO_FTP_PASSWORD", "secret")


# --------------------------------------------------------------------------- reading what servers send
def test_ftp_addresses() -> None:
    t = parse_ftp("ftps://me:p%40ss@host.example:2121/photos/My%20Pics")
    assert (t.host, t.port, t.tls, t.username, t.password, t.path) == ("host.example", 2121, True, "me", "p@ss", "/photos/My Pics")
    assert parse_ftp("ftp://host").port == 21 and parse_ftp("ftp://host").path == "/"
    with pytest.raises(LocationError):
        parse_ftp("dav://host/x")
    assert location_kind("FTP://h/x") == "ftp" and location_kind("ftps://h") == "ftp"


def test_ftp_times_are_utc_with_optional_fractions() -> None:
    assert parse_ftp_time("20200913122640") == 1600000000 * 10**9
    assert parse_ftp_time("20200913122640.250") == 1600000000 * 10**9 + 250_000_000
    for bad in ("", "2020", "20201345000000", "notatime"):
        assert parse_ftp_time(bad) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("-rw-r--r--   1 ftp      ftp          1234 Mar  3 12:30 report final.txt", ("report final.txt", "file", False, True, 1234)),
        ("drwxr-xr-x   2 1000     1000         4096 Jan 10  2020 My Photos", ("My Photos", "dir", True, False, 0)),
        ("lrwxrwxrwx   1 root     root            7 Mar  3 12:30 current -> release-1", ("current", "link", False, False, 0)),
        ("-rw-r--r--+  1 ftp      ftp             5 Mar  3 12:30 acl.txt", ("acl.txt", "file", False, True, 5)),
        ("03-15-20  02:30PM       <DIR>          Photos 2020", ("Photos 2020", "dir", True, False, 0)),
        ("03-15-2020  02:30PM            123456 clip.mp4", ("clip.mp4", "file", False, True, 123456)),
        ("total 12", None),
        ("this is not a listing line", None),
    ],
)
def test_list_lines_in_unix_and_windows_styles(line: str, expected: tuple | None) -> None:
    assert parse_list_line(line) == expected


def test_unix_line_without_a_group_column() -> None:
    assert parse_list_line("-rw-r--r-- 1 owner 5120 Mar  3 12:30 a.bin") == ("a.bin", "file", False, True, 5120)


# --------------------------------------------------------------------------- servers and features
def test_wrong_and_missing_passwords_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with FtpServer(tmp_path / "s") as srv:
        monkeypatch.setenv("DUPLIO_FTP_PASSWORD", "wrong")
        with BackendRegistry() as reg, pytest.raises(PermissionError):
            reg.open(srv.url()).backend.stat("/")
        monkeypatch.delenv("DUPLIO_FTP_PASSWORD")
        with BackendRegistry() as reg, pytest.raises(PermissionError):
            reg.open(srv.url()).backend.stat("/")  # a user name but no password: refused, not asked for on the sly


def test_a_password_can_come_from_a_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUPLIO_FTP_PASSWORD")
    asked: list[tuple[str, str | None]] = []

    def prompt(host: str, user: str | None) -> str | None:
        asked.append((host, user))
        return "secret"

    with FtpServer(tmp_path / "s") as srv, BackendRegistry(credential_provider=prompt) as reg:
        write(srv.root / "f.txt", b"x", mtime=OLD)
        b = reg.open(srv.url()).backend
        assert b.stat("/f.txt") is not None and b.stat("/f.txt") is not None
        assert asked == [("127.0.0.1", "alice")]  # asked once, remembered for the next connection


def test_anonymous_access_is_read_only(tmp_path: Path) -> None:
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        write(srv.root / "pub" / "f.txt", b"public", mtime=OLD)
        b = reg.open(srv.url("pub", user=None)).backend
        assert [e.rel for e in b.walk("/pub")] == ["f.txt"]
        with pytest.raises(PermissionError), b.open_write("/pub/new.txt") as handle:
            handle.write(b"x")
        assert [p.name for p in (srv.root / "pub").iterdir()] == ["f.txt"]


def test_a_server_that_cannot_report_times_is_refused(tmp_path: Path) -> None:
    with FtpServer(tmp_path / "s", without=("MLSD", "MLST", "MDTM")) as srv, BackendRegistry() as reg:
        write(srv.root / "f.txt", b"x", mtime=OLD)
        with pytest.raises(LocationError, match="cannot report when files were changed"):
            reg.open(srv.url()).backend.stat("/f.txt")


def test_without_restart_support_reads_still_return_the_right_bytes(tmp_path: Path) -> None:
    data = rand_bytes(300_000, 4)
    with FtpServer(tmp_path / "s", without=("REST",)) as srv, BackendRegistry() as reg:
        write(srv.root / "f.bin", data, mtime=OLD)
        b = reg.open(srv.url()).backend
        assert b.read_range("/f.bin", 250_000, 1000) == data[250_000:251_000]
        assert b.capabilities("/").sampling is Sampling.SEQUENTIAL  # sampling mid-file would read the file up to there
        with b.open_read("/f.bin") as handle:
            handle.seek(100_000)
            assert handle.read(50) == data[100_000:100_050]


def test_partial_reads_leave_the_connection_usable(tmp_path: Path) -> None:
    data = rand_bytes(2_000_000, 5)
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        write(srv.root / "f.bin", data, mtime=OLD)
        b = reg.open(srv.url()).backend
        for i in range(12):
            assert b.read_range("/f.bin", i * 1000, 64) == data[i * 1000 : i * 1000 + 64]
        assert srv.commands.count("PASS") <= 2  # the same logged-in connection served them, no login per read


def test_a_connection_closed_by_the_server_is_noticed_before_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ftpmod, "STALE_AFTER", 0.0)
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        write(srv.root / "f.txt", b"abc", mtime=OLD)
        b = reg.open(srv.url()).backend
        assert b.stat("/f.txt") is not None
        for conn in list(b._pool._idle):  # type: ignore[attr-defined]
            conn.sock.close()  # the server (or a firewall) dropped it while idle
        assert b.stat("/f.txt") is not None and b.read_range("/f.txt", 0, 3) == b"abc"


def test_an_overwrite_keeps_the_old_file_until_the_new_one_is_in_place(tmp_path: Path) -> None:
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        write(srv.root / "f.txt", b"old contents", mtime=OLD)
        b = reg.open(srv.url()).backend
        with pytest.raises(RuntimeError), b.open_write("/f.txt", overwrite=True) as handle:
            handle.write(b"new")
            raise RuntimeError("stop")
        assert (srv.root / "f.txt").read_bytes() == b"old contents"
        with b.open_write("/f.txt", overwrite=True) as handle:
            handle.write(b"new contents")
        assert (srv.root / "f.txt").read_bytes() == b"new contents"
        assert [p.name for p in srv.root.iterdir()] == ["f.txt"]


def test_names_the_server_sends_cannot_escape(tmp_path: Path) -> None:
    class Evil(FtpBackend):
        def _list(self, path: str):  # type: ignore[no-untyped-def]
            from duplio.vfs.remote import Child

            return [
                Child("ok.txt", False, True, 1, 10**18),
                Child("../../etc/passwd", False, True, 1, 10**18),
                Child("a/b", False, True, 1, 10**18),
            ]

    backend = Evil(parse_ftp("ftp://127.0.0.1:1/x"))
    stats = WalkStats()
    assert [e.rel for e in backend.walk("/x", stats=stats)] == ["ok.txt"]
    assert len(stats.errors) == 2


def test_ftp_over_tls_is_verified_and_works(tmp_path: Path) -> None:
    pytest.importorskip("OpenSSL")
    pytest.importorskip("cryptography")
    cert = self_signed_certificate(tmp_path)
    context = ssl.create_default_context(cafile=str(cert))
    with FtpServer(tmp_path / "s", tls=cert) as srv:
        write(srv.root / "d" / "f.bin", rand_bytes(200_000, 6), mtime=OLD)
        good = FtpBackend(parse_ftp(srv.url("d", scheme="ftps")), context=context)
        try:
            assert [(e.rel, e.size) for e in good.walk("/d")] == [("f.bin", 200_000)]
            assert good.read_range("/d/f.bin", 10, 20) == (srv.root / "d" / "f.bin").read_bytes()[10:30]
            with good.open_write("/d/up.bin") as handle:
                handle.write(b"over tls")
            assert (srv.root / "d" / "up.bin").read_bytes() == b"over tls"
            assert "tls" in good.capabilities("/").label.lower() or "NOT" not in good.capabilities("/").label
        finally:
            good.close()
        # The default trust store does not know the test certificate, so a normal client must refuse the server.
        strict = FtpBackend(parse_ftp(srv.url("d", scheme="ftps")))
        with pytest.raises(OSError, match=r"certificate|CERTIFICATE|SSL"):
            strict.stat("/d")
        strict.close()


def test_plain_ftp_says_it_is_not_encrypted(tmp_path: Path) -> None:
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        assert "NOT encrypted" in reg.open(srv.url()).caps.label


def test_many_threads_share_a_few_connections(tmp_path: Path) -> None:
    with FtpServer(tmp_path / "s") as srv, BackendRegistry() as reg:
        for i in range(30):
            write(srv.root / f"f{i}.txt", f"file {i}" * 100, mtime=OLD)
        b = reg.open(srv.url()).backend
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                for j in range(6):
                    k = (i * 6 + j) % 30
                    with b.open_read(f"/f{k}.txt") as handle:
                        assert handle.read() == (f"file {k}" * 100).encode()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert srv.commands.count("PASS") <= ftpmod.MAX_CONNECTIONS
