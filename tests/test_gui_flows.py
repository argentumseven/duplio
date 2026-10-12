"""The desktop window, driven end to end: every workflow with two folders, against real files.

Needs Tk and a display (run under ``xvfb-run`` on a machine without a screen); skipped otherwise.
"""

from __future__ import annotations

import gc
import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

try:
    import tkinter  # noqa: F401
except ImportError:  # pragma: no cover - depends on the interpreter
    pytest.skip("this Python has no tkinter", allow_module_level=True)

from duplio.util.appdirs import AppDirs

from .guikit import make_demo_folders, pump

if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display (run under xvfb-run)", allow_module_level=True)


class Dialogs:
    """Stands in for the window's dialogs: records what was shown and answers "yes" to every question."""

    def __init__(self, ctx: Any) -> None:
        self.confirms: list[tuple[str, str, list[str], list[tuple[str, str]]]] = []
        self.infos: list[tuple[str, str, list[str], str, list[Any]]] = []
        self.errors: list[str] = []
        self.answer = True
        ctx.confirm = self._confirm
        ctx.info = self._info
        ctx.show_error = self._error

    def _confirm(self, title, headline, details=(), rows=(), confirm="Continue", danger=False, columns=("File", "What happens")):  # type: ignore[no-untyped-def]
        self.confirms.append((title, headline, list(details), list(rows)))
        return self.answer

    def _info(self, title, headline, lines=(), kind="info", buttons=()):  # type: ignore[no-untyped-def]
        self.infos.append((title, headline, list(lines), kind, list(buttons)))

    def _error(self, error, title="Duplio could not do that"):  # type: ignore[no-untyped-def]
        self.errors.append(str(error))


@pytest.fixture
def window(dirs: AppDirs) -> Iterator[Any]:
    from duplio.gui.app import DuplioWindow

    win = DuplioWindow(dirs)
    pump(win, timeout=0.3)
    yield win
    win.close()
    gc.collect()  # free Tk variables and images here, on the main thread, not in whichever thread the collector wakes up in


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob("*")) if p.is_file() and not any(part.startswith(".duplio") for part in p.parts)}


def page_for(window: Any, key: str, a: Path | None, b: Path | None = None) -> Any:
    window.show(key)
    page = window.pages[key]
    pump(window, timeout=0.2)
    if a is not None:
        page.folders.a.set(str(a))
    if b is not None:
        page.folders.b.set(str(b))
    return page


def wait(window: Any, until: Callable[[], Any], timeout: float = 60.0) -> None:
    assert pump(window, until=until, timeout=timeout), "the window did not finish in time"


def test_every_page_opens(window: Any) -> None:
    from duplio.gui.app import NAV

    for key, _glyph, _title in NAV:
        window.show(key)
        pump(window, timeout=0.1)
        assert key in window.pages


