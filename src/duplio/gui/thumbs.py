"""Real thumbnails for the window: fetched cheaply in the background, upright, and sized for the tile.

``PreviewService`` already knows how to get a small picture of a file as cheaply as the drive allows (see
``duplio.preview``). This module turns that picture into pixels Tk can draw, on worker threads so the window never waits
for a drive, and hands the finished image back on the window's own thread (Tk is not thread-safe).

Requests are served newest first: when you scroll, the tiles on screen now matter more than the ones you passed.
"""

from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from duplio.events import CancelToken
from duplio.gui.memory import LOWEST_BUDGET, MIB, available_memory, default_image_budget
from duplio.gui.model import PICTURE, VIDEO, Tile
from duplio.preview import render
from duplio.preview.service import PreviewService, Unavailable
from duplio.preview.store import StoredThumb

# EXIF orientation -> the Pillow transpose that makes the picture upright
_ORIENT = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def make_ppm(thumb: StoredThumb, box: int) -> tuple[bytes, int, int]:
    """``(PPM image data, width, height)``: the thumbnail made upright and shrunk to fit a ``box`` x ``box`` square.

    PPM is a format Tk reads natively, so no image library is needed on the window's side. Pillow is used when installed
    (faster); otherwise the decoders in ``duplio.preview`` are.
    """
    try:
        from PIL import Image
    except ImportError:
        raster = render.decode_raster(thumb.data).oriented(thumb.orientation).fit(box, box)
        rgb, w, h = raster.rgb, raster.width, raster.height
    else:
        with Image.open(io.BytesIO(thumb.data)) as img:
            img.draft("RGB", (box * 2, box * 2))
            rgb_img = img.convert("RGB")
            op = _ORIENT.get(thumb.orientation)
            if op:
                rgb_img = rgb_img.transpose(getattr(Image.Transpose, op))
            rgb_img.thumbnail((box, box))
            w, h, rgb = rgb_img.width, rgb_img.height, rgb_img.tobytes()
    return b"P6\n%d %d\n255\n" % (w, h) + rgb, w, h


Ready = Callable[[str, Any, str], None]  # (the tile's picture key, tk.PhotoImage or None, why there is none)


@dataclass
class _Request:
    tile: Tile
    box: int
    ready: Ready
    wanted: Callable[[], bool] | None
    allow_download: bool | None
    allow_online_only: bool

    @property
    def cache_key(self) -> tuple[str, int]:
        return (self.tile.picture_key, self.box)


