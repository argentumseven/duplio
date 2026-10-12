from __future__ import annotations

import io
import os
import struct
from datetime import datetime
from pathlib import Path

import pytest

from duplio.backup.ingest import IngestOptions, ingest, render_layout
from duplio.backup.media import capture_date, date_from_mp4, date_from_name
from duplio.backup.mirror import MirrorOptions, plan_mirror, prune_versions, run_mirror
from duplio.backup.repository import (
    Repository,
    SnapshotMeta,
    decode_object,
    encode_object,
    object_id,
)
from duplio.backup.retention import RetentionPolicy, apply_policy
from duplio.errors import ConfigError, DuplioError, LockError, RepositoryError, SafetyError
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    write(src / "docs" / "a.txt", "alpha " * 1000, mtime=1_600_000_000)
    write(src / "docs" / "b.txt", "alpha " * 1000, mtime=1_600_000_000)  # identical content: stored once
    write(src / "big.bin", rand_bytes(300_000, 1), mtime=1_600_000_000)
    write(src / "empty", "", mtime=1_600_000_000)
    write(src / ".git" / "HEAD", "ref: refs/heads/main", mtime=1_600_000_000)
    os.chmod(src / "big.bin", 0o640)
    return src


def make_repo(registry: BackendRegistry, tmp_path: Path) -> Repository:
    return Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)


def test_object_encoding_roundtrip() -> None:
    data = b"x" * 10_000
    assert decode_object(encode_object(data, True)) == data
    assert len(encode_object(data, True)) < 1000
    assert decode_object(encode_object(data, False)) == data
    with pytest.raises(RepositoryError):
        decode_object(b"nope")


def test_init_refuses_non_empty(registry: BackendRegistry, tmp_path: Path) -> None:
    write(tmp_path / "busy" / "file", "x")
    with pytest.raises(RepositoryError):
        Repository.init(registry.open(str(tmp_path / "busy")))
    repo = make_repo(registry, tmp_path)
    with pytest.raises(RepositoryError):
        Repository.init(repo.loc)
    with pytest.raises(RepositoryError):
        Repository.open(registry.open(str(tmp_path / "missing")))