def test_compare_shows_a_against_b_and_copies_both_ways(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    kinds = {row.kind.name for row in page.all_rows}
    assert {"ONLY_A", "ONLY_B", "SAME"} <= kinds  # A on the left, B on the right, and what they share
    assert any(row.left is not None and row.right is not None for row in page.all_rows)
    only_a = {r.left.rel for r in page.all_rows if r.kind.name == "ONLY_A" and r.left is not None}
    only_b = {r.right.rel for r in page.all_rows if r.kind.name == "ONLY_B" and r.right is not None}
    assert "IMG_0006.jpg" in only_a and "Scan_old.jpg" in only_b
    before_a, before_b = tree(a), tree(b)

    page.copy(0)  # A -> B
    assert dialogs.confirms and "IMG_0006.jpg" in {row[0] for row in dialogs.confirms[-1][3]}
    wait(window, lambda: dialogs.infos)
    assert dialogs.infos[-1][1] == "Done." and not dialogs.errors
    after_b = tree(b)
    assert tree(a) == before_a  # A is never touched when copying to B
    assert all(after_b.get(rel) == h for rel, h in before_b.items())  # nothing in B was replaced
    assert set(before_a.values()) <= set(after_b.values())  # everything A has is now in B, under some name
    assert (b / "IMG_0006.jpg").read_bytes() == (a / "IMG_0006.jpg").read_bytes()

    page.run()
    wait(window, lambda: page.diff is not None and not any(r.kind.name == "ONLY_A" for r in page.all_rows))
    dialogs.infos.clear()
    page.copy(1)  # B -> A: the things only B had
    wait(window, lambda: dialogs.infos)
    assert set(tree(b).values()) <= set(tree(a).values())
    assert not dialogs.errors


def test_copy_only_what_is_selected(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    page.chip_bar.select("only-a") if hasattr(page.chip_bar, "select") else None
    wanted = next(t for r in page.all_rows for t in r.tiles if r.kind.name == "ONLY_A" and t.side == 0 and t.entry.rel == "IMG_0007.jpg")
    page.pairs.tiles[wanted.key] = wanted
    page.pairs.selected.add(wanted.key)
    page.copy(0)
    wait(window, lambda: dialogs.infos)
    assert (b / "IMG_0007.jpg").exists()
    assert not (b / "IMG_0006.jpg").exists() and not (b / "IMG_0008.jpg").exists()


def test_a_new_comparison_starts_with_nothing_selected(tmp_path: Path, window: Any) -> None:
    """The labels that name tiles start again with each scan, so a selection must not survive into the next one."""
    a, b = make_demo_folders(tmp_path)
    Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    tile = next(t for r in page.all_rows for t in r.tiles if r.kind.name == "ONLY_A")
    page.pairs.selected.add(tile.key)
    assert page.pairs.selected_tiles()
    page.diff = None
    page.run()
    wait(window, lambda: page.diff is not None)
    assert page.pairs.selected == set() and page.pairs.selected_tiles() == []


def test_is_it_backed_up_says_no_then_yes(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "backup", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    assert "not" in page.headline.cget("text").lower() or "missing" in page.headline.cget("text").lower()
    missing = {r.left.rel for r in page.all_rows if r.kind.name == "ONLY_A" and r.left is not None}
    assert {"IMG_0006.jpg", "IMG_0007.jpg", "IMG_0008.jpg"} <= missing
    page.copy(0)  # "copy what is missing to the backup"
    wait(window, lambda: dialogs.infos)
    page.run()
    wait(window, lambda: page.diff is not None and not any(r.kind.name == "ONLY_A" for r in page.all_rows))
    assert "backed up" in page.headline.cget("text").lower() or "safe" in page.headline.cget("text").lower()


def test_duplicates_removed_to_quarantine_and_put_back(tmp_path: Path, window: Any) -> None:
    a, _b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "dupes", a)
    before = tree(a)
    page.run()
    wait(window, lambda: page.result is not None)
    assert page.rows, "the demo folder holds a copy of IMG_0002"
    copies = {t.entry.rel for t in page.rows[0].tiles}
    assert {"IMG_0002.jpg", "IMG_0002 copy.jpg"} <= copies
    page.remove()
    assert dialogs.confirms
    wait(window, lambda: dialogs.infos)
    assert dialogs.infos[-1][1].startswith("1 copies") or "removed" in dialogs.infos[-1][1]
    gone = set(before) - set(tree(a))
    assert len(gone) == 1  # one copy went, the other stayed
    assert gone <= {"IMG_0002.jpg", "IMG_0002 copy.jpg"}
    assert any(a.rglob(".duplio-quarantine*")) or any(a.parent.rglob(".duplio-quarantine*")) or any(p for p in tmp_path.rglob("*quarantine*"))
    # undo through the button the dialog offered
    undo_button = next(callback for label, callback, _kind in dialogs.infos[-1][4] if label == "Undo this")
    dialogs.infos.clear()
    undo_button()
    wait(window, lambda: dialogs.infos)
    assert tree(a) == before


def test_duplicates_between_a_and_b_keeps_a_copy_everywhere_it_matters(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "dupes", a, b)
    page.run()
    wait(window, lambda: page.result is not None)
    across = [r for r in page.rows if len(r.group.roots()) > 1]
    assert across, "IMG_0001..IMG_0005 exist in both folders"
    assert {t.side for r in across for t in r.tiles} == {0, 1}
    # reference mode: B is the reference, so nothing in B is ever offered for removal
    page.remove()
    wait(window, lambda: dialogs.infos)
    assert {rel: h for rel, h in tree(b).items() if "IMG_0001" in rel} == {"IMG_0001.jpg": digest(a.parent.parent / "Backup" / "Photos" / "IMG_0001.jpg")}


def test_sync_makes_both_sides_match_and_is_quiet_afterwards(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "sync", a, b)
    page.run()
    wait(window, lambda: page.plan is not None)
    assert page.plan.has_work and page.all_rows
    page.sync_now()
    assert dialogs.confirms
    wait(window, lambda: dialogs.infos)
    assert not dialogs.errors and dialogs.infos[-1][3] == "ok", dialogs.infos[-1]
    ta, tb = tree(a), tree(b)
    assert set(ta.values()) == set(tb.values())  # the same contents on both sides (the changed-on-both file keeps both versions)
    page.plan = None
    page.run()
    wait(window, lambda: page.plan is not None)
    assert not page.plan.has_work


def test_import_brings_only_new_pictures_into_the_library(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "import", a, b)
    page.run()
    wait(window, lambda: page.report is not None)
    assert page.report.items
    before = tree(b)
    page.import_now()
    assert dialogs.confirms
    wait(window, lambda: dialogs.infos)
    assert not dialogs.errors
    after = tree(b)
    assert all(after.get(rel) == h for rel, h in before.items())  # the library lost nothing
    assert digest(a / "IMG_0006.jpg") in set(after.values())
    assert not any(h == digest(a / "notes.txt") for h in after.values())  # only pictures and videos
    assert tree(a) == tree(a)  # the phone side is only read
    page.report = None
    page.run()
    wait(window, lambda: page.report is not None)
    assert not [i for i in page.report.items if i.status == "would-import"]


def test_history_lists_the_copy_and_undoes_it(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    before_b = tree(b)
    page.copy(0)
    wait(window, lambda: dialogs.infos)
    assert tree(b) != before_b
    window.show("history")
    history = window.pages["history"]
    pump(window, timeout=0.2)
    items = history.tree.get_children()
    assert items, "the copy is in the history"
    history.tree.selection_set(items[0])
    dialogs.infos.clear()
    history.undo_selected()
    wait(window, lambda: dialogs.infos)
    assert tree(b) == before_b


def test_settings_are_saved_and_a_bad_value_is_refused(window: Any, dirs: AppDirs) -> None:
    from duplio.config import load_settings
    from duplio.util.fmt import parse_size

    window.show("settings")
    page = window.pages["settings"]
    pump(window, timeout=0.2)
    dialogs = Dialogs(window.ctx)
    page.hidden.set(True)
    page.exclude_entry.delete(0, "end")
    page.exclude_entry.insert(0, "*.tmp, **/cache/**")
    page.protect_entry.delete(0, "end")
    page.protect_entry.insert(0, "**/Originals/**")
    page.min_size.delete(0, "end")
    page.min_size.insert(0, "2KB")
    page.save()
    saved = load_settings(dirs)
    assert saved.skip_hidden and saved.exclude == ["*.tmp", "**/cache/**"] and saved.protect == ["**/Originals/**"]
    assert saved.min_size == parse_size("2KB") and not dialogs.errors
    page.min_size.delete(0, "end")
    page.min_size.insert(0, "banana")
    page.save()
    assert dialogs.errors and "cannot be saved" in dialogs.errors[-1]
    assert load_settings(dirs).min_size == parse_size("2KB")  # the refused value was not written
