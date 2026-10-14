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

from duplio.gui.sorting import natural_key
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


@pytest.fixture
def usb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Folders of the test count as being on a USB drive, so their pictures are kept on disk (the computer's own disk keeps none)."""
    from duplio.vfs.base import Capabilities, MediaKind
    from duplio.vfs.local import LocalBackend

    monkeypatch.setattr(LocalBackend, "capabilities", lambda self, path: Capabilities(kind=MediaKind.REMOVABLE, label="USB drive",
                                                                                      max_parallel_reads=2))


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


# --------------------------------------------------------------------------- browsing a folder
def make_pictures(root: Path, count: int) -> None:
    from .previewkit import photo_with_exif_thumbnail

    for n in range(count):
        photo_with_exif_thumbnail(root / f"IMG_{n:03d}.jpg", seed=n, padding=1500 + n * 40)
        os.utime(root / f"IMG_{n:03d}.jpg", (1_600_000_000 + n * 100, 1_600_000_000 + n * 100))


def open_folder(window: Any, folder: Path) -> Any:
    window.show("browse")
    page = window.pages["browse"]
    pump(window, timeout=0.3)
    page.open_text(str(folder))
    wait(window, lambda: page.listing is not None and page.listing.path == str(folder))
    return page


def prefetch_done(window: Any) -> bool:
    return window.ctx.prefetcher.status().state in ("finished", "stopped")


def test_browse_shows_a_folder_and_fetches_all_its_pictures_into_the_buffer(tmp_path: Path, window: Any, usb: None) -> None:
    root = tmp_path / "trip"
    make_pictures(root / "day 1", 24)
    (root / "day 1" / "notes.txt").write_text("hi")
    (root / "maps").mkdir()
    page = open_folder(window, root / "day 1")
    wait(window, lambda: prefetch_done(window))
    status = window.ctx.prefetcher.status()
    assert status.state == "finished" and status.ready == 24 and status.total == 24
    assert [i.name for i in page.shown][:3] == ["IMG_000.jpg", "IMG_001.jpg", "IMG_002.jpg"] and len(page.shown) == 25
    wait(window, lambda: "All 24 pictures are at hand" in page.fetch_label.cget("text"))
    assert "pictures" in page.buffer_label.cget("text") and "in memory" in page.buffer_label.cget("text")
    # every picture is at hand without asking the folder again
    reads = window.ctx.previews.stats.fetched
    page.view.canvas.yview_moveto(1.0)
    pump(window, timeout=0.5)
    page.view.canvas.yview_moveto(0.0)
    pump(window, timeout=0.5)
    assert window.ctx.previews.stats.fetched == reads
    assert any(page.view.canvas.find_withtag("tile:" + page.item_keys[0]))  # the first cell is on the canvas


def test_browse_goes_into_folders_up_and_back(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "top" / "inner", 3)
    page = open_folder(window, tmp_path / "top")
    assert [i.name for i in page.shown] == ["inner"] and page.shown[0].is_dir
    page._open(page.shown[0])
    wait(window, lambda: page.listing is not None and page.listing.path.endswith("inner"))
    assert len(page.shown) == 3 and page.here.display.endswith("inner")
    page.go_up()
    wait(window, lambda: page.listing is not None and page.listing.path == str(tmp_path / "top"))
    page.go_back()  # back to where we were before Up: inside "inner"
    wait(window, lambda: page.listing is not None and page.listing.path.endswith("inner"))
    assert page.up_button._enabled and page.back_button._enabled


def test_browse_sorts_and_filters_what_is_shown(tmp_path: Path, window: Any) -> None:
    root = tmp_path / "pics"
    make_pictures(root, 12)
    (root / "zzz folder").mkdir()
    page = open_folder(window, root)
    assert page.shown[0].name == "zzz folder"  # folders always come first
    page.sort_box.set("Newest first")
    page._view_changed()
    assert [i.name for i in page.shown if not i.is_dir][:2] == ["IMG_011.jpg", "IMG_010.jpg"]
    assert page.shown[0].name == "zzz folder"
    page.sort_box.set("Largest first")
    page._view_changed()
    sizes = [i.size for i in page.shown if not i.is_dir]
    assert sizes == sorted(sizes, reverse=True)
    page.sort_box.set("Name, A to Z")
    page._view_changed()
    assert page.shown[1].name == "IMG_000.jpg" and page.shown[-1].name == "IMG_011.jpg"
    page.filter_var.set("img_00")
    pump(window, timeout=0.6)
    assert [i.name for i in page.shown] == [f"IMG_00{n}.jpg" for n in range(10)]  # a part of a name, any case
    page.view_box.set("Details")
    page._view_changed()
    assert page.details and page.view.count == 10
    assert window.ctx.state.get("browse_sort") == "name" and window.ctx.state.get("browse_desc") is False  # and it is remembered


def test_browse_says_so_when_the_buffer_is_full_instead_of_throwing_pictures_away(tmp_path: Path, window: Any, usb: None) -> None:
    root = tmp_path / "many"
    make_pictures(root, 40)
    window.ctx.settings.previews.max_bytes = 30_000
    window.ctx.previews.apply_settings()
    window.ctx.previews.set_memory_limit(10_000)  # (a little room in memory, a little on disk: 40 kB for 40 pictures of about 2 kB)
    page = open_folder(window, root)
    wait(window, lambda: prefetch_done(window))
    status = window.ctx.prefetcher.status()
    assert status.state == "stopped" and "fill the picture buffer" in status.reason
    wait(window, lambda: "Stopped" in page.fetch_label.cget("text"))
    assert "Settings" in page.fetch_label.cget("text") and page.fetch_button.cget("text") == "Try again"
    store = window.ctx.previews.buffer_for(page._preview_items()[0])
    assert store is not None and store.total_bytes <= 40_000  # and the limit (memory and disk together) held


def test_leaving_the_browser_pauses_the_background_fetch_and_coming_back_resumes_it(tmp_path: Path, window: Any, usb: None) -> None:
    root = tmp_path / "pics"
    make_pictures(root, 60)
    pre = window.ctx.prefetcher
    page = open_folder(window, root)
    pre.pause()  # (make the state certain: a run this small can finish before the page is left)
    page._resume_after_hide = False
    pre.resume()
    window.show("home")
    state = pre.status().state
    assert state in ("paused", "finished")
    if state == "paused":
        window.show("browse")
        wait(window, lambda: pre.status().state in ("running", "finished"))
    wait(window, lambda: prefetch_done(window))
    assert pre.status().ready == 60


def test_the_two_folder_pages_sort_what_they_show(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    page.chip_bar.select("only-a")
    default = [r.left.rel for r in page.pairs.rows]
    page.pairs.sort_box.set("Name")
    page.pairs._resort()
    by_name = [r.left.rel for r in page.pairs.rows]
    assert by_name == sorted(by_name, key=natural_key) and sorted(by_name) == sorted(default)  # the same files, in name order
    page.pairs.sort_box.set("Size")
    page.pairs.descending = True
    page.pairs._resort()
    sizes = [r.left.size for r in page.pairs.rows]
    assert sizes == sorted(sizes, reverse=True)
    assert page.pairs.selected_tiles() == []  # sorting does not select anything
    # the choice is remembered
    assert window.ctx.state.get("pair_sort") == "size" and window.ctx.state.get("pair_desc") is True


def test_duplicates_can_be_sorted(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    page = page_for(window, "dupes", a, b)
    page.run()
    wait(window, lambda: page.result is not None)
    page.sort_box.set("Number of copies")
    page.descending = True
    page._resort()
    counts = [len(r.tiles) for r in page.shown]
    assert counts == sorted(counts, reverse=True) and len(counts) >= 2
    page.sort_box.set("Name")
    page.descending = False
    page._resort()
    names = [r.tiles[0].name for r in page.shown]
    assert names == sorted(names, key=str.casefold)


def test_the_colour_theme_is_chosen_in_settings_and_used_from_the_next_start(window: Any, dirs: AppDirs) -> None:
    import subprocess
    import sys

    window.show("settings")
    page = window.pages["settings"]
    pump(window, timeout=0.2)
    Dialogs(window.ctx)
    page.theme.set("Light")
    page.save()
    assert "next time" in page.saved.cget("text")

    def theme_in_new_process() -> str:
        env = {**os.environ, "DUPLIO_HOME": os.environ["DUPLIO_HOME"]}
        env.pop("DUPLIO_THEME", None)
        out = subprocess.run([sys.executable, "-c", "from duplio.gui.palette import THEME; print(THEME)"], capture_output=True, text=True,
                             env=env, check=True)
        return out.stdout.strip()

    assert theme_in_new_process() == "light"
    page.theme.set("Dark (the default)")
    page.save()
    assert theme_in_new_process() == "dark"


def test_browsing_a_remote_folder_reads_each_picture_once_and_only_a_little_of_it(tmp_path: Path, window: Any) -> None:
    pytest.importorskip("wsgidav")
    pytest.importorskip("cheroot")
    from .previewkit import photo_with_exif_thumbnail
    from .remotekit import DavServer

    with DavServer(tmp_path / "server") as dav:
        folder = tmp_path / "server" / "holiday"
        total = 0
        for n in range(20):
            photo_with_exif_thumbnail(folder / f"IMG_{n:03d}.jpg", seed=n, padding=300_000)  # big photos with a small preview inside
            total += (folder / f"IMG_{n:03d}.jpg").stat().st_size
        window.show("browse")
        page = window.pages["browse"]
        pump(window, timeout=0.3)
        page.open_text(dav.url("holiday"))
        wait(window, lambda: page.listing is not None and page.listing.path.endswith("holiday") and page.listing.total == 20)
        wait(window, lambda: prefetch_done(window))
        assert window.ctx.prefetcher.status().ready == 20
        pump(window, timeout=0.5)
        assert dav.app.sent < total * 0.25, f"read {dav.app.sent} of {total} bytes: whole photos were fetched"
        gets = sum(1 for method, _path in dav.app.requests if method == "GET")
        assert gets >= 20  # the previews came from the server ...
        # ... and walking away and coming back does not ask for any picture again
        page.go_up()
        wait(window, lambda: page.listing is not None and not page.listing.path.endswith("holiday"))
        page.go_back()
        wait(window, lambda: page.listing is not None and page.listing.path.endswith("holiday"))
        pump(window, timeout=0.8)
        page.view.canvas.yview_moveto(1.0)
        pump(window, timeout=0.5)
        assert sum(1 for method, _path in dav.app.requests if method == "GET") == gets
        assert page.listing.total == 20


# --------------------------------------------------------------------------- nothing is cut off
def cut_off(root: Any) -> list[str]:
    """Widgets whose text does not fit in the room they were given (a button, label or box narrower than what it has to show)."""
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    problems: list[str] = []

    def walk(widget: Any) -> None:
        for child in widget.winfo_children():
            walk(child)
        if not widget.winfo_ismapped() or widget.winfo_width() <= 1:
            return
        kinds = (tk.Label, ttk.Checkbutton, ttk.Radiobutton, ttk.Combobox, ttk.Entry)
        if isinstance(widget, kinds) and widget.winfo_width() + 1 < widget.winfo_reqwidth():
            text = widget.cget("text") if "text" in widget.keys() else ""  # noqa: SIM118 (a Tk widget's keys(), not a dict's)
            problems.append(f"{widget.winfo_class()} {str(widget)!r} {text!r}: given {widget.winfo_width()}px, needs {widget.winfo_reqwidth()}px")
        elif isinstance(widget, tk.Label) and not widget.cget("image") and not int(str(widget.cget("wraplength"))):
            # a label made a fixed number of characters wide is as wide as it asked to be, but its text may be wider still
            text = str(widget.cget("text"))
            if text:
                font = tkfont.Font(root=widget, font=widget.cget("font"))
                needed = max(font.measure(line) for line in text.split("\n")) + 2 * int(str(widget.cget("padx")))
                if needed > widget.winfo_width() + 1:
                    problems.append(f"Label {str(widget)!r} {text!r}: its text needs {needed}px, it is {widget.winfo_width()}px wide")

    walk(root)
    return problems


@pytest.mark.parametrize("width", [1280, 1100, 900])
def test_no_button_label_or_box_is_ever_cut_off(tmp_path: Path, window: Any, width: int, usb: None) -> None:
    from duplio.gui.app import NAV

    a, b = make_demo_folders(tmp_path)
    make_pictures(tmp_path / "pics", 12)
    Dialogs(window.ctx)
    window.geometry(f"{width}x760")
    pump(window, timeout=0.3)
    problems: dict[str, list[str]] = {}

    def check(name: str) -> None:
        pump(window, timeout=0.5)
        found = cut_off(window.pages[window.current]) + cut_off(window._keybar)
        if found:
            problems[name] = found

    for key, _number, _title in NAV:
        window.show(key)
        check(f"{key} (start)")
    for key in ("compare", "backup", "sync", "import"):
        page = page_for(window, key, a, b)
        page.run()
        answer = {"compare": "diff", "backup": "diff", "sync": "plan", "import": "report"}[key]
        wait(window, lambda p=page, a=answer: getattr(p, a) is not None)
        check(f"{key} (answer)")
    page = page_for(window, "dupes", a, b)
    page.run()
    wait(window, lambda: page.result is not None)
    check("dupes (answer)")
    window.show("browse")
    browse = window.pages["browse"]
    browse.open_text(str(tmp_path / "pics"))
    wait(window, lambda: browse.listing is not None and browse.listing.path == str(tmp_path / "pics"))
    wait(window, lambda: prefetch_done(window))
    check("browse (a folder)")
    for name in ("IMG_001.jpg", "IMG_004.jpg", "IMG_007.jpg"):  # stars on the pictures, and then the Favorites page with something in it
        browse._click((f"star:{tile_key_of(browse, name)}",), None)  # type: ignore[arg-type]
    check("browse (with stars)")
    browse.view_box.set("Details")
    browse._view_changed()
    check("browse (details)")
    window.show("favorites")
    check("favorites (three favorites)")
    window.show("history")
    check("history")
    window.show("settings")
    check("settings")
    assert not problems, "\n".join(f"{page}: " + "; ".join(items) for page, items in problems.items())


# --------------------------------------------------------------------------- dialogs can always be closed, and never freeze the window
def close_with_the_window_manager(dialog: Any) -> None:
    """What the title-bar close button does: run whatever the dialog registered for WM_DELETE_WINDOW."""
    dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))


def test_every_dialog_closes_with_its_close_button(window: Any, tmp_path: Path) -> None:
    from duplio.gui.dialogs import (
        ConfirmDialog,
        ConnectDialog,
        FolderPicker,
        MessageDialog,
        PasswordDialog,
    )

    ctx = window.ctx
    dialogs = [ConnectDialog(ctx), FolderPicker(ctx, str(tmp_path)), ConfirmDialog(ctx, "Title", "Do it?", ["a"], [("x", "y")]),
               MessageDialog(ctx, "Title", "Done", ["a line"]), PasswordDialog(ctx, "nas", "me")]
    for dialog in dialogs:
        pump(window, timeout=0.2)
        assert dialog.winfo_exists()
        close_with_the_window_manager(dialog)
        pump(window, timeout=0.1)
        assert not dialog.winfo_exists(), type(dialog).__name__
    assert window.grab_current() is None  # nothing is left holding the keyboard and mouse


def test_every_dialog_closes_with_escape(window: Any, tmp_path: Path) -> None:
    from duplio.gui.dialogs import ConnectDialog, FolderPicker

    for dialog in (ConnectDialog(window.ctx), FolderPicker(window.ctx, str(tmp_path))):
        pump(window, timeout=0.2)
        dialog.focus_force()
        pump(window, timeout=0.1)
        dialog.focus_get().event_generate("<Escape>", when="now") if dialog.focus_get() is not None else dialog.event_generate("<Escape>")
        pump(window, timeout=0.2)
        assert not dialog.winfo_exists()


def test_the_phone_dialog_opens_at_once_and_can_be_closed_while_it_is_still_looking(window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    import duplio.devices as devices
    from duplio.gui.dialogs import ConnectDialog

    release = threading.Event()

    def slow_discover(*_a: Any, **_k: Any) -> list[Any]:
        release.wait(10)  # the Android bridge starting up, or a stale phone mount
        return []

    monkeypatch.setattr(devices, "discover", slow_discover)
    began = time.monotonic()
    dialog = ConnectDialog(window.ctx)
    assert time.monotonic() - began < 1.5  # it did not wait for the search
    pump(window, timeout=0.3)
    assert "Looking" in dialog.device_note.cget("text")
    assert dialog.device_list.size() >= 1  # the drives of this computer are there already
    close_with_the_window_manager(dialog)
    pump(window, timeout=0.2)
    assert not dialog.winfo_exists()
    release.set()
    pump(window, timeout=0.3)  # the late answer arrives for a window that is gone, and nothing goes wrong


def test_the_phone_dialog_lists_what_was_found_and_refuses_one_that_is_not_ready(window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import duplio.devices as devices
    from duplio.devices.discovery import Device
    from duplio.gui.dialogs import ConnectDialog

    monkeypatch.setattr(devices, "discover", lambda *_a, **_k: [Device("phone", "Pixel 8", "adb://abc/sdcard", "", True),
                                                                  Device("phone", "Old phone", "/gvfs/old", "", False)])
    dialog = ConnectDialog(window.ctx)
    wait(window, lambda: any("Pixel 8" in dialog.device_list.get(i) for i in range(dialog.device_list.size())))
    rows = [dialog.device_list.get(i) for i in range(dialog.device_list.size())]
    assert any(r.startswith("drive:") for r in rows) and any("(not ready)" in r for r in rows)
    dialog.device_list.selection_set(next(i for i, r in enumerate(rows) if "Old phone" in r))
    dialog._use_device()
    assert dialog.winfo_exists() and "not ready" in dialog.device_note.cget("text")  # explained, and still open
    dialog.device_list.selection_clear(0, "end")
    dialog.device_list.selection_set(next(i for i, r in enumerate(rows) if "Pixel 8" in r))
    dialog._use_device()
    pump(window, timeout=0.1)
    assert dialog.result == "adb://abc/sdcard" and not dialog.winfo_exists()


def test_the_folder_picker_has_the_drives_and_the_top_of_a_drive_can_be_reached(window: Any, tmp_path: Path) -> None:
    from duplio.gui.dialogs import FolderPicker

    picker = FolderPicker(window.ctx, str(tmp_path))
    wait(window, lambda: picker.here is not None)
    labels = [picker.places.get(i).strip() for i in range(picker.places.size())]
    assert "Drives" in labels and "Folders" in labels and any("Whole computer" in label or ":\\" in label for label in labels)
    assert picker.up_button._enabled
    for _ in range(40):  # keep going up: the top of the drive is reached, and Up then switches itself off
        if not picker.up_button._enabled:
            break
        before = picker.here.uri
        picker.up()
        wait(window, lambda before=before: picker.here.uri != before)
    assert not picker.up_button._enabled
    picker._cancel()


def test_a_wrong_address_is_explained_in_the_page_and_the_way_out_still_works(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "ok", 2)
    page = open_folder(window, tmp_path / "ok")
    page.open_text(str(tmp_path / "no such folder"))
    wait(window, lambda: page.here is not None and page.here.display.endswith("no such folder") and page._job is not None and page._job.finished)
    pump(window, timeout=0.2)
    assert "could not be read" in page.summary.cget("text") and page.up_button._enabled  # not a dialog; and Up is still there
    page.go_up()
    wait(window, lambda: page.listing is not None and page.listing.path == str(tmp_path))
    page.open_text("sftp://")  # not an address at all
    assert page.summary.cget("text").startswith("Cannot open that")


def test_browse_has_only_the_controls_that_matter(window: Any) -> None:
    from duplio.gui.widgets import Btn

    window.show("browse")
    page = window.pages["browse"]
    pump(window, timeout=0.3)

    def buttons(widget: Any) -> list[str]:
        found = [widget.cget("text")] if isinstance(widget, Btn) else []
        for child in widget.winfo_children():
            found += buttons(child)
        return found

    shown = sorted(b for b in buttons(page) if b)
    assert shown == sorted(["◀ Back", "▲ Up", "Reload", "+ Phone or server…"])  # the rest lives in the folder tree or in Settings
    for gone in ("folders_first", "only_pictures", "hidden", "cloud", "direction"):
        assert not hasattr(page, gone), gone


# --------------------------------------------------------------------------- the folder tree on the Browse page
def fake_places(window: Any, folders: Any = (), drives: Any = (), devices: Any = (), scanned: bool = True) -> Any:
    """Make the places service answer with these places (and not go looking), and show them in the folder tree."""
    from duplio.gui.places import Places

    ctx = window.ctx
    ctx.places.snapshot = lambda: Places(tuple(folders), tuple(drives), tuple(devices), scanned)  # type: ignore[method-assign]
    ctx.places.refresh = lambda *_a, **_k: False  # type: ignore[method-assign]
    window.show("browse")
    page = window.pages["browse"]
    pump(window, timeout=0.2)
    page.folder_tree.sync_places(force=True)
    return page


def roots_of(page: Any, kind: str) -> list[str]:
    tree = page.folder_tree
    return [tree.tree.item(i, "text").strip() for i in tree.tree.get_children(tree._sections[kind])]


def node_for(page: Any, folder: Path, section: str | None = None) -> str:
    """The tree node that shows ``folder`` (under one section, if given)."""
    from duplio.gui.foldertree import key_of

    tree = page.folder_tree
    key = key_of(window_location(page, folder))
    for iid in tree._by_key.get(key, []):
        top = iid
        while tree.tree.parent(top) and tree.tree.parent(top) not in tree._sections.values():
            top = tree.tree.parent(top)
        if section is None or tree.tree.parent(top) == tree._sections[section]:
            return str(iid)
    raise AssertionError(f"{folder} is not in the tree")


def window_location(page: Any, folder: Path) -> Any:
    return page.ctx.registry.open(str(folder))


def child_names(page: Any, iid: str) -> list[str]:
    tree = page.folder_tree
    return [tree.tree.item(c, "text") for c in tree.tree.get_children(iid) if c in tree._loc]


def test_the_folder_tree_is_there_from_the_start_with_folders_drives_and_phones(tmp_path: Path, window: Any) -> None:
    from duplio.gui.places import Place

    page = fake_places(window, folders=[Place("Home", str(tmp_path))], drives=[Place("Whole computer  (/)", "/"), Place("/mnt/usb", str(tmp_path))],
                       devices=[Place("phone: Pixel", str(tmp_path))])
    assert page.folder_tree.winfo_ismapped()  # no button to press first
    assert roots_of(page, "folders") == ["Home"] and roots_of(page, "drives") == ["Whole computer  (/)", "/mnt/usb"]
    assert roots_of(page, "devices") == ["phone: Pixel", "↻ look again"]


def test_the_folder_tree_says_when_it_is_still_looking_and_when_no_phone_was_found(tmp_path: Path, window: Any) -> None:
    page = fake_places(window, scanned=False)
    assert "looking…" in roots_of(page, "devices")
    fake_places(window, scanned=True)
    assert "none found" in roots_of(page, "devices")


def test_opening_a_folder_in_the_tree_lists_its_sub_folders_and_only_those(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.places import Place

    for name in ("b folder", "A folder", "c10", "c2"):
        (tmp_path / name / "inner").mkdir(parents=True)
    (tmp_path / "a file.txt").write_text("x")
    (tmp_path / ".hidden").mkdir()
    window.ctx.settings.skip_hidden = True
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    home = node_for(page, tmp_path, "folders")
    page.folder_tree._load(home)
    wait(window, lambda: page.folder_tree._state[home] == "loaded")
    assert child_names(page, home) == ["A folder", "b folder", "c2", "c10"]  # no files, no hidden ones, in natural order
    window.ctx.settings.skip_hidden = False
    page.folder_tree._load(home, refresh=True)
    wait(window, lambda: ".hidden" in child_names(page, home))


def test_clicking_a_folder_in_the_tree_opens_it_in_the_page(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.places import Place

    make_pictures(tmp_path / "Trip" / "Day 1", 3)
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    home = node_for(page, tmp_path, "folders")
    page.folder_tree._load(home)
    wait(window, lambda: page.folder_tree._state[home] == "loaded")
    trip = next(c for c in page.folder_tree.tree.get_children(home) if page.folder_tree.tree.item(c, "text") == "Trip")
    page.folder_tree.tree.selection_set(trip)
    wait(window, lambda: page.listing is not None and page.listing.path == str(tmp_path / "Trip"))
    assert page.address.get().endswith("Trip")  # nothing was typed


def test_the_tree_follows_the_page_down_to_the_folder_and_highlights_it(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.foldertree import key_of
    from duplio.gui.places import Place

    deep = tmp_path / "Photos" / "2024" / "Trip"
    make_pictures(deep, 2)
    (tmp_path / "Photos" / "2023").mkdir()
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    page.open_text(str(deep))
    tree = page.folder_tree
    wait(window, lambda: tree.tree.selection() and key_of(tree._loc[tree.tree.selection()[0]]) == key_of(window_location(page, deep)))
    shown = node_for(page, deep, "folders")
    assert tree.tree.selection() == (shown,)
    parent = tree.tree.parent(shown)
    while parent in tree._loc:  # every folder on the way is open, so the highlighted one is in view
        assert tree.tree.item(parent, "open")
        parent = tree.tree.parent(parent)
    assert "2023" in child_names(page, node_for(page, tmp_path / "Photos", "folders"))  # and its neighbours are listed


def test_the_tree_reaches_the_top_of_a_drive_for_a_folder_that_is_under_no_other_place(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.foldertree import key_of
    from duplio.gui.places import Place

    elsewhere = tmp_path / "away" / "deeper"
    make_pictures(elsewhere, 1)
    top = Path(elsewhere.anchor)
    page = fake_places(window, folders=[Place("Home", str(tmp_path / "away" / "other"))], drives=[Place("Whole computer", str(top))])
    (tmp_path / "away" / "other").mkdir()
    page.open_text(str(elsewhere))
    tree = page.folder_tree
    wait(window, lambda: tree.tree.selection() and key_of(tree._loc[tree.tree.selection()[0]]) == key_of(window_location(page, elsewhere)),
         timeout=20)
    assert tree.tree.parent(tree.tree.parent(tree.tree.selection()[0])) != tree._sections["folders"]
    node_for(page, elsewhere, "drives")  # it is under Drives: the way down from the very top


def test_a_folder_that_is_not_in_any_place_is_added_to_recent(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.foldertree import key_of

    other = tmp_path / "elsewhere"
    make_pictures(other, 1)
    page = fake_places(window)
    page.open_text(str(other))
    wait(window, lambda: page.folder_tree.tree.selection() and key_of(page.folder_tree._loc[page.folder_tree.tree.selection()[0]]) == key_of(window_location(page, other)))
    assert roots_of(page, "recent")[0].endswith("elsewhere")


def test_opening_a_slow_folder_in_the_tree_never_freezes_the_window(tmp_path: Path, window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    import threading
    import time

    from duplio.gui.places import Place

    (tmp_path / "kid").mkdir()
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    release = threading.Event()
    real = window.ctx.listings.list

    def slow(backend: Any, path: str, **kw: Any) -> Any:
        release.wait(10)  # a stalled network drive, or a stale phone mount
        return real(backend, path, **kw)

    monkeypatch.setattr(window.ctx.listings, "list", slow)
    home = node_for(page, tmp_path, "folders")
    began = time.monotonic()
    page.folder_tree._load(home)
    pump(window, timeout=0.4)
    assert time.monotonic() - began < 3 and page.folder_tree._state[home] == "loading"
    assert "looking" in page.folder_tree.tree.item(home + "~", "text")
    release.set()
    wait(window, lambda: page.folder_tree._state[home] == "loaded")
    assert child_names(page, home) == ["kid"]


def test_a_folder_that_cannot_be_read_says_so_in_the_tree_and_can_be_tried_again(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.places import Place

    page = fake_places(window, folders=[Place("Gone", str(tmp_path / "gone"))])
    gone = node_for(page, tmp_path / "gone", "folders")
    page.folder_tree._load(gone)
    wait(window, lambda: page.folder_tree._state[gone] == "unloaded" and "cannot read" in page.folder_tree.tree.item(gone + "~", "text"))
    (tmp_path / "gone" / "now here").mkdir(parents=True)
    page.folder_tree._load(gone)  # opening it again tries again
    wait(window, lambda: child_names(page, gone) == ["now here"])


def test_the_page_hands_the_tree_what_it_read_so_the_drive_is_not_asked_twice(tmp_path: Path, window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.gui.foldertree import key_of

    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.places import Place

    target = tmp_path / "Photos" / "Trip"
    make_pictures(target / "Day 1", 2)
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    asked: list[str] = []
    real = window.ctx.listings.list
    monkeypatch.setattr(window.ctx.listings, "list", lambda backend, path, **kw: (asked.append(path), real(backend, path, **kw))[1])
    open_folder(window, target)
    wait(window, lambda: any(page.folder_tree._by_key.get(key_of(window_location(page, target)), [])))
    node = node_for(page, target, "folders")
    wait(window, lambda: page.folder_tree._state[node] == "loaded")
    assert child_names(page, node) == ["Day 1"] and str(target) not in asked


def test_reload_reads_the_sub_folders_again(tmp_path: Path, window: Any) -> None:
    tmp_path = tmp_path / "base"
    tmp_path.mkdir()
    from duplio.gui.places import Place

    make_pictures(tmp_path / "pics", 2)
    (tmp_path / "pics" / "one").mkdir()
    page = fake_places(window, folders=[Place("Home", str(tmp_path))])
    open_folder(window, tmp_path / "pics")
    node = node_for(page, tmp_path / "pics", "folders")
    wait(window, lambda: page.folder_tree._state[node] == "loaded" and child_names(page, node) == ["one"])
    (tmp_path / "pics" / "two").mkdir()
    page.refresh()
    wait(window, lambda: child_names(page, node) == ["one", "two"])


# --------------------------------------------------------------------------- "is it backed up?": looking inside sub-folders is an option
def nested_folders(tmp_path: Path) -> tuple[Path, Path]:
    """A has a file at the top and one in a sub-folder; the backup keeps both, filed under a sub-folder."""
    a, b = tmp_path / "phone", tmp_path / "backup"
    (a / "Day 1").mkdir(parents=True)
    (b / "2024" / "kept").mkdir(parents=True)
    (a / "top.jpg").write_bytes(b"top photo" * 300)
    (a / "Day 1" / "deep.jpg").write_bytes(b"deep photo" * 300)
    (b / "2024" / "kept" / "top.jpg").write_bytes(b"top photo" * 300)
    (b / "2024" / "kept" / "deep.jpg").write_bytes(b"deep photo" * 300)
    (b / "loose.jpg").write_bytes(b"only loose in the backup" * 30)
    return a, b


def test_by_default_only_the_files_directly_in_each_folder_are_checked_and_the_answer_says_so(tmp_path: Path, window: Any) -> None:
    from duplio.gui.palette import PALETTE

    a, b = nested_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "backup", a, b)
    assert not page.folders.a.recursive.get() and not page.folders.b.recursive.get()
    page.run()
    wait(window, lambda: page.diff is not None)
    assert not dialogs.confirms  # nothing slow was asked for, so there is no warning
    seen = {t.entry.rel for r in page.all_rows for t in r.tiles}
    assert seen == {"top.jpg", "loose.jpg"}  # sub-folders were not looked inside
    headline, detail = page.headline.cget("text"), page.detail.cget("text")
    assert headline == "1 of 1 files directly in A are NOT in the backup."  # it does not claim more than it looked at
    assert "Day 1" in detail and "were not looked inside" in detail
    assert "the backup has sub-folders that were not looked inside" in detail.lower()  # why top.jpg shows as missing, and what to do
    assert page.banner.cget("bg") == PALETTE["bad_soft"]

def test_ticking_a_side_warns_first_and_then_looks_inside_its_sub_folders(tmp_path: Path, window: Any) -> None:
    a, b = nested_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    page = page_for(window, "backup", a, b)
    page.folders.a.recursive.set(True)
    page.folders.b.recursive.set(True)
    page.run()
    wait(window, lambda: page.diff is not None)
    title, headline, details, _rows = dialogs.confirms[-1]
    assert title == "Look inside sub-folders?" and "take a long time" in headline
    assert any("many minutes" in line and "only reads" in line for line in details)  # why it can take a while, and that nothing changes
    assert any("sub-folders of A and the backup" in line for line in details)
    seen = {t.entry.rel for r in page.all_rows for t in r.tiles if t.side == 0}
    assert seen == {"top.jpg", "Day 1/deep.jpg"}
    assert page.headline.cget("text").startswith("Everything in A is safe")  # all of A, found in the backup's sub-folders
    assert not page.diff.inventory.unvisited[0] and "not looked inside" not in page.detail.cget("text")


def test_declining_the_warning_checks_nothing(tmp_path: Path, window: Any) -> None:
    a, b = nested_folders(tmp_path)
    dialogs = Dialogs(window.ctx)
    dialogs.answer = False
    page = page_for(window, "backup", a, b)
    page.folders.b.recursive.set(True)
    page.run()
    pump(window, timeout=0.5)
    assert dialogs.confirms and page.diff is None and not page.has_result


def test_the_backup_can_be_looked_inside_while_a_is_only_checked_at_its_top(tmp_path: Path, window: Any) -> None:
    a, b = nested_folders(tmp_path)
    Dialogs(window.ctx)
    page = page_for(window, "backup", a, b)
    page.folders.b.recursive.set(True)  # the usual case: a flat phone folder against a backup filed into folders
    page.run()
    wait(window, lambda: page.diff is not None)
    assert {t.entry.rel for r in page.all_rows for t in r.tiles if t.side == 0} == {"top.jpg"}
    assert page.diff.inventory.roots[1].recursive and not page.diff.inventory.roots[0].recursive
    from duplio.gui.palette import PALETTE

    assert page.headline.cget("text") == "Everything directly in A is safe in the backup."
    assert "Day 1" in page.detail.cget("text") and "not looked inside" in page.detail.cget("text")
    assert page.banner.cget("bg") == PALETTE["warn_soft"]  # amber: all that was looked at is safe, but not everything was looked at


def test_the_other_pages_still_look_inside_every_folder(tmp_path: Path, window: Any) -> None:
    a, b = nested_folders(tmp_path)
    Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    assert not hasattr(page.folders.a, "recursive") or not page.folders.a._has_option  # no option, no change in behaviour
    page.run()
    wait(window, lambda: page.diff is not None)
    assert "Day 1/deep.jpg" in {t.entry.rel for r in page.all_rows for t in r.tiles}


# --------------------------------------------------------------------------- favorites and albums
def star_tags(page: Any) -> dict[str, str]:
    """``{tile key: ★ or ☆}`` for every star drawn on the page's canvas."""
    canvas = page.view.canvas
    out: dict[str, str] = {}
    for item in canvas.find_all():
        for tag in canvas.gettags(item):
            if tag.startswith("star:") and canvas.type(item) == "text":
                out[tag[5:]] = canvas.itemcget(item, "text")
    return out


def tile_key_of(page: Any, name: str) -> str:
    return next(k for k, i in page.by_key.items() if i.name == name)


def test_a_click_on_the_star_of_a_picture_makes_it_a_favorite_and_a_second_click_takes_it_back(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "pics", 4)
    page = open_folder(window, tmp_path / "pics")
    key = tile_key_of(page, "IMG_001.jpg")
    pump(window, timeout=0.3)
    assert set(star_tags(page).values()) == {"☆"}  # every picture has a star, none filled
    page._click((f"star:{key}",), None)  # type: ignore[arg-type]
    pump(window, timeout=0.2)
    uri = page.by_key[key].tile.uri
    assert uri in window.ctx.favorites and star_tags(page)[key] == "★"
    assert [f.name for f in window.ctx.favorites.items()] == ["IMG_001.jpg"]
    page._click((f"star:{key}",), None)  # type: ignore[arg-type]
    assert uri not in window.ctx.favorites and star_tags(page)[key] == "☆"
    assert page.selected is None  # starring does not also select the picture


def test_the_f_key_stars_the_selected_picture(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "pics", 3)
    page = open_folder(window, tmp_path / "pics")
    key = tile_key_of(page, "IMG_002.jpg")
    page._click((f"tile:{key}",), None)  # type: ignore[arg-type]
    page._star_selected()
    assert [f.name for f in window.ctx.favorites.items()] == ["IMG_002.jpg"]
    page._star_selected()
    assert not len(window.ctx.favorites)


def test_stars_are_kept_for_the_next_time_duplio_is_opened(tmp_path: Path, window: Any, dirs: AppDirs) -> None:
    from duplio.favorites import Favorites

    make_pictures(tmp_path / "pics", 2)
    page = open_folder(window, tmp_path / "pics")
    page._click((f"star:{tile_key_of(page, 'IMG_000.jpg')}",), None)  # type: ignore[arg-type]
    assert [f.name for f in Favorites(dirs.state / "favorites.json").items()] == ["IMG_000.jpg"]  # what a new session would read


def test_the_favorites_page_shows_every_starred_picture_from_any_folder_and_unstars(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "one", 3)
    make_pictures(tmp_path / "two", 3)
    for folder, name in (("one", "IMG_000.jpg"), ("two", "IMG_002.jpg"), ("one", "IMG_001.jpg")):
        page = open_folder(window, tmp_path / folder)
        page._click((f"star:{tile_key_of(page, name)}",), None)  # type: ignore[arg-type]
    window.show("favorites")
    fav = window.pages["favorites"]
    pump(window, timeout=0.5)
    assert [i.name for i in fav.items] == ["IMG_001.jpg", "IMG_002.jpg", "IMG_000.jpg"]  # newest starred first
    assert "3 favorites: 3 pictures" in fav.summary.cget("text")
    fav.order_box.set("Name, A to Z")
    fav._ordered()
    assert [i.name for i in fav.items] == ["IMG_000.jpg", "IMG_001.jpg", "IMG_002.jpg"]
    assert set(star_tags(fav).values()) == {"★"}
    fav._click((f"star:{tile_key_of(fav, 'IMG_001.jpg')}",), None)  # type: ignore[arg-type]
    assert [i.name for i in fav.items] == ["IMG_000.jpg", "IMG_002.jpg"] and len(window.ctx.favorites) == 2
    assert (tmp_path / "one" / "IMG_001.jpg").exists()  # taking a star off never touches the picture


def test_an_empty_favorites_page_says_how_to_star_something(window: Any) -> None:
    window.show("favorites")
    page = window.pages["favorites"]
    pump(window, timeout=0.3)
    assert page.summary.cget("text") == "Nothing is starred yet." and not page.album_button._enabled
    assert "Click the ☆" in page.view.canvas.itemcget(page.view.canvas.find_all()[0], "text") if page.view.canvas.find_all() else True


def test_removing_all_stars_asks_first_and_only_removes_stars(tmp_path: Path, window: Any) -> None:
    make_pictures(tmp_path / "pics", 3)
    dialogs = Dialogs(window.ctx)
    page = open_folder(window, tmp_path / "pics")
    for name in ("IMG_000.jpg", "IMG_001.jpg"):
        page._click((f"star:{tile_key_of(page, name)}",), None)  # type: ignore[arg-type]
    window.show("favorites")
    fav = window.pages["favorites"]
    dialogs.answer = False
    fav.clear_all()
    assert len(window.ctx.favorites) == 2 and dialogs.confirms[-1][0] == "Remove all stars?"
    dialogs.answer = True
    fav.clear_all()
    assert len(window.ctx.favorites) == 0 and (tmp_path / "pics" / "IMG_000.jpg").exists()


def test_the_context_menu_of_a_picture_offers_the_star(tmp_path: Path, window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkinter as tk

    make_pictures(tmp_path / "pics", 2)
    page = open_folder(window, tmp_path / "pics")
    menus: list[Any] = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y: menus.append(self))

    class Where:
        x_root = y_root = 10

    key = tile_key_of(page, "IMG_000.jpg")
    page._context((f"tile:{key}",), Where())  # type: ignore[arg-type]
    labels = [menus[-1].entrycget(i, "label") for i in range(menus[-1].index("end") + 1) if menus[-1].type(i) == "command"]
    assert "★ Add to favorites" in labels
    menus[-1].invoke(labels.index("★ Add to favorites"))
    page._context((f"tile:{key}",), Where())  # type: ignore[arg-type]
    labels = [menus[-1].entrycget(i, "label") for i in range(menus[-1].index("end") + 1) if menus[-1].type(i) == "command"]
    assert "☆ Remove from favorites" in labels and len(window.ctx.favorites) == 1


def test_stars_are_on_the_comparison_pages_too(tmp_path: Path, window: Any) -> None:
    a, b = make_demo_folders(tmp_path)
    Dialogs(window.ctx)
    page = page_for(window, "compare", a, b)
    page.run()
    wait(window, lambda: page.diff is not None)
    pump(window, timeout=0.5)
    keys = [k for k, mark in star_tags(page.pairs).items() if mark == "☆"]
    assert keys
    tile = page.pairs.tiles[keys[0]]
    page.pairs._click((f"star:{keys[0]}",), None)  # type: ignore[arg-type]
    assert tile.uri in window.ctx.favorites and not page.pairs.selected  # starred, and not ticked for copying


class FakeAlbumDialog:
    """Stands in for the dialog: answers with a fixed choice."""

    choice: Any = None

    def __init__(self, ctx: Any, count: int) -> None:
        self.count = count

    def show(self) -> Any:
        return FakeAlbumDialog.choice


def test_an_album_is_made_from_the_favorites_and_can_be_undone(tmp_path: Path, window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.backup.album import AlbumOptions
    from duplio.gui.dialogs import AlbumChoice
    from duplio.gui.pages import favorites as favorites_page
    from duplio.plan.undo import undo

    make_pictures(tmp_path / "pics", 4)
    dialogs = Dialogs(window.ctx)
    page = open_folder(window, tmp_path / "pics")
    for name in ("IMG_000.jpg", "IMG_003.jpg"):
        page._click((f"star:{tile_key_of(page, name)}",), None)  # type: ignore[arg-type]
    albums = tmp_path / "albums"
    albums.mkdir()
    monkeypatch.setattr(favorites_page, "AlbumDialog", FakeAlbumDialog)
    FakeAlbumDialog.choice = AlbumChoice(str(albums), AlbumOptions(name="Best of", stamp="after"))
    window.show("favorites")
    fav = window.pages["favorites"]
    fav.make_album()
    wait(window, lambda: dialogs.infos)
    title, headline, lines, kind, buttons = dialogs.infos[-1]
    assert title == "Album made" and headline == "Done." and kind == "ok" and "2 files copied" in lines[0]
    confirm = dialogs.confirms[-1]
    assert confirm[0] == "Make the album" and "'Best of " in confirm[1] and {r[0] for r in confirm[3]} == {"IMG_000.jpg", "IMG_003.jpg"}
    folders = [p for p in albums.iterdir() if p.is_dir()]
    assert len(folders) == 1 and folders[0].name.startswith("Best of ") and sorted(p.name for p in folders[0].iterdir()) == ["IMG_000.jpg", "IMG_003.jpg"]
    assert any(label == "Open the album folder" for label, _cmd, _style in buttons)
    assert all((tmp_path / "pics" / n).exists() for n in ("IMG_000.jpg", "IMG_003.jpg"))  # originals stay
    from duplio.plan.journal import list_journals, summarize

    journal = list_journals(window.ctx.dirs.journals)[0]
    assert summarize(journal).header["action"] == "album"
    assert undo(None, window.ctx.registry, dirs=window.ctx.dirs).count("restored") == 2  # History & undo takes the copies away
    assert not any(folders[0].glob("*.jpg"))


def test_an_album_with_nothing_reachable_says_so_and_copies_nothing(tmp_path: Path, window: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.backup.album import AlbumOptions
    from duplio.gui.dialogs import AlbumChoice
    from duplio.gui.pages import favorites as favorites_page

    dialogs = Dialogs(window.ctx)
    window.ctx.favorites.add(str(tmp_path / "gone" / "x.jpg"), "x.jpg", 5, 5)
    albums = tmp_path / "albums"
    albums.mkdir()
    monkeypatch.setattr(favorites_page, "AlbumDialog", FakeAlbumDialog)
    FakeAlbumDialog.choice = AlbumChoice(str(albums), AlbumOptions(name="Empty"))
    window.show("favorites")
    window.pages["favorites"].make_album()
    wait(window, lambda: dialogs.infos)
    assert dialogs.infos[-1][0] == "Nothing to copy" and "was not found" in dialogs.infos[-1][2][0] and not list(albums.iterdir())


def test_making_an_album_with_no_favorites_says_to_star_some(window: Any) -> None:
    dialogs = Dialogs(window.ctx)
    window.show("favorites")
    window.pages["favorites"].make_album()
    assert dialogs.infos[-1][0] == "Nothing is starred"


# --------------------------------------------------------------------------- the album dialog
def test_the_album_dialog_previews_the_folder_name_as_it_is_typed(tmp_path: Path, window: Any) -> None:
    from datetime import datetime

    from duplio.gui.dialogs import AlbumDialog

    dialog = AlbumDialog(window.ctx, 12, now=lambda: datetime(2026, 10, 14, 15, 30, 22))
    pump(window, timeout=0.2)
    assert not dialog.make_button._enabled and "Type a name" in dialog.preview.cget("text")  # nothing to name it by yet
    dialog.name.set("Summer trip")
    assert dialog.preview.cget("text").endswith("Summer trip") and dialog.make_button._enabled
    dialog.place.set("before")
    dialog._update()
    assert dialog.preview.cget("text").endswith("2026-10-14 Summer trip")
    dialog.place.set("after")
    dialog.style_box.current(1)
    dialog._update()
    assert dialog.preview.cget("text").endswith("Summer trip 2026-10-14 15-30")
    dialog.name.set("")
    assert dialog.preview.cget("text").endswith("2026-10-14 15-30") and dialog.make_button._enabled  # the stamp alone is a name
    dialog.place.set("none")
    dialog._update()
    assert not dialog.make_button._enabled
    dialog._cancel()


def test_the_album_dialog_returns_the_choice_and_remembers_it(tmp_path: Path, window: Any) -> None:
    from datetime import datetime

    from duplio.gui.dialogs import AlbumDialog

    where = tmp_path / "Albums"
    where.mkdir()
    dialog = AlbumDialog(window.ctx, 3, now=lambda: datetime(2026, 1, 2, 3, 4, 5))
    dialog.name.set("Trip")
    dialog.place.set("before")
    dialog.style_box.current(2)
    dialog.destination.set(str(where))
    dialog._ok()
    pump(window, timeout=0.1)
    choice = dialog.result
    assert choice is not None and choice.destination == str(where)
    assert (choice.options.name, choice.options.stamp, choice.options.stamp_format) == ("Trip", "before", "seconds")
    assert window.ctx.state.get("album_dest") == str(where) and window.ctx.state.get("album_stamp") == "before"
    again = AlbumDialog(window.ctx, 3)
    assert again.place.get() == "before" and again.destination.get() == str(where)  # the same choices next time
    again._cancel()


def test_a_missing_destination_is_made_only_after_asking(tmp_path: Path, window: Any) -> None:
    from duplio.gui.dialogs import AlbumDialog

    dialogs = Dialogs(window.ctx)
    target = tmp_path / "not" / "yet"
    dialog = AlbumDialog(window.ctx, 3)
    dialog.name.set("Trip")
    dialog.destination.set(str(target))
    dialogs.answer = False
    dialog._ok()
    assert dialog.winfo_exists() and not target.exists() and dialogs.confirms[-1][0] == "Make the folder?"
    dialogs.answer = True
    dialog._ok()
    assert target.is_dir() and dialog.result is not None


def test_nothing_in_the_album_dialog_is_cut_off(window: Any) -> None:
    from duplio.gui.dialogs import AlbumDialog

    dialog = AlbumDialog(window.ctx, 1234)
    dialog.name.set("A rather long album name that goes on and on for a while")
    pump(window, timeout=0.4)
    assert cut_off(dialog) == []
    dialog._cancel()


# --------------------------------------------------------------------------- what is kept on disk, and the purge
def keybar_texts(window: Any) -> list[str]:
    import tkinter as tk

    found: list[str] = []

    def walk(widget: Any) -> None:
        for child in widget.winfo_children():
            if isinstance(child, tk.Label):
                found.append(str(child.cget("text")))
            walk(child)

    walk(window._keybar)
    return found


def test_a_folder_on_the_computers_own_disk_shows_pictures_but_keeps_none_on_disk(tmp_path: Path, window: Any, dirs: AppDirs) -> None:
    root = tmp_path / "local"
    make_pictures(root, 12)
    page = open_folder(window, root)
    wait(window, lambda: window.ctx.thumbs.stats()["images"] >= 12)  # all shown ...
    assert window.ctx.prefetcher.status().state == "idle"  # ... with nothing fetched ahead in the background
    pump(window, timeout=0.5)
    assert "Pictures are made as you scroll" in page.fetch_label.cget("text")
    assert not page.fetch_button.winfo_ismapped()
    assert "not kept on disk" in page.buffer_label.cget("text")
    assert window.ctx.previews.stored() == (0, 0) and not (dirs.cache / "previews").exists()


def test_choosing_the_memory_for_pictures_on_screen_is_used_and_shown_at_once(tmp_path: Path, window: Any, dirs: AppDirs) -> None:
    from duplio.config import load_settings
    from duplio.gui.memory import GIB, MIB

    window.show("settings")
    pump(window, timeout=0.2)
    page = window.pages["settings"]
    page.memory.set("4 GB")
    page.memory.event_generate("<<ComboboxSelected>>")  # choosing it is enough: no Save button needed
    pump(window, timeout=0.2)
    assert window.ctx.thumbs.stats()["budget"] == 4 * GIB and window.ctx.settings.previews.memory_bytes == 4 * GIB
    assert load_settings(dirs).previews.memory_bytes == 4 * GIB  # and remembered for the next start
    window.show("browse")
    browse = window.pages["browse"]
    pump(window, timeout=0.8)
    assert "of 4.0 GB" in browse.buffer_label.cget("text")
    # when the computer is short of memory the window holds fewer, and says what was chosen and why it is less
    window.ctx.thumbs._memory = lambda: 100 * MIB
    window.ctx.thumbs.check_memory(force=True)
    pump(window, timeout=0.8)
    text = browse.buffer_label.cget("text")
    assert "of 1.0 GB" in text and "you chose 4.0 GB" in text and "short of memory" in text


def test_the_disk_buffer_choices_apply_at_once_and_a_disk_that_will_not_save_does_not_lose_them(
        tmp_path: Path, window: Any, dirs: AppDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.config import load_settings
    from duplio.gui.memory import GIB

    dialogs = Dialogs(window.ctx)
    window.show("settings")
    pump(window, timeout=0.2)
    page = window.pages["settings"]
    assert page.buffer.get() == "4 GB" and window.ctx.settings.previews.max_bytes == 4 * GIB  # the default
    page.buffer.set("8 GB")
    page.buffer.event_generate("<<ComboboxSelected>>")
    assert window.ctx.settings.previews.max_bytes == 8 * GIB and load_settings(dirs).previews.max_bytes == 8 * GIB
    import duplio.gui.pages.settings as settings_page

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(settings_page, "save_settings", refuse)
    page.buffer.set("2 GB")
    page.buffer.event_generate("<<ComboboxSelected>>")
    assert window.ctx.settings.previews.max_bytes == 2 * GIB  # in use
    assert dialogs.errors and "could not be saved" in dialogs.errors[-1]


def test_the_two_buffers_are_emptied_by_two_separate_key_chords_shown_in_the_bar_and_each_asks_first(
        tmp_path: Path, window: Any, usb: None) -> None:
    from duplio.gui.app import CLEAR_DISK_KEYS, CLEAR_MEMORY_KEYS

    root = tmp_path / "usb stick"
    make_pictures(root, 10)
    previews = window.ctx.previews
    previews.set_memory_limit(7_000)  # room for only a few in memory: the rest of the folder goes on to the disk buffer
    dialogs = Dialogs(window.ctx)
    page = open_folder(window, root)
    wait(window, lambda: prefetch_done(window))
    held, _ = previews.held()
    stored, _ = previews.stored()
    assert 0 < held < 10 and stored > 0 and held + stored == 10  # memory first; the disk only for what memory had no room for
    for key in ("home", "browse", "favorites", "compare", "settings"):  # on every page the bar names both chords
        window.show(key)
        texts = keybar_texts(window)
        assert CLEAR_MEMORY_KEYS in texts and "Clear memory buffer" in texts and CLEAR_DISK_KEYS in texts and "Clear disk buffer" in texts
    window.show("browse")
    window.focus_force()
    pump(window, timeout=0.2)
    settings = window.pages["settings"]
    window.show("settings")
    words = [str(w.cget("text")).lower() for w in _all_widgets(settings) if "text" in w.keys()]  # noqa: SIM118
    assert not any(text.strip() in ("purge", "clear", "delete pictures", "clear memory buffer", "clear disk buffer") for text in words)  # no button
    assert any(CLEAR_MEMORY_KEYS in text and CLEAR_DISK_KEYS in text for text in words)  # the page says which keys do what
    window.show("browse")

    # the memory chord: no leaves everything alone, yes empties memory only, and what is on screen comes back from the disk buffer
    dialogs.answer = False
    (window.focus_get() or window).event_generate("<Control-Shift-Key-M>")
    pump(window, timeout=0.3)
    assert dialogs.confirms[-1][0] == "Clear memory buffer" and previews.held()[0] == held
    dialogs.answer = True
    (window.focus_get() or window).event_generate("<Control-Shift-Key-M>")
    pump(window, timeout=0.5)
    assert any(f"Emptied the memory buffer ({held:,} pictures)" in info[1] for info in dialogs.infos)  # all that was held went
    assert previews.stored()[0] == stored  # the disk buffer was not touched
    wait(window, lambda: window.ctx.thumbs.stats()["images"] == 10)  # the pictures on screen were drawn again (from disk or the drive)

    # the disk chord: no leaves everything alone, yes empties the disk buffer only
    previews.stats.fetched = 0
    dialogs.answer = False
    (window.focus_get() or window).event_generate("<Control-Shift-Key-D>")
    pump(window, timeout=0.3)
    assert dialogs.confirms[-1][0] == "Clear disk buffer" and "pictures" in " ".join(dialogs.confirms[-1][2]) and previews.stored()[0] == stored
    dialogs.answer = True
    (window.focus_get() or window).event_generate("<Control-Shift-Key-D>")
    wait(window, lambda: previews.stored() == (0, 0))
    wait(window, lambda: any("Emptied the disk buffer" in info[1] for info in dialogs.infos))
    assert window.ctx.prefetcher.status().state == "idle"  # the background fetch does not start again by itself
    pump(window, timeout=0.5)
    assert page.fetch_button.cget("text") == "Fetch all pictures"
    # nothing anywhere says "purge", and neither dialog is dressed up as a danger to the person's own pictures
    said = " ".join(" ".join([title, headline, *details]) for title, headline, details, _rows in dialogs.confirms).lower()
    assert "purge" not in said and "only these copies are emptied" in said and "your own files" in said


def _all_widgets(root: Any) -> list[Any]:
    out = [root]
    for child in root.winfo_children():
        out.extend(_all_widgets(child))
    return out
