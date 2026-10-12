"""Chunking: fixed blocks and content-defined chunks (boundaries that follow the data)."""

from __future__ import annotations

import io
import random
import secrets
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from duplio.backup.chunker import FixedChunker, GearChunker, build, gear_table  # noqa: E402
from duplio.backup.repository import Repository  # noqa: E402
from duplio.errors import RepositoryError  # noqa: E402
from duplio.vfs.location import BackendRegistry  # noqa: E402

from .conftest import rand_bytes, write  # noqa: E402

T0 = 1_600_000_000.0
KIB = 1024


def cdc(seed: bytes = b"seed", minimum: int = 16 * KIB, average: int = 64 * KIB, maximum: int = 256 * KIB) -> GearChunker:
    return GearChunker(minimum, average, maximum, seed)


def chunks_of(chunker, data: bytes, limit: int | None = None) -> list[bytes]:  # type: ignore[no-untyped-def]
    return list(chunker.split(io.BytesIO(data), limit))


def test_fixed_chunker_cuts_blocks_and_honours_the_limit() -> None:
    chunker = FixedChunker(100)
    assert [len(c) for c in chunks_of(chunker, bytes(250))] == [100, 100, 50]
    assert [len(c) for c in chunks_of(chunker, bytes(250), limit=120)] == [100, 20]
    assert chunks_of(chunker, b"") == []


@pytest.mark.parametrize("size", [0, 1, 63, 64, 16 * KIB - 1, 16 * KIB, 16 * KIB + 1, 64 * KIB, 256 * KIB,
                                  256 * KIB + 1, 700 * KIB, 2_000_003])
def test_cdc_chunks_always_add_up_to_the_data_and_respect_the_bounds(size: int) -> None:
    data = rand_bytes(size, size + 1)
    chunker = cdc()
    chunks = chunks_of(chunker, data)
    assert b"".join(chunks) == data
    for chunk in chunks[:-1]:
        assert chunker.minimum <= len(chunk) <= chunker.maximum
    if chunks:
        assert len(chunks[-1]) <= chunker.maximum


def test_cdc_is_deterministic_and_sizes_cluster_around_the_average() -> None:
    data = rand_bytes(8 * 1024 * KIB, 5)
    first = [len(c) for c in chunks_of(cdc(), data)]
    assert first == [len(c) for c in chunks_of(cdc(), data)]
    average = sum(first) / len(first)
    assert 0.6 * 64 * KIB < average < 1.8 * 64 * KIB, average


def test_cdc_limit_stops_reading_where_told() -> None:
    data = rand_bytes(600 * KIB, 6)
    handle = io.BytesIO(data)
    chunks = list(cdc().split(handle, limit=300 * KIB))
    assert b"".join(chunks) == data[: 300 * KIB]


def test_cdc_cut_points_resynchronise_after_an_insertion() -> None:
    data = rand_bytes(6 * 1024 * KIB, 7)
    edited = data[:1000] + b"INSERTED" + data[1000:]
    chunker = cdc()
    before, after = chunks_of(chunker, data), chunks_of(chunker, edited)
    shared = set(before) & set(after)
    assert len(shared) >= len(after) - 3, f"only {len(shared)} of {len(after)} chunks survived an 8-byte insertion"
    fixed = FixedChunker(64 * KIB)
    assert not set(chunks_of(fixed, data)) & set(chunks_of(fixed, edited))  # fixed blocks share nothing


def test_the_cut_points_depend_on_the_key() -> None:
    data = rand_bytes(2 * 1024 * KIB, 8)
    a = [len(c) for c in chunks_of(cdc(b"repository one"), data)]
    b = [len(c) for c in chunks_of(cdc(b"repository two"), data)]
    assert a != b  # nobody holding one repository can predict another's boundaries, or recognise a file by them


def test_the_gear_table_is_a_pure_function_of_the_seed() -> None:
    assert (gear_table(b"x") == gear_table(b"x")).all() and not (gear_table(b"x") == gear_table(b"y")).all()


def test_repetitive_data_still_gets_cut() -> None:
    data = bytes(2 * 1024 * KIB)  # all zero: no content-based cut ever fires, the maximum size decides
    chunks = chunks_of(cdc(), data)
    assert b"".join(chunks) == data and all(len(c) <= 256 * KIB for c in chunks)


def test_unknown_chunker_and_bad_sizes_are_refused() -> None:
    with pytest.raises(RepositoryError):
        build({"type": "mystery"}, seed=b"x")
    with pytest.raises(RepositoryError):
        GearChunker(10, 100, 1000, b"x")  # the minimum cannot be below the hash window


# --------------------------------------------------------------------------- inside a repository
def cdc_repo(registry: BackendRegistry, tmp_path: Path, password: str | None = None) -> Repository:
    return Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * KIB, chunker="cdc",
                           password=password, scrypt_n=2**10)


