"""Real thumbnails for the window: fetched cheaply in the background, upright, and sized for the tile.

``PreviewService`` already knows how to get a small picture of a file as cheaply as the drive allows (see
``duplio.preview``). This module turns that picture into pixels Tk can draw, on worker threads so the window never waits
for a drive, and hands the finished image back on the window's own thread (Tk is not thread-safe).

Requests are served newest first: when you scroll, the tiles on screen now matter more than the ones you passed.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from duplio.events import CancelToken
from duplio.gui.model import PICTURE, Tile
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
    allow_download: bool
    allow_online_only: bool

    @property
    def cache_key(self) -> tuple[str, int]:
        return (self.tile.picture_key, self.box)


class Thumbnailer:
    """Asks the preview service for pictures on worker threads and delivers Tk images on the window's thread."""

    def __init__(self, master: Any, previews: PreviewService, *, workers: int = 3, keep_images: int = 700) -> None:
        self._master = master
        self._previews = previews
        self._cond = threading.Condition()
        self._stack: list[_Request] = []
        self._finished: list[tuple[_Request, tuple[bytes, int, int] | None, str, bool]] = []
        self._outstanding = 0  # requests made and not yet delivered or dropped (window thread only)
        self._finished_lock = threading.Lock()
        self._images: OrderedDict[tuple[str, int], Any] = OrderedDict()
        self._reasons: dict[str, str] = {}
        self._keep = keep_images
        self._closing = False
        self._pump_scheduled = False
        self._workers = [threading.Thread(target=self._work, name=f"duplio-thumb-{n}", daemon=True) for n in range(workers)]
        for w in self._workers:
            w.start()

    # ---- asking ---------------------------------------------------------
    def peek(self, tile: Tile, box: int) -> Any | None:
        """The finished image, if there is one (window thread only)."""
        image = self._images.get((tile.picture_key, box))
        if image is not None:
            self._images.move_to_end((tile.picture_key, box))
        return image

    def reason(self, tile: Tile) -> str:
        return self._reasons.get(tile.picture_key, "")

    def request(self, tile: Tile, box: int, ready: Ready, wanted: Callable[[], bool] | None = None, *,
                allow_download: bool = False, allow_online_only: bool = False) -> None:
        """Deliver a picture of ``tile`` through ``ready`` (at once when it is already at hand)."""
        if tile.kind != PICTURE:
            ready(tile.picture_key, None, "")
            return
        image = self.peek(tile, box)
        if image is not None:
            ready(tile.picture_key, image, "")
            return
        if tile.picture_key in self._reasons and not (allow_download or allow_online_only):
            ready(tile.picture_key, None, self._reasons[tile.picture_key])
            return
        req = _Request(tile, box, ready, wanted, allow_download, allow_online_only)
        self._outstanding += 1
        with self._cond:
            self._stack.append(req)
            self._cond.notify()
        self._ensure_pump()

    def forget_reason(self, tile: Tile) -> None:
        self._reasons.pop(tile.picture_key, None)

    # ---- the workers ------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._stack and not self._closing:
                    self._cond.wait()
                if self._closing:
                    return
                req = self._stack.pop()  # newest first
            if req.wanted is not None and not req.wanted():
                with self._finished_lock:
                    self._finished.append((req, None, "", True))  # scrolled out of sight while waiting
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
            with self._finished_lock:
                self._finished.append((req, ppm, reason, False))

    # ---- on the window's thread (workers never touch Tk) ---------------------
    def _ensure_pump(self) -> None:
        if not self._pump_scheduled:
            self._pump_scheduled = True
            self._master.after(25, self.pump)

    def pump(self) -> None:
        self._pump_scheduled = False
        with self._finished_lock:
            batch, self._finished = self._finished[:24], self._finished[24:]
        for req, ppm, reason, dropped in batch:
            self._outstanding -= 1
            if dropped:
                continue
            image = None
            if ppm is not None:
                image = self._make_image(ppm[0])
                if image is not None:
                    self._images[req.cache_key] = image
                    while len(self._images) > self._keep:
                        self._images.popitem(last=False)
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
        self._images.clear()  # on the window's thread, while Tk is still there to free them
