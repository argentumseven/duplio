"""How the window is started: ``duplio gui``, a bare ``duplio``, and what happens on a computer that cannot show it."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from duplio import gui
from duplio.cli.main import cli


def test_tk_problem_names_the_missing_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        assert "python3-tk" in (gui.tk_problem() or "")
    else:
        assert "no DISPLAY" in (gui.tk_problem() or "")
        monkeypatch.setenv("DISPLAY", ":0")
        assert gui.tk_problem() is None


def test_gui_command_says_why_when_there_is_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui, "tk_problem", lambda: "There is no screen to open a window on (no DISPLAY).")
    result = CliRunner().invoke(cli, ["gui"])
    assert result.exit_code != 0
    assert "no screen" in result.output and "duplio ui" in result.output  # and points at the text screen


def test_gui_command_opens_the_window_when_it_can(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tkinter")
    opened: list[object] = []
    monkeypatch.setattr(gui, "tk_problem", lambda: None)
    monkeypatch.setattr("duplio.gui.app.run", lambda dirs=None: opened.append(dirs))
    result = CliRunner().invoke(cli, ["gui"])
    assert result.exit_code == 0, result.output
    assert len(opened) == 1


def test_a_bare_command_without_a_terminal_still_prints_the_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripts and pipes must never be handed a window."""
    monkeypatch.setattr(gui, "tk_problem", lambda: None)
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0 and "Quick start" in result.output


def test_the_command_list_shows_the_window_first() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.output.index("Open a screen") < result.output.index("Find & clean duplicates")
    assert "gui" in result.output
