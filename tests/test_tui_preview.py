"""Pictures in the review screen: one fetch per duplicate group, nothing downloaded unasked."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("textual")
pytest.importorskip("PIL")

from textual.widgets import DataTable

from duplio.tui.dedupe import ReviewScreen
from duplio.tui.preview import PreviewPane, preview_items

from .previewkit import photo_with_exif_thumbnail
from .test_tui import add_locations, run_app, wait_for


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    for n in range(4):
        photo_with_exif_thumbnail(tmp_path / "a" / f"IMG_{n}.jpg", seed=n, padding=300_000)
    (tmp_path / "b").mkdir(exist_ok=True)
    for n in range(4):
        shutil.copy(tmp_path / "a" / f"IMG_{n}.jpg", tmp_path / "b" / f"copy of IMG_{n}.jpg")
    return tmp_path


def test_one_picture_serves_a_group_and_the_next_groups_are_fetched_ahead(photos: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        await add_locations(app, pilot, [photos / "a", photos / "b"])
        await pilot.click("#start")
        await wait_for(pilot, lambda: isinstance(app.screen, ReviewScreen))
        review: ReviewScreen = app.screen
        pane = review.query_one("#preview", PreviewPane)
        await wait_for(pilot, lambda: pane._current is not None)
        assert pane._current.thumb.source == "embedded"
        # the other groups were fetched while the first was on screen: one fetch per group, not per file
        await wait_for(pilot, lambda: app.previews.stats.fetched >= 4)
        assert app.previews.stats.fetched == 4 and len(review.result.groups) == 4
        # moving to another group shows its picture from the cache, without reading the drive again
        before = app.previews.stats.bytes_read
        groups = review.query_one("#groups", DataTable)
        groups.focus()
        groups.move_cursor(row=2)
        await wait_for(pilot, lambda: pane._current is not None and pane._current.from_cache)
        assert app.previews.stats.bytes_read == before
        app.previews.close()

    run_app(flow)


def test_cloud_only_files_wait_until_asked(photos: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        await add_locations(app, pilot, [photos / "a", photos / "b"])
        await pilot.click("#start")
        await wait_for(pilot, lambda: isinstance(app.screen, ReviewScreen))
        review: ReviewScreen = app.screen
        # mark every copy as a cloud-only placeholder, then look at a group that has not been fetched yet
        for e in review.result.inventory.entries:
            e.offline = True
        await pilot.pause(0.5)  # let the prefetch of the first run settle, then forget its results
        app.previews.clear()
        fetched_before = app.previews.stats.fetched
        pane = review.query_one("#preview", PreviewPane)
        items = preview_items(review.groups_by_id[review.result.groups[0].id].members, review.result.inventory.roots)
        assert all(i.offline for i in items)
        pane.show_group(items)
        await pilot.pause(0.3)
        assert pane._current is None
        assert "Press F" in pane.message and app.previews.stats.fetched == fetched_before  # nothing was downloaded
        await pilot.press("f")  # the user asks for it
        await wait_for(pilot, lambda: pane._current is not None)
        app.previews.close()

    run_app(flow)
