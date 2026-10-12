"""The launchers start Duplio from the folder they sit in, on a computer that has only Python."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "duplio.sh"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="shell launcher")


def launch(script: Path, *args: str, env: dict[str, str] | None = None, stdin: int | None = subprocess.DEVNULL) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DUPLIO_PYTHON")}
    full.update(env or {})
    return subprocess.run(["sh", str(script), *args], capture_output=True, text=True, env=full, stdin=stdin, timeout=120, check=False)


@pytest.fixture
def bare_python(tmp_path: Path) -> Path:
    """A Python with no installed packages at all: only what the launcher puts on its path can be imported."""
    wrapper = tmp_path / "bare-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


# --------------------------------------------------------------------------- the shell launcher
@posix_only
def test_the_launcher_starts_duplio_from_its_own_folder(tmp_path: Path) -> None:
    result = launch(SH, "--version")
    assert result.returncode == 0 and result.stdout.startswith("duplio, version")


@posix_only
def test_nothing_needs_to_be_installed(bare_python: Path, tmp_path: Path) -> None:
    """With a Python that has no packages, the bundled libraries in vendor/ are enough for the whole command line."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.txt").write_text("same" * 100)
    (tmp_path / "a" / "two.txt").write_text("same" * 100)
    env = {"DUPLIO_PYTHON": str(bare_python)}
    version = launch(SH, "--version", env=env)
    assert version.returncode == 0, version.stderr
    doctor = launch(SH, "doctor", "--no-benchmark", env=env)
    assert doctor.returncode == 0 and "Python" in doctor.stdout, doctor.stderr
    scan = launch(SH, "scan", str(tmp_path / "a"), "--json", env=env)
    assert scan.returncode == 0, scan.stderr
    assert '"groups"' in scan.stdout or '"wasted' in scan.stdout


@posix_only
def test_the_interface_libraries_are_bundled_too(bare_python: Path) -> None:
    code = "import duplio.tui.app, textual, rich, click; print('ok')"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{ROOT / 'vendor'}"
    result = subprocess.run([str(bare_python), "-c", code], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


@posix_only
def test_a_folder_name_with_spaces_and_a_link_to_the_launcher_both_work(tmp_path: Path) -> None:
    folder = tmp_path / "My Duplio Folder"
    folder.mkdir()
    shutil.copy(SH, folder / "duplio.sh")
    (folder / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (folder / "vendor").symlink_to(ROOT / "vendor", target_is_directory=True)
    direct = launch(folder / "duplio.sh", "--version")
    assert direct.returncode == 0 and "duplio, version" in direct.stdout, direct.stderr
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "duplio").symlink_to(folder / "duplio.sh")
    (bin_dir / "again").symlink_to("duplio")  # a link to a link
    for name in ("duplio", "again"):
        linked = launch(bin_dir / name, "--version")
        assert linked.returncode == 0 and "duplio, version" in linked.stdout, linked.stderr


@posix_only
def test_a_launcher_without_its_files_explains_the_zip_problem(tmp_path: Path) -> None:
    shutil.copy(SH, tmp_path / "duplio.sh")
    result = launch(tmp_path / "duplio.sh", "--version")
    assert result.returncode == 1 and "extract the whole ZIP" in result.stderr


@posix_only
def test_arguments_and_exit_status_are_passed_through(tmp_path: Path) -> None:
    result = launch(SH, "no-such-command")
    assert result.returncode == 2 and "No such command" in result.stderr


@posix_only
def test_no_arguments_and_no_terminal_prints_the_help(tmp_path: Path) -> None:
    result = launch(SH)
    assert result.returncode == 0 and "Quick start" in result.stdout and "duplio" in result.stdout


@posix_only
def test_a_python_that_is_too_old_is_explained_not_used(tmp_path: Path) -> None:
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "src").symlink_to(ROOT / "src", target_is_directory=True)
    text = SH.read_text()
    search = 'for dir in /opt/homebrew/bin /usr/local/bin /Library/Frameworks/Python.framework/Versions/Current/bin "$HOME/.local/bin" /usr/bin; do'
    assert search in text, "the launcher's fallback search changed; update this test"
    (folder / "duplio.sh").write_text(text.replace(search, "for dir in /nonexistent; do"))
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "readlink", "uname", "cat"):
        (tools / name).symlink_to(shutil.which(name) or f"/usr/bin/{name}")
    old = tools / "python3"
    old.write_text("#!/bin/sh\nexit 1\n")  # what `python3 -c 'sys.version_info >= (3, 11)'` gives on an old Python
    old.chmod(0o755)
    result = subprocess.run(["/bin/sh", str(folder / "duplio.sh"), "--version"], capture_output=True, text=True,
                            env={"PATH": str(tools)}, check=False)  # fmt: skip
    assert result.returncode == 1
    assert "Python 3.11 or newer" in result.stderr and "python.org" in result.stderr


