"""Optional programs that make pictures better: Pillow (a Python library) and ffmpeg (a separate program).

Neither is needed (Duplio reads the common picture formats itself), and neither is installed without being asked: the
Settings page says what is missing and offers a button, and the button says what it will run before it runs it.
"""

from __future__ import annotations

import contextlib
import importlib
import site
import subprocess
import sys
from collections.abc import Callable

from duplio.preview import ffmpeg
from duplio.preview.service import pillow_available

PIP_COMMAND = ("-m", "pip", "install", "--disable-pip-version-check", "Pillow>=10.0")


def pillow_status() -> str:
    """One sentence about whether Pillow is there."""
    if pillow_available():
        try:
            import PIL

            return f"Pillow {PIL.__version__} is installed: pictures are made quickly, in many formats."
        except Exception:
            return "Pillow is installed."
    return ("Pillow is not installed. Duplio makes pictures with its own, slower code (JPEG, PNG, GIF, BMP and a few more); "
            "WebP, HEIC and similar formats cannot be shown without it or ffmpeg.")


def ffmpeg_status(configured: str = "") -> str:
    found = ffmpeg.find_ffmpeg(configured)
    if found is None:
        return ("ffmpeg was not found. Without it a movie shows a picture only if it has a cover inside it. "
                "Install ffmpeg (winget install ffmpeg · brew install ffmpeg · apt install ffmpeg) and press Look again.")
    name = ffmpeg.version(found)
    return f"ffmpeg is installed ({found}" + (f", {name.split(' Copyright')[0]}" if name else "") + "): movies and unusual pictures can be shown."


def pip_command_text() -> str:
    return "python " + " ".join(PIP_COMMAND)


def install_pillow(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> tuple[bool, str]:
    """Install Pillow with pip, for this person only (``--user``) when the Python allows it. ``(worked, message)``."""
    attempts = [[sys.executable, *PIP_COMMAND, "--user"], [sys.executable, *PIP_COMMAND]]
    last = ""
    for command in attempts:
        try:
            done = run(command, capture_output=True, text=True, timeout=600, check=False)
        except FileNotFoundError:
            return False, "Python's package installer (pip) was not found, so Pillow could not be installed from here."
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Pillow could not be installed: {exc}"
        output = (done.stdout or "") + (done.stderr or "")
        if done.returncode == 0:
            _make_importable()
            if pillow_available():
                return True, "Pillow is installed. Pictures will use it from now on."
            return True, "Pillow was installed. Close and reopen Duplio to start using it."
        last = next((line for line in reversed(output.strip().splitlines()) if line.strip()), "no details")
        if "No module named pip" in output:
            return False, "Python's package installer (pip) is not available in this Python, so Pillow could not be installed from here."
    return False, f"Pillow could not be installed ({last}). You can install it yourself with:  {pip_command_text()}"


def _make_importable() -> None:
    """A package installed while the program runs is not on the import path yet: tell Python where to look."""
    importlib.invalidate_caches()
    with contextlib.suppress(Exception):
        user_site = site.getusersitepackages()
        if user_site and user_site not in sys.path:
            site.addsitedir(user_site)
        importlib.invalidate_caches()
