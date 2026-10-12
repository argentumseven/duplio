"""A console that cannot be crashed or hijacked by file names.

File names are untrusted input. They may contain bytes that are not valid
UTF-8 (which makes ``print`` raise), terminal escape sequences (which can
rewrite the screen, set the window title or hide text) and bidirectional
controls (which make ``photo‮txt.exe`` look like ``photoexe.txt``).
:class:`SafeConsole` makes every segment of text safe at the single point
where Rich turns it into output, so no renderer can forget to.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator

from rich.console import Console
from rich.segment import Segment

from duplio.util.text import _UNSAFE_KEEP_NEWLINE, printable


class SafeConsole(Console):
    def _render_buffer(self, buffer: Iterable[Segment]) -> str:
        return super()._render_buffer(_sanitise(buffer))


def _sanitise(buffer: Iterable[Segment]) -> Iterator[Segment]:
    for segment in buffer:
        text, style, control = segment
        if control or not _UNSAFE_KEEP_NEWLINE.search(text):
            yield segment
        else:
            yield Segment(printable(text, keep_newlines=True), style, control)


def harden_streams() -> None:
    """Never crash on output: unencodable characters are written as escapes."""
    for stream in {sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__}:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # already closed or not reconfigurable
                pass
