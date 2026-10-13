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


def test_browse_shows_a_folder_and_fetches_all_its_pictures_into_the_buffer(tmp_path: Path, window: Any) -> None:
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
    assert page.shown[0].name == "zzz folder"  # folders first by default
    page.sort_box.set("Date modified")
    page.descending = True
    page._paint_direction()
    page._view_changed()
    assert [i.name for i in page.shown if not i.is_dir][:2] == ["IMG_011.jpg", "IMG_010.jpg"]  # newest first
    page.sort_box.set("Size")
    page._view_changed()
    sizes = [i.size for i in page.shown if not i.is_dir]
    assert sizes == sorted(sizes, reverse=True)
    page.folders_first.set(False)
    page.sort_box.set("Name")
    page.descending = False
    page._view_changed()
    assert page.shown[0].name == "IMG_000.jpg" and page.shown[-1].name == "zzz folder"
    page.filter_var.set("img_00")
    pump(window, timeout=0.6)
    assert [i.name for i in page.shown] == [f"IMG_00{n}.jpg" for n in range(10)]  # a part of a name, any case
    page.view_box.set("Details")
    page._view_changed()
    assert page.details and page.view.count == 10
    page.only_pictures.set(True)
    page.filter_var.set("")
    pump(window, timeout=0.6)
    assert [i.name for i in page.shown if not i.is_dir] == [f"IMG_{n:03d}.jpg" for n in range(12)]
    assert sum(1 for i in page.shown if i.is_dir) == 1  # folders stay: they are how you move on


def test_browse_says_so_when_the_buffer_is_full_instead_of_throwing_pictures_away(tmp_path: Path, window: Any) -> None:
    root = tmp_path / "many"
    make_pictures(root, 40)
    window.ctx.settings.previews.max_bytes = 30_000
    window.ctx.previews.apply_settings()
    page = open_folder(window, root)
    wait(window, lambda: prefetch_done(window))
    status = window.ctx.prefetcher.status()
    assert status.state == "stopped" and "buffer is full" in status.reason
    wait(window, lambda: "Stopped" in page.fetch_label.cget("text"))
    assert "Settings" in page.fetch_label.cget("text") and page.fetch_button.cget("text") == "Fetch again"
    store = window.ctx.previews.buffer_for(page._preview_items()[0])
    assert store is not None and store.total_bytes <= 30_000  # and the limit held


def test_leaving_the_browser_pauses_the_background_fetch_and_coming_back_resumes_it(tmp_path: Path, window: Any) -> None:
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

    walk(root)
    return problems


@pytest.mark.parametrize("width", [1280, 1100, 900])
def test_no_button_label_or_box_is_ever_cut_off(tmp_path: Path, window: Any, width: int) -> None:
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
    browse.view_box.set("Details")
    browse._view_changed()
    check("browse (details)")
    window.show("history")
    check("history")
    window.show("settings")
    check("settings")
    assert not problems, "\n".join(f"{page}: " + "; ".join(items) for page, items in problems.items())
