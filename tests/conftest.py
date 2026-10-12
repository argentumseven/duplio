from __future__ import annotations

import os
import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.vfs.location import BackendRegistry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own Duplio state/config/cache folder."""
    home = tmp_path / "duplio-home"
    monkeypatch.setenv("DUPLIO_HOME", str(home))
    for name in ("SMB", "DAV", "FTP", "SFTP"):
        monkeypatch.delenv(f"DUPLIO_{name}_PASSWORD", raising=False)
    return home


@pytest.fixture
def dirs(isolated_home: Path) -> AppDirs:
    return get_app_dirs().ensure()


@pytest.fixture
def registry() -> Iterator[BackendRegistry]:
    reg = BackendRegistry()
    yield reg
    reg.close()


def write(path: Path, data: bytes | str, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def rand_bytes(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


@pytest.fixture
def old_mtime() -> float:
    """A timestamp old enough for the hash cache to trust it."""
    return 1_700_000_000.0
