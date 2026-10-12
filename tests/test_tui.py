from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input

from duplio.tui.app import DuplioApp, HomeScreen
from duplio.tui.dedupe import (
    CompareResultScreen,
    ConfirmPlanScreen,
    ReviewScreen,
    SetupScreen,
)
from duplio.tui.job import JobScreen
from duplio.tui.tools import BackupScreen, HistoryScreen, IngestScreen, MirrorScreen
from duplio.tui.widgets import ConfirmDialog, HelpScreen

from .conftest import rand_bytes, write


async def wait_for(pilot, predicate, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    steps = int(timeout / 0.05)
    for _ in range(steps):
        await pilot.pause(0.05)
        if predicate():
            return
    raise AssertionError("condition not reached")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    data = rand_bytes(150_000, 1)
    write(tmp_path / "a" / "IMG.jpg", data, mtime=1_600_000_000)
    write(tmp_path / "b" / "IMG (1).jpg", data, mtime=1_600_000_000)
    write(tmp_path / "b" / "IMG - Copy.jpg", data, mtime=1_600_000_000)
    write(tmp_path / "b" / "[odd].txt", "same" * 40)
    write(tmp_path / "a" / "[odd].txt", "same" * 40)
    write(tmp_path / "b" / "lonely.bin", rand_bytes(777, 2))
    return tmp_path


def run_app(coro_fn):  # type: ignore[no-untyped-def]
    async def main() -> None:
        app = DuplioApp()
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await coro_fn(app, pilot)

    asyncio.run(main())


async def add_locations(app, pilot, paths, *, reference=()):  # type: ignore[no-untyped-def]
    screen = app.screen
    for path in paths:
        screen.query_one("#loc-input", Input).value = str(path)
        await pilot.click("#add-normal")
        await pilot.pause(0.3)  # Textual ignores a second press during the button's active animation
    for path in reference:
        screen.query_one("#loc-input", Input).value = str(path)
        await pilot.click("#add-ref")
        await pilot.pause(0.3)


def test_every_screen_opens() -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        assert isinstance(app.screen, HomeScreen)
        for key, screen_type in (("1", SetupScreen), ("2", SetupScreen), ("3", IngestScreen), ("4", MirrorScreen),
                                 ("5", BackupScreen), ("6", HistoryScreen)):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, screen_type), (key, app.screen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.press("f2")
        await pilot.pause()
        assert app.theme == "duplio-light"

    run_app(flow)


def test_review_toggle_apply_and_undo(tree: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        await add_locations(app, pilot, [tree / "a", tree / "b"])
        await pilot.click("#start")
        await wait_for(pilot, lambda: isinstance(app.screen, ReviewScreen))
        review: ReviewScreen = app.screen
        jpg_group = next(g for g in review.result.groups if g.count == 3)
        # Default rules keep the clean original.
        kept = [m.rel for m in jpg_group.members if m.id in review.keep[jpg_group.id]]
        assert kept == ["IMG.jpg"]
        # Focus the jpg group's file list and keep one more copy.
        groups = review.query_one("#groups", DataTable)
        groups.move_cursor(row=[g.id for g in review.result.groups].index(jpg_group.id))
        await pilot.pause()
        members = review.query_one("#members", DataTable)
        members.focus()
        copy_row = [m.rel for m in jpg_group.members].index("IMG - Copy.jpg")
        members.move_cursor(row=copy_row)
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert len(review.keep[jpg_group.id]) == 2
        # The last kept copy can never be unmarked.
        review.keep[jpg_group.id] = {next(m.id for m in jpg_group.members if m.rel == "IMG.jpg")}
        review._after_change()
        orig_row = [m.rel for m in jpg_group.members].index("IMG.jpg")
        members.move_cursor(row=orig_row)
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert len(review.keep[jpg_group.id]) == 1
        # Skip the text group entirely.
        txt_group = next(g for g in review.result.groups if g.count == 2)
        groups.focus()
        groups.move_cursor(row=[g.id for g in review.result.groups].index(txt_group.id))
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert txt_group.id in review.excluded
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmPlanScreen)
        plan = app.screen.plan
        assert sorted(i.victim.rel for i in plan.items) == ["IMG (1).jpg", "IMG - Copy.jpg"]
        await pilot.click("#apply")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        assert app.screen.result.count("done") == 2
        await pilot.click("#job-done")
        await wait_for(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert not (tree / "b" / "IMG (1).jpg").exists()
        assert (tree / "b" / "[odd].txt").exists()  # skipped group untouched
        # Undo from History.
        await pilot.press("6")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        await pilot.click("#undo")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.click("#cd-ok")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        assert app.screen.result.count("restored") == 2
        assert (tree / "b" / "IMG (1).jpg").exists()

    run_app(flow)


def test_delete_needs_typed_confirmation(tree: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        await add_locations(app, pilot, [tree / "a", tree / "b"])
        await pilot.click("#start")
        await wait_for(pilot, lambda: isinstance(app.screen, ReviewScreen))
        app.screen.query_one("#action").value = "delete"
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.click("#apply")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ConfirmDialog)
        assert dialog.query_one("#cd-ok").disabled
        dialog.query_one("#cd-word", Input).value = "delete"  # wrong case
        await pilot.pause()
        assert dialog.query_one("#cd-ok").disabled
        await pilot.click("#cd-cancel")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmPlanScreen)
        assert (tree / "b" / "IMG (1).jpg").exists()

    run_app(flow)


def test_compare_flow(tree: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("2")
        await pilot.pause()
        await add_locations(app, pilot, [tree / "b"], reference=[tree / "a"])
        await pilot.click("#start")
        await wait_for(pilot, lambda: isinstance(app.screen, CompareResultScreen))
        result = app.screen.result
        assert [e.rel for e in result.unique] == ["lonely.bin"]
        app.screen.query_one("#copy-dest", Input).value = str(tree / "rescued")
        await pilot.click("#copy")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        assert (tree / "rescued" / "lonely.bin").exists()

    run_app(flow)


def test_setup_validation(tree: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("2")
        await pilot.pause()
        await add_locations(app, pilot, [tree / "b"])
        await pilot.click("#start")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)  # needs a location to check against

    run_app(flow)


def test_backup_and_ingest_screens(tree: Path, tmp_path: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("5")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#repo", Input).value = str(tmp_path / "repo")
        screen.query_one("#source", Input).value = str(tree / "a")
        await pilot.click("#init")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        await pilot.click("#job-done")
        await pilot.pause(0.3)
        await pilot.click("#run")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        await pilot.click("#job-done")
        await wait_for(pilot, lambda: bool(getattr(app.screen, "snapshot_ids", [])))
        assert len(app.screen.snapshot_ids) == 1
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        screen = app.screen
        write(tmp_path / "phone" / "IMG_20240101_101010.jpg", rand_bytes(2000, 9), mtime=1_700_000_000)
        screen.query_one("#source", Input).value = str(tmp_path / "phone")
        screen.query_one("#library", Input).value = str(tmp_path / "lib")
        await pilot.click("#run")
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.finished)
        assert app.screen.result.count("imported") == 1

    run_app(flow)
