"""The picture shown beside a file in the review screens."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence

from rich.console import Group
from rich.text import Text
from textual import work
from textual.widgets import Static

from duplio.events import CancelToken
from duplio.preview.service import (
    PICTURE_EXT,
    Preview,
    PreviewItem,
    PreviewService,
    Unavailable,
    extension,
)
from duplio.util.fmt import human_size
from duplio.util.opener import open_in_viewer

ROWS = 12  # text rows of picture; each row is two pixels tall


def preview_items(entries: Sequence, roots: Sequence) -> list[PreviewItem]:  # type: ignore[type-arg]
    """Preview items for scan entries (``FileEntry``) that are pictures."""
    out = []
    for e in entries:
        if extension(e.rel) in PICTURE_EXT:
            root = roots[e.root]
            out.append(PreviewItem(root.backend, e.path, e.size, e.mtime_ns, e.rel, getattr(e, "offline", False)))
    return out


class PreviewPane(Static):
    """Shows the picture for the file under the cursor, fetching it in the background."""

    DEFAULT_CSS = """
    PreviewPane { height: auto; min-height: 3; padding: 0 1; border-top: tall $panel; }
    """

    def __init__(self, service: PreviewService | None = None, **kwargs: object) -> None:
        super().__init__("", **kwargs)  # type: ignore[arg-type]
        self._service = service
        self._item: PreviewItem | None = None
        self._cancel = CancelToken()
        self._current: Preview | None = None
        self._note = ""
        self.message = ""  # the last explanation shown instead of a picture

    @property
    def service(self) -> PreviewService:
        if self._service is None:
            self._service = self.app.previews  # type: ignore[attr-defined]
        return self._service

    # ---- what to show --------------------------------------------------
    def show_group(self, items: Sequence[PreviewItem], note: str = "") -> None:
        """Show a picture for a set of identical files: the one cheapest to read."""
        if not items:
            self.clear()
            return
        self.show_item(self.service.pick_source(list(items)), note)

    def show_item(self, item: PreviewItem, note: str = "", *, force: bool = False) -> None:
        self._cancel.cancel("moved on")
        self._cancel = CancelToken()
        self._item, self._note = item, note
        self._current = None  # whatever was shown before is no longer this file's picture
        self.message = ""
        cached = self.service.peek(item)
        if cached is not None:
            self._show(cached)
            return
        if item.offline and not force and not self.service.settings.online_only:
            self.message = "Only stored in the cloud — showing it would download it. Press F to fetch the picture."
            self.update(self._message(self.message))
            return
        self.message = "Fetching the picture…"
        self.update(self._message(self.message))
        self._fetch(item, force, self._cancel)

    def clear(self) -> None:
        self._cancel.cancel("cleared")
        self._item = None
        self._current = None
        self.message = ""
        self.update("")

    def refetch(self) -> None:
        """Fetch what the cheap routes could not (reads the whole picture; downloads cloud-only files)."""
        if self._item is not None:
            self.show_item(self._item, self._note, force=True)

    def open_in_viewer(self) -> str:
        if self._current is None:
            return "There is no picture to open yet."
        suffix = "." + self._current.thumb.fmt
        handle, path = tempfile.mkstemp(prefix="duplio-preview-", suffix=suffix)
        with os.fdopen(handle, "wb") as out:
            out.write(self._current.thumb.data)
        return "Opened the preview in your image viewer." if open_in_viewer(path) else f"No image viewer found; saved at {path}"

    # ---- work ----------------------------------------------------------
    @work(thread=True, exclusive=True, group="preview")
    def _fetch(self, item: PreviewItem, force: bool, cancel: CancelToken) -> None:
        try:
            preview = self.service.get(item, cancel=cancel, allow_download=force, allow_online_only=force)
        except Unavailable as exc:
            hint = " Press F to fetch it." if exc.code in ("needs-download", "online-only") else ""
            self.app.call_from_thread(self._after, item, None, exc.reason + hint)
            return
        except Exception as exc:  # the drive may be unplugged, slow, or refuse; never break the screen
            if cancel.cancelled:
                return
            self.app.call_from_thread(self._after, item, None, f"Could not read the picture: {exc}")
            return
        self.app.call_from_thread(self._after, item, preview, "")

    def _after(self, item: PreviewItem, preview: Preview | None, problem: str) -> None:
        if item is not self._item:
            return  # the cursor has moved on
        if preview is None:
            self.message = problem
            self.update(self._message(problem))
        else:
            self._show(preview)

    def _show(self, preview: Preview) -> None:
        self._current = preview
        width = max(10, min((self.size.width or 60) - 2, 72))
        color = True
        picture = self.service.render(preview, cols=width, rows=ROWS, color=color)
        stats = self.service.stats
        where = "from the cache" if preview.from_cache else f"fetched {human_size(preview.bytes_read)} from the drive"
        info = Text(f"{where} · {preview.thumb.source} · {stats.hits} from cache, {stats.fetched} fetched, "
                    f"{human_size(stats.bytes_read)} read in total", style="dim")
        parts: list[object] = [picture, info]
        if self._note:
            parts.append(Text(self._note, style="dim"))
        self.update(Group(*parts))  # type: ignore[arg-type]

    @staticmethod
    def _message(text: str) -> Text:
        return Text(text, style="yellow")
