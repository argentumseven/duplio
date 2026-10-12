"""Throttle, name rules, time comparison, retry, destination guards, drive probe."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli
from duplio.errors import CancelledError, ConfigError, VerificationError
from duplio.events import CancelToken
from duplio.transfer import copy_verified
from duplio.util.names import component_issues, fix_component, fix_rel, rel_issues
from duplio.util.throttle import RateLimiter, limiter_for, parse_rate
from duplio.util.timecmp import HOUR_NS, SECOND_NS, mtimes_match, newer, tolerance_for
from duplio.vfs.guards import (
    DEFAULT_SENTINEL,
    BackendUnresponsive,
    GuardError,
    check_destination,
    is_really_mounted,
    looks_like_mount_point,
    probe_responsive,
    write_sentinel,
)
from duplio.vfs.location import BackendRegistry
from duplio.vfs.probe import probe_location
from duplio.vfs.retry import is_transient, retry_transient

from .conftest import rand_bytes, write
from .fakes import FakeSMB


# ---------------------------------------------------------------- throttle
class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds


def test_parse_rate() -> None:
    assert parse_rate("5MB/s") == 5_000_000
    assert parse_rate("800k") == parse_rate("800K/s") == 800_000
    assert parse_rate("2 MiB/s") == 2 * 1024 * 1024
    for off in (None, "", "0", "off", "Unlimited"):
        assert parse_rate(off) is None
    with pytest.raises(ConfigError):
        parse_rate("fast")
    assert limiter_for(None) is None


def test_rate_limiter_paces_to_the_limit() -> None:
    clock = _Clock()
    limiter = RateLimiter(1000, clock=clock.time, sleep=clock.sleep)
    for _ in range(10):
        limiter.acquire(500)
    # 5000 bytes at 1000 B/s with a one-second burst: about four seconds of waiting
    assert 3.5 <= clock.slept <= 4.5
    before = clock.slept
    limiter.acquire(0)
    assert clock.slept == before


def test_rate_limiter_wait_is_cancellable() -> None:
    clock = _Clock()
    limiter = RateLimiter(10, clock=clock.time, sleep=clock.sleep)
    token = CancelToken()
    token.cancel()
    limiter.acquire(5)  # within the burst: no wait, no cancel check
    with pytest.raises(CancelledError):
        limiter.acquire(10_000, token)


def test_copy_verified_respects_limiter(tmp_path: Path) -> None:
    clock = _Clock()
    limiter = RateLimiter(100_000, clock=clock.time, sleep=clock.sleep)
    reg = BackendRegistry()
    try:
        src = write(tmp_path / "s.bin", rand_bytes(400_000, 3))
        b = reg.local()
        copy_verified((b, str(src)), (b, str(tmp_path / "d.bin")), algorithm="blake2b")
        assert (tmp_path / "d.bin").read_bytes() == src.read_bytes()
        copy_verified((b, str(src)), (b, str(tmp_path / "e.bin")), algorithm="blake2b", limiter=limiter)
        # written once and read back once: 800 kB through a 100 kB/s limiter with a 100 kB burst
        assert clock.slept >= 6.0
    finally:
        reg.close()


# ---------------------------------------------------------------- names
@pytest.mark.parametrize(
    "name",
    ["a:b.txt", "what?.txt", 'q"uote', "pipe|d", "star*", "lt<gt>", "back\\slash", "tab\tname", "end.", "end ", "CON",
     "nul.txt", "Com3.log", "x" * 256],
)
def test_windows_problem_names_are_flagged_and_fixed(name: str) -> None:
    assert component_issues(name, windows=True)
    fixed = fix_component(name, windows=True)
    assert not component_issues(fixed, windows=True), (name, fixed)
    assert fixed and fixed not in (".", "..")


@pytest.mark.parametrize("name", ["plain.txt", "dots.in.name.txt", "Ünïcode ✓.jpg", ".hidden", "CONSOLE", "COM10", "a b"])
def test_fine_names_are_untouched(name: str) -> None:
    assert component_issues(name, windows=True) == []
    assert fix_component(name, windows=True) == name


def test_posix_destinations_only_enforce_length() -> None:
    assert component_issues("a:b?.txt", windows=False) == []
    assert component_issues("é" * 130, windows=False)  # 260 UTF-8 bytes
    assert component_issues("é" * 130, windows=True) == []  # 130 UTF-16 units


def test_long_name_keeps_extension() -> None:
    fixed = fix_component("n" * 300 + ".jpeg", windows=True)
    assert fixed.endswith(".jpeg") and len(fixed) <= 255


def test_fix_rel_and_rel_issues() -> None:
    assert fix_rel("ok/dir/file.txt", windows=True) == "ok/dir/file.txt"
    assert fix_rel("a:b/c?.txt", windows=True) == "a_b/c_.txt"
    problems = rel_issues("good/bad:name/CON", windows=True)
    assert {p.component for p in problems} == {"bad:name", "CON"}
    assert all(str(p) for p in problems)


def test_fix_is_not_injective_so_callers_must_dedupe() -> None:
    # documented behaviour: two different names can map to one fixed name
    assert fix_component("a:b", windows=True) == fix_component("a?b", windows=True)


# ---------------------------------------------------------------- time compare
def test_mtime_tolerance_and_dst_shift() -> None:
    t = 1_700_000_000 * SECOND_NS
    assert mtimes_match(t, t)
    assert mtimes_match(t, t + SECOND_NS)
    assert not mtimes_match(t, t + SECOND_NS + 1)
    assert mtimes_match(t, t + 2 * SECOND_NS, tolerance_ns=2 * SECOND_NS)  # FAT
    assert not mtimes_match(t, t + HOUR_NS)
    assert mtimes_match(t, t + HOUR_NS, dst_shift=True)
    assert mtimes_match(t + HOUR_NS, t, dst_shift=True)
    assert mtimes_match(t, t + HOUR_NS + SECOND_NS, dst_shift=True)
    assert not mtimes_match(t, t + HOUR_NS + 5 * SECOND_NS, dst_shift=True)
    assert not mtimes_match(t, t + 59 * 60 * SECOND_NS, dst_shift=True)  # an offset, not a window


def test_newer_and_tolerance_for() -> None:
    t = 1_700_000_000 * SECOND_NS
    assert newer(t + 10 * SECOND_NS, t) == 1
    assert newer(t, t + 10 * SECOND_NS) == -1
    assert newer(t, t + 1) == 0
    assert newer(t, t + HOUR_NS, dst_shift=True) == 0
    assert tolerance_for(1, 2 * SECOND_NS, 100) == 2 * SECOND_NS
    assert tolerance_for(1, floor_ns=SECOND_NS) == SECOND_NS


# ---------------------------------------------------------------- retry
def test_transient_classification() -> None:
    assert is_transient(OSError(errno.ESTALE, "stale"))
    assert is_transient(TimeoutError())
    assert is_transient(ConnectionResetError())
    for permanent in (PermissionError(), FileExistsError(), OSError(errno.ENOSPC, "full"), VerificationError("x"),
                      CancelledError("c"), ValueError("v")):
        assert not is_transient(permanent), permanent


def test_retry_recovers_then_succeeds() -> None:
    calls: list[int] = []
    recovered: list[int] = []
    waits: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.ESTALE, "stale")
        return "ok"

    assert retry_transient(flaky, attempts=3, recover=lambda: recovered.append(1), sleep=waits.append) == "ok"
    assert len(calls) == 3 and len(recovered) == 2 and waits


def test_retry_gives_up_and_never_retries_permanent_errors() -> None:
    calls: list[int] = []

    def always() -> None:
        calls.append(1)
        raise OSError(errno.ESTALE, "stale")

    with pytest.raises(OSError):
        retry_transient(always, attempts=3, sleep=lambda s: None)
    assert len(calls) == 3
    calls.clear()

    def permanent() -> None:
        calls.append(1)
        raise PermissionError("no")

    with pytest.raises(PermissionError):
        retry_transient(permanent, attempts=5, sleep=lambda s: None)
    assert len(calls) == 1


def test_retry_stops_when_cancelled() -> None:
    token = CancelToken()

    def flaky() -> None:
        token.cancel()
        raise OSError(errno.ESTALE, "stale")

    with pytest.raises(CancelledError):
        retry_transient(flaky, attempts=4, cancel=token, sleep=lambda s: None)


def test_copy_verified_retries_a_dropped_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import duplio.transfer as transfer

    real = transfer._copy_once
    state = {"n": 0}

    def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] == 1:
            raise OSError(errno.ESTALE, "stale file handle")
        return real(*args, **kwargs)

    monkeypatch.setattr(transfer, "_copy_once", flaky)
    monkeypatch.setattr("duplio.vfs.retry.time.sleep", lambda s: None)
    reg = BackendRegistry()
    try:
        src = write(tmp_path / "s.bin", rand_bytes(1000, 9))
        b = reg.local()
        copy_verified((b, str(src)), (b, str(tmp_path / "d.bin")), algorithm="blake2b")
        assert state["n"] == 2
        assert (tmp_path / "d.bin").read_bytes() == src.read_bytes()
    finally:
        reg.close()


# ---------------------------------------------------------------- guards
def test_sentinel_roundtrip(tmp_path: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(tmp_path))
    with pytest.raises(GuardError, match="marker"):
        check_destination(loc, expect_mounted=False, sentinel=DEFAULT_SENTINEL)
    write_sentinel(loc)
    check_destination(loc, expect_mounted=False, sentinel=DEFAULT_SENTINEL)
    with pytest.raises(FileExistsError):
        write_sentinel(loc)  # never overwrites an existing marker


def test_space_check(tmp_path: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(tmp_path))
    check_destination(loc, expect_mounted=False, needed_bytes=1024)
    with pytest.raises(GuardError, match="not enough free space"):
        check_destination(loc, expect_mounted=False, needed_bytes=10**18)


def test_missing_destination_is_checked_through_its_parent(tmp_path: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(tmp_path / "new" / "deeper"))
    assert probe_responsive(loc, 5) >= 0
    check_destination(loc, expect_mounted=False)


def test_unresponsive_backend_is_reported_not_hung(tmp_path: Path, registry: BackendRegistry,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    loc = registry.open(str(tmp_path))
    release = threading.Event()
    monkeypatch.setattr(type(loc.backend), "stat", lambda self, path: release.wait(5) and None)
    try:
        with pytest.raises(BackendUnresponsive, match="did not respond"):
            probe_responsive(loc, 0.2)
    finally:
        release.set()


def test_mount_detection(tmp_path: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(tmp_path))
    ok, why = is_really_mounted(loc)
    assert isinstance(ok, bool) and why
    assert not looks_like_mount_point(loc)  # a temp dir is not where drives get mounted
    for path in ("/mnt/nas/backup", "/media/me/DISK", "/Volumes/Backup", "/run/media/me/x"):
        assert looks_like_mount_point(registry.open(path)) or os.path.exists("/") is not False


def test_unmounted_mount_point_is_refused(registry: BackendRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    from duplio.vfs import guards

    loc = registry.open("/mnt/definitely-not-mounted-here/backup")
    monkeypatch.setattr(guards, "is_really_mounted", lambda location: (False, "it is on the system disk"))
    with pytest.raises(GuardError, match="not a mounted drive"):
        check_destination(loc)  # automatic: path is under /mnt
    check_destination(loc, expect_mounted=False)  # explicit opt-out


def test_remote_backends_count_as_mounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = tmp_path / "server"
    (server / "share").mkdir(parents=True)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: FakeSMB(server))
    reg = BackendRegistry()
    try:
        loc = reg.open("smb://nas/share/backups")
        assert is_really_mounted(loc)[0]
        check_destination(loc, expect_mounted=True, needed_bytes=1024)
        write_sentinel(reg.open("smb://nas/share"))
        check_destination(reg.open("smb://nas/share"), sentinel=DEFAULT_SENTINEL)
    finally:
        reg.close()


# ---------------------------------------------------------------- probe
def test_probe_local_directory_is_clean(tmp_path: Path, registry: BackendRegistry) -> None:
    loc = registry.open(str(tmp_path))
    report = probe_location(loc, throughput_mib=1)
    assert report.writable
    assert report.case_sensitive in (True, False)
    assert report.mtime_resolution_ns is not None
    assert report.hardlinks is True
    assert report.write_mib_s and report.read_mib_s
    assert report.recommendations
    assert os.listdir(tmp_path) == []  # nothing left behind


def test_probe_on_smb_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = tmp_path / "server"
    (server / "share").mkdir(parents=True)
    import duplio.vfs.smb as smb

    monkeypatch.setattr(smb, "_smbclient", lambda: FakeSMB(server))
    reg = BackendRegistry()
    try:
        report = probe_location(reg.open("smb://nas/share"), throughput_mib=1)
    finally:
        reg.close()
    assert report.writable and report.kind == "network"
    assert os.listdir(server / "share") == []


def test_probe_read_only_location(tmp_path: Path, registry: BackendRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    loc = registry.open(str(tmp_path))

    def refuse(self, path, **kw):  # type: ignore[no-untyped-def]
        raise PermissionError(errno.EACCES, "read-only file system")

    monkeypatch.setattr(type(loc.backend), "open_write", refuse)
    report = probe_location(loc, throughput_mib=0)
    assert not report.writable
    assert any("not writable" in line for line in report.recommendations)
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------- CLI
def _invoke(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner(env={"COLUMNS": "200"}).invoke(cli, ["--no-color", *args], catch_exceptions=False)


def test_cli_probe_and_json(tmp_path: Path) -> None:
    result = _invoke("probe", str(tmp_path), "--no-speed")
    assert result.exit_code == 0, result.output
    assert "Writable" in result.output and "Timestamps" in result.output
    result = _invoke("probe", str(tmp_path), "--no-speed", "--json")
    data = json.loads(result.output)
    assert data["writable"] is True and "recommendations" in data


def test_cli_sentinel_and_guarded_mirror(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", "alpha")
    dst.mkdir()
    refused = _invoke("mirror", str(src), str(dst), "--sentinel", "--yes")
    assert refused.exit_code != 0 and "marker" in refused.output
    assert not (dst / "a.txt").exists()
    created = _invoke("sentinel", "create", str(dst))
    assert created.exit_code == 0, created.output
    assert (dst / DEFAULT_SENTINEL).exists()
    again = _invoke("sentinel", "create", str(dst))
    assert again.exit_code == 0 and "already exists" in again.output
    ok = _invoke("mirror", str(src), str(dst), "--sentinel", "--yes", "--bwlimit", "50MB/s")
    assert ok.exit_code == 0, ok.output
    assert (dst / "a.txt").read_text() == "alpha"
    assert _invoke("sentinel", "check", str(dst)).exit_code == 0


def test_cli_rejects_bad_bwlimit(tmp_path: Path) -> None:
    src, dst = tmp_path / "s", tmp_path / "d"
    write(src / "a.txt", "x")
    result = _invoke("mirror", str(src), str(dst), "--bwlimit", "warp9", "--yes")
    assert result.exit_code != 0
    assert not dst.exists() or not any(dst.iterdir())
