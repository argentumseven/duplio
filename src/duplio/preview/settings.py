"""Preview settings (the ``[previews]`` table of ``config.toml``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from duplio.errors import ConfigError
from duplio.util.fmt import parse_size

MODES = ("auto", "always", "off")
KEEP = ("session", "persistent")


@dataclass
class PreviewSettings:
    mode: str = "auto"  # auto: cache pictures from slow, remote and cloud drives · always: everywhere · off: never
    keep: str = "session"  # session: deleted when Duplio closes · persistent: kept in the cache folder between runs
    max_bytes: int = 1024 * 1024 * 1024  # size limit of the persistent cache
    size_px: int = 256  # longest side of a stored thumbnail, in pixels
    prefetch: int = 12  # how many pictures ahead of the one on screen are fetched in the background
    small_download: int = 1024 * 1024  # a picture up to this size may be read whole on a slow drive without asking
    max_download: int = 64 * 1024 * 1024  # never read more than this of one file to make a thumbnail
    online_only: bool = False  # fetch previews of cloud-only placeholder files without asking (this downloads them)
    memory_bytes: int = 0  # memory the window may use for pictures it is showing (0: a share of the computer's memory)
    prefetch_folder: bool = True  # when a folder is browsed, fetch the pictures of the whole folder in the background

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"[previews] mode must be one of {', '.join(MODES)}")
        if self.keep not in KEEP:
            raise ConfigError(f"[previews] keep must be one of {', '.join(KEEP)}")
        if not 64 <= self.size_px <= 2048:
            raise ConfigError("[previews] size_px must be between 64 and 2048")
        if self.prefetch < 0 or self.prefetch > 200:
            raise ConfigError("[previews] prefetch must be between 0 and 200")
        if self.memory_bytes < 0:
            raise ConfigError("[previews] memory cannot be negative")

    @classmethod
    def from_table(cls, table: dict[str, Any]) -> PreviewSettings:
        known = {"mode", "keep", "max_size", "size_px", "prefetch", "small_download", "max_download", "online_only", "memory",
                 "prefetch_folder"}
        unknown = set(table) - known
        if unknown:
            raise ConfigError(f"unknown [previews] setting(s): {', '.join(sorted(unknown))}")
        out = cls()
        if "mode" in table:
            out.mode = str(table["mode"])
        if "keep" in table:
            out.keep = str(table["keep"])
        for name, attr in (("max_size", "max_bytes"), ("small_download", "small_download"), ("max_download", "max_download"),
                           ("memory", "memory_bytes")):
            if name in table:
                value = table[name]
                setattr(out, attr, int(value) if isinstance(value, int) else parse_size(str(value)))
        for name in ("size_px", "prefetch"):
            if name in table:
                setattr(out, name, int(table[name]))
        if "online_only" in table:
            out.online_only = bool(table["online_only"])
        if "prefetch_folder" in table:
            out.prefetch_folder = bool(table["prefetch_folder"])
        out.validate()
        return out
