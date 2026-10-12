"""Awkward file names: invalid UTF-8, Unicode normalisation, control characters, long names.

File names are untrusted input. They must never crash Duplio, corrupt its own
files, rewrite a terminal, or make two different files look the same.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unicodedata
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.text import Text

from duplio.backup.mirror import MirrorOptions, plan_mirror, run_mirror
from duplio.backup.repository import Repository
from duplio.cli.console import SafeConsole
from duplio.cli.main import cli
from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions, Verify
from duplio.compare.session import load_session, save_session
from duplio.hashing.cache import HashCache
from duplio.plan.executor import apply_plan
from duplio.plan.journal import list_journals, read_journal
from duplio.plan.model import Plan
from duplio.plan.planner import build_plan
from duplio.plan.undo import undo
from duplio.reporting.export import to_csv, to_html, to_json
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.transfer import copy_verified
from duplio.util.text import fs_bytes, fs_str, json_dumps, name_key, nfc, printable, temp_name
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="byte-string paths are POSIX-only")


def put(path: bytes, data: bytes, mtime: float = 1_600_000_000.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:  # e.g. macOS refuses invalid UTF-8 names
        pytest.skip(f"this file system rejects the name: {exc}")
    os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_printable_escapes_only_what_is_unsafe() -> None:
    assert printable("holiday 2024 – café 日本語 👍🏽.jpg") == "holiday 2024 – café 日本語 👍🏽.jpg"
    assert printable("caf\udce9.txt") == "caf\\xe9.txt"
    assert printable("a\nb\tc\rd") == "a\\nb\\tc\\rd"
    assert "\x1b" not in printable("x\x1b[2Jy\x1b]0;title\x07")
    assert printable("x\x1b[2J") == "x\\x1b[2J"
    assert printable("photo‮gnp.exe") == "photo\\u202egnp.exe"  # bidi override cannot spoof an extension
    assert printable("l1\nl2", keep_newlines=True) == "l1\nl2"
    assert printable("a　b") == "a　b"  # ideographic space is ordinary text


def test_lossless_representations() -> None:
    name = "caf\udce9 \udcff.txt"
    assert fs_str(fs_bytes(name)) == name
    assert json.loads(json_dumps({"p": name}))["p"] == name
    assert json_dumps({"p": name}).isascii()
    assert nfc("café") == "café"
    assert name_key("Café", fold=True) == name_key("CAFÉ", fold=True)
    assert name_key("Café") != name_key("CAFÉ")  # case still matters unless folding


def test_temp_names_fit_the_filesystem_limit() -> None:
    long = "é" * 120 + ".dat"  # 244 bytes
    tmp = temp_name(long, ".duplio-tmp-0123abcd")
    assert len(tmp.encode()) <= 255 and tmp.startswith(".") and tmp.endswith(".duplio-tmp-0123abcd")
    assert temp_name("short.txt", ".t") == ".short.txt.t"


def test_safe_console_never_emits_control_sequences_from_names() -> None:
    out = io.StringIO()
    console = SafeConsole(file=out, force_terminal=False, width=100, highlight=False)
    console.print(Text("name: x\x1b[2Jy \x1b]0;PWNED\x07 caf\udce9 photo‮gnp.exe\nsecond line"))
    text = out.getvalue()
    assert "\x1b" not in text and "\x07" not in text and "‮" not in text
    assert "caf\\xe9" in text and "second line" in text and "\\x1b[2J" in text
    # With colours on, the only escapes are Rich's own styling codes, never the name's.
    out = io.StringIO()
    SafeConsole(file=out, force_terminal=True, color_system="standard", width=100).print(
        Text("a\x1b[2Jb", style="bold red")
    )
    assert "\x1b[2J" not in out.getvalue()


# ---------------------------------------------------------------------------
# Detection is about content, not names
# ---------------------------------------------------------------------------


def scan_pair(registry: BackendRegistry, a: Path, b: Path, **opts):  # type: ignore[no-untyped-def]
    roots = make_roots([(registry.open(str(a)), RootRole.NORMAL), (registry.open(str(b)), RootRole.NORMAL)])
    options = ScanOptions(**opts)
    return compare(scan(roots, ScanFilter()), options, cache=opts.pop("cache", None))


def test_identical_content_is_matched_whatever_the_names(registry: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(60_000, 1)
    put(os.fsencode(tmp_path / "a" / "holiday.jpg"), data)
    put(os.fsencode(tmp_path / "b" / "IMG_0042.jpeg"), data)  # different name AND extension
    put(os.fsencode(tmp_path / "b" / "backup-of-something.bin"), data)
    put(os.fsencode(tmp_path / "b" / "holiday.jpg"), rand_bytes(60_000, 2))  # same name, different content
    res = scan_pair(registry, tmp_path / "a", tmp_path / "b")
    assert len(res.groups) == 1
    assert sorted(m.rel for m in res.groups[0].members) == ["IMG_0042.jpeg", "backup-of-something.bin", "holiday.jpg"]
    assert {m.root for m in res.groups[0].members if m.rel == "holiday.jpg"} == {0}  # b/holiday.jpg is not a duplicate


def test_names_are_irrelevant_but_quick_mode_compares_nfc_names(registry: BackendRegistry, tmp_path: Path) -> None:
    put(os.fsencode(tmp_path / "a" / "café.txt"), b"aaaa")
    put(os.fsencode(tmp_path / "b" / "CAFÉ.TXT"), b"bbbb")  # same size, same name after NFC + case folding
    quick = scan_pair(registry, tmp_path / "a", tmp_path / "b", verify=Verify.QUICK)
    assert len(quick.groups) == 1 and "not content-verified" in quick.groups[0].method
    assert scan_pair(registry, tmp_path / "a", tmp_path / "b").groups == []  # different content: not duplicates


def test_phone_backup_check_ignores_renames(registry: BackendRegistry, tmp_path: Path) -> None:
    """A renamed file on the phone is still recognised as backed up; a new one is not."""
    data = rand_bytes(80_000, 3)
    put(os.fsencode(tmp_path / "phone" / "IMG_20240101.jpg"), data)
    put(os.fsencode(tmp_path / "phone" / "new.jpg"), rand_bytes(80_000, 4))
    put(os.fsencode(tmp_path / "backup" / "2024" / "renamed-by-me.jpg"), data)
    roots = make_roots([(registry.open(str(tmp_path / "phone")), RootRole.NORMAL),
                        (registry.open(str(tmp_path / "backup")), RootRole.REFERENCE)])
    res = compare(scan(roots, ScanFilter()), ScanOptions(mode=Mode.UNIQUE))
    assert [e.rel for e in res.unique] == ["new.jpg"]


HOSTILE = [
    b"caf\xe9.txt",  # latin-1 'café' written by an old tool
    b"clear\x1b[2Jscreen.txt",  # terminal escape sequence
    "photo‮gnp.exe".encode(),  # bidi override
    b"new\nline.txt",
    b"-rf [x] {y} %s $(z).bin",  # leading dash, markup, format and shell metacharacters
    "café-nfd.txt".encode(),
    ("é" * 125 + ".dat").encode(),  # 254-byte name
]


@pytest.fixture
def hostile(tmp_path: Path) -> Path:
    for i, name in enumerate(HOSTILE):
        data = rand_bytes(6_000 + i, 100 + i)
        put(os.fsencode(tmp_path) + b"/a/" + name, data)
        put(os.fsencode(tmp_path) + b"/b/copy-of-" + name[:60], data)
    return tmp_path


def test_scan_apply_undo_with_hostile_names(registry: BackendRegistry, hostile: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_pair(registry, hostile / "a", hostile / "b")
    assert len(res.groups) == len(HOSTILE) and not res.stats.errors
    path = save_session(res, dirs)  # JSON must hold names that are not valid UTF-8
    loaded = load_session(path, registry)
    assert sorted(m.path for g in loaded.groups for m in g.members) == sorted(m.path for g in res.groups for m in g.members)
    plan = build_plan(loaded)
    saved = plan.save(hostile / "plan.json")
    assert saved.read_bytes().isascii()
    assert [i.victim.path for i in Plan.load(saved).items] == [i.victim.path for i in plan.items]
    report = apply_plan(plan, registry, dirs=dirs)
    assert report.count("done") == len(HOSTILE), [o.message for o in report.outcomes]
    journal = list_journals(dirs.journals)[0]
    assert journal.read_bytes().isascii() and read_journal(journal)
    assert not [n for n in os.listdir(os.fsencode(hostile / "b")) if n.startswith(b"copy-of-")]
    assert undo(None, registry, dirs=dirs).count("restored") == len(HOSTILE)
    assert len([n for n in os.listdir(os.fsencode(hostile / "b")) if n.startswith(b"copy-of-")]) == len(HOSTILE)


def test_hash_cache_keys_survive_undecodable_names(dirs) -> None:  # type: ignore[no-untyped-def]
    with HashCache(dirs.hash_cache) as cache:
        cache.put("local", "/x/caf\udce9.txt", "full:blake2b", 5, 1_000, "abc")
        assert cache.get("local", "/x/caf\udce9.txt", "full:blake2b", 5, 1_000) == "abc"
        cache.flush()
    with HashCache(dirs.hash_cache) as cache:
        assert cache.get("local", "/x/caf\udce9.txt", "full:blake2b", 5, 1_000) == "abc"
        cache.invalidate("local", "/x/caf\udce9.txt")
        assert cache.get("local", "/x/caf\udce9.txt", "full:blake2b", 5, 1_000) is None


def test_rescan_with_cache_and_hostile_names(registry: BackendRegistry, hostile: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    with HashCache(dirs.hash_cache) as cache:
        first = compare(scan(make_roots([(registry.open(str(hostile / "a")), RootRole.NORMAL),
                                         (registry.open(str(hostile / "b")), RootRole.NORMAL)]), ScanFilter()),
                        ScanOptions(), cache=cache)
    with HashCache(dirs.hash_cache) as cache:
        second = compare(scan(make_roots([(registry.open(str(hostile / "a")), RootRole.NORMAL),
                                          (registry.open(str(hostile / "b")), RootRole.NORMAL)]), ScanFilter()),
                         ScanOptions(), cache=cache)
    assert len(first.groups) == len(second.groups) == len(HOSTILE)
    assert second.stats.bytes_read == 0 and second.stats.cache_hits > 0


def test_exports_are_safe(registry: BackendRegistry, hostile: Path) -> None:
    res = scan_pair(registry, hostile / "a", hostile / "b")
    as_json = to_json(res)
    assert as_json.isascii() and json.loads(as_json)["groups"]
    for rendered in (to_csv(res), to_html(res)):
        assert "\x1b" not in rendered and "‮" not in rendered and "\udce9" not in rendered
        assert "caf\\xe9.txt" in rendered
        rendered.encode("utf-8")  # must be writable as UTF-8


def test_cli_with_hostile_names(hostile: Path) -> None:
    runner = CliRunner(env={"COLUMNS": "160"})
    result = runner.invoke(cli, ["--no-color", "scan", str(hostile / "a"), str(hostile / "b"), "--top", "20"],
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "‮" not in result.output
    assert "caf\\xe9.txt" in result.output and "\\x1b[2J" in result.output
    as_json = runner.invoke(cli, ["--no-color", "scan", str(hostile / "a"), str(hostile / "b"), "--json"],
                            catch_exceptions=False)
    assert json.loads(as_json.output)["groups"]
    result = runner.invoke(cli, ["--no-color", "dedupe", str(hostile / "a"), str(hostile / "b"), "--yes"],
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert runner.invoke(cli, ["--no-color", "undo", "--yes"], catch_exceptions=False).exit_code == 0
    assert runner.invoke(cli, ["--no-color", "history"], catch_exceptions=False).exit_code == 0


def test_compare_list_prints_exact_bytes_and_keeps_stdout_clean(hostile: Path, tmp_path: Path) -> None:
    only = os.fsencode(tmp_path) + b"/only/not-backed-up-caf\xe9.bin"
    put(only, rand_bytes(900, 77))
    runner = CliRunner(env={"COLUMNS": "160"})
    args = ["--no-color", "compare", str(tmp_path / "only"), "--against", str(hostile / "a"), "--list"]
    out = runner.invoke(cli, args, catch_exceptions=False)
    assert out.stdout_bytes == only + b"\n"  # raw bytes, nothing else: safe to pipe
    out = runner.invoke(cli, [*args, "-0"], catch_exceptions=False)
    assert out.stdout_bytes == only + b"\0"


def test_backup_and_restore_keep_exact_names(registry: BackendRegistry, hostile: Path, tmp_path: Path) -> None:
    repo = Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)
    report = repo.backup(registry.open(str(hostile / "a")))
    assert not report.errors and report.snapshot.files == len(HOSTILE)
    assert (tmp_path / "repo" / "snapshots").exists()
    assert repo.check(read_data=True).ok
    restored = repo.restore("latest", registry.open(str(tmp_path / "out")))
    assert restored.restored == len(HOSTILE) and not restored.errors
    original = sorted(os.listdir(os.fsencode(hostile / "a")))
    assert sorted(os.listdir(os.fsencode(tmp_path / "out"))) == original
    for name in original:
        before = Path(os.fsdecode(os.fsencode(hostile / "a") + b"/" + name)).read_bytes()
        after = Path(os.fsdecode(os.fsencode(tmp_path / "out") + b"/" + name)).read_bytes()
        assert before == after


# ---------------------------------------------------------------------------
# Names that differ only in Unicode form or case
# ---------------------------------------------------------------------------


def test_mirror_does_not_duplicate_nfc_and_nfd_spellings(registry: BackendRegistry, tmp_path: Path) -> None:
    put(os.fsencode(tmp_path / "src" / "café.txt"), b"x" * 100)
    put(os.fsencode(tmp_path / "dst" / "café.txt"), b"x" * 100)  # same name as macOS/SMB would store it
    s, d = registry.open(str(tmp_path / "src")), registry.open(str(tmp_path / "dst"))
    plan = plan_mirror(s, d)
    assert not plan.actions  # recognised as the same file, nothing to copy
    assert sorted(os.listdir(tmp_path / "dst")) == [unicodedata.normalize("NFD", "café.txt")]


def test_mirror_nfc_rename_is_a_move_not_a_copy(registry: BackendRegistry, tmp_path: Path) -> None:
    data = rand_bytes(5000, 5)
    put(os.fsencode(tmp_path / "src" / "café-renamed.txt"), data)
    put(os.fsencode(tmp_path / "dst" / "old name.txt"), data)
    put(os.fsencode(tmp_path / "src" / "keep.txt"), b"keep")
    put(os.fsencode(tmp_path / "dst" / "keep.txt"), b"keep")
    opts = MirrorOptions(delete_extraneous=True, force=True)
    s, d = registry.open(str(tmp_path / "src")), registry.open(str(tmp_path / "dst"))
    plan = plan_mirror(s, d, options=opts)
    assert [(a.kind, a.from_rel) for a in plan.actions] == [("move", "old name.txt")]
    assert not run_mirror(plan, options=opts).errors
    assert (tmp_path / "dst" / "café-renamed.txt").read_bytes() == data


# ---------------------------------------------------------------------------
# Long names and writing
# ---------------------------------------------------------------------------


def test_copy_of_maximum_length_names(registry: BackendRegistry, tmp_path: Path) -> None:
    name = "é" * 127 + "x"  # 255 bytes: the longest a file name can be
    put(os.fsencode(tmp_path / "src") + b"/" + name.encode(), rand_bytes(3000, 8))
    src, dst = registry.open(str(tmp_path / "src")), registry.open(str(tmp_path / "dst"))
    local = src.backend
    copy_verified((local, str(tmp_path / "src" / name)), (local, str(tmp_path / "dst" / name)), algorithm="blake2b")
    assert os.listdir(dst.path) == [name]
    assert not [n for n in os.listdir(dst.path) if "duplio-tmp" in n]


# ---------------------------------------------------------------------------
# The terminal UI
# ---------------------------------------------------------------------------


def test_tui_survives_hostile_names_and_bracketed_messages(hostile: Path) -> None:
    pytest.importorskip("textual")
    import asyncio

    from textual.widgets import DataTable, Input

    from duplio.tui.app import DuplioApp
    from duplio.tui.dedupe import ReviewScreen

    async def flow() -> None:
        app = DuplioApp()
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
            screen = app.screen
            for path in ("a", "b"):
                screen.query_one("#loc-input", Input).value = str(hostile / path)
                await pilot.click("#add-normal")
                await pilot.pause(0.3)
            await pilot.click("#start")
            for _ in range(200):
                await pilot.pause(0.05)
                if isinstance(app.screen, ReviewScreen):
                    break
            review = app.screen
            assert isinstance(review, ReviewScreen) and len(review.result.groups) == len(HOSTILE)
            members = review.query_one("#members", DataTable)
            for group in review.result.groups:
                review._show_group(group.id)
                for row in range(members.row_count):
                    cell = members.get_row_at(row)[2]
                    assert "\x1b" not in str(cell) and "‮" not in str(cell) and "\udce9" not in str(cell)
            # Messages carry OS errors like "[Errno 2] No such file" and names with brackets: never markup.
            app.notify("[Errno 2] No such file or directory: '/tmp/[x]/caf\udce9 \x1b[2J'", severity="error")
            await pilot.pause(0.2)

    asyncio.run(flow())


def test_names_are_never_lost_when_mixed_encodings_collide(registry: BackendRegistry, tmp_path: Path) -> None:
    """Two different byte names that decode alike must stay two different files."""
    put(os.fsencode(tmp_path / "a") + b"/caf\xe9.txt", b"latin-1 spelling " * 50)
    put(os.fsencode(tmp_path / "a") + b"/caf\xc3\xa9.txt", b"utf-8 spelling " * 50)  # 'café.txt'
    put(os.fsencode(tmp_path / "b") + b"/other.txt", b"unrelated")
    res = scan_pair(registry, tmp_path / "a", tmp_path / "b")
    assert res.groups == [] and len(res.inventory.entries) == 3
    assert sorted(e.path for e in res.inventory.entries if e.root == 0) == sorted(
        [str(tmp_path / "a") + "/caf\udce9.txt", str(tmp_path / "a") + "/café.txt"]
    )


# ---------------------------------------------------------------------------
# Name collisions must never silently drop a file from a mirror
# ---------------------------------------------------------------------------


def test_mirror_keeps_source_names_that_differ_only_by_unicode_form(registry: BackendRegistry, tmp_path: Path) -> None:
    """On Linux 'café' (NFC) and 'café' (NFD) are two different files; both must be mirrored."""
    put(os.fsencode(tmp_path / "src" / "café.txt"), b"composed " * 50)
    put(os.fsencode(tmp_path / "src" / "café.txt"), b"decomposed " * 50)
    s, d = registry.open(str(tmp_path / "src")), registry.open(str(tmp_path / "dst"))
    plan = plan_mirror(s, d)
    assert sorted(a.kind for a in plan.actions) == ["copy", "copy"]
    assert not run_mirror(plan).errors
    assert sorted(os.listdir(tmp_path / "dst")) == sorted(os.listdir(tmp_path / "src"))
    assert (tmp_path / "dst" / "café.txt").read_bytes() == b"composed " * 50
    assert (tmp_path / "dst" / "café.txt").read_bytes() == b"decomposed " * 50


def test_mirror_reports_case_collisions_on_case_insensitive_destinations(
    registry: BackendRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from duplio.vfs.local import LocalBackend

    put(os.fsencode(tmp_path / "src" / "Report.txt"), b"upper " * 50)
    put(os.fsencode(tmp_path / "src" / "report.txt"), b"lower " * 50)
    real = LocalBackend.capabilities
    monkeypatch.setattr(
        LocalBackend, "capabilities",
        lambda self, path: real(self, path).with_(case_insensitive=str(path).startswith(str(tmp_path / "dst"))),
    )
    s, d = registry.open(str(tmp_path / "src")), registry.open(str(tmp_path / "dst"))
    plan = plan_mirror(s, d)
    assert any("differ only by case" in w for w in plan.warnings), plan.warnings
    assert len(plan.actions) == 2  # nothing is silently dropped from the plan
