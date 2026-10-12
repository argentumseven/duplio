"""Open a file in the operating system's default viewer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def open_in_viewer(path: str) -> bool:
    """Ask the system to open ``path``. Returns False if there is nothing to open it with."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        command = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(command) is None:
            return False
        subprocess.Popen([command, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
