"""Hash algorithm registry.

Two roles, two kinds of algorithm:

* **Full-content digests** decide whether files are identical. Only
  *cryptographic* digests (BLAKE3, BLAKE2b-256, SHA-256) are trusted to
  declare a match on their own; anything weaker (xxh3, SHA-1, MD5) is followed
  by a byte-for-byte comparison before Duplio will act on the result.
* **Sample digests** only *rule out* matches (different sample ⇒ different
  file), so a fast non-cryptographic hash is ideal there.

Optional accelerators (``blake3``, ``xxhash``) are used when installed; the
standard library always provides a safe fallback.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from duplio.errors import ConfigError


class Hasher(Protocol):
    def update(self, data: bytes | bytearray | memoryview, /) -> Any: ...

    def hexdigest(self) -> str: ...


@dataclass(frozen=True)
class HashAlgorithm:
    name: str
    factory: Callable[[], Hasher]
    digest_bits: int
    cryptographic: bool
    description: str

    def new(self) -> Hasher:
        return self.factory()


def _blake2b_256() -> Hasher:
    return hashlib.blake2b(digest_size=32)


def _blake2b_128() -> Hasher:
    return hashlib.blake2b(digest_size=16)


_REGISTRY: dict[str, HashAlgorithm] = {}


def _register(alg: HashAlgorithm) -> None:
    _REGISTRY[alg.name] = alg


_register(HashAlgorithm("blake2b", _blake2b_256, 256, True, "BLAKE2b-256 (standard library)"))
_register(HashAlgorithm("blake2b-128", _blake2b_128, 128, False, "BLAKE2b-128 (sampling only)"))
_register(HashAlgorithm("sha256", hashlib.sha256, 256, True, "SHA-256 (also computed on Android phones)"))
_register(HashAlgorithm("sha1", hashlib.sha1, 160, False, "SHA-1 (legacy; verified byte-for-byte)"))
_register(HashAlgorithm("md5", hashlib.md5, 128, False, "MD5 (legacy; verified byte-for-byte)"))

try:
    import blake3 as _blake3

    def _blake3_new() -> Hasher:
        # One thread per hasher: Duplio already parallelises across files.
        return _blake3.blake3()

    _register(HashAlgorithm("blake3", _blake3_new, 256, True, "BLAKE3 (fast, cryptographic)"))
    HAVE_BLAKE3 = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_BLAKE3 = False

try:
    import xxhash as _xxhash

    _register(
        HashAlgorithm("xxh3_128", _xxhash.xxh3_128, 128, False, "XXH3-128 (very fast, non-cryptographic)")
    )
    _register(HashAlgorithm("xxh3_64", _xxhash.xxh3_64, 64, False, "XXH3-64 (sampling only)"))
    HAVE_XXHASH = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_XXHASH = False


def get_algorithm(name: str) -> HashAlgorithm:
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"unknown or unavailable hash algorithm {name!r} (available: {available})") from None


def available_algorithms() -> list[HashAlgorithm]:
    return list(_REGISTRY.values())


def default_full_algorithm() -> str:
    return "blake3" if HAVE_BLAKE3 else "blake2b"


def default_sample_algorithm() -> str:
    return "xxh3_128" if HAVE_XXHASH else "blake2b-128"


def full_hash_choices() -> list[str]:
    return [a.name for a in _REGISTRY.values() if a.digest_bits >= 128 and a.name not in ("blake2b-128",)]
