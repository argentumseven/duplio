"""Duplicate detection: the staged comparison engine and its result model."""

from duplio.compare.engine import Engine, compare
from duplio.compare.model import (
    Confidence,
    DuplicateGroup,
    Mode,
    ScanOptions,
    ScanResult,
    SimilarGroup,
    Verify,
)

__all__ = [
    "Confidence",
    "DuplicateGroup",
    "Engine",
    "Mode",
    "ScanOptions",
    "ScanResult",
    "SimilarGroup",
    "Verify",
    "compare",
]
