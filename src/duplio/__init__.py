"""Duplio — safety-first duplicate finder and backup toolkit.

Duplio finds identical files across local folders, mounted phones (MTP/PTP/AFC),
Android devices over ADB and SMB shares, and offers versioned, deduplicated
backups, mirrors and phone imports. Every destructive operation is planned,
reviewed, re-verified against fresh bytes, journaled and (wherever the storage
allows it) reversible.
"""

__version__ = "0.1.0"
APP_NAME = "duplio"

__all__ = ["APP_NAME", "__version__"]
