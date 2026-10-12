from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from duplio.backup.repository import Repository
from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.errors import OverlappingRootsError
from duplio.plan.executor import apply_plan
from duplio.plan.planner import build_plan
from duplio.plan.undo import undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.adb import ADBBackend, list_devices
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write
from .fakes import FakeSMB, install_fake_adb

needs_coreutils = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("sh", "find", "stat", "sha256sum", "mv")), reason="needs POSIX coreutils"
)


# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_adb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    adb = install_fake_adb(tmp_path / "bin")
    monkeypatch.setenv("DUPLIO_ADB", str(adb))
    return adb


@needs_coreutils
def test_adb_device_listing(fake_adb: Path) -> None:
    assert list_devices() == [("FAKE123", "device", "Fake_Phone fake")]


@needs_coreutils
def test_adb_backend_basics(fake_adb: Path, tmp_path: Path) -> None:
    phone = tmp_path / "sdcard"
    write(phone / "DCIM" / "Camera" / "IMG_1.jpg", rand_bytes(10_000, 1), mtime=1_600_000_000)
    write(phone / "DCIM" / "it's got 'quotes' & spaces.jpg", b"q" * 50, mtime=1_600_000_000)
    write(phone / "DCIM" / ".thumbnails" / "t.jpg", b"t" * 10)
    write(phone / "Android" / "data" / "x.bin", b"x" * 10)
    from duplio.scan.filters import ScanFilter

    backend = ADBBackend()
    assert backend.serial == "FAKE123"
    entries = sorted(backend.walk(str(phone), walk_filter=ScanFilter()), key=lambda e: e.rel)
    assert [e.rel for e in entries] == ["Android/data/x.bin", "DCIM/Camera/IMG_1.jpg", "DCIM/it's got 'quotes' & spaces.jpg"]
    assert entries[1].mtime_ns == 1_600_000_000 * 10**9
    info = backend.stat(str(phone / "DCIM" / "Camera" / "IMG_1.jpg"))
    assert info is not None and info.is_file and info.size == 10_000
    assert backend.stat(str(phone / "nope")) is None
    quoted = str(phone / "DCIM" / "it's got 'quotes' & spaces.jpg")
    with backend.open_read(quoted) as handle:
        assert handle.read() == b"q" * 50
    digests = backend.remote_hash([quoted, str(phone / "DCIM" / "Camera" / "IMG_1.jpg")], "sha256")
    import hashlib

    assert digests[quoted].digest == hashlib.sha256(b"q" * 50).hexdigest()
    assert digests[quoted].size == 50
    dst = str(phone / "moved" / "q.jpg")
    backend.rename(quoted, dst)
    assert backend.stat(dst) is not None and backend.stat(quoted) is None
    with pytest.raises(FileExistsError):
        backend.rename(str(phone / "DCIM" / "Camera" / "IMG_1.jpg"), dst)
    with backend.open_write(str(phone / "pushed.txt")) as handle:
        handle.write(b"pushed")
    assert (phone / "pushed.txt").read_bytes() == b"pushed"
    backend.remove(dst)
    assert not Path(dst).exists()


@needs_coreutils
def test_phone_vs_computer_uses_on_device_hashing(fake_adb: Path, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    phone, pc = tmp_path / "sdcard" / "DCIM", tmp_path / "pc"
    shared = rand_bytes(300_000, 7)
    write(phone / "IMG_1.jpg", shared, mtime=1_600_000_000)
    write(phone / "IMG_2.jpg", rand_bytes(300_000, 8), mtime=1_600_000_000)
    write(pc / "Pictures" / "IMG_1.jpg", shared, mtime=1_600_000_000)
    reg = BackendRegistry()
    try:
        roots = make_roots([(reg.open(f"adb:{phone}"), RootRole.NORMAL), (reg.open(str(pc)), RootRole.REFERENCE)])
        res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.UNIQUE))
        assert [e.rel for e in res.unique] == ["IMG_2.jpg"]
        assert res.stats.remote_hashed == 2 and res.groups[0].algorithm == "sha256"
        # Clean the phone of what is already on the computer, then undo.
        res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.REFERENCE))
        plan = build_plan(res)
        assert [i.victim.rel for i in plan.items] == ["IMG_1.jpg"]
        report = apply_plan(plan, reg, dirs=dirs)
        assert report.count("done") == 1
        assert not (phone / "IMG_1.jpg").exists()
        assert list((phone / ".duplio-quarantine").rglob("IMG_1.jpg"))
        assert undo(None, reg, dirs=dirs).count("restored") == 1
        assert (phone / "IMG_1.jpg").read_bytes() == shared
    finally:
        reg.close()


