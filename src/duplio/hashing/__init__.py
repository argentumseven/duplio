"""Hash algorithms, streaming/sampled hashing and the persistent digest cache."""

from duplio.hashing.algorithms import (
    HashAlgorithm,
    available_algorithms,
    default_full_algorithm,
    default_sample_algorithm,
    get_algorithm,
)
from duplio.hashing.cache import HashCache, NullCache
from duplio.hashing.core import files_identical, hash_file, sample_file

__all__ = [
    "HashAlgorithm",
    "HashCache",
    "NullCache",
    "available_algorithms",
    "default_full_algorithm",
    "default_sample_algorithm",
    "files_identical",
    "get_algorithm",
    "hash_file",
    "sample_file",
]
