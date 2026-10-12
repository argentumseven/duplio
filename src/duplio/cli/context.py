"""Shared state for CLI commands: console, settings, backends, cache."""

from __future__ import annotations

import getpass
import logging
import logging.handlers
import sys
from dataclasses import dataclass, field

from rich.console import Console

from duplio.config import Settings, load_settings
from duplio.hashing.cache import HashCache, NullCache
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.vfs.location import BackendRegistry


def _prompt_password(server: str, username: str | None) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(f"Password for {username or 'guest'} on {server}: ")
    except (EOFError, KeyboardInterrupt):
        return None


@dataclass
class AppContext:
    console: Console
    quiet: bool = False
    json_output: bool = False
    password_file: str | None = None  # --password-file of the 'backup' group
    dirs: AppDirs = field(default_factory=lambda: get_app_dirs().ensure())
    _settings: Settings | None = None
    _registry: BackendRegistry | None = None
    _caches: list[HashCache] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(self.dirs)
        return self._settings

    @property
    def registry(self) -> BackendRegistry:
        if self._registry is None:
            self._registry = BackendRegistry(credential_provider=_prompt_password)
        return self._registry

    def cache(self, enabled: bool = True) -> HashCache:
        if not enabled or not self.settings.cache:
            return NullCache()
        try:
            cache = HashCache(self.dirs.hash_cache)
        except Exception as exc:  # a broken cache must never block work
            self.console.print(f"[yellow]Hash cache unavailable ({exc}); continuing without it.[/]")
            return NullCache()
        self._caches.append(cache)
        return cache

    def close(self) -> None:
        for cache in self._caches:
            try:
                cache.close()
            except Exception:
                pass
        self._caches.clear()
        if self._registry is not None:
            self._registry.close()


def setup_logging(dirs: AppDirs, verbose: bool) -> None:
    logger = logging.getLogger("duplio")
    if logger.handlers:
        return
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(dirs.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError:
        pass
