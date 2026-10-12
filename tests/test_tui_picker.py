"""The folder browser in the terminal interface."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, OptionList, Static

from duplio.tui.picker import UP, FolderPicker

from .conftest import write
from .test_tui import run_app, wait_for


def names(picker: FolderPicker) -> list[str]:
    return [str(o.id) for o in picker.query_one("#fp-list", OptionList).options]


def status(picker: FolderPicker) -> str:
    return str(picker.query_one("#fp-status", Static).render())


def test_ctrl_o_on_a_folder_box_opens_the_browser_and_fills_the_box(tmp_path: Path) -> None:
    (tmp_path / "Photos" / "2024").mkdir(parents=True)
    (tmp_path / "Photos" / "2023").mkdir()
    write(tmp_path / "Photos" / "readme.txt", "hi")

    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        box = app.screen.query_one("#loc-input", Input)
        box.value = str(tmp_path)
        box.focus()
        await pilot.press("ctrl+o")
        await wait_for(pilot, lambda: isinstance(app.screen, FolderPicker))
        picker: FolderPicker = app.screen
        await wait_for(pilot, lambda: "Photos" in names(picker))
        assert names(picker)[0] == UP
        # go into Photos with the keyboard
        picker.query_one("#fp-list", OptionList).highlighted = names(picker).index("Photos")
        await pilot.press("enter")
        await wait_for(pilot, lambda: picker.here is not None and picker.here.path.endswith("Photos"))
        await wait_for(pilot, lambda: "2023" in names(picker))
        assert names(picker)[1:] == ["2023", "2024"]  # folders only, in order
        assert "2 folders, 1 files here" in status(picker)
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: not isinstance(app.screen, FolderPicker))
        assert app.screen.query_one("#loc-input", Input).value == str(tmp_path / "Photos")

    run_app(flow)


def test_up_cancel_and_a_wrong_address(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)

    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        box = app.screen.query_one("#loc-input", Input)
        box.value = str(tmp_path / "a" / "b")
        box.focus()
        await pilot.press("ctrl+o")
        await wait_for(pilot, lambda: isinstance(app.screen, FolderPicker) and app.screen.here is not None)
        picker: FolderPicker = app.screen
        await pilot.press("backspace")  # one folder up
        await wait_for(pilot, lambda: picker.here is not None and picker.here.path == str(tmp_path / "a"))
        where = picker.query_one("#fp-where", Input)
        where.value = str(tmp_path / "missing")
        where.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: "Cannot open that" in status(picker))
        assert picker.here is not None and picker.here.path == str(tmp_path / "a")  # still where it was
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, FolderPicker))
        assert app.screen.query_one("#loc-input", Input).value == str(tmp_path / "a" / "b")  # unchanged

    run_app(flow)


def test_ctrl_o_on_other_boxes_does_not_open_it(tmp_path: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        app.screen.query_one("#min-size", Input).focus()
        await pilot.press("ctrl+o")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, FolderPicker)

    run_app(flow)


def test_the_browse_button_opens_it(tmp_path: Path) -> None:
    async def flow(app, pilot):  # type: ignore[no-untyped-def]
        await pilot.press("1")
        await pilot.pause()
        await pilot.click("#browse")
        await wait_for(pilot, lambda: isinstance(app.screen, FolderPicker))
        await pilot.press("escape")

    run_app(flow)


def test_a_server_is_asked_once_while_you_walk_up_and_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pyftpdlib")
    from .remotekit import FtpServer

    monkeypatch.setenv("DUPLIO_FTP_PASSWORD", "secret")
    with FtpServer(tmp_path / "srv") as srv:
        (srv.root / "music" / "rock").mkdir(parents=True)
        (srv.root / "music" / "jazz").mkdir()
        (srv.root / "photos").mkdir()

        async def flow(app, pilot):  # type: ignore[no-untyped-def]
            await pilot.press("1")
            await pilot.pause()
            box = app.screen.query_one("#loc-input", Input)
            box.value = srv.url()
            box.focus()
            await pilot.press("ctrl+o")
            await wait_for(pilot, lambda: isinstance(app.screen, FolderPicker))
            picker: FolderPicker = app.screen
            await wait_for(pilot, lambda: "music" in names(picker))
            assert "FTP" in status(picker) and "remembered" in status(picker)
            for _ in range(3):  # down into music, back up, down again
                picker.query_one("#fp-list", OptionList).highlighted = names(picker).index("music")
                await pilot.press("enter")
                await wait_for(pilot, lambda: picker.here is not None and picker.here.path.endswith("music") and "jazz" in names(picker))
                await pilot.press("backspace")
                await wait_for(pilot, lambda: picker.here is not None and picker.here.path == "/" and "music" in names(picker))
            listings = srv.commands.count("MLSD")
            assert listings == 2  # "/" once and "/music" once, however many times we came back
            assert app.listings.hits >= 4
            await pilot.press("f5")  # asks the server again
            await wait_for(pilot, lambda: srv.commands.count("MLSD") == listings + 1)
            await pilot.press("escape")

        run_app(flow)