@needs_coreutils
def test_same_phone_via_adb_and_mtp_refused(fake_adb: Path, tmp_path: Path) -> None:
    mtp = tmp_path / "gvfs" / "mtp:host=Google_Pixel_FAKE123" / "Internal"
    mtp.mkdir(parents=True)
    (tmp_path / "sdcard").mkdir()
    reg = BackendRegistry()
    try:
        with pytest.raises(OverlappingRootsError):
            make_roots([(reg.open(f"adb:{tmp_path / 'sdcard'}"), RootRole.NORMAL), (reg.open(str(mtp)), RootRole.NORMAL)])
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# SMB
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_smb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSMB:
    server = tmp_path / "nas"
    (server / "photos").mkdir(parents=True)
    fake = FakeSMB(server)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: fake)
    monkeypatch.setenv("DUPLIO_SMB_PASSWORD", "s3cret")
    return fake


def test_smb_dedupe_quarantine_and_undo(fake_smb: FakeSMB, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    data = rand_bytes(120_000, 5)
    write(fake_smb.root / "photos" / "2024" / "a.jpg", data, mtime=1_600_000_000)
    write(fake_smb.root / "photos" / "dupes" / "a (1).jpg", data, mtime=1_600_000_000)
    write(tmp_path / "local" / "a.jpg", data, mtime=1_600_000_000)
    reg = BackendRegistry()
    try:
        loc = reg.open("smb://me@nas/photos")
        assert fake_smb.sessions == []  # connections are opened lazily
        assert loc.uri == "smb://me@nas/photos"
        roots = make_roots([(loc, RootRole.NORMAL), (reg.open(str(tmp_path / "local")), RootRole.REFERENCE)])
        assert fake_smb.sessions == [("nas", "me", "s3cret", 445)]
        res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.REFERENCE))
        assert len(res.groups) == 1 and res.groups[0].count == 3
        plan = build_plan(res)
        assert sorted(i.victim.rel for i in plan.items) == ["2024/a.jpg", "dupes/a (1).jpg"]
        report = apply_plan(plan, reg, dirs=dirs)
        assert report.count("done") == 2
        assert not (fake_smb.root / "photos" / "2024" / "a.jpg").exists()
        assert len(list((fake_smb.root / "photos" / ".duplio-quarantine").rglob("*.jpg"))) == 2
        assert undo(None, reg, dirs=dirs).count("restored") == 2
        assert (fake_smb.root / "photos" / "2024" / "a.jpg").read_bytes() == data
    finally:
        reg.close()


def test_smb_write_semantics(fake_smb: FakeSMB) -> None:
    reg = BackendRegistry()
    try:
        loc = reg.open("smb://nas/photos")
        b = loc.backend
        target = b.join(loc.path, "new/dir/file.txt")
        assert target == "\\\\nas\\photos\\new\\dir\\file.txt"
        with b.open_write(target) as handle:
            handle.write(b"hello")
        assert (fake_smb.root / "photos" / "new" / "dir" / "file.txt").read_bytes() == b"hello"
        with pytest.raises(FileExistsError), b.open_write(target) as handle:
            handle.write(b"x")
        assert b.parent(target) == "\\\\nas\\photos\\new\\dir"
        assert b.is_within(target, "\\\\NAS\\Photos")  # SMB is case-insensitive
        assert not b.is_within("\\\\nas\\photos2\\x", "\\\\nas\\photos")
    finally:
        reg.close()


def test_backup_repository_on_smb(fake_smb: FakeSMB, tmp_path: Path) -> None:
    write(tmp_path / "docs" / "a.txt", "hello " * 500, mtime=1_600_000_000)
    reg = BackendRegistry()
    try:
        repo = Repository.init(reg.open("smb://nas/photos/repo"))
        repo.backup(reg.open(str(tmp_path / "docs")))
        assert repo.check(read_data=True).ok
        out = tmp_path / "out"
        Repository.open(reg.open("smb://nas/photos/repo")).restore("latest", reg.open(str(out)))
        assert (out / "a.txt").read_text() == "hello " * 500
    finally:
        reg.close()
