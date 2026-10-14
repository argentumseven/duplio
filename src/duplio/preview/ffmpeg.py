"""ffmpeg, when it is installed: the one tool that makes a picture of almost any image format, and of a movie.

Duplio never needs it: JPEG, PNG, BMP, GIF and friends are read by Duplio's own code, and a photo's own small preview is
used when it has one. ffmpeg fills the gaps: movies (the first moments of the film), and the formats Duplio cannot read
itself (WebP, HEIC, AVIF, JPEG 2000, PSD, big progressive or odd JPEGs…). It runs as a separate program on one file at a
time, with a time limit, and can be stopped between a file and the next.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from duplio.errors import CancelledError

TIMEOUT = 40.0  # seconds one file may take

_COMMON = (
    "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg", "/snap/bin/ffmpeg",
    r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
)  # fmt: skip


class FfmpegError(Exception):
    """ffmpeg is missing, failed, or produced nothing."""


def find_ffmpeg(configured: str = "") -> str | None:
    """The ffmpeg program to use: the one named in Settings or ``DUPLIO_FFMPEG``, else the first found on the PATH or in a
    place installers use. ``None`` when there is none."""
    for candidate in (configured, os.environ.get("DUPLIO_FFMPEG", "")):
        if candidate and os.path.isfile(candidate):
            return candidate
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget puts links in a folder a double-clicked program may not have on its PATH
    local = os.environ.get("LOCALAPPDATA")
    extra = (str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"),) if local else ()
    return next((c for c in (*_COMMON, *extra) if os.path.isfile(c)), None)


def version(path: str) -> str:
    """The first line ffmpeg prints about itself, or an empty string if it will not run."""
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10, check=False, creationflags=_flags())
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.splitlines()[0] if out.stdout else ""


def _flags() -> int:
    """Process creation flags that stop a console window flashing up on Windows (0, which means none, elsewhere)."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def _scale(want: int) -> str:
    """Shrink to fit ``want`` pixels on the long side; never enlarge. ``-2`` keeps the other side even, as the encoder needs."""
    return f"scale=w='if(gt(iw,ih),min(iw,{want}),-2)':h='if(gt(iw,ih),-2,min(ih,{want}))'"


def _run(command: list[str], data: bytes | None, cancel: Callable[[], bool] | None, timeout: float) -> bytes:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=_flags())
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            try:
                out, err = proc.communicate(data if first else None, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                first = False
                if cancel is not None and cancel():
                    raise CancelledError("cancelled") from None
                if time.monotonic() > deadline:
                    raise FfmpegError("ffmpeg took too long over this file") from None
    except BaseException:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.communicate(timeout=5)
        raise
    if proc.returncode != 0 or not out:
        reason = err.decode("utf-8", "replace").strip().splitlines()
        raise FfmpegError(reason[-1] if reason else "ffmpeg could not read this file")
    return out


def _codec(fmt: str) -> list[str]:
    """JPEG is smaller; PNG is what Duplio can decode quickly when Pillow is absent."""
    return ["-c:v", "png"] if fmt == "png" else ["-c:v", "mjpeg", "-q:v", "4"]


def picture_from_bytes(ffmpeg: str, data: bytes, want: int, *, fmt: str = "jpg", autorotate: bool = True,
                       cancel: Callable[[], bool] | None = None, timeout: float = TIMEOUT) -> bytes:
    """A JPEG or PNG of at most ``want`` pixels of a picture in any format ffmpeg reads (the file's bytes are fed to it).

    ``autorotate=False`` leaves the picture as stored, for the caller that applies the EXIF orientation itself (ffmpeg 6 turns
    JPEGs upright, older versions do not, so relying on it would turn some twice and some never)."""
    command = [ffmpeg, "-nostdin", "-v", "error", *([] if autorotate else ["-noautorotate"]), "-i", "pipe:0", "-frames:v", "1",
               "-vf", _scale(want), "-f", "image2pipe", *_codec(fmt), "pipe:1"]
    return _run(command, data, cancel, timeout)


def frame_from_file(ffmpeg: str, path: str, want: int, *, fmt: str = "jpg", cancel: Callable[[], bool] | None = None,
                    timeout: float = TIMEOUT) -> bytes:
    """A picture of a movie's first moments (a second in, to get past a black start; the very first frame if it is shorter)."""
    last: Exception | None = None
    for seek in ("1", "0"):
        command = [ffmpeg, "-nostdin", "-v", "error", "-ss", seek, "-i", path, "-frames:v", "1", "-vf", _scale(want), "-f",
                   "image2pipe", *_codec(fmt), "pipe:1"]
        try:
            return _run(command, None, cancel, timeout)
        except FfmpegError as exc:
            last = exc
    raise FfmpegError(str(last) if last else "ffmpeg could not read this movie")