@posix_only
def test_an_unusable_python_override_falls_back_to_a_good_one() -> None:
    result = launch(SH, "--version", env={"DUPLIO_PYTHON": "/bin/false"})
    assert result.returncode == 0 and "not Python 3.11 or newer" in result.stderr and "duplio, version" in result.stdout


# --------------------------------------------------------------------------- the files that cannot be run here
def test_the_windows_launcher_is_plain_ascii_with_windows_line_ends() -> None:
    data = (ROOT / "Duplio.bat").read_bytes()
    data.decode("ascii")  # no accents or smart quotes: they would break in a console using another code page
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")  # every line ends the Windows way
    text = data.decode("ascii")
    assert text.startswith("@echo off")
    # a "!" in a folder name or argument would be swallowed if delayed expansion were on for the whole run
    before_nopython, _, after = text.partition(":nopython")
    assert "DisableDelayedExpansion" in before_nopython and "EnableDelayedExpansion" not in before_nopython.replace("DisableDelayedExpansion", "")
    assert "EnableDelayedExpansion" in after
    for needle in ("py -%%V", "sys.version_info >= (3, 11)", '"%PYEXE%" %PYARGS% -m duplio %*', "PYTHONPATH", "winget"):
        assert needle in text, needle


def test_every_launcher_exists_and_the_shell_ones_can_be_run() -> None:
    for name in ("Duplio.bat", "Duplio.command", "duplio.sh", "START-HERE.txt"):
        assert (ROOT / name).is_file(), name
    if sys.platform != "win32":
        for name in ("Duplio.command", "duplio.sh"):
            assert os.access(ROOT / name, os.X_OK), f"{name} must be executable"
            assert (ROOT / name).read_text().startswith("#!/bin/sh")
    start = (ROOT / "START-HERE.txt").read_text()
    for name in ("Duplio.bat", "Duplio.command", "duplio.sh"):
        assert name in start


def test_line_endings_are_pinned_for_each_launcher() -> None:
    rules = (ROOT / ".gitattributes").read_text()
    assert "*.bat" in rules and "*.sh" in rules and "*.command" in rules


def test_the_bundled_libraries_are_complete_and_licensed() -> None:
    vendor = ROOT / "vendor"
    packages = {p.name.split("-")[0].lower().replace("_", "-") for p in vendor.glob("*.dist-info")}
    assert {"click", "rich", "textual", "pygments", "markdown-it-py", "platformdirs", "typing-extensions"} <= packages
    for info in vendor.glob("*.dist-info"):
        assert (info / "licenses").is_dir() or (info / "LICENSE").is_file(), f"{info.name} has no licence file"
    compiled = [p for p in vendor.rglob("*") if p.suffix in (".so", ".pyd", ".dll", ".dylib")]
    assert not compiled, compiled  # compiled code would work on one kind of computer only
    assert (vendor / "README.md").is_file()


@posix_only
def test_the_launcher_prefers_a_python_that_can_open_the_window(tmp_path: Path) -> None:
    """A newer Python without Tk loses to an older one that has it; with no Tk anywhere, any new-enough Python still runs the commands."""
    folder = tmp_path / "app"
    folder.mkdir()
    for name in ("src", "vendor"):
        (folder / name).symlink_to(ROOT / name, target_is_directory=True)
    text = SH.read_text()
    search = 'for dir in /opt/homebrew/bin /usr/local/bin /Library/Frameworks/Python.framework/Versions/Current/bin "$HOME/.local/bin" /usr/bin; do'
    assert search in text, "the launcher's fallback search changed; update this test"
    (folder / "duplio.sh").write_text(text.replace(search, "for dir in /nonexistent; do"))
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "readlink", "uname", "cat"):
        (tools / name).symlink_to(shutil.which(name) or f"/usr/bin/{name}")
    marker = tmp_path / "which"

    def shim(name: str, has_tk: bool) -> None:
        path = tools / name
        path.write_text(f'#!/bin/sh\ncase "$*" in *"import tkinter"*) exit {0 if has_tk else 1};; esac\n'
                        f'case "$1" in -m) echo {name} > "{marker}";; esac\nexec "{sys.executable}" -S "$@"\n')  # -S: only what the launcher provides
        path.chmod(0o755)

    def run() -> str:
        marker.unlink(missing_ok=True)
        result = subprocess.run(["/bin/sh", str(folder / "duplio.sh"), "--version"], capture_output=True, text=True,
                                env={"PATH": str(tools)}, check=False)  # fmt: skip
        assert result.returncode == 0 and "duplio, version" in result.stdout, result.stderr
        return marker.read_text().strip()

    shim("python3.14", has_tk=False)
    shim("python3.12", has_tk=True)
    assert run() == "python3.12"  # the newest Python has no Tk, the older one does
    shim("python3.12", has_tk=False)
    assert run() == "python3.14"  # no Tk anywhere: the newest one still runs the text commands