class Thumbnailer:
    """Asks the preview service for pictures on worker threads and delivers Tk images on the window's thread.

    Memory is bounded three ways. The memory budget is shared by two things: a quarter of it (at most 512 MB) for the
    finished images, which are what is drawn (the least recently shown go first), and the rest for the small compressed
    pictures the preview service holds in memory before they move on to the disk buffer. An image that goes is made again
    from those, never fetched from the drive again. If the computer runs short of memory the budget shrinks by itself and comes
    back when memory does. And the queues are bounded, so scrolling through a huge folder cannot pile up work faster than it
    can be done."""

    STACK_LIMIT = 6000  # requests waiting for a worker; the oldest (scrolled past long ago) are dropped beyond this
    FINISHED_LIMIT = 256  # pictures made but not yet turned into images; workers wait beyond this
    LOW_MEMORY = 400 * MIB  # shrink the image budget when the computer has less than this free ...
    MEMORY_OK = 800 * MIB  # ... and restore it when it has more than this

    def __init__(self, master: Any, previews: PreviewService, *, workers: int = 3, image_budget: int | None = None,
                 memory: Callable[[], int | None] = available_memory) -> None:
        self._master = master
        self._previews = previews
        self._cond = threading.Condition()
        self._stack: list[_Request] = []  # served newest first
        self._background: list[_Request] = []  # read-ahead: served when nothing is waiting on the stack, last first
        self._finished: list[tuple[_Request, tuple[bytes, int, int] | None, str, bool]] = []
        self._outstanding = 0  # requests made and not yet delivered or dropped (window thread only)
        self._finished_lock = threading.Lock()
        self._room = threading.Condition(self._finished_lock)
        self._images: OrderedDict[tuple[str, int], tuple[Any, int]] = OrderedDict()  # key -> (image, bytes it takes)
        self._image_bytes = 0
        self._reasons: dict[str, str] = {}
        self.base_budget = image_budget or default_image_budget()
        self._budget = self.base_budget
        self._image_budget = self._share(self._budget)
        self._memory = memory
        self._last_memory_check = 0.0
        self.low_memory = False
        self.dropped = 0  # requests thrown away because too many were waiting
        self._closing = False
        self._pump_scheduled = False
        self._workers = [threading.Thread(target=self._work, name=f"duplio-thumb-{n}", daemon=True) for n in range(workers)]
        self._apply_budget()
        for w in self._workers:
            w.start()

    # ---- the budget -----------------------------------------------------
    @property
    def budget(self) -> int:
        """The memory pictures may use altogether (images drawn and pictures held), as it stands now."""
        return self._budget

    @staticmethod
    def _share(budget: int) -> int:
        """The part of the memory budget for finished images: a quarter, at most 512 MB, never more than there is."""
        return min(budget, max(LOWEST_BUDGET, min(budget // 4, 512 * MIB)))

    def _apply_budget(self) -> None:
        """Divide the budget between the finished images and the pictures the preview service holds in memory."""
        self._image_budget = self._share(self._budget)
        setter = getattr(self._previews, "set_memory_limit", None)
        if setter is not None:
            setter(self._budget - self._image_budget)  # what no longer fits there moves on to the disk buffer
        self._evict()

    def set_budget(self, nbytes: int | None) -> None:
        """Choose how much memory pictures may use altogether (``None``: automatic)."""
        self.base_budget = nbytes or default_image_budget()
        self._budget = max(LOWEST_BUDGET, self.base_budget // 4) if self.low_memory else self.base_budget
        self._apply_budget()

    def stats(self) -> dict[str, Any]:
        held, held_bytes = self._previews.held() if hasattr(self._previews, "held") else (0, 0)
        return {"images": len(self._images), "image_bytes": self._image_bytes, "held": held, "held_bytes": held_bytes,
                "bytes": self._image_bytes + held_bytes, "budget": self._budget, "base_budget": self.base_budget,
                "low_memory": self.low_memory, "dropped": self.dropped}

    def check_memory(self, *, force: bool = False) -> bool:
        """Shrink the budget when the computer is short of memory, restore it when it is not. Returns ``low_memory``."""
        now = time.monotonic()
        if not force and now - self._last_memory_check < 3.0:
            return self.low_memory
        self._last_memory_check = now
        free = self._memory()
        if free is None:
            return self.low_memory
        if not self.low_memory and free < self.LOW_MEMORY:
            self.low_memory = True
            self._budget = max(LOWEST_BUDGET, self.base_budget // 4)
            self._apply_budget()
        elif self.low_memory and free > self.MEMORY_OK:
            self.low_memory = False
            self._budget = self.base_budget
            self._apply_budget()
        return self.low_memory

    def _evict(self) -> None:
        while self._image_bytes > self._image_budget and self._images:
            _key, (_image, cost) = self._images.popitem(last=False)
            self._image_bytes -= cost

    def clear_images(self) -> None:
        self._images.clear()
        self._image_bytes = 0

    def clear_memory(self) -> int:
        """Empty the memory: the finished images and the pictures held in memory (nothing moves to the disk buffer). Returns
        how many pictures were held. Pictures on screen are drawn again, from the disk buffer or the drive."""
        self.clear_images()
        clear = getattr(self._previews, "clear_memory", None)
        return int(clear()) if clear is not None else 0

    # ---- asking ---------------------------------------------------------
    def peek(self, tile: Tile, box: int) -> Any | None:
        """The finished image, if there is one (window thread only)."""
        found = self._images.get((tile.picture_key, box))
        if found is None:
            return None
        self._images.move_to_end((tile.picture_key, box))
        return found[0]

    def reason(self, tile: Tile) -> str:
        return self._reasons.get(tile.picture_key, "")

    def request(self, tile: Tile, box: int, ready: Ready, wanted: Callable[[], bool] | None = None, *,
                allow_download: bool | None = None, allow_online_only: bool = False, background: bool = False) -> None:
        """Deliver a picture of ``tile`` through ``ready`` (at once when it is already at hand). ``background`` marks
        read-ahead: it is served only when nothing that is on screen is waiting."""
        if tile.kind not in (PICTURE, VIDEO):
            ready(tile.picture_key, None, "")
            return
        image = self.peek(tile, box)
        if image is not None:
            ready(tile.picture_key, image, "")
            return
        known = self._reasons.get(tile.picture_key)
        if known is not None and not (allow_download or (allow_online_only and known == "online-only")):
            ready(tile.picture_key, None, known)  # asked before and could not be shown: do not ask the drive again
            return
        req = _Request(tile, box, ready, wanted, allow_download, allow_online_only)
        self._outstanding += 1
        with self._cond:
            queue = self._background if background else self._stack
            queue.append(req)
            while len(queue) > self.STACK_LIMIT:
                queue.pop(0)  # long out of sight by now
                self._outstanding -= 1
                self.dropped += 1
            self._cond.notify()
        self._ensure_pump()

    def forget_reason(self, tile: Tile) -> None:
        self._reasons.pop(tile.picture_key, None)

    # ---- the workers ------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._stack and not self._background and not self._closing:
                    self._cond.wait()
                if self._closing:
                    return
                req = self._stack.pop() if self._stack else self._background.pop()  # newest first; read-ahead only when idle
            if req.wanted is not None and not req.wanted():
                self._deliver(req, None, "", True)  # scrolled out of sight while waiting
                continue
            item = req.tile.preview_item
            ppm: tuple[bytes, int, int] | None = None
            reason = ""
            if item is None:
                reason = ""
            else:
                try:
                    preview = self._previews.get(item, cancel=CancelToken(), allow_download=req.allow_download,
                                                 allow_online_only=req.allow_online_only)
                    ppm = make_ppm(preview.thumb, req.box)
                except Unavailable as exc:
                    reason = exc.reason if exc.code != "online-only" else "online-only"
                except (render.CannotDecode, OSError, ValueError, KeyError, IndexError) as exc:
                    reason = f"the picture could not be read ({exc})"
                except Exception as exc:  # a broken file must never take a worker down
                    reason = f"the picture could not be shown ({type(exc).__name__})"
            self._deliver(req, ppm, reason, False)

    def _deliver(self, req: _Request, ppm: tuple[bytes, int, int] | None, reason: str, dropped: bool) -> None:
        """Hand a finished picture to the window's thread. A worker waits here while that thread is far behind, so the pile of
        finished pictures (each one is pixels) stays bounded."""
        with self._room:
            while len(self._finished) >= self.FINISHED_LIMIT and not self._closing:
                self._room.wait(0.05)
            self._finished.append((req, ppm, reason, dropped))

    # ---- on the window's thread (workers never touch Tk) ---------------------
    def _ensure_pump(self) -> None:
        if not self._pump_scheduled:
            self._pump_scheduled = True
            self._master.after(25, self.pump)

    def pump(self) -> None:
        self._pump_scheduled = False
        self.check_memory()
        with self._room:
            batch, self._finished = self._finished[:24], self._finished[24:]
            self._room.notify_all()
        for req, ppm, reason, dropped in batch:
            self._outstanding -= 1
            if dropped:
                continue
            image = None
            if ppm is not None:
                image = self._make_image(ppm[0])
                if image is not None:
                    cost = ppm[1] * ppm[2] * 4 + 512  # Tk keeps four bytes per pixel
                    old = self._images.pop(req.cache_key, None)
                    if old is not None:
                        self._image_bytes -= old[1]
                    self._images[req.cache_key] = (image, cost)
                    self._image_bytes += cost
                    self._evict()
                    self._reasons.pop(req.tile.picture_key, None)
            if image is None:
                self._reasons[req.tile.picture_key] = reason or "no picture"
            try:
                req.ready(req.tile.picture_key, image, "" if image is not None else self._reasons.get(req.tile.picture_key, ""))
            except Exception:
                pass
        if self._outstanding > 0:
            self._ensure_pump()

    def _make_image(self, ppm: bytes) -> Any | None:
        import tkinter

        try:
            return tkinter.PhotoImage(master=self._master, data=ppm, format="ppm")
        except tkinter.TclError:
            return None

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        with self._room:
            self._room.notify_all()
        self.clear_images()  # on the window's thread, while Tk is still there to free them
