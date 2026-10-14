"""Things a person does with one file on screen: open it, find it, copy its path."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING

from duplio.gui.model import Tile
from duplio.util.fmt import human_size
from duplio.util.opener import open_in_viewer
from duplio.vfs.local import LocalBackend

if TYPE_CHECKING:
    from duplio.gui.app import Context

DOWNLOAD_LIMIT = 400 * 1024 * 1024


def is_local(tile: Tile) -> bool:
    return isinstance(tile.root.backend, LocalBackend)


def open_tile(ctx: Context, tile: Tile) -> None:
    """Open the file in the person's own viewer. A file on a server or phone is fetched to a temporary place first."""
    if is_local(tile):
        if not open_in_viewer(tile.entry.path):
            ctx.show_error("This computer has no program set up to open that kind of file.")
        return
    if tile.online_only and not ctx.confirm("Open a file that is only in the cloud?", f"{tile.name} is only stored in the cloud.",
                                           ["Opening it downloads it first."], confirm="Download and open"):
        return
    if tile.size > DOWNLOAD_LIMIT:
        ctx.show_error(f"{tile.name} is {human_size(tile.size)}, which is too large to fetch just to look at.")
        return
    if tile.size > 20 * 1024 * 1024 and not ctx.confirm("Fetch this file?", f"{tile.name} is {human_size(tile.size)}.",
                                                       ["It is copied to a temporary place on this computer and opened."],
                                                       confirm="Fetch and open"):
        return

    def fetch(progress, cancel):  # type: ignore[no-untyped-def]
        folder = tempfile.mkdtemp(prefix="duplio-open-")
        target = os.path.join(folder, tile.name)
        progress.stage(f"Fetching {tile.name}", total_bytes=tile.size)
        with tile.root.backend.open_read(tile.entry.path) as src, open(target, "wb") as dst:
            while True:
                cancel.check()
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
                progress.advance(nbytes=len(block))
        return target

    def opened(path: str) -> None:
        if not open_in_viewer(path):
            ctx.show_error("This computer has no program set up to open that kind of file.")

    ctx.run_job("Fetching the file", fetch, opened)


def show_in_folder(ctx: Context, tile: Tile) -> None:
    if not is_local(tile):
        ctx.info("Where is it?", tile.location)
        return
    path = tile.entry.path
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            open_in_viewer(os.path.dirname(path))
    except OSError:
        open_in_viewer(os.path.dirname(path))


def is_favorite(ctx: Context, tile: Tile) -> bool:
    return tile.uri in ctx.favorites


def toggle_favorite(ctx: Context, tile: Tile) -> bool:
    """Star the picture if it is not starred, unstar it if it is. Returns whether it is starred now. Nothing is done to the file."""
    starred = ctx.favorites.toggle(tile.uri, tile.name, tile.entry.size, tile.entry.mtime_ns)
    if ctx.favorites.problem:
        ctx.show_error(ctx.favorites.problem, "Favorites")
    return starred


def copy_path(ctx: Context, tile: Tile) -> None:
    ctx.window.clipboard_clear()
    ctx.window.clipboard_append(tile.location)


def context_menu(ctx: Context, tile: Tile, event_x: int, event_y: int, extra: list[tuple[str, Callable[[], None]]] | None = None,
                 fetch_picture: Callable[[], None] | None = None, on_star: Callable[[], None] | None = None) -> None:
    import tkinter as tk

    menu = tk.Menu(ctx.window, tearoff=0)
    menu.add_command(label="Open", command=lambda: open_tile(ctx, tile))
    if tile.kind in ("picture", "video"):
        menu.add_command(label="☆ Remove from favorites" if is_favorite(ctx, tile) else "★ Add to favorites",
                         command=lambda: (toggle_favorite(ctx, tile), on_star() if on_star else None))
    if is_local(tile):
        menu.add_command(label="Show in its folder", command=lambda: show_in_folder(ctx, tile))
    menu.add_command(label="Copy its address", command=lambda: copy_path(ctx, tile))
    if fetch_picture is not None:
        menu.add_command(label="Show the picture (may download it)", command=fetch_picture)
    if extra:
        menu.add_separator()
        for label, command in extra:
            menu.add_command(label=label, command=command)
    try:
        menu.tk_popup(event_x, event_y)
    finally:
        menu.grab_release()


def cleanup_temp() -> None:
    root = tempfile.gettempdir()
    for name in os.listdir(root):
        if name.startswith("duplio-open-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
