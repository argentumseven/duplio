"""Storage backends: local (incl. mounted phones/shares), native SMB, WebDAV, FTP/FTPS, SFTP and ADB."""

from duplio.vfs.base import (
    Backend,
    Capabilities,
    MediaKind,
    RawEntry,
    Sampling,
    StatInfo,
    WalkStats,
)
from duplio.vfs.location import BackendRegistry, Location, location_kind

__all__ = [
    "Backend",
    "BackendRegistry",
    "Capabilities",
    "Location",
    "MediaKind",
    "RawEntry",
    "Sampling",
    "StatInfo",
    "WalkStats",
    "location_kind",
]