@pytest.mark.parametrize("password", [None, "correct horse battery"], ids=["plain", "encrypted"])
def test_cdc_repository_round_trips_and_checks(registry: BackendRegistry, tmp_path: Path, password: str | None) -> None:
    repo = cdc_repo(registry, tmp_path, password)
    data = rand_bytes(900 * KIB, 9)
    write(tmp_path / "src" / "big.bin", data, mtime=T0)
    write(tmp_path / "src" / "small.txt", b"hello", mtime=T0)
    repo.backup(registry.open(str(tmp_path / "src")))
    reopened = Repository.open(repo.loc, password=password)
    assert reopened.config["chunker"]["type"] == "cdc"
    assert reopened.check(read_data=True).ok
    result = reopened.restore("latest", registry.open(str(tmp_path / "out")))
    assert not result.errors and (tmp_path / "out" / "big.bin").read_bytes() == data
    again = reopened.restore("latest", registry.open(str(tmp_path / "out")))  # identical files are recognised
    assert again.unchanged == 2 and again.restored == 0


def test_an_insertion_into_a_big_file_costs_little_with_cdc_and_everything_with_fixed(registry: BackendRegistry,
                                                                                      tmp_path: Path,
                                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    # Where the chunk boundaries fall depends on a seed taken from the repository's random id, so with a fresh id every run
    # the cost of the edit varies (rarely past the limit below). A fixed id makes this check repeatable.
    real_token_hex = secrets.token_hex
    monkeypatch.setattr(secrets, "token_hex", lambda nbytes=None: "00112233445566778899aabbccddeeff" if nbytes == 16 else real_token_hex(nbytes))
    data = rand_bytes(3 * 1024 * KIB, 10)
    stored = {}
    for kind in ("fixed", "cdc"):
        repo = Repository.init(registry.open(str(tmp_path / f"repo-{kind}")), chunk_size=64 * KIB, chunker=kind)
        write(tmp_path / f"src-{kind}" / "disk.img", data, mtime=T0)
        repo.backup(registry.open(str(tmp_path / f"src-{kind}")))
        write(tmp_path / f"src-{kind}" / "disk.img", data[:5000] + b"X" * 100 + data[5000:], mtime=T0 + 100)
        stored[kind] = repo.backup(registry.open(str(tmp_path / f"src-{kind}"))).snapshot.new_bytes
    assert stored["fixed"] > 2.5 * 1024 * KIB  # every block after the insertion is new
    assert stored["cdc"] < 0.3 * 1024 * KIB  # only the chunks around the edit


def test_without_numpy_a_cdc_repository_can_still_be_restored_but_not_written(registry: BackendRegistry,
                                                                              tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    repo = cdc_repo(registry, tmp_path)
    write(tmp_path / "src" / "a.bin", rand_bytes(300 * KIB, 11), mtime=T0)
    repo.backup(registry.open(str(tmp_path / "src")))
    from duplio.backup import chunker as chunker_module

    def missing():  # type: ignore[no-untyped-def]
        raise RepositoryError("this repository uses content-defined chunking, which needs numpy: pip install 'duplio[cdc]'")

    monkeypatch.setattr(chunker_module, "_numpy", missing)
    reopened = Repository.open(repo.loc)
    assert reopened.check(read_data=True).ok
    assert not reopened.restore("latest", registry.open(str(tmp_path / "out"))).errors
    write(tmp_path / "src" / "b.bin", rand_bytes(300 * KIB, 12), mtime=T0)
    with pytest.raises(RepositoryError, match="needs numpy"):
        reopened.backup(registry.open(str(tmp_path / "src")))


def test_chunk_size_limits_for_cdc(registry: BackendRegistry, tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="at most 16 MiB"):
        Repository.init(registry.open(str(tmp_path / "r")), chunk_size=32 * 1024 * KIB, chunker="cdc")
    with pytest.raises(RepositoryError):
        Repository.init(registry.open(str(tmp_path / "r2")), chunker="rolling")


def test_cdc_snapshots_survive_the_crash_sweep_invariants(registry: BackendRegistry, tmp_path: Path) -> None:
    # (the sweeps themselves run on fixed-block repositories; this checks the CDC path once through the same flow)
    repo = cdc_repo(registry, tmp_path)
    random.Random(1)
    for n in range(3):
        write(tmp_path / "src" / f"f{n}.bin", rand_bytes(400 * KIB, 20 + n), mtime=T0 + n)
        repo.backup(registry.open(str(tmp_path / "src")))
    with repo.lock("forget"):
        repo.delete_snapshot(repo.snapshots()[0].id)
    repo.prune(grace_s=0, min_age_s=0)
    assert repo.check(read_data=True).ok
