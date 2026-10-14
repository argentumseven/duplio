"""Dialog windows: progress, passwords, confirmations, the folder browser and the "connect to a server" form."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.backup.album import AlbumOptions, album_name
from duplio.errors import ConfigError
from duplio.gui.jobs import Job
from duplio.gui.model import build_server_address
from duplio.gui.places import Place
from duplio.gui.style import PALETTE, shorten
from duplio.gui.widgets import Btn, autowrap, center_on
from duplio.util.fmt import human_duration, human_size

if TYPE_CHECKING:
    from duplio.gui.app import Context


def _modal(win: tk.Toplevel, master: tk.Misc, on_close: Callable[[], None]) -> None:
    """Make ``win`` a dialog: on top of the window and taking the keyboard.

    The title-bar close button always does something (``on_close``), so no dialog can ever be left that cannot be dismissed.
    """
    win.transient(master.winfo_toplevel())  # type: ignore[arg-type]
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.update_idletasks()
    win.lift()
    try:
        win.grab_set()
    except tk.TclError:
        pass  # the window is not visible yet on some systems; the dialog still works, just not exclusively
    win.focus_force()


def _dismiss(win: tk.Toplevel) -> None:
    """Close a dialog; safe to call twice and from any of its buttons."""
    try:
        win.grab_release()
    except tk.TclError:
        pass
    try:
        win.destroy()
    except tk.TclError:
        pass


class ProgressDialog(tk.Toplevel):
    """Shows what a background job is doing and lets the person stop it."""

    def __init__(self, ctx: Context, job: Job, on_done: Callable[[Any], None], on_error: Callable[[BaseException], None],
                 on_cancel: Callable[[], None] | None = None) -> None:
        super().__init__(ctx.window)
        self.ctx, self.job = ctx, job
        self._on_done, self._on_error, self._on_cancel = on_done, on_error, on_cancel
        self.title(job.title)
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        body = tk.Frame(self, bg=PALETTE["panel"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=job.title, font=f.heading, bg=PALETTE["panel"], fg=PALETTE["text"], anchor="w").pack(fill="x")
        self.stage = tk.Label(body, text="Starting…", font=f.base, bg=PALETTE["panel"], fg=PALETTE["text"], anchor="w")
        self.stage.pack(fill="x", pady=(14, 6))
        self.bar = ttk.Progressbar(body, mode="indeterminate", length=520)
        self.bar.pack(fill="x")
        self.counts = tk.Label(body, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w")
        self.counts.pack(fill="x", pady=(6, 0))
        self.detail = tk.Label(body, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w")
        self.detail.pack(fill="x")
        row = tk.Frame(body, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(16, 0))
        self.cancel_button = Btn(row, "Stop", self._cancel, fonts=f)
        self.cancel_button.pack(side="right")
        self.note = tk.Label(row, text="You can stop at any time; nothing is left half-done.", font=f.small, bg=PALETTE["panel"],
                             fg=PALETTE["muted"])
        self.note.pack(side="left")
        center_on(self, ctx.window, 600, 230)
        _modal(self, ctx.window, self._cancel)  # closing the window stops the job; the dialog goes when the job has stopped
        self.bar.start(60)
        self._indeterminate = True
        self.after(100, self._poll)

    def _cancel(self) -> None:
        self.job.cancel.cancel()
        self.stage.configure(text="Stopping…")
        self.cancel_button.set_enabled(False)

    def _poll(self) -> None:
        snap = self.job.progress.snapshot()
        if not self.job.finished:
            self.stage.configure(text=snap.stage if not self.job.cancel.cancelled else "Stopping…")
            fraction = snap.fraction
            if fraction is None:
                if not self._indeterminate:
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(60)
                    self._indeterminate = True
            else:
                if self._indeterminate:
                    self.bar.stop()
                    self.bar.configure(mode="determinate", maximum=1000)
                    self._indeterminate = False
                self.bar.configure(value=int(fraction * 1000))
            parts = []
            if snap.total_items:
                parts.append(f"{snap.done_items:,} of {snap.total_items:,}")
            elif snap.done_items:
                parts.append(f"{snap.done_items:,} so far")
            if snap.done_bytes:
                parts.append(human_size(snap.done_bytes))
            parts.append(human_duration(snap.elapsed))
            self.counts.configure(text="  ·  ".join(parts))
            self.detail.configure(text=shorten(snap.detail, 540, self.ctx.fonts.small, middle=True))
            self.after(100, self._poll)
            return
        self.bar.stop()
        _dismiss(self)
        if self.job.cancelled:
            if self._on_cancel:
                self._on_cancel()
        elif self.job.error is not None:
            self._on_error(self.job.error)
        else:
            self._on_done(self.job.result)


class PasswordDialog(tk.Toplevel):
    def __init__(self, ctx: Context, server: str, username: str | None) -> None:
        super().__init__(ctx.window)
        self.result: str | None = None
        self.title("Password needed")
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        body = tk.Frame(self, bg=PALETTE["panel"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        who = f"{username} on {server}" if username else server
        tk.Label(body, text=f"Password for {who}", font=f.heading, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        tk.Label(body, text="It is used for this connection only and is never saved.", font=f.small, bg=PALETTE["panel"],
                 fg=PALETTE["muted"], anchor="w").pack(fill="x", pady=(4, 12))
        self.entry = ttk.Entry(body, show="•", width=40)
        self.entry.pack(fill="x")
        row = tk.Frame(body, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(16, 0))
        Btn(row, "Connect", self._ok, kind="primary", fonts=f).pack(side="right")
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right", padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        center_on(self, ctx.window, 480, 210)
        _modal(self, ctx.window, self._cancel)
        self.entry.focus_set()

    def _ok(self) -> None:
        self.result = self.entry.get()
        _dismiss(self)

    def _cancel(self) -> None:
        _dismiss(self)

    def show(self) -> str | None:
        self.wait_window()
        return self.result


class ConfirmDialog(tk.Toplevel):
    """Says exactly what is about to happen, lists it, and waits for a yes."""

    def __init__(self, ctx: Context, title: str, headline: str, details: Sequence[str] = (), rows: Sequence[tuple[str, str]] = (),
                 confirm: str = "Continue", danger: bool = False, columns: tuple[str, str] = ("File", "What happens")) -> None:
        super().__init__(ctx.window)
        self.result = False
        self.title(title)
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        body = tk.Frame(self, bg=PALETTE["panel"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=headline, font=f.heading, bg=PALETTE["panel"], anchor="w", justify="left", wraplength=620).pack(fill="x")
        for line in details:
            tk.Label(body, text=line, font=f.base, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w", justify="left",
                     wraplength=620).pack(fill="x", pady=(6, 0))
        if rows:
            frame = tk.Frame(body, bg=PALETTE["panel"])
            frame.pack(fill="both", expand=True, pady=(14, 0))
            tree = ttk.Treeview(frame, columns=("a", "b"), show="headings", height=min(len(rows), 10))
            tree.heading("a", text=columns[0])
            tree.heading("b", text=columns[1])
            tree.column("a", width=360)
            tree.column("b", width=260)
            for left, right in rows[:400]:
                tree.insert("", "end", values=(left, right))
            bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=bar.set)
            tree.pack(side="left", fill="both", expand=True)
            bar.pack(side="right", fill="y")
            if len(rows) > 400:
                tk.Label(body, text=f"…and {len(rows) - 400:,} more", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"],
                         anchor="w").pack(fill="x")
        row = tk.Frame(body, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(18, 0))
        Btn(row, confirm, self._yes, kind="danger" if danger else "primary", fonts=f).pack(side="right")
        Btn(row, "Cancel", self._no, fonts=f).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _e: self._no())
        center_on(self, ctx.window, 700, 260 + (230 if rows else 0) + 24 * len(details))
        _modal(self, ctx.window, self._no)

    def _yes(self) -> None:
        self.result = True
        _dismiss(self)

    def _no(self) -> None:
        _dismiss(self)

    def show(self) -> bool:
        self.wait_window()
        return self.result


class MessageDialog(tk.Toplevel):
    """A result or an error, with optional extra buttons."""

    def __init__(self, ctx: Context, title: str, headline: str, lines: Sequence[str] = (), *, kind: str = "info",
                 buttons: Sequence[tuple[str, Callable[[], None], str]] = ()) -> None:
        super().__init__(ctx.window)
        self.title(title)
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        body = tk.Frame(self, bg=PALETTE["panel"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        color = {"info": PALETTE["text"], "ok": PALETTE["ok"], "error": PALETTE["bad"], "warn": PALETTE["warn"]}[kind]
        tk.Label(body, text=headline, font=f.heading, bg=PALETTE["panel"], fg=color, anchor="w", justify="left",
                 wraplength=560).pack(fill="x")
        for line in lines:
            tk.Label(body, text=line, font=f.base, bg=PALETTE["panel"], fg=PALETTE["text"], anchor="w", justify="left",
                     wraplength=560).pack(fill="x", pady=(8, 0))
        row = tk.Frame(body, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(18, 0))
        Btn(row, "OK", self._close, kind="primary", fonts=f).pack(side="right")
        for text, command, style in buttons:
            Btn(row, text, self._then(command), kind=style, fonts=f).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _e: self._close())
        self.bind("<Return>", lambda _e: self._close())
        center_on(self, ctx.window, 620, 190 + 28 * len(lines))
        _modal(self, ctx.window, self._close)

    def _close(self) -> None:
        _dismiss(self)

    def _then(self, command: Callable[[], object]) -> Callable[[], None]:
        """A button action that closes this dialog first and then does ``command``."""

        def run() -> None:
            self._close()
            command()

        return run




# --------------------------------------------------------------------------- the folder browser
class FolderPicker(tk.Toplevel):
    """Walk through folders on this computer, a phone, or a server, and choose one."""

    def __init__(self, ctx: Context, start: str = "", title: str = "Choose a folder") -> None:
        super().__init__(ctx.window)
        self.ctx = ctx
        self.result: str | None = None
        self.here: Any = None
        self._request = 0
        self._places_version = -1
        self._inbox: queue.SimpleQueue[tuple[int, str, Any]] = queue.SimpleQueue()
        self.title(title)
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        outer = tk.Frame(self, bg=PALETTE["panel"], padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        # The buttons are packed first so that they stay on screen however small the window is made.
        row = tk.Frame(outer, bg=PALETTE["panel"])
        row.pack(side="bottom", fill="x", pady=(10, 0))
        Btn(row, "Use this folder", self._choose, kind="primary", fonts=f).pack(side="right")
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right", padx=(0, 8))
        self.status = tk.Label(outer, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w")
        self.status.pack(side="bottom", fill="x", pady=(8, 0))
        tk.Label(outer, text=title, font=f.heading, bg=PALETTE["panel"], fg=PALETTE["heading"], anchor="w").pack(fill="x")
        bar = tk.Frame(outer, bg=PALETTE["panel"])
        bar.pack(fill="x", pady=(10, 8))
        self.up_button = Btn(bar, "▲ Up", self.up, fonts=f, padx=10, pady=5)
        self.up_button.pack(side="left")
        self.where = ttk.Entry(bar, font=f.base)
        self.where.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.where.bind("<Return>", lambda _e: self.go(self.where.get()))
        middle = tk.Frame(outer, bg=PALETTE["panel"])
        middle.pack(fill="both", expand=True)
        places = tk.Frame(middle, bg=PALETTE["panel"])
        places.pack(side="left", fill="y", padx=(0, 12))
        self.places = tk.Listbox(places, width=30, height=14, activestyle="none", borderwidth=1, relief="solid", font=f.base,
                                 exportselection=False, highlightthickness=0, bg=PALETTE["field"], fg=PALETTE["text"])
        self.places.pack(fill="y", expand=True)
        self.places.bind("<<ListboxSelect>>", self._place_chosen)
        self._place_targets: list[str | None] = []
        listing = tk.Frame(middle, bg=PALETTE["panel"])
        listing.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("name",), show="tree", selectmode="browse", height=14)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-Button-1>", self._open_selected)
        self.tree.bind("<Return>", self._open_selected)
        self.bind("<Escape>", lambda _e: self._cancel())
        self._fill_places()
        center_on(self, ctx.window, 860, 560)
        _modal(self, ctx.window, self._cancel)
        ctx.places.refresh()
        self.go(start or os.path.expanduser("~"))
        self.after(80, self._drain)

    def _fill_places(self) -> None:
        """Folders, drives, phones and recent places, under small headings. The scan for phones runs in the background."""
        found = self.ctx.places.snapshot()
        self._places_version = self.ctx.places.version
        recent = [Place(shorten(r, 210, self.ctx.fonts.base, middle=True), r) for r in self.ctx.state.recent[:6]]
        groups = (("Folders", found.folders), ("Drives", found.drives),
                  ("Phones and devices", found.devices if found.scanned else (Place("looking…", ""),)), ("Recent", recent))
        self.places.delete(0, "end")
        self._place_targets = []
        for heading, items in groups:
            if not items:
                continue
            self.places.insert("end", heading)
            self.places.itemconfigure("end", foreground=PALETTE["heading"], selectforeground=PALETTE["heading"],
                                      selectbackground=PALETTE["field"])
            self._place_targets.append(None)
            for place in items:
                self.places.insert("end", "  " + place.label)
                self._place_targets.append(place.target or None)

    def _place_chosen(self, _e: tk.Event) -> None:
        sel = self.places.curselection()
        if sel and sel[0] < len(self._place_targets) and self._place_targets[sel[0]]:
            self.go(self._place_targets[sel[0]] or "")

    # ---- loading, off the window's thread ---------------------------------
    def go(self, text: str, refresh: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        self._request += 1
        request = self._request
        self.status.configure(text="Reading the folder…  (a network drive can take a moment)", fg=PALETTE["muted"])

        def work() -> None:
            try:
                loc = self.ctx.registry.open(text)
                info = loc.backend.stat(loc.path)
                if info is None:
                    raise FileNotFoundError(f"{loc.display} does not exist")
                if not info.is_dir:
                    raise NotADirectoryError(f"{loc.display} is not a folder")
                entries = self.ctx.listings.list(loc.backend, loc.path, refresh=refresh)
                self._inbox.put((request, "ok", (loc, entries)))
            except Exception as exc:
                self._inbox.put((request, "error", str(exc) or type(exc).__name__))

        threading.Thread(target=work, name="duplio-picker", daemon=True).start()

    def _drain(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                request, status, payload = self._inbox.get_nowait()
                if request != self._request:
                    continue
                if status == "error":
                    self.status.configure(text=f"Cannot open that: {payload}", fg=PALETTE["bad"])
                else:
                    self._show(*payload)
        except queue.Empty:
            pass
        if self.ctx.places.version != self._places_version:
            self._fill_places()
        self.after(80, self._drain)

    def _show(self, loc: Any, entries: list[tuple[str, bool]]) -> None:
        self.here = loc
        self.where.delete(0, "end")
        self.where.insert(0, loc.display)
        self.tree.delete(*self.tree.get_children())
        folders = sorted((n for n, is_dir in entries if is_dir), key=str.casefold)
        files = sum(1 for _n, is_dir in entries if not is_dir)
        for name in folders:
            self.tree.insert("", "end", iid=name, text="  ▸  " + name)
        self.status.configure(text=f"{len(folders)} folders, {files} files here  ·  {loc.caps.label}", fg=PALETTE["muted"])
        above = loc.backend.parent(loc.path)
        self.up_button.set_enabled(bool(above) and above != loc.path)

    def _open_selected(self, _e: tk.Event | None = None) -> None:
        sel = self.tree.selection()
        if sel and self.here is not None:
            child = self.here.backend.join(self.here.path, sel[0])
            self.go(self.here.backend.uri(child))

    def up(self) -> None:
        if self.here is not None:
            above = self.here.backend.parent(self.here.path)
            if above != self.here.path:
                self.go(self.here.backend.uri(above))

    def _choose(self) -> None:
        if self.here is None:
            return
        self.result = self.here.uri
        _dismiss(self)

    def _cancel(self) -> None:
        _dismiss(self)

    def show(self) -> str | None:
        self.wait_window()
        return self.result


# --------------------------------------------------------------------------- connect to a server or a phone
SERVER_KINDS = [
    ("SFTP (SSH) — most NAS boxes and Linux servers, encrypted", "sftp", 22, True),
    ("WebDAV, encrypted (https) — Nextcloud, ownCloud, Synology", "davs", 443, True),
    ("FTP over TLS (ftps) — encrypted", "ftps", 21, True),
    ("Windows or Samba share (SMB)", "smb", 445, True),
    ("WebDAV, NOT encrypted (http)", "dav", 80, False),
    ("FTP, NOT encrypted", "ftp", 21, False),
]


class ConnectDialog(tk.Toplevel):
    """Pick a phone or a drive, or fill in a server's details. Returns an address.

    Looking for phones can take a few seconds, so it happens on a worker thread; the dialog opens at once and can be closed
    at any moment (its close button, Esc and Cancel all work, while it is still looking too).
    """

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx.window)
        self.ctx = ctx
        self.result: str | None = None
        self._found: queue.SimpleQueue[list[Any]] = queue.SimpleQueue()
        self._looking = False
        self.title("Phone, drive or server")
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        outer = tk.Frame(self, bg=PALETTE["panel"], padx=20, pady=16)
        outer.pack(fill="both", expand=True)
        row = tk.Frame(outer, bg=PALETTE["panel"])  # packed first, so Cancel is always on screen
        row.pack(side="bottom", fill="x", pady=(14, 0))
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right")
        tk.Label(outer, text="Connect to a phone, drive or server", font=f.heading, bg=PALETTE["panel"], fg=PALETTE["heading"],
                 anchor="w").pack(fill="x")
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(12, 0))

        devices = tk.Frame(notebook, bg=PALETTE["panel"], padx=12, pady=12)
        notebook.add(devices, text="Phones and drives")
        buttons = tk.Frame(devices, bg=PALETTE["panel"])
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        Btn(buttons, "Use the selected one", self._use_device, kind="primary", fonts=f).pack(side="right")
        Btn(buttons, "Look again", self._look, fonts=f).pack(side="left")
        self.device_note = tk.Label(devices, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.device_note.pack(side="bottom", fill="x", pady=(8, 0))
        autowrap(self.device_note, margin=4, minimum=240)
        self.device_list = tk.Listbox(devices, height=9, activestyle="none", font=f.base, borderwidth=1, relief="solid",
                                      exportselection=False, bg=PALETTE["field"], fg=PALETTE["text"])
        self.device_list.pack(fill="both", expand=True)
        self._device_targets: list[tuple[str, bool]] = []
        self.device_list.bind("<Double-Button-1>", lambda _e: self._use_device())

        server = tk.Frame(notebook, bg=PALETTE["panel"], padx=12, pady=12)
        notebook.add(server, text="Server")
        self.kind = ttk.Combobox(server, state="readonly", values=[k[0] for k in SERVER_KINDS], width=60, font=f.base)
        self.kind.current(0)
        self.kind.bind("<<ComboboxSelected>>", self._kind_changed)
        self.fields: dict[str, ttk.Entry] = {}
        tk.Label(server, text="Type", font=f.bold, bg=PALETTE["panel"], anchor="w").grid(row=0, column=0, sticky="w")
        self.kind.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        for n, (key, label) in enumerate((("host", "Server name or address (for example nas.local)"), ("user", "User name"),
                                          ("port", "Port (leave empty unless you know it)"), ("folder", "Folder on the server (for example /photos)"))):
            tk.Label(server, text=label, font=f.bold, bg=PALETTE["panel"], anchor="w").grid(row=2 + n * 2, column=0, columnspan=2, sticky="w")
            entry = ttk.Entry(server, font=f.base)
            entry.grid(row=3 + n * 2, column=0, columnspan=2, sticky="we", pady=(2, 8))
            self.fields[key] = entry
        server.columnconfigure(0, weight=1)
        self.safety = tk.Label(server, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w", justify="left")
        self.safety.grid(row=10, column=0, columnspan=2, sticky="we")
        autowrap(self.safety, margin=4, minimum=240)
        tk.Label(server, text="The password is asked for when the connection is made, and is never saved.", font=f.small,
                 bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w").grid(row=11, column=0, columnspan=2, sticky="we", pady=(4, 0))
        Btn(server, "Use this server", self._use_server, kind="primary", fonts=f).grid(row=12, column=1, sticky="e", pady=(10, 0))
        self._kind_changed()

        self.bind("<Escape>", lambda _e: self._cancel())
        center_on(self, ctx.window, 680, 560)
        _modal(self, ctx.window, self._cancel)
        self._show_devices([])
        self._look()

    # ---- phones and drives -----------------------------------------------------
    def _look(self) -> None:
        """Start looking for phones and drives on a worker thread (if it is not already looking)."""
        if self._looking:
            return
        self._looking = True
        self.device_note.configure(text="Looking for phones and drives…  You can close this window at any time.")

        def work() -> None:
            from duplio.devices import discover

            try:
                found = list(discover())
            except Exception:
                found = []
            self._found.put(found)

        threading.Thread(target=work, name="duplio-devices", daemon=True).start()
        self.after(120, self._poll)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            found = self._found.get_nowait()
        except queue.Empty:
            self.after(120, self._poll)
            return
        self._looking = False
        self._show_devices(found)

    def _show_devices(self, found: list[Any]) -> None:
        from duplio.devices import platform_tip

        self.device_list.delete(0, "end")
        self._device_targets = []
        for place in self.ctx.places.snapshot().drives:
            self.device_list.insert("end", f"drive:  {place.label}")
            self._device_targets.append((place.target, True))
        for dev in found:
            self.device_list.insert("end", f"{dev.kind}:  {dev.name}" + ("" if dev.ready else "   (not ready)"))
            self._device_targets.append((dev.location, dev.ready))
        if self._looking or found:
            self.device_note.configure(text=platform_tip() if found else "Looking for phones and drives…")
        else:
            self.device_note.configure(text="No phone found. " + platform_tip() + " Then press Look again.")

    def _use_device(self) -> None:
        sel = self.device_list.curselection()
        if not sel or sel[0] >= len(self._device_targets):
            self.device_note.configure(text="Click one of the entries above first.", fg=PALETTE["bad"])
            return
        target, ready = self._device_targets[sel[0]]
        if not ready:
            self.device_note.configure(text="That one is not ready yet. Unlock the phone and choose 'File transfer', then press Look again.",
                                       fg=PALETTE["bad"])
            return
        self._finish(target)

    # ---- a server -------------------------------------------------------------------
    def _kind_changed(self, _e: Any = None) -> None:
        index = self.kind.current()
        _label, _scheme, _port, encrypted = SERVER_KINDS[index]
        self.safety.configure(
            text="Encrypted: your password and files are protected on the way." if encrypted
            else "NOT ENCRYPTED: the password and every file cross the network in the clear. Use this only on a network you trust.",
            fg=PALETTE["ok"] if encrypted else PALETTE["bad"])

    def _use_server(self) -> None:
        _label, scheme, _port, _enc = SERVER_KINDS[self.kind.current()]
        host = self.fields["host"].get()
        if not host.strip():
            self.safety.configure(text="Type the server's name or address first.", fg=PALETTE["bad"])
            return
        self._finish(build_server_address(scheme, host, self.fields["user"].get(), self.fields["port"].get(),
                                          self.fields["folder"].get()))

    def _finish(self, address: str) -> None:
        self.result = address
        _dismiss(self)

    def _cancel(self) -> None:
        _dismiss(self)

    def show(self) -> str | None:
        self.wait_window()
        return self.result


# --------------------------------------------------------------------------- make an album of the favorites
@dataclass
class AlbumChoice:
    destination: str  # the folder the album folder is made in
    options: AlbumOptions


class AlbumDialog(tk.Toplevel):
    """Name the album (your own words, a date and time, or both) and say where it goes."""

    PLACES = (("Name only", "none"), ("Date and time, then the name", "before"), ("The name, then the date and time", "after"))
    STYLES = (("Date  (2026-10-14)", "date"), ("Date and time  (2026-10-14 15-30)", "minutes"),
              ("Date, time and seconds  (2026-10-14 15-30-22)", "seconds"))

    def __init__(self, ctx: Context, count: int, *, now: Callable[[], Any] | None = None) -> None:
        super().__init__(ctx.window)
        self.ctx = ctx
        self.result: AlbumChoice | None = None
        self._now = now or datetime.now
        self.title("Make an album")
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        state = ctx.state
        outer = tk.Frame(self, bg=PALETTE["panel"], padx=22, pady=18)
        outer.pack(fill="both", expand=True)
        row = tk.Frame(outer, bg=PALETTE["panel"])  # packed first, so the buttons stay on screen
        row.pack(side="bottom", fill="x", pady=(16, 0))
        self.make_button = Btn(row, "Make the album", self._ok, kind="primary", fonts=f)
        self.make_button.pack(side="right")
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right", padx=(0, 8))
        tk.Label(outer, text="Make an album", font=f.heading, bg=PALETTE["panel"], fg=PALETTE["heading"], anchor="w").pack(fill="x")
        intro = tk.Label(outer, text=f"A new folder with copies of your {count:,} favorites. The originals are not touched, every copy is "
                         "checked, and History & undo can take the album away again.", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"],
                         anchor="w", justify="left")
        intro.pack(fill="x", pady=(4, 12))
        autowrap(intro, margin=4, minimum=300)

        tk.Label(outer, text="Name", font=f.bold, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        self.name = tk.StringVar(value="")
        entry = ttk.Entry(outer, textvariable=self.name, font=f.base)
        entry.pack(fill="x", pady=(2, 10))
        tk.Label(outer, text="Put the date and time in the name", font=f.bold, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        self.place = tk.StringVar(value=str(state.get("album_stamp", "none")))
        for label, value in self.PLACES:
            ttk.Radiobutton(outer, text=label, value=value, variable=self.place, command=self._update).pack(anchor="w", pady=1)
        style_row = tk.Frame(outer, bg=PALETTE["panel"])
        style_row.pack(fill="x", pady=(6, 0))
        tk.Label(style_row, text="Style", font=f.base, bg=PALETTE["panel"], anchor="w", width=8).pack(side="left")
        self.style_box = ttk.Combobox(style_row, state="readonly", values=[label for label, _v in self.STYLES], font=f.base, width=46)
        saved = str(state.get("album_format", "date"))
        self.style_box.current(next((i for i, (_l, v) in enumerate(self.STYLES) if v == saved), 0))
        self.style_box.pack(side="left")
        self.style_box.bind("<<ComboboxSelected>>", lambda _e: self._update())

        self.preview = tk.Label(outer, text="", font=f.bold, bg=PALETTE["panel"], fg=PALETTE["ok"], anchor="w", justify="left")
        self.preview.pack(fill="x", pady=(14, 2))
        autowrap(self.preview, margin=4, minimum=300)
        tk.Label(outer, text="Make it in this folder", font=f.bold, bg=PALETTE["panel"], anchor="w").pack(fill="x", pady=(8, 0))
        where = tk.Frame(outer, bg=PALETTE["panel"])
        where.pack(fill="x", pady=(2, 0))
        self.destination = tk.StringVar(value=str(state.get("album_dest") or os.path.join(os.path.expanduser("~"), "Pictures", "Duplio albums")))
        ttk.Entry(where, textvariable=self.destination, font=f.base).pack(side="left", fill="x", expand=True)
        Btn(where, "Choose folder…", self._choose, fonts=f, padx=10, pady=4).pack(side="left", padx=(8, 0))
        self.problem = tk.Label(outer, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["bad"], anchor="w", justify="left")
        self.problem.pack(fill="x", pady=(8, 0))
        autowrap(self.problem, margin=4, minimum=300)

        self.name.trace_add("write", lambda *_a: self._update())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._ok())
        self._update()
        center_on(self, ctx.window, 640, 560)
        _modal(self, ctx.window, self._cancel)
        entry.focus_set()

    # ---- what the choices make ---------------------------------------------------
    def options(self) -> AlbumOptions:
        style = self.STYLES[self.style_box.current()][1]
        return AlbumOptions(name=self.name.get(), stamp=self.place.get(), stamp_format=style)

    def folder_name(self) -> str | None:
        """The name the album folder will get now, or ``None`` while there is nothing to name it by."""
        try:
            return album_name(self.options(), self._now())
        except ConfigError:
            return None

    def _update(self) -> None:
        name = self.folder_name()
        if name is None:
            self.preview.configure(text="Type a name, or choose to use the date and time.", fg=PALETTE["muted"])
        else:
            self.preview.configure(text=f"The folder will be called:  {name}", fg=PALETTE["ok"])
        self.make_button.set_enabled(name is not None)
        self.style_box.configure(state="readonly" if self.place.get() != "none" else "disabled")

    def _choose(self) -> None:
        from duplio.gui.folders import choose_folder

        picked = choose_folder(self.ctx, self.destination.get())
        if picked:
            self.destination.set(picked)

    def _ok(self) -> None:
        if self.folder_name() is None:
            return
        where = self.destination.get().strip()
        if not where:
            self.problem.configure(text="Choose the folder the album should be made in.")
            return
        from duplio.vfs.location import location_kind

        if location_kind(where) == "local":
            path = os.path.abspath(os.path.expanduser(where))
            if not os.path.isdir(path):
                if not self.ctx.confirm("Make the folder?", f"{path} does not exist yet.", ["The album folder is made inside it."],
                                        confirm="Make the folder"):
                    return
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as exc:
                    self.problem.configure(text=f"That folder could not be made: {exc}")
                    return
            where = path
        state = self.ctx.state
        state.set("album_stamp", self.place.get())
        state.set("album_format", self.STYLES[self.style_box.current()][1])
        state.set("album_dest", where)
        state.save()
        self.result = AlbumChoice(where, self.options())
        _dismiss(self)

    def _cancel(self) -> None:
        _dismiss(self)

    def show(self) -> AlbumChoice | None:
        self.wait_window()
        return self.result
