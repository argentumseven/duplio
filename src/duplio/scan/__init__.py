"""File discovery: filters, roots and the inventory."""

from duplio.scan.filters import DEFAULT_EXCLUDES, DEFAULT_PROTECT, ScanFilter
from duplio.scan.inventory import FileEntry, Inventory, Root, RootRole, make_roots, scan

__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_PROTECT",
    "FileEntry",
    "Inventory",
    "Root",
    "RootRole",
    "ScanFilter",
    "make_roots",
    "scan",
]
