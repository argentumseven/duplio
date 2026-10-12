"""What every network backend (WebDAV, FTP, SFTP) must do the same way, run against real servers of each kind."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.transfer import copy_verified
from duplio.vfs.base import MediaKind, WalkStats
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

OLD = 1_600_000_000


@dataclass
class Remote:
    kind: str
    root: Path  # the folder on disk that the server shows as "/"
    base: str  # location string for "/"
    keeps_mtime: bool  # copies keep their original modification time
    deletes_empty_folders: bool = False

    def url(self, sub: str = "") -> str:
        return self.base + sub.lstrip("/")


KINDS = ["webdav", "ftp", "ftp-without-mlsd", "sftp"]


@pytest.fixture(params=KINDS)
def remote(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Remote]:
    kind: str = request.param
    root = tmp_path / "server"
    root.mkdir()
    if kind == "webdav":
        pytest.importorskip("wsgidav")
        pytest.importorskip("cheroot")
        from .remotekit import DavServer

        with DavServer(root) as dav:
            yield Remote(kind, root, dav.url(), keeps_mtime=False)
    elif kind in ("ftp", "ftp-without-mlsd"):
        pytest.importorskip("pyftpdlib")
        from .remotekit import FtpServer

        monkeypatch.setenv("DUPLIO_FTP_PASSWORD", "secret")
        without = ("MLSD", "MLST") if kind == "ftp-without-mlsd" else ()
        with FtpServer(root, without=without) as ftp:
            yield Remote(kind, root, ftp.url(), keeps_mtime=True, deletes_empty_folders=True)
    else:
        pytest.importorskip("paramiko")
        from .remotekit import SftpServer

        with SftpServer(root, tmp_path / "ssh") as sftp:
            sftp.install(monkeypatch)
            yield Remote(kind, root, sftp.url(), keeps_mtime=True, deletes_empty_folders=True)


@pytest.fixture
def reg() -> Iterator[BackendRegistry]:
    with BackendRegistry() as registry:
        yield registry


def test_walk_lists_files_with_sizes_and_exact_times(remote: Remote, reg: BackendRegistry) -> None:
    write(remote.root / "photos" / "a b.jpg", b"x" * 1234, mtime=OLD)
    write(remote.root / "photos" / "sub" / "ünï côdé.txt", b"hello", mtime=OLD + 5)
    write(remote.root / "photos" / "empty.bin", b"", mtime=OLD)
    loc = reg.open(remote.url("photos"))
    stats = WalkStats()
    found = {e.rel: (e.size, e.mtime_ns) for e in loc.backend.walk(loc.path, stats=stats)}
    assert found == {
        "a b.jpg": (1234, OLD * 10**9),
        "sub/ünï côdé.txt": (5, (OLD + 5) * 10**9),
        "empty.bin": (0, OLD * 10**9),
    }
    assert stats.files == 3 and stats.dirs == 2 and not stats.errors
    assert loc.caps.kind is MediaKind.NETWORK and loc.caps.read_cost == 2


def test_stat_and_listdir(remote: Remote, reg: BackendRegistry) -> None:
    write(remote.root / "d" / "f.txt", b"abc", mtime=OLD)
    (remote.root / "d" / "inner").mkdir()
    b = reg.open(remote.url()).backend
    f, d = b.stat("/d/f.txt"), b.stat("/d")
    assert f is not None and f.is_file and f.size == 3 and f.mtime_ns == OLD * 10**9
    assert d is not None and d.is_dir and not d.is_file
    assert b.stat("/nope") is None and b.stat("/d/nope.txt") is None and b.stat("/d/f.txt/x") is None
    assert sorted(b.listdir("/d")) == [("f.txt", False), ("inner", True)]


def test_stat_agrees_with_the_listing(remote: Remote, reg: BackendRegistry) -> None:
    """The change check compares what the scan saw with a fresh stat; the two must use the same facts."""
    write(remote.root / "d" / "f.txt", b"abc" * 100, mtime=OLD)
    loc = reg.open(remote.url("d"))
    (entry,) = list(loc.backend.walk(loc.path))
    info = loc.backend.stat(entry.path)
    assert info is not None and (info.size, info.mtime_ns) == (entry.size, entry.mtime_ns)


def test_reads_are_exact_ranged_and_seekable(remote: Remote, reg: BackendRegistry) -> None:
    data = rand_bytes(900_000, 1)
    write(remote.root / "big.bin", data, mtime=OLD)
    b = reg.open(remote.url()).backend
    assert b.read_range("/big.bin", 1000, 500) == data[1000:1500]
    assert b.read_range("/big.bin", 899_900, 500) == data[899_900:]
    assert b.read_range("/big.bin", 900_000, 10) == b""
    assert b.read_range("/big.bin", 0, 0) == b""
    with b.open_read("/big.bin") as handle:
        assert handle.read(10) == data[:10]
        handle.seek(700_000)
        assert handle.read(100) == data[700_000:700_100]
        handle.seek(5)
        assert handle.read(5) == data[5:10]
        handle.seek(0)
        assert handle.read() == data
        assert handle.read(10) == b""
    with pytest.raises(FileNotFoundError):
        b.open_read("/missing.bin")
    (remote.root / "folder").mkdir()
    with pytest.raises(IsADirectoryError):
        b.open_read("/folder")


def test_binary_files_survive_after_listings(remote: Remote, reg: BackendRegistry) -> None:
    """Line ends must never be rewritten: some protocols switch to text mode for listings."""
    data = b"line one\r\nline two\nline three\r\n\x00\xff\x0a\x0d" * 5000
    write(remote.root / "bin" / "crlf.dat", data, mtime=OLD)
    loc = reg.open(remote.url("bin"))
    for _ in range(3):
        assert [e.size for e in loc.backend.walk(loc.path)] == [len(data)]  # a listing, then a read on the same connection
        assert loc.backend.read_range(loc.backend.join(loc.path, "crlf.dat"), 3, 40) == data[3:43]
        with loc.backend.open_read(loc.backend.join(loc.path, "crlf.dat")) as handle:
            assert handle.read() == data


def test_writing_never_replaces_unless_asked(remote: Remote, reg: BackendRegistry) -> None:
    b = reg.open(remote.url()).backend
    with b.open_write("/new/deep/file.txt") as handle:
        handle.write(b"hello")
    target = remote.root / "new" / "deep" / "file.txt"
    assert target.read_bytes() == b"hello"
    with pytest.raises(FileExistsError), b.open_write("/new/deep/file.txt") as handle:
        handle.write(b"other")
    assert target.read_bytes() == b"hello"
    with b.open_write("/new/deep/file.txt", overwrite=True) as handle:
        handle.write(b"replaced")
    assert target.read_bytes() == b"replaced"
    assert sorted(p.name for p in target.parent.iterdir()) == ["file.txt"]  # no temporary files left


def test_an_error_while_writing_leaves_nothing_behind(remote: Remote, reg: BackendRegistry) -> None:
    b = reg.open(remote.url()).backend
    with pytest.raises(RuntimeError), b.open_write("/x/file.bin") as handle:
        handle.write(b"half")
        raise RuntimeError("interrupted")
    assert not (remote.root / "x" / "file.bin").exists()
    assert [p.name for p in (remote.root / "x").iterdir()] == []


def test_a_large_upload_round_trips(remote: Remote, reg: BackendRegistry) -> None:
    data = rand_bytes(12_000_000, 3)
    b = reg.open(remote.url()).backend
    with b.open_write("/big/up.bin") as handle:
        for i in range(0, len(data), 1_000_000):
            handle.write(data[i : i + 1_000_000])
    assert (remote.root / "big" / "up.bin").read_bytes() == data


def test_remove_deletes_single_files_and_refuses_folders(remote: Remote, reg: BackendRegistry) -> None:
    write(remote.root / "folder" / "keep.txt", b"important", mtime=OLD)
    write(remote.root / "gone.txt", b"x", mtime=OLD)
    b = reg.open(remote.url()).backend
    b.remove("/gone.txt")
    assert not (remote.root / "gone.txt").exists()
    with pytest.raises(IsADirectoryError):
        b.remove("/folder")
    assert (remote.root / "folder" / "keep.txt").read_bytes() == b"important"
    with pytest.raises(FileNotFoundError):
        b.remove("/gone.txt")


def test_rename_does_not_overwrite(remote: Remote, reg: BackendRegistry) -> None:
    write(remote.root / "a.txt", b"A", mtime=OLD)
    write(remote.root / "b.txt", b"B", mtime=OLD)
    b = reg.open(remote.url()).backend
    with pytest.raises(FileExistsError):
        b.rename("/a.txt", "/b.txt")
    assert (remote.root / "a.txt").read_bytes() == b"A" and (remote.root / "b.txt").read_bytes() == b"B"
    b.rename("/a.txt", "/moved/into/new/c.txt")
    assert (remote.root / "moved" / "into" / "new" / "c.txt").read_bytes() == b"A" and not (remote.root / "a.txt").exists()


def test_duplicates_are_found_across_a_computer_and_the_server(remote: Remote, reg: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(150_000, 7)
    write(remote.root / "photos" / "a.jpg", data, mtime=OLD)
    write(remote.root / "photos" / "unique.jpg", rand_bytes(150_000, 8), mtime=OLD)
    write(tmp_path / "local" / "copy.jpg", data, mtime=OLD)
    roots = make_roots([(reg.open(remote.url("photos")), RootRole.NORMAL), (reg.open(str(tmp_path / "local")), RootRole.NORMAL)])
    res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.ALL))
    assert len(res.groups) == 1
    assert sorted(m.rel for m in res.groups[0].members) == ["a.jpg", "copy.jpg"]


def test_verified_copies_in_both_directions(remote: Remote, reg: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(400_000, 9)
    write(tmp_path / "src.bin", data, mtime=OLD)
    local, there = reg.open(str(tmp_path / "src.bin")), reg.open(remote.url("backup/src.bin"))
    copy_verified((local.backend, local.path), (there.backend, there.path), algorithm="sha256", mtime_ns=OLD * 10**9)
    assert (remote.root / "backup" / "src.bin").read_bytes() == data
    if remote.keeps_mtime:
        assert int((remote.root / "backup" / "src.bin").stat().st_mtime) == OLD
    back = reg.open(str(tmp_path / "back.bin"))
    copy_verified((there.backend, there.path), (back.backend, back.path), algorithm="sha256")
    assert (tmp_path / "back.bin").read_bytes() == data


def test_a_connection_that_was_dropped_while_idle_is_replaced(remote: Remote, reg: BackendRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    write(remote.root / "f.txt", b"abc", mtime=OLD)
    b = reg.open(remote.url()).backend
    assert b.stat("/f.txt") is not None
    b.recover()  # what the retry logic does after a transient error: drop idle connections
    assert b.stat("/f.txt") is not None
    assert b.read_range("/f.txt", 0, 3) == b"abc"


def test_the_same_server_is_one_backend_and_the_password_is_not_in_the_address(remote: Remote, reg: BackendRegistry) -> None:
    a, c = reg.open(remote.url("a")), reg.open(remote.url("b"))
    assert a.backend is c.backend
    assert "secret" not in a.uri and a.uri.endswith("/a")


def test_empty_folders_go_but_never_ones_with_files(remote: Remote, reg: BackendRegistry) -> None:
    write(remote.root / "a" / "keep.txt", b"keep me", mtime=OLD)
    (remote.root / "a" / "plain-empty").mkdir()
    (remote.root / "a" / ".duplio-own-empty").mkdir()
    write(remote.root / "b" / "c" / "inner.txt", b"inside", mtime=OLD)
    b = reg.open(remote.url()).backend
    b.remove_empty_dirs("/a/.duplio-own-empty", "/")  # a folder Duplio made for itself, now empty
    assert not (remote.root / "a" / ".duplio-own-empty").exists()
    b.remove_empty_dirs("/a/plain-empty", "/")
    assert (not (remote.root / "a" / "plain-empty").exists()) is remote.deletes_empty_folders
    b.remove_empty_dirs("/b/c", "/")  # has a file: must stay, and so must its parents
    b.remove_empty_dirs("/a", "/")
    assert (remote.root / "b" / "c" / "inner.txt").read_bytes() == b"inside"
    assert (remote.root / "a" / "keep.txt").read_bytes() == b"keep me"


def test_the_drive_probe_runs_and_cleans_up(remote: Remote, reg: BackendRegistry) -> None:
    from duplio.vfs.probe import probe_location

    loc = reg.open(remote.url("probe-here"))
    loc.backend.makedirs(loc.path)
    report = probe_location(loc, throughput_mib=2)
    assert report.writable and report.latency_ms is not None
    assert report.read_mib_s is not None and report.write_mib_s is not None
    leftovers = sorted(p.name for p in (remote.root / "probe-here").iterdir())
    assert all(name.startswith(".duplio-probe-") for name in leftovers) and len(leftovers) <= (0 if remote.deletes_empty_folders else 1)
    if remote.kind == "webdav":
        # WsgiDAV cuts a name at a '?' when moving a file, so that character is correctly reported as not storable, and
        # its misnamed copy stays inside Duplio's own temporary folder.
        assert "?" in report.rejected_chars
        for folder in (remote.root / "probe-here").iterdir():
            assert {p.name for p in folder.iterdir()} <= {"chr-a"}
    else:
        for folder in (remote.root / "probe-here").iterdir():
            assert list(folder.iterdir()) == []  # whatever is left is an empty folder, never a stray file


def test_a_backup_repository_works_on_the_server(remote: Remote, reg: BackendRegistry, tmp_path: Path) -> None:
    from duplio.backup.repository import Repository

    write(tmp_path / "docs" / "a.txt", "hello " * 500, mtime=OLD)
    write(tmp_path / "docs" / "sub" / "b.bin", rand_bytes(300_000, 21), mtime=OLD)
    repo = Repository.init(reg.open(remote.url("repo")))
    repo.backup(reg.open(str(tmp_path / "docs")))
    repo.backup(reg.open(str(tmp_path / "docs")))  # a second run shares everything with the first
    assert repo.check(read_data=True).ok
    out = tmp_path / "out"
    Repository.open(reg.open(remote.url("repo"))).restore("latest", reg.open(str(out)))
    assert (out / "a.txt").read_text() == "hello " * 500
    assert (out / "sub" / "b.bin").read_bytes() == rand_bytes(300_000, 21)


def test_a_backslash_is_part_of_a_name_not_a_folder_separator(remote: Remote, reg: BackendRegistry) -> None:
    """On these servers `a\\b.txt` and `a/b.txt` are different files; mixing them up would hash and act on the wrong one."""
    write(remote.root / "dir" / "a\\b.txt", b"the file with a backslash in its name", mtime=OLD)
    write(remote.root / "dir" / "a" / "b.txt", b"the file inside folder a", mtime=OLD)
    loc = reg.open(remote.url("dir"))
    found = {e.rel: e for e in loc.backend.walk(loc.path)}
    assert sorted(found) == ["a/b.txt", "a\\b.txt"]
    for rel, expected in (("a\\b.txt", b"the file with a backslash in its name"), ("a/b.txt", b"the file inside folder a")):
        with loc.backend.open_read(found[rel].path) as handle:
            assert handle.read() == expected, rel
        info = loc.backend.stat(found[rel].path)
        assert info is not None and info.size == len(expected)
