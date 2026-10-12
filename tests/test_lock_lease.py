"""The repository lock is a lease: refreshed while held, judged by the share's clock, and enforced on its owner."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from duplio.backup import lockfile, repo_gc
from duplio.backup.repository import Repository
from duplio.errors import LockError, LockLostError
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

T0 = 1_600_000_000.0


def backend_for(registry: BackendRegistry, tmp_path: Path):  # type: ignore[no-untyped-def]
    return registry.open(str(tmp_path)).backend


def foreign_lock(path: Path, *, age_s: float, content_time: float | None = None, token: str = "theirs") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.time() - age_s
    path.write_text(json.dumps({"pid": 1, "host": "some-other-host", "purpose": "backup", "token": token,
                                "time": stamp if content_time is None else content_time}))
    os.utime(path, (stamp, stamp))


def test_the_owner_keeps_refreshing_the_lock(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = str(tmp_path / "locks" / "x.lock")
    with lockfile.exclusive(b, path, "backup", refresh_s=0.05):
        first = json.loads(Path(path).read_text())
        time.sleep(0.4)
        later = json.loads(Path(path).read_text())
    assert later["seq"] > first["seq"] and later["token"] == first["token"]
    assert not Path(path).exists()


def test_a_lock_that_is_still_being_refreshed_is_respected(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    foreign_lock(path, age_s=30)
    with pytest.raises(LockError, match="locked by backup"):
        with lockfile.exclusive(b, str(path), "backup", stale_s=600):
            pass
    assert path.exists()


def test_a_lock_nobody_refreshes_is_taken_over(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    foreign_lock(path, age_s=3600)
    with lockfile.exclusive(b, str(path), "backup", stale_s=600) as lease:
        assert json.loads(path.read_text())["token"] == lease.token
    assert not path.exists()


def test_staleness_follows_the_file_not_the_clock_written_inside_it(registry: BackendRegistry, tmp_path: Path) -> None:
    # The writer's clock was hours behind: the time inside the file says "long ago", yet it was refreshed a moment ago.
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    foreign_lock(path, age_s=5, content_time=time.time() - 6 * 3600)
    with pytest.raises(LockError):
        with lockfile.exclusive(b, str(path), "backup", stale_s=600):
            pass
    # ...and the reverse: a clock running ahead cannot keep a dead lock alive.
    foreign_lock(path, age_s=7200, content_time=time.time() + 6 * 3600)
    with lockfile.exclusive(b, str(path), "backup", stale_s=600):
        pass


def test_a_lock_in_the_future_is_not_stale(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    foreign_lock(path, age_s=-86400)  # stamped a day ahead of the share's clock
    with pytest.raises(LockError):
        with lockfile.exclusive(b, str(path), "backup", stale_s=600):
            pass


def test_a_dead_process_on_this_host_is_reclaimed_at_once(registry: BackendRegistry, tmp_path: Path) -> None:
    import socket

    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 999_999_999, "host": socket.gethostname(), "time": time.time(), "token": "x"}))
    with lockfile.exclusive(b, str(path), "backup", stale_s=600):
        pass


def test_a_lock_taken_over_is_noticed_by_its_former_owner(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    with lockfile.exclusive(b, str(path), "backup", refresh_s=3600) as lease:
        lease.check()
        foreign_lock(path, age_s=0, token="thief")  # someone else now holds it
        with pytest.raises(LockLostError, match="another process"):
            lease.verify()
        with pytest.raises(LockLostError):
            lease.check()  # and it stays lost
    assert json.loads(path.read_text())["token"] == "thief"  # the former owner did not remove the new owner's lock


def test_a_vanished_lock_file_counts_as_lost(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    with lockfile.exclusive(b, str(path), "backup", refresh_s=3600) as lease:
        path.unlink()
        with pytest.raises(LockLostError, match="gone"):
            lease.verify()


def test_a_process_that_slept_reconfirms_the_lock_instead_of_assuming_it(registry: BackendRegistry,
                                                                         tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    with lockfile.exclusive(b, str(path), "backup", refresh_s=3600, stale_s=600) as lease:
        lease._ok_wall -= 3600  # the machine slept for an hour; nothing refreshed the lock meanwhile
        lease._ok_mono -= 3600
        lease.check()  # nobody touched it: confirmed and refreshed, carry on
        assert lease._unconfirmed_s() < 5
        lease._ok_wall -= 3600
        lease._ok_mono -= 3600
        foreign_lock(path, age_s=0, token="thief")  # ...whereas here someone did
        with pytest.raises(LockLostError):
            lease.check()


def test_the_heartbeat_notices_a_takeover_by_itself(registry: BackendRegistry, tmp_path: Path) -> None:
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    with lockfile.exclusive(b, str(path), "backup", refresh_s=0.05) as lease:
        foreign_lock(path, age_s=0, token="thief")
        deadline = time.time() + 3
        while not lease.lost and time.time() < deadline:
            time.sleep(0.02)
        assert lease.lost


def test_breaking_a_stale_lock_never_removes_a_live_one(registry: BackendRegistry, tmp_path: Path) -> None:
    # Two processes decide the same lock is stale; one has already replaced it with its own by the time the other
    # acts. The second one's "break" must put the live lock back.
    b = backend_for(registry, tmp_path)
    path = tmp_path / "locks" / "x.lock"
    foreign_lock(path, age_s=0, token="live")
    lockfile._break_stale(b, str(path), "the-stale-one")
    assert json.loads(path.read_text())["token"] == "live"
    lockfile._break_stale(b, str(path), "live")  # the one that really was stale goes
    assert not path.exists()


def test_a_backup_that_lost_its_lock_publishes_nothing(registry: BackendRegistry, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)
    write(tmp_path / "s" / "a.bin", rand_bytes(300_000, 1), mtime=T0)
    real = Repository._store_file

    def takeover_then_store(self, source, raw, cancel, progress, lease=None):  # type: ignore[no-untyped-def]
        foreign_lock(tmp_path / "repo" / "locks" / "repo.lock", age_s=0, token="thief")
        return real(self, source, raw, cancel, progress, lease)

    monkeypatch.setattr(Repository, "_store_file", takeover_then_store)
    with pytest.raises(LockLostError):
        repo.backup(registry.open(str(tmp_path / "s")))
    assert not list((tmp_path / "repo" / "snapshots").iterdir())
    assert not list((tmp_path / "repo" / "trees").iterdir())
    monkeypatch.undo()
    assert (tmp_path / "repo" / "locks" / "repo.lock").exists()  # the new owner's lock is untouched


def test_prune_stops_before_moving_anything_when_its_lock_was_lost(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024)
    write(tmp_path / "s" / "a.bin", rand_bytes(300_000, 1), mtime=T0)
    first = repo.backup(registry.open(str(tmp_path / "s"))).snapshot
    with repo.lock("forget"):
        repo.delete_snapshot(first.id)
    old = time.time() - 3 * 86400
    for p in (tmp_path / "repo" / "objects").rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    before = sorted(p.name for p in (tmp_path / "repo" / "objects").rglob("*") if p.is_file())
    with repo.lock("prune") as lease:
        foreign_lock(tmp_path / "repo" / "locks" / "repo.lock", age_s=0, token="thief")
        with pytest.raises(LockLostError):
            repo_gc.prune(repo, lease=lease, grace_s=0, min_age_s=0)
    assert sorted(p.name for p in (tmp_path / "repo" / "objects").rglob("*") if p.is_file()) == before