def test_backup_restore_roundtrip(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    report = repo.backup(registry.open(str(source)), label="docs")
    snap = report.snapshot
    assert snap.files == 5  # empty files and VCS data are part of backups
    assert snap.bytes == 6000 * 2 + 300_000 + len("ref: refs/heads/main")
    assert snap.new_bytes == 6000 + 300_000 + len("ref: refs/heads/main")  # duplicate a/b stored once
    target = tmp_path / "restored"
    r = Repository.open(repo.loc).restore("latest", registry.open(str(target)))
    assert r.restored == 5 and not r.errors
    for rel in ("docs/a.txt", "docs/b.txt", "big.bin", "empty", ".git/HEAD"):
        assert (target / rel).read_bytes() == (source / rel).read_bytes()
        assert (target / rel).stat().st_mtime_ns == (source / rel).stat().st_mtime_ns
    assert (target / "big.bin").stat().st_mode & 0o777 == 0o640


def test_incremental_backup_reuses_unchanged(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    write(source / "new.txt", "brand new", mtime=1_600_000_001)
    second = repo.backup(registry.open(str(source))).snapshot
    assert second.reused_files == 5
    assert second.new_bytes == len("brand new")
    assert second.parent is not None
    diff = repo.diff(second.parent, second.id)
    assert diff["added"] == ["new.txt"] and not diff["removed"] and not diff["modified"]


def test_restore_conflicts(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    target = tmp_path / "t"
    write(target / "docs" / "a.txt", "local edits")
    write(target / "big.bin", (source / "big.bin").read_bytes())
    r = repo.restore("latest", registry.open(str(target)), conflict="skip")
    assert r.conflicts == ["docs/a.txt"] and r.unchanged == 1
    assert (target / "docs" / "a.txt").read_text() == "local edits"
    r = repo.restore("latest", registry.open(str(target)), conflict="rename", include=["docs/**"])
    assert r.renamed and (target / r.renamed[0]).read_bytes() == (source / "docs" / "a.txt").read_bytes()
    assert (target / "docs" / "a.txt").read_text() == "local edits"
    r = repo.restore("latest", registry.open(str(target)), conflict="overwrite", include=["docs/a.txt"])
    assert (target / "docs" / "a.txt").read_bytes() == (source / "docs" / "a.txt").read_bytes()


def test_check_detects_corruption_and_missing(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    assert repo.check(read_data=True).ok
    objects = sorted((tmp_path / "repo" / "objects").rglob("*"))
    files = [p for p in objects if p.is_file()]
    victim = files[0]
    blob = bytearray(victim.read_bytes())
    blob[-1] ^= 0xFF
    victim.write_bytes(bytes(blob))
    report = Repository.open(repo.loc).check(read_data=True)
    assert not report.ok and victim.name in report.corrupt
    files[1].unlink()
    report = Repository.open(repo.loc).check()
    assert report.missing
    # Restoring reports the damage per file instead of writing bad data.
    r = Repository.open(repo.loc).restore("latest", registry.open(str(tmp_path / "out")))
    assert r.errors


def test_forget_and_prune(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    first = repo.backup(registry.open(str(source))).snapshot
    write(source / "big.bin", rand_bytes(300_000, 2), mtime=1_600_000_002)
    repo.backup(registry.open(str(source)))
    report = repo.prune(dry_run=True, grace_s=0, min_age_s=0)
    objects = report.objects
    assert objects == 0
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    report = repo.prune(grace_s=0, min_age_s=0)
    assert report.objects > 0 and report.bytes > 0
    assert repo.check(read_data=True).ok


def test_lock_blocks_concurrent_writers(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make_repo(registry, tmp_path)
    with repo.lock("backup"), pytest.raises(LockError):
        repo.backup(registry.open(str(source)))
    # A stale lock from a dead process on this host is reclaimed automatically.
    import json
    import socket

    (tmp_path / "repo" / "locks" / "repo.lock").write_text(json.dumps({"pid": 999999999, "host": socket.gethostname(), "time": 0}))
    repo.backup(registry.open(str(source)))


def test_repository_inside_source_refused(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = Repository.init(registry.open(str(source / "repo-inside")))
    with pytest.raises(RepositoryError):
        repo.backup(registry.open(str(source)))


def test_object_id_is_blake2b() -> None:
    import hashlib

    assert object_id(b"abc") == hashlib.blake2b(b"abc", digest_size=32).hexdigest()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def snap(day: datetime, n: int = 0, label: str = "") -> SnapshotMeta:
    return SnapshotMeta(id=f"{day:%Y%m%d}-{n}", time=day.isoformat(), host="h", source="/s", label=label, tags=[],
                        parent=None, files=1, bytes=1)


def test_retention_policy() -> None:
    days = [datetime(2026, 1, d, 12) for d in range(1, 32)] + [datetime(2025, m, 15, 12) for m in range(1, 13)]
    snaps = [snap(d) for d in days]
    decision = apply_policy(snaps, RetentionPolicy(keep_daily=7, keep_monthly=3))
    kept = sorted(s.id for s, _ in decision.keep)
    assert len(kept) == 7 + 2  # 7 most recent days; months Jan-2026 (already kept), Dec, Nov 2025
    assert "20260131-0" in kept and "20251215-0" in kept and "20251115-0" in kept
    assert "20251015-0" not in kept
    assert len(decision.keep) + len(decision.remove) == len(snaps)


def test_retention_keeps_newest_and_refuses_empty() -> None:
    snaps = [snap(datetime(2026, 1, 1)), snap(datetime(2026, 1, 2), label="phone")]
    with pytest.raises(ConfigError):
        apply_policy(snaps, RetentionPolicy())
    decision = apply_policy(snaps, RetentionPolicy(keep_tags=["nothing-matches"]))
    assert not decision.remove  # newest of each (source, label) group is always kept


# ---------------------------------------------------------------------------
# Mirror
# ---------------------------------------------------------------------------


def test_mirror_copies_versions_and_moves(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", "A1", mtime=1_600_000_000)
    write(src / "photos" / "p.jpg", rand_bytes(50_000, 1), mtime=1_600_000_000)
    s, d = registry.open(str(src)), registry.open(str(dst))
    plan = plan_mirror(s, d)
    assert plan.count("copy") == 2
    report = run_mirror(plan)
    assert report.done["copy"] == 2 and (dst / "photos" / "p.jpg").read_bytes() == (src / "photos" / "p.jpg").read_bytes()
    assert (dst / "a.txt").stat().st_mtime_ns == (src / "a.txt").stat().st_mtime_ns
    assert not plan_mirror(s, d).actions  # in sync
    # Change, move and delete in the source.
    write(src / "a.txt", "A2-changed", mtime=1_600_000_100)
    os.rename(src / "photos" / "p.jpg", src / "p-moved.jpg")
    write(src / "new.txt", "new", mtime=1_600_000_200)
    opts = MirrorOptions(delete_extraneous=True, force=True)
    plan = plan_mirror(s, d, options=opts)
    kinds = sorted((a.kind, a.rel) for a in plan.actions)
    assert kinds == [("copy", "new.txt"), ("move", "p-moved.jpg"), ("update", "a.txt")]
    report = run_mirror(plan, options=opts)
    assert not report.errors
    assert (dst / "a.txt").read_text() == "A2-changed"
    assert (dst / "p-moved.jpg").exists() and not (dst / "photos" / "p.jpg").exists()
    versions = list((dst / ".duplio-versions").rglob("a.txt"))
    assert len(versions) == 1 and versions[0].read_text() == "A1"


def test_mirror_archives_instead_of_deleting(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "keep.txt", "k")
    write(dst / "keep.txt", "k")
    write(dst / "gone.txt", "only in dest")
    s, d = registry.open(str(src)), registry.open(str(dst))
    plan = plan_mirror(s, d)
    assert plan.count("archive") == 0  # without --delete extraneous files stay
    opts = MirrorOptions(delete_extraneous=True, force=True)
    run_mirror(plan_mirror(s, d, options=opts), options=opts)
    assert not (dst / "gone.txt").exists()
    assert [p.read_text() for p in (dst / ".duplio-versions").rglob("gone.txt")] == ["only in dest"]


def test_mirror_brake_on_empty_or_massive_change(registry: BackendRegistry, tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for i in range(30):
        write(dst / f"f{i}.txt", f"{i}")
    s, d = registry.open(str(src)), registry.open(str(dst))
    with pytest.raises(SafetyError, match="source is empty"):
        plan_mirror(s, d, options=MirrorOptions(delete_extraneous=True))
    write(src / "f0.txt", "0")
    with pytest.raises(SafetyError, match="would be replaced or archived"):
        plan_mirror(s, d, options=MirrorOptions(delete_extraneous=True))
    assert plan_mirror(s, d, options=MirrorOptions(delete_extraneous=True, force=True)).count("archive") == 29


def test_mirror_refuses_overlap(registry: BackendRegistry, tmp_path: Path) -> None:
    write(tmp_path / "a" / "x", "1")
    with pytest.raises(SafetyError):
        plan_mirror(registry.open(str(tmp_path / "a")), registry.open(str(tmp_path / "a" / "backup")))


def test_prune_versions(registry: BackendRegistry, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    write(dst / ".duplio-versions" / "20000101T000000Z" / "old.txt", "old")
    write(dst / ".duplio-versions" / "29990101T000000Z" / "future.txt", "new")
    assert prune_versions(registry.open(str(dst)), "30d") == 1
    assert not (dst / ".duplio-versions" / "20000101T000000Z").exists()
    assert (dst / ".duplio-versions" / "29990101T000000Z" / "future.txt").exists()


# ---------------------------------------------------------------------------
# Media dates and ingest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_20240131_235959.jpg", datetime(2024, 1, 31, 23, 59, 59)),
        ("PXL_20240712_101500123.jpg", datetime(2024, 7, 12, 10, 15, 0)),
        ("Screenshot_2024-01-31-23-59-59-123_com.app.png", datetime(2024, 1, 31, 23, 59, 59)),
        ("IMG-20240131-WA0007.jpg", datetime(2024, 1, 31)),
        ("signal-2024-02-29-120000.jpg", datetime(2024, 2, 29, 12, 0, 0)),
        ("20231231_101010.mp4", datetime(2023, 12, 31, 10, 10, 10)),
    ],
)
def test_date_from_name(name: str, expected: datetime) -> None:
    assert date_from_name(name) == expected


def test_date_from_name_rejects_nonsense() -> None:
    assert date_from_name("IMG_1234.jpg") is None
    assert date_from_name("20241399_999999.jpg") is None


def _mp4(created: int) -> bytes:
    mvhd_payload = bytes([0, 0, 0, 0]) + struct.pack(">I", created) + b"\0" * 92
    mvhd = struct.pack(">I4s", 8 + len(mvhd_payload), b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
    mdat = struct.pack(">I4s", 8 + 100, b"mdat") + b"\0" * 100
    return ftyp + mdat + moov  # moov at the end, as many phones write it


def test_date_from_mp4() -> None:
    epoch = int(datetime(2024, 5, 6, 7, 8, 9).timestamp())
    dt = date_from_mp4(io.BytesIO(_mp4(epoch + 2082844800)))
    assert dt == datetime(2024, 5, 6, 7, 8, 9)
    assert date_from_mp4(io.BytesIO(b"not a video")) is None


def test_date_from_exif() -> None:
    PIL = pytest.importorskip("PIL.Image")
    img = PIL.new("RGB", (8, 8))
    exif = img.getexif()
    exif[0x8769] = {36867: "2021:03:04 05:06:07"}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    dt, source = capture_date("whatever.jpg", buf, 0)
    assert (dt, source) == (datetime(2021, 3, 4, 5, 6, 7), "EXIF")


def test_render_layout() -> None:
    when = datetime(2024, 7, 12, 10, 15, 0)
    assert render_layout("{year}/{year}-{month}/{name}", name="a.jpg", when=when, kind="Photos", device="", source_dir="") == "2024/2024-07/a.jpg"
    assert render_layout("{kind}/{date}/{stem}.{ext}", name="B.JPG", when=when, kind="Photos", device="", source_dir="") == "Photos/2024-07-12/B.jpg"
    assert render_layout("../{name}", name="a.jpg", when=when, kind="", device="", source_dir="") == "a.jpg"
    with pytest.raises(DuplioError):
        render_layout("{nope}/{name}", name="a.jpg", when=when, kind="", device="", source_dir="")


def test_ingest_flow(registry: BackendRegistry, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    phone, lib = tmp_path / "phone" / "DCIM", tmp_path / "library"
    a = rand_bytes(40_000, 1)
    b = rand_bytes(40_000, 2)
    write(phone / "Camera" / "IMG_20240131_101010.jpg", a, mtime=1_700_000_000)
    write(phone / "Camera" / "VID_20231225_090000.mp4", b, mtime=1_700_000_000)
    write(phone / "Camera" / "notes.txt", "not media", mtime=1_700_000_000)
    already = rand_bytes(30_000, 3)
    write(phone / "Camera" / "IMG_20220101_000000.jpg", already, mtime=1_700_000_000)
    write(lib / "old" / "existing.jpg", already, mtime=1_600_000_000)
    src, dst = registry.open(str(tmp_path / "phone")), registry.open(str(lib))

    preview = ingest(src, dst, options=IngestOptions(dry_run=True), dirs=dirs)
    assert preview.count("would-import") == 2 and preview.count("maybe-duplicate") == 1
    assert not (lib / "2024").exists()

    report = ingest(src, dst, dirs=dirs)
    assert report.count("imported") == 2 and report.count("duplicate") == 1
    assert (lib / "2024" / "2024-01" / "IMG_20240131_101010.jpg").read_bytes() == a
    assert (lib / "2023" / "2023-12" / "VID_20231225_090000.mp4").read_bytes() == b
    assert not list(lib.rglob("notes.txt"))
    assert not list(lib.rglob("*.part"))

    # Second run: everything is known from the ledger, nothing is read again.
    again = ingest(src, dst, dirs=dirs)
    assert again.count("previously-imported") == 3 and again.count("imported") == 0

    # Deleting from the library does not cause a re-import...
    (lib / "2024" / "2024-01" / "IMG_20240131_101010.jpg").unlink()
    assert ingest(src, dst, dirs=dirs).count("imported") == 0
    # ...unless asked.
    assert ingest(src, dst, dirs=dirs, options=IngestOptions(reimport_deleted=True)).count("imported") == 1


def test_ingest_name_collision_and_free_source(registry: BackendRegistry, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    phone, lib = tmp_path / "phone", tmp_path / "library"
    write(phone / "a" / "IMG_20240101_120000.jpg", rand_bytes(5000, 1), mtime=1_700_000_000)
    write(phone / "b" / "IMG_20240101_120000.jpg", rand_bytes(5000, 2), mtime=1_700_000_000)
    src, dst = registry.open(str(phone)), registry.open(str(lib))
    with pytest.raises(SafetyError):
        ingest(src, dst, dirs=dirs, options=IngestOptions(free_source=True))
    report = ingest(src, dst, dirs=dirs, options=IngestOptions(free_source=True), confirm_free=True)
    assert report.count("imported") == 2
    names = sorted(p.name for p in (lib / "2024" / "2024-01").iterdir())
    assert names == ["IMG_20240101_120000 (2).jpg", "IMG_20240101_120000.jpg"]
    assert report.freed == 2
    assert not list(phone.rglob("*.jpg"))


def test_ingest_never_frees_files_only_known_from_ledger(registry: BackendRegistry, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    phone, lib = tmp_path / "phone", tmp_path / "library"
    write(phone / "IMG_20240101_120000.jpg", rand_bytes(5000, 1), mtime=1_700_000_000)
    src, dst = registry.open(str(phone)), registry.open(str(lib))
    ingest(src, dst, dirs=dirs)
    for p in lib.rglob("*.jpg"):
        p.unlink()  # the user deleted the library copy
    report = ingest(src, dst, dirs=dirs, options=IngestOptions(free_source=True), confirm_free=True)
    assert report.freed == 0
    assert (phone / "IMG_20240101_120000.jpg").exists()
