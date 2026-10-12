"""Notifications, hooks around saved jobs, the new profile types, and watch mode."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli
from duplio.config import load_settings, save_settings, validate_profile
from duplio.errors import ConfigError
from duplio.events import CancelToken
from duplio.notify import (
    Notification,
    describe_target,
    send,
    should_notify,
    split_target,
    validate_target,
)
from duplio.scan.filters import ScanFilter
from duplio.vfs.location import BackendRegistry
from duplio.watch import tree_fingerprint, watch

from .conftest import write

T0 = 1_600_000_000.0


def invoke(*args: str, env: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "170", **(env or {})}).invoke(cli, ["--no-color", *args], catch_exceptions=False, **kw)


# ---------------------------------------------------------------- notifications
class _Hook(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        _Hook.received.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # silence the test server
        pass


@pytest.fixture
def webhook():  # type: ignore[no-untyped-def]
    _Hook.received = []
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/secret-token-123", _Hook.received
    server.shutdown()
    server.server_close()


def test_target_parsing_and_masking() -> None:
    assert split_target("https://x.example/hook") == ("webhook", "https://x.example/hook")
    assert split_target("email:me@example.com") == ("email", "me@example.com")
    for bad in ("ftp:whatever", "webhook:", "just text", "email"):
        with pytest.raises(ConfigError):
            split_target(bad)
    for bad in ("webhook:ftp://x", "email:not-an-address"):
        with pytest.raises(ConfigError):
            validate_target(bad)
    assert "secret-token" not in describe_target("webhook:https://hooks.example.com/secret-token-123")
    assert describe_target("webhook:https://hooks.example.com/secret-token-123") == "webhook hooks.example.com"
    assert describe_target("command:/usr/bin/notify --secret abc") == "command /usr/bin/notify"


def test_notification_policy() -> None:
    table = {
        "failure": {"ok": False, "problems": False, "failed": True, "cancelled": False, "skipped": False},
        "problems": {"ok": False, "problems": True, "failed": True, "cancelled": True, "skipped": False},
        "always": {"ok": True, "problems": True, "failed": True, "cancelled": True, "skipped": False},
        "never": {"ok": False, "problems": False, "failed": False, "cancelled": False, "skipped": False},
    }
    for policy, expected in table.items():
        assert {s: should_notify(policy, s) for s in expected} == expected, policy


def test_webhook_delivery(webhook) -> None:  # type: ignore[no-untyped-def]
    url, received = webhook
    results = send([f"webhook:{url}"], Notification("Backup", "failed", "disk full", {"files": 3}))
    assert results == [("webhook 127.0.0.1:" + url.split(":")[2].split("/")[0], None)]
    (payload,) = received
    assert payload["status"] == "failed" and payload["details"] == {"files": 3}
    assert payload["text"].startswith("[FAILED] Backup") and payload["content"] == payload["text"]


def test_failed_delivery_is_reported_not_raised() -> None:
    (label, error), = send(["webhook:http://127.0.0.1:9/never"], Notification("t", "ok", "m"))
    assert error is not None and "token" not in label


def test_command_target_gets_the_message_in_the_environment(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    script = f'printf "%s|%s|%s" "$DUPLIO_STATUS" "$DUPLIO_TITLE" "$DUPLIO_MESSAGE" > {out}'
    assert send([f"command:{script}"], Notification("My job", "problems", "3 files; quote\" and $(whoami)")) == [
        (describe_target(f"command:{script}"), None)]
    assert out.read_text() == 'problems|My job|3 files; quote" and $(whoami)'  # data, never shell code
    assert send(["command:exit 7"], Notification("t", "ok", "m"))[0][1] is not None


def test_email_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 0) -> None:
            self.host, self.port = host, port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def starttls(self) -> None:
            sent.append("starttls")

        def login(self, user: str, password: str) -> None:
            sent.append(("login", user, password))

        def send_message(self, message) -> None:  # type: ignore[no-untyped-def]
            sent.append(message)

    import duplio.notify as notify_mod

    monkeypatch.setattr(notify_mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("DUPLIO_SMTP_PASSWORD", "hunter2")
    smtp = {"host": "mail.example.com", "user": "bot", "sender": "duplio@example.com"}
    assert send(["email:me@example.com"], Notification("Nightly", "failed", "oops"), smtp=smtp)[0][1] is None
    assert sent[0] == "starttls" and sent[1] == ("login", "bot", "hunter2")
    message = sent[2]
    assert message["To"] == "me@example.com" and "failed" in message["Subject"] and "oops" in message.get_content()  # type: ignore[attr-defined]
    error = send(["email:me@example.com"], Notification("n", "ok", "m"), smtp=None)[0][1]
    assert error is not None and "SMTP" in error


def test_cli_notify(webhook) -> None:  # type: ignore[no-untyped-def]
    url, received = webhook
    result = invoke("notify", url, "--title", "T", "--status", "ok", "--message", "hello")
    assert result.exit_code == 0 and "sent" in result.output and received[0]["message"] == "hello"
    assert "secret-token" not in result.output
    assert invoke("notify", "http://127.0.0.1:9/x").exit_code == 1
    assert invoke("notify", "carrier-pigeon:me").exit_code == 2


# ---------------------------------------------------------------- profiles
def test_profile_validation() -> None:
    ok = validate_profile("p", {"type": "mirror", "source": "a", "dest": "b", "pre": ["mount /mnt/nas"],
                                "post": ["umount /mnt/nas"], "notify": ["email:me@example.com"],
                                "notify_on": "problems", "bwlimit": "5MB/s"})
    assert ok["bwlimit"] == "5MB/s"
    for bad in ({"notify": ["nope"]}, {"notify_on": "sometimes"}, {"bwlimit": "warp9"}, {"pre": "not a list"}):
        with pytest.raises(ConfigError):
            validate_profile("p", {"type": "mirror", "source": "a", "dest": "b", **bad})
    validate_profile("m", {"type": "merge", "source": "a", "dest": "b", "on_conflict": "newer", "move": True})
    validate_profile("s", {"type": "sync", "a": "x", "b": "y", "deletions": "restore"})
    validate_profile("n", {"type": "snapshot", "source": "a", "dest": "b", "keep_daily": 7})
    with pytest.raises(ConfigError):
        validate_profile("s", {"type": "sync", "a": "x"})
    with pytest.raises(ConfigError):
        validate_profile("m", {"type": "merge", "source": "a", "dest": "b", "on_conflict": "explode"})
    with pytest.raises(ConfigError):
        validate_profile("s", {"type": "sync", "a": "x", "b": "y", "deletions": "vanish"})


def test_smtp_settings_roundtrip(dirs) -> None:  # type: ignore[no-untyped-def]
    settings = load_settings(dirs)
    settings.notify_smtp = {"host": "mail.example.com", "port": 587, "user": "bot", "starttls": True}
    settings.profiles["x"] = validate_profile("x", {"type": "sync", "a": "a", "b": "b", "notify": ["email:me@example.com"]})
    save_settings(settings, dirs)
    again = load_settings(dirs)
    assert again.notify_smtp == settings.notify_smtp and again.profiles["x"]["notify"] == ["email:me@example.com"]


def add_profile(name: str, kind: str, *settings: str) -> None:
    args = ["profile", "add", name, "--type", kind]
    for item in settings:
        args += ["--set", item]
    result = invoke(*args)
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------- run: hooks and notifications
@pytest.fixture
def jobs(tmp_path: Path) -> dict[str, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", b"alpha", mtime=T0)
    dst.mkdir()
    return {"src": src, "dst": dst, "log": tmp_path / "log.txt"}


def test_run_orders_pre_job_post_and_passes_the_status(jobs: dict[str, Path]) -> None:
    log = jobs["log"]
    add_profile("nightly", "mirror", f"source={jobs['src']}", f"dest={jobs['dst']}",
                f"pre=echo pre >> {log}", f"post=echo post $DUPLIO_STATUS $DUPLIO_PROFILE >> {log}")
    result = invoke("run", "nightly", "--yes")
    assert result.exit_code == 0, result.output
    assert (jobs["dst"] / "a.txt").read_bytes() == b"alpha"
    assert log.read_text().splitlines() == ["pre", "post ok nightly"]


def test_failing_pre_command_stops_the_job_but_still_runs_post(jobs: dict[str, Path]) -> None:
    log = jobs["log"]
    add_profile("p", "mirror", f"source={jobs['src']}", f"dest={jobs['dst']}", "pre=exit 5",
                f"post=echo cleaned $DUPLIO_STATUS >> {log}", f"on_failure=echo failure-hook >> {log}")
    result = invoke("run", "p", "--yes")
    assert result.exit_code == 4 and not (jobs["dst"] / "a.txt").exists()
    assert log.read_text().splitlines() == ["failure-hook", "cleaned failed"]


def test_pre_command_can_make_the_drive_available(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", b"x", mtime=T0)
    dst.mkdir()
    add_profile("late", "mirror", f"source={tmp_path / 'phone'}", f"dest={dst}", f"pre=cp -r {src} {tmp_path / 'phone'}")
    result = invoke("run", "late", "--yes")  # the source only exists after the pre command ran
    assert result.exit_code == 0, result.output
    assert (dst / "a.txt").exists()


def test_unreachable_source_is_skipped_quietly(jobs: dict[str, Path]) -> None:
    log = jobs["log"]
    add_profile("away", "mirror", f"source={jobs['src'].parent / 'not-plugged-in'}", f"dest={jobs['dst']}",
                f"notify=command:echo notified >> {log}", "notify_on=problems", f"post=echo post >> {log}")
    result = invoke("run", "away", "--yes")
    assert result.exit_code == 3 and "Skipping" in result.output
    assert log.read_text().splitlines() == ["post"]  # no notification for a drive that is simply not connected


def test_notify_on_failure_only_by_default(jobs: dict[str, Path]) -> None:
    log = jobs["log"]
    cmd = f"notify=command:echo \"$DUPLIO_STATUS: $DUPLIO_MESSAGE\" >> {log}"
    add_profile("good", "mirror", f"source={jobs['src']}", f"dest={jobs['dst']}", cmd)
    assert invoke("run", "good", "--yes").exit_code == 0 and not log.exists()
    shutil_rm(jobs["src"])
    add_profile("bad", "backup", f"source={jobs['src']}", f"repo={jobs['dst'] / 'repo'}", cmd)
    # source is gone -> unreachable (skipped), so use a failing job instead
    add_profile("broken", "mirror", f"source={jobs['dst']}", f"dest={jobs['dst'] / 'inside'}", cmd)
    assert invoke("run", "broken", "--yes").exit_code == 2
    assert log.read_text().startswith("failed: The job 'broken' failed")


def shutil_rm(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def test_notify_always_and_dry_run(jobs: dict[str, Path]) -> None:
    log = jobs["log"]
    add_profile("chatty", "mirror", f"source={jobs['src']}", f"dest={jobs['dst']}",
                f"notify=command:echo \"$DUPLIO_STATUS\" >> {log}", "notify_on=always")
    invoke("run", "chatty", "--dry-run")
    assert not log.exists()  # a preview never notifies
    invoke("run", "chatty", "--yes")
    assert log.read_text().strip() == "ok"


def test_run_merge_sync_and_snapshot_profiles(tmp_path: Path) -> None:
    a, b, snaps = tmp_path / "A", tmp_path / "B", tmp_path / "snaps"
    write(a / "from-a.txt", b"A", mtime=T0)
    write(b / "from-b.txt", b"B", mtime=T0)
    add_profile("m", "merge", f"source={a}", f"dest={b}")
    assert invoke("run", "m", "--yes").exit_code == 0 and (b / "from-a.txt").exists()
    add_profile("s", "sync", f"a={a}", f"b={b}")
    assert invoke("run", "s", "--yes").exit_code == 0 and (a / "from-b.txt").exists()
    add_profile("snap", "snapshot", f"source={a}", f"dest={snaps}", "keep_last=1", "label=docs")
    assert invoke("run", "snap", "--yes").exit_code == 0
    assert invoke("run", "snap", "--yes").exit_code == 0
    kept = [p for p in snaps.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]
    assert len(kept) == 1  # retention applied after the second snapshot
    assert invoke("profile", "add", "x", "--type", "sync", "--set", "a=1", "--set", "b=2", "--set", "bogus=1").exit_code == 2


# ---------------------------------------------------------------- watch
def test_tree_fingerprint_tracks_changes(tmp_path: Path, registry: BackendRegistry) -> None:
    root = tmp_path / "w"
    write(root / "a.txt", b"1", mtime=T0)
    loc = registry.open(str(root))
    flt = ScanFilter.for_backup()
    first = tree_fingerprint(loc, flt)
    assert tree_fingerprint(loc, flt) == first
    write(root / "b.txt", b"22", mtime=T0)
    second = tree_fingerprint(loc, flt)
    assert second != first and second[0] == 2
    write(root / "b.txt", b"22", mtime=T0 + 5)  # only the time changed
    third = tree_fingerprint(loc, flt)
    assert third != second and third[:2] == second[:2]
    os.remove(root / "b.txt")
    assert tree_fingerprint(loc, flt) == first


class Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_watch_runs_once_per_settled_change() -> None:
    clock = Clock()
    state = {"value": 0, "runs": 0}
    script = iter([0, 0, 1, 2, 2, 2, 2, 2, 2])  # a change that is still moving, then quiet

    def fingerprint() -> int:
        state["value"] = next(script, state["value"])
        return int(state["value"])

    def action() -> bool:
        state["runs"] += 1
        return True

    stats = watch(fingerprint, action, interval=10, settle=3, run_first=False, max_runs=1, sleep=clock.sleep)
    assert stats.runs == 1 and state["runs"] == 1
    assert 3 in clock.slept  # waited for the folder to go quiet before acting


def test_watch_first_run_and_changes_made_by_the_action() -> None:
    clock = Clock()
    token = CancelToken()
    tree = {"v": 0}
    runs: list[int] = []

    def fingerprint() -> int:
        return tree["v"]

    def action() -> bool:
        runs.append(tree["v"])
        tree["v"] += 100  # the job itself changes what we watch (the other half of a sync)
        if len(runs) == 2:
            token.cancel()
        return True

    def external_change() -> None:
        pass

    # run_first: acts immediately; its own changes must not trigger another run
    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.slept) == 4:
            tree["v"] = 1  # someone edits a file
    stats = watch(fingerprint, action, interval=5, settle=1, run_first=True, cancel=token, sleep=sleep,
                  on_change=external_change)
    assert stats.runs == 2 and runs[0] == 0


def test_watch_counts_failures_and_continues() -> None:
    values = iter(range(1, 100))
    stats = watch(lambda: next(values) // 3, lambda: False, interval=1, settle=0.1, run_first=False, max_runs=3,
                  sleep=lambda s: None)
    assert stats.runs == 3 and stats.failures == 3


def test_cli_watch_mirrors_a_change(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", b"alpha", mtime=T0)
    dst.mkdir()
    result = invoke("watch", str(src), str(dst), "--interval", "1s", "--settle", "1s", "--max-runs", "1")
    assert result.exit_code == 0, result.output
    assert (dst / "a.txt").read_bytes() == b"alpha" and "Stopped after 1 run(s)" in result.output
    assert invoke("watch", str(src), str(dst), "--interval", "soon").exit_code == 2
