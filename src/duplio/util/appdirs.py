"""Per-platform locations for configuration, state (journals, sessions) and caches.

``DUPLIO_HOME`` overrides everything and places all three under one folder,
which keeps tests and portable installs self-contained.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from duplio import APP_NAME


@dataclass(frozen=True)
class AppDirs:
    config: Path
    state: Path
    cache: Path

    @property
    def config_file(self) -> Path:
        return self.config / "config.toml"

    @property
    def journals(self) -> Path:
        return self.state / "journals"

    @property
    def sessions(self) -> Path:
        return self.state / "sessions"

    @property
    def quarantine(self) -> Path:
        return self.state / "quarantine"

    @property
    def hash_cache(self) -> Path:
        return self.cache / "hashes.sqlite3"

    @property
    def log_file(self) -> Path:
        return self.state / "duplio.log"

    def ensure(self) -> AppDirs:
        for path in (self.config, self.state, self.cache, self.journals, self.sessions):
            path.mkdir(parents=True, exist_ok=True)
        return self


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else default


def get_app_dirs() -> AppDirs:
    override = os.environ.get("DUPLIO_HOME")
    if override:
        base = Path(override).expanduser()
        return AppDirs(base / "config", base / "state", base / "cache")

    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return AppDirs(appdata / "Duplio", local / "Duplio" / "State", local / "Duplio" / "Cache")
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support" / "Duplio"
        return AppDirs(support, support / "State", home / "Library" / "Caches" / "Duplio")
    return AppDirs(
        _xdg("XDG_CONFIG_HOME", home / ".config") / APP_NAME,
        _xdg("XDG_STATE_HOME", home / ".local" / "state") / APP_NAME,
        _xdg("XDG_CACHE_HOME", home / ".cache") / APP_NAME,
    )
