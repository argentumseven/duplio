"""The window's logic without a window: what rows a comparison becomes, which duplicate copy stays, how jobs and thumbnails behave.

Nothing here needs Tk, so it runs on every Python Duplio supports.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from duplio.backup.ingest import IngestItem, IngestReport
from duplio.compare.diff import DiffOptions, diff_inventory
from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions, Verify
from duplio.errors import ConfigError
from duplio.gui.jobs import Job, explain
from duplio.gui.model import (
    Mark,
    RowKind,
    apply_rules,
    backup_verdict,
    build_group_rows,
    build_pair_rows,
    build_server_address,
    chosen_group_ids,
    count_rows,
    file_kind,
    ingest_rows,
    keep_overrides,
)
from duplio.gui.state import GuiState
from duplio.hashing.cache import NullCache
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

OLD = 1_600_000_000


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "phone", tmp_path / "backup"
    write(a / "same.jpg", rand_bytes(5000, 1), mtime=OLD)
    write(b / "same.jpg", rand_bytes(5000, 1), mtime=OLD)
    write(a / "IMG_1.jpg", rand_bytes(4000, 2), mtime=OLD)
    write(b / "2024" / "Holiday.jpg", rand_bytes(4000, 2), mtime=OLD)  # the same picture, another name and folder
    write(a / "only-a.jpg", rand_bytes(3000, 3), mtime=OLD)
    write(b / "only-b.jpg", rand_bytes(3500, 4), mtime=OLD)
    write(a / "edited.txt", rand_bytes(2000, 5), mtime=OLD)
    write(b / "edited.txt", rand_bytes(2000, 6), mtime=OLD + 100)  # same name, other content
    write(a / "twin1.bin", rand_bytes(1500, 7), mtime=OLD)
    write(a / "twin2.bin", rand_bytes(1500, 7), mtime=OLD)
    return a, b


def diff_of(a: Path, b: Path):  # type: ignore[no-untyped-def]
    with BackendRegistry() as reg:
        roots = make_roots([(reg.open(str(a)), RootRole.NORMAL), (reg.open(str(b)), RootRole.NORMAL)])
        return diff_inventory(scan(roots, ScanFilter(min_size=0)), DiffOptions(), cache=NullCache())


# --------------------------------------------------------------------------- A against B
def test_rows_put_a_on_the_left_and_b_on_the_right(folders: tuple[Path, Path]) -> None:
    rows = build_pair_rows(diff_of(*folders))
    by_name = {(r.left.rel if r.left else None, r.right.rel if r.right else None): r for r in rows}
    assert by_name[("same.jpg", "same.jpg")].kind is RowKind.SAME
    assert by_name[("only-a.jpg", None)].kind is RowKind.ONLY_A
    assert by_name[(None, "only-b.jpg")].kind is RowKind.ONLY_B
    moved = next(r for r in rows if r.kind is RowKind.MOVED)
    assert moved.left is not None and moved.left.rel == "IMG_1.jpg" and moved.right is not None and moved.right.rel == "2024/Holiday.jpg"
    changed = next(r for r in rows if r.kind is RowKind.CHANGED)
    assert changed.left is not None and changed.right is not None and changed.left.rel == changed.right.rel == "edited.txt"
    assert all(r.left is None or r.left.side == 0 for r in rows) and all(r.right is None or r.right.side == 1 for r in rows)
    counts = count_rows(rows)
    assert counts[RowKind.ONLY_A] >= 1 and counts[RowKind.ONLY_B] == 1 and counts[RowKind.SAME] == 1


def test_every_file_appears_exactly_once(folders: tuple[Path, Path]) -> None:
    a, b = folders
    rows = build_pair_rows(diff_of(a, b))
    shown = sorted(t.key for r in rows for t in r.tiles)
    assert len(shown) == len(set(shown))  # no file is drawn twice
    on_disk = len([p for p in a.rglob("*") if p.is_file()]) + len([p for p in b.rglob("*") if p.is_file()])
    assert len(shown) == on_disk  # and none is left out


def test_the_backup_verdict_never_calls_missing_files_safe(folders: tuple[Path, Path]) -> None:
    a, b = folders
    verdict = backup_verdict(diff_of(a, b), "the phone", "the backup")
    assert not verdict.ok
    assert verdict.missing >= 1 and "NOT in the backup" in verdict.headline
    assert verdict.safe >= 2  # same.jpg and the renamed one count as safe
    (b / "only-a.jpg").write_bytes((a / "only-a.jpg").read_bytes())
    (b / "edited.txt").write_bytes((a / "edited.txt").read_bytes())
    (b / "twin.bin").write_bytes((a / "twin1.bin").read_bytes())
    again = backup_verdict(diff_of(a, b), "the phone", "the backup")
    assert again.ok and again.missing == 0 and again.different == 0 and "safe" in again.headline


def test_an_empty_folder_a_is_not_reported_as_a_problem(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    write(tmp_path / "b" / "x.txt", b"x" * 100)
    verdict = backup_verdict(diff_of(tmp_path / "a", tmp_path / "b"))
    assert verdict.ok and "no files" in verdict.headline


# --------------------------------------------------------------------------- duplicates
def duplicates(folders: tuple[Path, Path], mode: Mode = Mode.ALL, reference: bool = False):  # type: ignore[no-untyped-def]
    a, b = folders
    with BackendRegistry() as reg:
        roots = make_roots([(reg.open(str(a)), RootRole.NORMAL), (reg.open(str(b)), RootRole.REFERENCE if reference else RootRole.NORMAL)])
        inventory = scan(roots, ScanFilter(min_size=0))
        result = compare(inventory, ScanOptions(mode=mode, verify=Verify.HASH), cache=NullCache())
        return result, inventory.roots


def test_one_copy_always_stays_however_the_marks_are_clicked(folders: tuple[Path, Path]) -> None:
    result, roots = duplicates(folders)
    rows = build_group_rows(result.groups, roots, [])
    row = next(r for r in rows if {t.entry.rel for t in r.tiles} >= {"twin1.bin", "twin2.bin"})
    assert len(row.removals) == len(row.tiles) - 1
    keeper = next(t for t in row.tiles if row.mark(t) is Mark.KEEP)
    victim = next(t for t in row.tiles if row.mark(t) is Mark.REMOVE)
    assert row.toggle(keeper) is False and row.mark(keeper) is Mark.KEEP  # the last kept copy cannot be unmarked
    assert row.toggle(victim) is True and row.mark(victim) is Mark.KEEP  # but another can be kept as well
    assert row.toggle(keeper) is True  # and now either may go
    assert len(row.keep) == 1
    row.keep_only(victim)
    assert row.mark(victim) is Mark.KEEP and row.mark(keeper) is Mark.REMOVE
    row.skipped = True
    assert row.removals == [] and chosen_group_ids([row]) == []  # a skipped group removes nothing
    assert row.group.id not in keep_overrides([row])


def test_a_reference_folder_is_never_offered_for_removal(folders: tuple[Path, Path]) -> None:
    a, b = folders
    write(a / "dup-of-b.jpg", (b / "same.jpg").read_bytes(), mtime=OLD)  # a second copy of a file the reference folder has
    result, roots = duplicates(folders, mode=Mode.REFERENCE, reference=True)
    rows = build_group_rows(result.groups, roots, [])
    row = next(r for r in rows if any(t.entry.rel == "dup-of-b.jpg" for t in r.tiles))
    b_tiles = [t for t in row.tiles if t.side == 1]
    assert b_tiles and all(row.mark(t) is Mark.LOCKED for t in b_tiles)  # shown, marked as untouchable
    assert row.toggle(b_tiles[0]) is False and row.mark(b_tiles[0]) is Mark.LOCKED  # and a click cannot change that
    row.keep_only(b_tiles[0])
    assert all(row.mark(t) is Mark.LOCKED for t in b_tiles)
    assert {t.entry.rel for t in row.removals} == {"dup-of-b.jpg", "same.jpg"}  # every copy in A is a safe duplicate of B's
    apply_rules(rows, roots, [])  # choosing again keeps the lock
    assert all(t.side == 0 for r in rows for t in r.removals)


# --------------------------------------------------------------------------- an import preview
def test_import_rows_say_what_will_happen_to_each_file_on_the_phone(tmp_path: Path) -> None:
    with BackendRegistry() as reg:
        source = reg.open(str(tmp_path))
        report = IngestReport(items=[
            IngestItem("new.jpg", 100, "would-import", "2024/2024-05/new.jpg", mtime_ns=OLD * 10**9),
            IngestItem("maybe.jpg", 200, "maybe-duplicate", "2024/2024-05/maybe.jpg"),
            IngestItem("have.jpg", 300, "duplicate", "2023/have.jpg"),
            IngestItem("before.jpg", 400, "previously-imported"),
            IngestItem("broken.jpg", 500, "failed", message="unreadable"),
        ])
        rows = ingest_rows(report, source)
    assert [r.kind for r in rows] == [RowKind.ONLY_A, RowKind.ONLY_A, RowKind.SAME, RowKind.SAME, RowKind.UNREADABLE]
    assert all(r.left is not None and r.left.side == 0 for r in rows)  # the phone is always the left-hand side
    assert rows[0].left.details.endswith("2020-09-13") or "2020" in rows[0].left.details  # the picture's own date, not "unknown"


# --------------------------------------------------------------------------- the small things
@pytest.mark.parametrize(("name", "kind"), [("a.JPG", "picture"), ("b.heic", "picture"), ("c.mp4", "video"), ("d.mp3", "audio"), ("e.pdf", "document"),
                                            ("f.zip", "archive"), ("g", "other"), ("h.unknown", "other")])
def test_file_kinds(name: str, kind: str) -> None:
    assert file_kind(name) == kind


def test_server_addresses() -> None:
    assert build_server_address("sftp", "nas.local", "me", "2222", "photos/2024") == "sftp://me@nas.local:2222/photos/2024"
    assert build_server_address("smb", "nas", "", "", "") == "smb://nas/"
    assert build_server_address("webdav", " cloud.example ", "a b", "", "/x/") == "webdav://a%20b@cloud.example/x"
    assert build_server_address("ftp", "fe80::1", "u@x", "", "") == "ftp://u%40x@[fe80::1]/"


def test_a_job_reports_its_result_and_its_progress() -> None:
    def work(progress, cancel):  # type: ignore[no-untyped-def]
        progress.stage("Counting", total_items=4)
        progress.advance(items=2, detail="halfway")
        return 42

    job = Job("count", work)
    job.start()
    deadline = time.monotonic() + 10
    while not job.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.finished and job.result == 42 and job.error is None and not job.cancelled
    snap = job.progress.snapshot()
    assert snap.done_items == 2 and snap.fraction == 0.5 and snap.detail == "halfway"


def test_a_job_can_be_stopped_and_a_failure_is_kept_not_lost() -> None:
    started = threading.Event()

    def slow(progress, cancel):  # type: ignore[no-untyped-def]
        started.set()
        while True:
            cancel.check()
            time.sleep(0.01)

    job = Job("slow", slow)
    job.start()
    assert started.wait(5)
    job.cancel.cancel()
    deadline = time.monotonic() + 10
    while not job.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.finished and job.cancelled and job.result is None

    def broken(progress, cancel):  # type: ignore[no-untyped-def]
        raise ConfigError("no such folder")

    failed = Job("broken", broken)
    failed.start()
    while not failed.finished:
        time.sleep(0.01)
    assert isinstance(failed.error, ConfigError) and explain(failed.error) == "no such folder"
    assert "Permission" in explain(PermissionError("x")) and "not be found" in explain(FileNotFoundError("x"))
    assert "unexpected" in explain(ValueError("boom"))


def test_remembered_folders_are_newest_first_without_repeats(tmp_path: Path) -> None:
    path = tmp_path / "state" / "gui.json"
    state = GuiState(path)
    for place in ("/a", "/b", "/c", "/a", "  ", ""):
        state.remember(place)
    assert state.recent == ["/a", "/c", "/b"]
    for n in range(30):
        state.remember(f"/p{n}")
    assert len(state.recent) == 12
    state.set("thumb_size", "Large")
    state.save()
    again = GuiState(path)
    assert again.get("thumb_size") == "Large" and again.recent[0] == "/p29"


def test_a_damaged_state_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "gui.json"
    path.write_text("{not json")
    assert GuiState(path).recent == []
    path.write_text('["a list, not a mapping"]')
    assert GuiState(path).recent == []
    path.write_text('{"recent": "oops", "thumb_size": 5}')
    assert GuiState(path).recent == []


# --------------------------------------------------------------------------- pictures for Tk
def tiny_jpeg(width: int, height: int) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", (width, height), (200, 30, 30))
    for x in range(width // 2):
        for y in range(height):
            img.putpixel((x, y), (30, 30, 200))  # left half blue, right half red
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


def ppm_pixels(ppm: bytes) -> tuple[int, int, bytes]:
    header, rest = ppm.split(b"\n255\n", 1)
    magic, size = header.split(b"\n", 1)
    assert magic == b"P6"
    w, h = (int(x) for x in size.split())
    assert len(rest) == w * h * 3
    return w, h, rest


@pytest.mark.parametrize("pillow", [True, False])
def test_thumbnails_become_upright_pictures_that_fit_the_box(pillow: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL")
    from duplio.gui.thumbs import make_ppm
    from duplio.preview.store import StoredThumb

    data = tiny_jpeg(120, 80)
    if not pillow:
        monkeypatch.setitem(sys.modules, "PIL", None)  # as if Pillow were not installed
    plain = make_ppm(StoredThumb(data, "jpg", 120, 80, 1, "embedded", (1200, 800)), 60)
    w, h, pixels = ppm_pixels(plain[0])
    assert (w, h) == (plain[1], plain[2]) and max(w, h) <= 60 and w > h  # fits the box and keeps its shape
    left, right = pixels[:3], pixels[(w - 1) * 3:(w - 1) * 3 + 3]
    assert left[2] > left[0] and right[0] > right[2]  # blue on the left, red on the right
    turned = make_ppm(StoredThumb(data, "jpg", 120, 80, 6, "embedded", (1200, 800)), 60)  # EXIF: rotate a quarter turn
    tw, th, _ = ppm_pixels(turned[0])
    assert th > tw and max(tw, th) <= 60  # now portrait


# --------------------------------------------------------------------------- a picture is never shown on the wrong file
def first_tile(folder: Path):  # type: ignore[no-untyped-def]
    from duplio.gui.model import tiles_of

    with BackendRegistry() as reg:
        roots = make_roots([(reg.open(str(folder)), RootRole.NORMAL)])
        inventory = scan(roots, ScanFilter(min_size=0))
        return tiles_of(inventory.entries, inventory.roots)[0]


def test_the_numbers_that_name_a_tile_repeat_between_scans_but_the_picture_key_does_not(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    red, blue = tmp_path / "x", tmp_path / "y"
    write(red / "a.jpg", tiny_jpeg(40, 30), mtime=OLD)
    write(blue / "a.jpg", tiny_jpeg(41, 31), mtime=OLD)
    t1, t2 = first_tile(red), first_tile(blue)
    assert t1.key == t2.key == "0:0"  # the same label within two different scans ...
    assert t1.picture_key and t1.picture_key != t2.picture_key  # ... for two different files
    assert first_tile(red).picture_key == t1.picture_key  # the same file in a later scan is recognised
    write(red / "a.jpg", tiny_jpeg(40, 30), mtime=OLD + 5)  # edited
    assert first_tile(red).picture_key != t1.picture_key
    write(tmp_path / "z" / "note.txt", b"text", mtime=OLD)
    assert first_tile(tmp_path / "z").picture_key == ""  # not a picture


def test_a_picture_from_an_earlier_scan_never_appears_on_a_file_of_the_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL")
    from duplio.gui.thumbs import Thumbnailer
    from duplio.preview.store import StoredThumb

    first, second = tmp_path / "first", tmp_path / "second"
    write(first / "a.jpg", tiny_jpeg(40, 30), mtime=OLD)
    write(second / "a.jpg", tiny_jpeg(80, 20), mtime=OLD)  # another picture that happens to be file number 0 of its scan too
    t1, t2 = first_tile(first), first_tile(second)
    assert t1.key == t2.key

    class Master:
        def after(self, _ms: int, _fn: object) -> None:
            pass  # the test calls pump() itself

    class Previews:
        class Got:
            def __init__(self, data: bytes) -> None:
                self.thumb = StoredThumb(data, "jpg", None, None, 1, "decoded", None)

        def get(self, item, **_kw):  # type: ignore[no-untyped-def]
            return self.Got(Path(item.path).read_bytes())

    monkeypatch.setattr(Thumbnailer, "_make_image", lambda self, ppm: ("image", ppm))
    thumbs = Thumbnailer(Master(), Previews(), workers=1)  # type: ignore[arg-type]
    try:
        def deliver(tile) -> object:  # type: ignore[no-untyped-def]
            got: list[object] = []
            thumbs.request(tile, 60, lambda key, image, why: got.append((key, image, why)))
            deadline = time.monotonic() + 10
            while not got and time.monotonic() < deadline:
                thumbs.pump()
                time.sleep(0.01)
            assert got, "no picture arrived"
            return got[0]

        key1, image1, _ = deliver(t1)  # type: ignore[misc]
        assert key1 == t1.picture_key and image1 is not None
        assert thumbs.peek(t2, 60) is None  # nothing is remembered under the second scan's "0:0"
        key2, image2, _ = deliver(t2)  # type: ignore[misc]
        assert key2 == t2.picture_key and key2 != key1
        assert image2[1] != image1[1]  # its own picture, not the first scan's
        assert thumbs.peek(t1, 60) == image1 and thumbs.peek(t2, 60) == image2
    finally:
        thumbs.close()
