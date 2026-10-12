"""Exception hierarchy.

Every error Duplio raises on purpose derives from :class:`DuplioError` so the
CLI and TUI can present it cleanly instead of dumping a traceback.
"""

from __future__ import annotations


class DuplioError(Exception):
    """Base class for all expected, user-presentable errors."""


class ConfigError(DuplioError):
    """Invalid configuration, profile or command-line combination."""


class LocationError(DuplioError):
    """A location string could not be parsed or opened."""


class BackendUnavailable(LocationError):
    """A storage backend needs an optional dependency or tool that is missing."""


class OverlappingRootsError(DuplioError):
    """Two scan roots overlap; scanning both would make one file look like two."""


class SafetyError(DuplioError):
    """An operation was refused because it could put data at risk."""


class VerificationError(SafetyError):
    """Fresh verification of file contents did not match the expectation."""


class CancelledError(DuplioError):
    """The user cancelled a running operation."""


class RepositoryError(DuplioError):
    """A backup repository is missing, damaged or incompatible."""


class LockError(RepositoryError):
    """A repository or journal lock is held by another process."""


class UnsupportedOperation(DuplioError):
    """The storage backend does not support the requested operation."""


class LockLostError(LockError):
    """This process held a lock but can no longer prove it; it stops before changing anything else."""
