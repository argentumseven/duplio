"""Dialog windows: progress, passwords, confirmations, the folder browser and the "connect to a server" form."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from duplio.gui.jobs import Job
from duplio.gui.model import build_server_address
from duplio.gui.style import PALETTE, shorten
from duplio.gui.widgets import Btn, center_on
from duplio.util.fmt import human_duration, human_size

if TYPE_CHECKING:
    from duplio.gui.app import Context


def _modal(win: tk.Toplevel, master: tk.Misc) -> None:
    win.transient(master.winfo_toplevel())  # type: ignore[arg-type]
    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.update_idletasks()
    try:
        win.grab_set()
    except tk.TclError:
        pass  # the window is not visible yet on some systems; the dialog still works, just not exclusively
    win.focus_set()


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
        _modal(self, ctx.window)
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
        self.grab_release()
        self.destroy()
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
        _modal(self, ctx.window)
        self.entry.focus_set()

    def _ok(self) -> None:
        self.result = self.entry.get()
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()

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
        _modal(self, ctx.window)

    def _yes(self) -> None:
        self.result = True
        self.grab_release()
        self.destroy()

    def _no(self) -> None:
        self.grab_release()
        self.destroy()

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
        _modal(self, ctx.window)

    def _close(self) -> None:
        self.grab_release()
        self.destroy()

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
        self._inbox: queue.SimpleQueue[tuple[int, str, Any]] = queue.SimpleQueue()
        self.title(title)
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        outer = tk.Frame(self, bg=PALETTE["panel"], padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text=title, font=f.heading, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        bar = tk.Frame(outer, bg=PALETTE["panel"])
        bar.pack(fill="x", pady=(10, 8))
        Btn(bar, "↑ Up", self.up, fonts=f, padx=10, pady=5).pack(side="left")
        self.where = ttk.Entry(bar)
        self.where.pack(side="left", fill="x", expand=True, padx=8)
        self.where.bind("<Return>", lambda _e: self.go(self.where.get()))
        Btn(bar, "Go", lambda: self.go(self.where.get()), fonts=f, padx=10, pady=5).pack(side="left")
        Btn(bar, "Refresh", lambda: self.go(self.where.get(), refresh=True), fonts=f, padx=10, pady=5).pack(side="left", padx=(8, 0))
        middle = tk.Frame(outer, bg=PALETTE["panel"])
        middle.pack(fill="both", expand=True)
        places = tk.Frame(middle, bg=PALETTE["panel"])
        places.pack(side="left", fill="y", padx=(0, 12))
        tk.Label(places, text="Places", font=f.bold, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        self.places = tk.Listbox(places, width=26, height=14, activestyle="none", borderwidth=1, relief="solid", font=f.base,
                                 exportselection=False, highlightthickness=0)
        self.places.pack(fill="y", expand=True)
        self.places.bind("<<ListboxSelect>>", self._place_chosen)
        self._place_targets: list[str] = []
        self._fill_places()
        listing = tk.Frame(middle, bg=PALETTE["panel"])
        listing.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("name",), show="tree", selectmode="browse", height=14)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-Button-1>", self._open_selected)
        self.tree.bind("<Return>", self._open_selected)
        self.status = tk.Label(outer, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w")
        self.status.pack(fill="x", pady=(8, 0))
        row = tk.Frame(outer, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(10, 0))
        Btn(row, "Use this folder", self._choose, kind="primary", fonts=f).pack(side="right")
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _e: self._cancel())
        center_on(self, ctx.window, 860, 560)
        _modal(self, ctx.window)
        self.go(start or os.path.expanduser("~"))
        self.after(80, self._drain)

    def _fill_places(self) -> None:
        from duplio.tui.widgets import location_suggestions

        self.places.delete(0, "end")
        self._place_targets = []
        try:
            entries = location_suggestions()
        except Exception:
            entries = []
        for label, target in entries:
            self.places.insert("end", label.replace("folder: ", ""))
            self._place_targets.append(target)
        for recent in self.ctx.state.recent[:6]:
            self.places.insert("end", "recent: " + shorten(recent, 190, self.ctx.fonts.base, middle=True))
            self._place_targets.append(recent)

    def _place_chosen(self, _e: tk.Event) -> None:
        sel = self.places.curselection()
        if sel:
            self.go(self._place_targets[sel[0]])

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
        if self.winfo_exists():
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
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()

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
    """Pick a phone, a drive or a recent place, or fill in a server's details. Returns an address."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx.window)
        self.ctx = ctx
        self.result: str | None = None
        self.title("Phone, drive or server")
        self.configure(bg=PALETTE["panel"])
        f = ctx.fonts
        outer = tk.Frame(self, bg=PALETTE["panel"], padx=20, pady=16)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Connect to a phone, drive or server", font=f.heading, bg=PALETTE["panel"], anchor="w").pack(fill="x")
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(12, 0))

        devices = tk.Frame(notebook, bg=PALETTE["panel"], padx=12, pady=12)
        notebook.add(devices, text="Phones and drives")
        tk.Label(devices, text="Plugged-in phones, cameras and drives that Duplio can see:", font=f.base, bg=PALETTE["panel"],
                 anchor="w").pack(fill="x")
        self.device_list = tk.Listbox(devices, height=9, activestyle="none", font=f.base, borderwidth=1, relief="solid",
                                      exportselection=False)
        self.device_list.pack(fill="both", expand=True, pady=(8, 8))
        self._device_targets: list[str] = []
        self.device_note = tk.Label(devices, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w",
                                    justify="left", wraplength=520)
        self.device_note.pack(fill="x")
        self.device_list.bind("<Double-Button-1>", lambda _e: self._use_device())
        Btn(devices, "Use the selected one", self._use_device, kind="primary", fonts=f).pack(anchor="e", pady=(8, 0))
        self._fill_devices()

        server = tk.Frame(notebook, bg=PALETTE["panel"], padx=12, pady=12)
        notebook.add(server, text="Server")
        self.kind = ttk.Combobox(server, state="readonly", values=[k[0] for k in SERVER_KINDS], width=60)
        self.kind.current(0)
        self.kind.bind("<<ComboboxSelected>>", self._kind_changed)
        self.fields: dict[str, ttk.Entry] = {}
        tk.Label(server, text="Type", font=f.bold, bg=PALETTE["panel"], anchor="w").grid(row=0, column=0, sticky="w")
        self.kind.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        for n, (key, label) in enumerate((("host", "Server name or address (for example nas.local)"), ("user", "User name"),
                                          ("port", "Port (leave empty unless you know it)"), ("folder", "Folder on the server (for example /photos)"))):
            tk.Label(server, text=label, font=f.bold, bg=PALETTE["panel"], anchor="w").grid(row=2 + n * 2, column=0, columnspan=2, sticky="w")
            entry = ttk.Entry(server)
            entry.grid(row=3 + n * 2, column=0, columnspan=2, sticky="we", pady=(2, 8))
            self.fields[key] = entry
        server.columnconfigure(0, weight=1)
        self.safety = tk.Label(server, text="", font=f.small, bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w", justify="left",
                               wraplength=520)
        self.safety.grid(row=10, column=0, columnspan=2, sticky="we")
        tk.Label(server, text="The password is asked for when the connection is made, and is never saved.", font=f.small,
                 bg=PALETTE["panel"], fg=PALETTE["muted"], anchor="w").grid(row=11, column=0, columnspan=2, sticky="we", pady=(4, 0))
        Btn(server, "Use this server", self._use_server, kind="primary", fonts=f).grid(row=12, column=1, sticky="e", pady=(10, 0))
        self._kind_changed()

        recent = tk.Frame(notebook, bg=PALETTE["panel"], padx=12, pady=12)
        notebook.add(recent, text="Recent")
        self.recent_list = tk.Listbox(recent, height=9, activestyle="none", font=f.base, borderwidth=1, relief="solid", exportselection=False)
        self.recent_list.pack(fill="both", expand=True, pady=(0, 8))
        for item in ctx.state.recent:
            self.recent_list.insert("end", item)
        self.recent_list.bind("<Double-Button-1>", lambda _e: self._use_recent())
        Btn(recent, "Use the selected one", self._use_recent, kind="primary", fonts=f).pack(anchor="e")

        row = tk.Frame(outer, bg=PALETTE["panel"])
        row.pack(fill="x", pady=(14, 0))
        Btn(row, "Cancel", self._cancel, fonts=f).pack(side="right")
        self.bind("<Escape>", lambda _e: self._cancel())
        center_on(self, ctx.window, 640, 560)
        _modal(self, ctx.window)

    def _fill_devices(self) -> None:
        from duplio.devices import discover, platform_tip

        try:
            found = discover()
        except Exception:
            found = []
        for dev in found:
            self.device_list.insert("end", f"{dev.kind}:  {dev.name}" + ("" if dev.ready else "   (not ready)"))
            self._device_targets.append(dev.location)
        if not found:
            self.device_list.insert("end", "(nothing found)")
        self.device_note.configure(text=platform_tip())

    def _kind_changed(self, _e: Any = None) -> None:
        index = self.kind.current()
        _label, _scheme, _port, encrypted = SERVER_KINDS[index]
        self.safety.configure(
            text="Encrypted: your password and files are protected on the way." if encrypted
            else "NOT ENCRYPTED: the password and every file cross the network in the clear. Use this only on a network you trust.",
            fg=PALETTE["ok"] if encrypted else PALETTE["bad"])

    def _use_device(self) -> None:
        sel = self.device_list.curselection()
        if sel and sel[0] < len(self._device_targets):
            self._finish(self._device_targets[sel[0]])

    def _use_recent(self) -> None:
        sel = self.recent_list.curselection()
        if sel:
            self._finish(self.recent_list.get(sel[0]))

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
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()

    def show(self) -> str | None:
        self.wait_window()
        return self.result
