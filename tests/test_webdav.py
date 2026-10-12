"""WebDAV backend against a real WebDAV server (WsgiDAV), plus the ways a bad server could mislead it."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("wsgidav")
pytest.importorskip("cheroot")

from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.errors import LocationError
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.transfer import copy_verified
from duplio.vfs.base import MediaKind, WalkStats
from duplio.vfs.location import BackendRegistry, location_kind
from duplio.vfs.webdav import DavBackend, parse_dav, parse_http_date

from .conftest import rand_bytes, write
from .remotekit import DavServer

OLD = 1_600_000_000


@pytest.fixture
def dav(tmp_path: Path):  # type: ignore[no-untyped-def]
    with DavServer(tmp_path / "server") as server:
        yield server


@pytest.fixture
def reg():  # type: ignore[no-untyped-def]
    with BackendRegistry() as registry:
        yield registry


# --------------------------------------------------------------------------- addresses
def test_dav_addresses_are_parsed() -> None:
    t = parse_dav("davs://me:p%40ss@cloud.example.com/remote.php/dav/files/me/My%20Photos")
    assert (t.host, t.port, t.tls, t.username, t.password) == ("cloud.example.com", 443, True, "me", "p@ss")
    assert t.path == "/remote.php/dav/files/me/My Photos"
    plain = parse_dav("dav://nas:5005")
    assert (plain.port, plain.tls, plain.path) == (5005, False, "/")
    assert parse_dav("https://host/dav?x=1#frag").path == "/dav"
    assert parse_dav("davs://[::1]:8443/a").host == "::1"
    for bad in ("davs://", "davs://:80/x", "dav://host:notaport/x", "ftp://host/x"):
        with pytest.raises(LocationError):
            parse_dav(bad)


def test_dav_locations_are_recognised_and_shared(reg: BackendRegistry) -> None:
    assert location_kind("DAVS://h/x") == "dav" and location_kind("https://h/x") == "dav"
    a, b = reg.open("dav://alice@h:81/x"), reg.open("dav://alice@h:81/y")
    assert a.backend is b.backend and a.path == "/x"
    assert reg.open("dav://bob@h:81/x").backend is not a.backend  # another user sees other files
    assert a.uri == "dav://alice@h:81/x" and "secret" not in reg.open("dav://alice:secret@h:81/x").uri
    assert reg.open("dav://h/My%20Photos").path == "/My Photos"
    assert reg.open("dav://h/My%20Photos").uri == "dav://h/My%20Photos"


def test_http_dates_are_read_to_the_second() -> None:
    assert parse_http_date("Tue, 15 Nov 1994 08:12:31 GMT") == 784887151 * 10**9
    assert parse_http_date("2020-09-13T12:26:40Z") == 1600000000 * 10**9
    assert parse_http_date("") is None and parse_http_date("not a date") is None


# --------------------------------------------------------------------------- reading
def test_walk_lists_files_with_their_sizes_and_times(dav: DavServer, reg: BackendRegistry) -> None:
    write(dav.root / "photos" / "a b.jpg", b"x" * 1234, mtime=OLD)
    write(dav.root / "photos" / "sub" / "ünï.txt", b"hello", mtime=OLD + 5)
    write(dav.root / "photos" / "empty.bin", b"", mtime=OLD)
    loc = reg.open(dav.url("photos"))
    stats = WalkStats()
    found = {e.rel: (e.size, e.mtime_ns) for e in loc.backend.walk(loc.path, stats=stats)}
    assert found == {
        "a b.jpg": (1234, OLD * 10**9),
        "sub/ünï.txt": (5, (OLD + 5) * 10**9),
        "empty.bin": (0, OLD * 10**9),
    }
    assert stats.files == 3 and stats.dirs == 2 and not stats.errors
    assert loc.caps.kind is MediaKind.NETWORK and loc.caps.read_cost == 2


def test_stat_distinguishes_files_folders_and_nothing(dav: DavServer, reg: BackendRegistry) -> None:
    write(dav.root / "d" / "f.txt", b"abc", mtime=OLD)
    b = reg.open(dav.url()).backend
    f, d = b.stat("/d/f.txt"), b.stat("/d")
    assert f is not None and f.is_file and f.size == 3 and f.mtime_ns == OLD * 10**9
    assert d is not None and d.is_dir and not d.is_file
    assert b.stat("/nope") is None and b.stat("/d/nope.txt") is None
    assert b.listdir("/d") == [("f.txt", False)]


def test_ranged_reads_fetch_only_the_bytes_asked_for(dav: DavServer, reg: BackendRegistry) -> None:
    data = rand_bytes(3_000_000, 1)
    write(dav.root / "big.bin", data, mtime=OLD)
    b = reg.open(dav.url()).backend
    before = dav.app.sent
    assert b.read_range("/big.bin", 1_000_000, 500) == data[1_000_000:1_000_500]
    assert b.read_range("/big.bin", 2_999_900, 500) == data[2_999_900:]  # past the end: what exists
    assert b.read_range("/big.bin", 3_000_000, 10) == b""
    assert dav.app.sent - before < 20_000


def test_open_read_is_seekable_and_complete(dav: DavServer, reg: BackendRegistry) -> None:
    data = rand_bytes(700_000, 2)
    write(dav.root / "f.bin", data, mtime=OLD)
    b = reg.open(dav.url()).backend
    with b.open_read("/f.bin") as handle:
        assert handle.read(10) == data[:10]
        handle.seek(600_000)
        assert handle.read(100) == data[600_000:600_100]
        handle.seek(5)
        assert handle.read(5) == data[5:10]
        handle.seek(0)
        assert handle.read() == data
        assert handle.read(10) == b""
    with pytest.raises(FileNotFoundError):
        b.open_read("/missing")
    (dav.root / "folder").mkdir()
    with pytest.raises(IsADirectoryError):
        b.open_read("/folder")


def test_many_connections_are_reused(dav: DavServer, reg: BackendRegistry) -> None:
    for i in range(20):
        write(dav.root / f"f{i}.txt", f"file {i}", mtime=OLD)
    b = reg.open(dav.url()).backend
    for i in range(20):
        with b.open_read(f"/f{i}.txt") as handle:
            assert handle.read() == f"file {i}".encode()
    assert len(b._idle) <= 4  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- writing
def test_writing_never_replaces_unless_asked(dav: DavServer, reg: BackendRegistry) -> None:
    b = reg.open(dav.url()).backend
    with b.open_write("/new/deep/file.txt") as handle:
        handle.write(b"hello")
    assert (dav.root / "new" / "deep" / "file.txt").read_bytes() == b"hello"
    with pytest.raises(FileExistsError), b.open_write("/new/deep/file.txt") as handle:
        handle.write(b"other")
    assert (dav.root / "new" / "deep" / "file.txt").read_bytes() == b"hello"
    with b.open_write("/new/deep/file.txt", overwrite=True) as handle:
        handle.write(b"replaced")
    assert (dav.root / "new" / "deep" / "file.txt").read_bytes() == b"replaced"
    assert sorted(p.name for p in (dav.root / "new" / "deep").iterdir()) == ["file.txt"]  # no temporary files left


def test_an_error_while_writing_leaves_nothing_behind(dav: DavServer, reg: BackendRegistry) -> None:
    b = reg.open(dav.url()).backend
    with pytest.raises(RuntimeError), b.open_write("/x/file.bin") as handle:
        handle.write(b"half")
        raise RuntimeError("interrupted")
    assert not (dav.root / "x" / "file.bin").exists()
    assert [p.name for p in (dav.root / "x").iterdir()] == []


def test_large_upload_round_trips(dav: DavServer, reg: BackendRegistry) -> None:
    data = rand_bytes(20_000_000, 3)  # larger than the in-memory spool
    b = reg.open(dav.url()).backend
    with b.open_write("/big/up.bin") as handle:
        for i in range(0, len(data), 1_000_000):
            handle.write(data[i : i + 1_000_000])
    assert (dav.root / "big" / "up.bin").read_bytes() == data


def test_remove_deletes_files_but_refuses_folders(dav: DavServer, reg: BackendRegistry) -> None:
    write(dav.root / "folder" / "keep.txt", b"important", mtime=OLD)
    write(dav.root / "gone.txt", b"x", mtime=OLD)
    b = reg.open(dav.url()).backend
    b.remove("/gone.txt")
    assert not (dav.root / "gone.txt").exists()
    with pytest.raises(IsADirectoryError):
        b.remove("/folder")  # DELETE on a folder would remove everything in it
    assert (dav.root / "folder" / "keep.txt").read_bytes() == b"important"
    with pytest.raises(FileNotFoundError):
        b.remove("/gone.txt")
    b.remove_empty_dirs("/folder", "/")  # deliberately does nothing
    assert (dav.root / "folder").is_dir()


def test_rename_does_not_overwrite(dav: DavServer, reg: BackendRegistry) -> None:
    write(dav.root / "a.txt", b"A", mtime=OLD)
    write(dav.root / "b.txt", b"B", mtime=OLD)
    b = reg.open(dav.url()).backend
    with pytest.raises(FileExistsError):
        b.rename("/a.txt", "/b.txt")
    assert (dav.root / "a.txt").read_bytes() == b"A" and (dav.root / "b.txt").read_bytes() == b"B"
    b.rename("/a.txt", "/moved/into/new/c.txt")
    assert (dav.root / "moved" / "into" / "new" / "c.txt").read_bytes() == b"A" and not (dav.root / "a.txt").exists()


def test_free_space_is_reported_or_unknown(dav: DavServer, reg: BackendRegistry) -> None:
    free = reg.open(dav.url()).backend.free_space("/")
    assert free is None or free > 0  # WsgiDAV does not implement quotas; either answer is acceptable


# --------------------------------------------------------------------------- passwords
@pytest.mark.parametrize("auth", ["basic", "digest"])
def test_passwords_are_required_and_used(tmp_path: Path, auth: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with DavServer(tmp_path / "s", auth) as server, BackendRegistry() as reg:
        write(server.root / "f.txt", b"secret stuff", mtime=OLD)
        anonymous = reg.open(server.url()).backend
        with pytest.raises(PermissionError):
            anonymous.stat("/f.txt")
        monkeypatch.setenv("DUPLIO_DAV_PASSWORD", "wrong")
        wrong = BackendRegistry().open(server.url(user="alice")).backend
        with pytest.raises(PermissionError):
            wrong.stat("/f.txt")
        monkeypatch.setenv("DUPLIO_DAV_PASSWORD", "secret")
        good = BackendRegistry().open(server.url(user="alice")).backend
        info = good.stat("/f.txt")
        assert info is not None and info.size == 12
        with good.open_read("/f.txt") as handle:
            assert handle.read() == b"secret stuff"
        with good.open_write("/g.txt") as handle:
            handle.write(b"written with credentials")
        assert (server.root / "g.txt").read_bytes() == b"written with credentials"
        for _ in range(30):  # many requests on one nonce
            assert good.stat("/f.txt") is not None


def test_a_password_can_come_from_a_prompt(tmp_path: Path) -> None:
    asked: list[tuple[str, str | None]] = []

    def prompt(host: str, user: str | None) -> str | None:
        asked.append((host, user))
        return "secret"

    with DavServer(tmp_path / "s", "basic") as server, BackendRegistry(credential_provider=prompt) as reg:
        write(server.root / "f.txt", b"x", mtime=OLD)
        assert reg.open(server.url(user="alice")).backend.stat("/f.txt") is not None
        assert asked == [("127.0.0.1", "alice")]


# --------------------------------------------------------------------------- the whole tools on a WebDAV drive
def test_duplicates_are_found_across_a_computer_and_a_webdav_drive(dav: DavServer, reg: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(150_000, 7)
    write(dav.root / "photos" / "a.jpg", data, mtime=OLD)
    write(dav.root / "photos" / "unique.jpg", rand_bytes(150_000, 8), mtime=OLD)
    write(tmp_path / "local" / "copy.jpg", data, mtime=OLD)
    roots = make_roots([(reg.open(dav.url("photos")), RootRole.NORMAL), (reg.open(str(tmp_path / "local")), RootRole.NORMAL)])
    res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.ALL))
    assert len(res.groups) == 1
    assert sorted(m.rel for m in res.groups[0].members) == ["a.jpg", "copy.jpg"]


def test_copy_to_and_from_a_webdav_drive_is_verified(dav: DavServer, reg: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(400_000, 9)
    write(tmp_path / "src.bin", data, mtime=OLD)
    local, remote = reg.open(str(tmp_path / "src.bin")), reg.open(dav.url("backup/src.bin"))
    copy_verified((local.backend, local.path), (remote.backend, remote.path), algorithm="sha256", mtime_ns=OLD * 10**9)
    assert (dav.root / "backup" / "src.bin").read_bytes() == data
    back = reg.open(str(tmp_path / "back.bin"))
    copy_verified((remote.backend, remote.path), (back.backend, back.path), algorithm="sha256")
    assert (tmp_path / "back.bin").read_bytes() == data


def test_a_thumbnail_from_a_webdav_drive_costs_kilobytes(dav: DavServer, reg: BackendRegistry, dirs) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("PIL")
    from duplio.preview.service import PreviewItem, PreviewService

    from .previewkit import photo_with_exif_thumbnail

    photo_with_exif_thumbnail(dav.root / "IMG_1.jpg", padding=4_000_000)
    loc = reg.open(dav.url("IMG_1.jpg"))
    info = loc.backend.stat(loc.path)
    assert info is not None and info.size > 4_000_000
    service = PreviewService(dirs)
    before = dav.app.sent
    preview = service.get(PreviewItem(loc.backend, loc.path, info.size, info.mtime_ns, "IMG_1.jpg", False))
    assert preview.thumb.source == "embedded" and dav.app.sent - before < 100_000
    again = dav.app.sent
    assert service.get(PreviewItem(loc.backend, loc.path, info.size, info.mtime_ns, "IMG_1.jpg", False)).from_cache
    assert dav.app.sent == again


# --------------------------------------------------------------------------- a server that lies
class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""
    status = 207

    def do_PROPFIND(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


def _hostile(body: str):  # type: ignore[no-untyped-def]
    handler = type("H", (_Handler,), {"body": body.encode()})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _multistatus(*entries: str) -> str:
    return '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">' + "".join(entries) + "</D:multistatus>"


def _entry(href: str, *, dir: bool = False, size: int | None = 5, modified: str | None = "Tue, 15 Nov 1994 08:12:31 GMT") -> str:
    props = "<D:resourcetype><D:collection/></D:resourcetype>" if dir else "<D:resourcetype/>"
    if size is not None:
        props += f"<D:getcontentlength>{size}</D:getcontentlength>"
    if modified is not None:
        props += f"<D:getlastmodified>{modified}</D:getlastmodified>"
    return f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"


def test_names_that_would_escape_the_folder_are_refused() -> None:
    server = _hostile(
        _multistatus(
            _entry("/d/", dir=True),
            _entry("/d/good.txt"),
            _entry("/d/..%2F..%2Fescape.txt"),
            _entry("/d/a%2Fb.txt"),
            _entry("/d/%2E%2E"),
        )
    )
    try:
        backend = DavBackend(parse_dav(f"dav://127.0.0.1:{server.server_port}/d"))
        stats = WalkStats()
        assert [e.rel for e in backend.walk("/d", stats=stats)] == ["good.txt"]
        assert len(stats.errors) == 2 and all("separator" in why for _, why in stats.errors)
        assert backend.listdir("/d") == [("good.txt", False)]
    finally:
        server.shutdown()


def test_a_file_without_size_or_time_is_reported_not_guessed() -> None:
    server = _hostile(
        _multistatus(
            _entry("/d/", dir=True),
            _entry("/d/ok.txt"),
            _entry("/d/no-time.txt", modified=None),
            _entry("/d/no-size.txt", size=None),
        )
    )
    try:
        backend = DavBackend(parse_dav(f"dav://127.0.0.1:{server.server_port}/d"))
        stats = WalkStats()
        assert [e.rel for e in backend.walk("/d", stats=stats)] == ["ok.txt"]
        assert sorted(Path(p).name for p, _ in stats.errors) == ["no-size.txt", "no-time.txt"]
    finally:
        server.shutdown()


def test_a_listing_with_a_dtd_is_refused() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">]>' + _multistatus(_entry("/d/", dir=True))
    server = _hostile(bomb)
    try:
        backend = DavBackend(parse_dav(f"dav://127.0.0.1:{server.server_port}/d"))
        stats = WalkStats()
        assert list(backend.walk("/d", stats=stats)) == []
        assert stats.errors and "DTD" in stats.errors[0][1]
    finally:
        server.shutdown()


def test_a_server_that_goes_away_gives_an_error_not_a_hang(tmp_path: Path) -> None:
    with DavServer(tmp_path / "s") as server:
        port = server.port
    backend = DavBackend(parse_dav(f"dav://127.0.0.1:{port}/"), timeout=2)
    started = time.monotonic()
    with pytest.raises(OSError):
        backend.stat("/")
    assert time.monotonic() - started < 10
