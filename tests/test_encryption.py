"""Encrypted repositories, plus find / history / export for all repositories."""

from __future__ import annotations

import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.backup import crypto
from duplio.backup.crypto import PasswordRequired, WrongPassword
from duplio.backup.export import export_snapshot
from duplio.backup.repository import CONFIG_NAME, Repository, object_id
from duplio.cli.main import cli
from duplio.errors import ConfigError, RepositoryError
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write

PW = "correct horse battery"
SECRET = b"TOP-SECRET-MARKER-" * 40
T0 = 1_600_000_000.0
FAST = 2**10  # cheap key derivation for tests


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    write(src / "docs" / "secret.txt", SECRET, mtime=T0)
    write(src / "docs" / "copy.txt", SECRET, mtime=T0)  # identical content
    write(src / "big.bin", rand_bytes(300_000, 5), mtime=T0)
    return src


def make(registry: BackendRegistry, tmp_path: Path, password: str | None = PW) -> Repository:
    return Repository.init(registry.open(str(tmp_path / "repo")), chunk_size=64 * 1024, password=password, scrypt_n=FAST)


def raw_files(repo_dir: Path) -> list[Path]:
    return [p for p in repo_dir.rglob("*") if p.is_file()]


# ---------------------------------------------------------------- confidentiality
def test_nothing_readable_is_stored(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)), label="docs")
    blob = b"".join(p.read_bytes() for p in raw_files(tmp_path / "repo") if p.name != CONFIG_NAME)
    for needle in (b"TOP-SECRET", b"secret.txt", b"docs", b"src", b"duplio-snapshot", b"entries"):
        assert needle not in blob, needle
    config = json.loads((tmp_path / "repo" / CONFIG_NAME).read_text())
    assert config["version"] == 2 and "encryption" in config and PW not in json.dumps(config)
    assert config["encryption"]["scheme"] == "aes-256-gcm"


def test_roundtrip_dedup_and_keyed_ids(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    report = repo.backup(registry.open(str(source)))
    assert not report.errors
    snap = repo.snapshots()[-1]
    chunks = {c for e in repo.tree(snap.id) for c in e.chunks}
    assert len(chunks) == 1 + 5  # identical texts share a chunk: 1 + ceil(300000 / 65536) big.bin chunks
    assert object_id(SECRET) not in chunks  # ids are keyed: a known file cannot be looked for
    out = tmp_path / "out"
    result = Repository.open(repo.loc, password=PW).restore("latest", registry.open(str(out)))
    assert result.restored == 3 and not result.errors
    assert (out / "docs" / "secret.txt").read_bytes() == SECRET
    assert (out / "big.bin").read_bytes() == (source / "big.bin").read_bytes()
    assert Repository.open(repo.loc, password=PW).check(read_data=True).ok


def test_second_backup_stores_nothing_new(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    before = {p.name for p in raw_files(tmp_path / "repo" / "objects")}
    again = repo.backup(registry.open(str(source)), rehash=True)
    assert again.snapshot.new_bytes == 0 and {p.name for p in raw_files(tmp_path / "repo" / "objects")} == before


# ---------------------------------------------------------------- passwords
def test_password_rules(registry: BackendRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make(registry, tmp_path)
    with pytest.raises(PasswordRequired, match="DUPLIO_REPO_PASSWORD"):
        Repository.open(repo.loc)
    with pytest.raises(WrongPassword):
        Repository.open(repo.loc, password="not the password")
    monkeypatch.setenv("DUPLIO_REPO_PASSWORD", PW)
    assert Repository.open(repo.loc).encrypted
    monkeypatch.delenv("DUPLIO_REPO_PASSWORD")
    pwfile = tmp_path / "pw"
    pwfile.write_text(PW + "\nsecond line is ignored\n")
    monkeypatch.setenv("DUPLIO_REPO_PASSWORD_FILE", str(pwfile))
    assert Repository.open(repo.loc).encrypted
    monkeypatch.setenv("DUPLIO_REPO_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RepositoryError, match="cannot read the password file"):
        Repository.open(repo.loc)
    with pytest.raises(RepositoryError, match="at least"):
        Repository.init(registry.open(str(tmp_path / "weak")), password="short")


def test_password_is_unicode_normalised(registry: BackendRegistry, tmp_path: Path) -> None:
    import unicodedata

    password = "Pässwörd-ñandú-2024"
    repo = Repository.init(registry.open(str(tmp_path / "u")), password=unicodedata.normalize("NFC", password), scrypt_n=FAST)
    assert Repository.open(repo.loc, password=unicodedata.normalize("NFD", password)).encrypted


def test_change_password_keeps_the_data(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    objects = {p.name: p.read_bytes() for p in raw_files(tmp_path / "repo" / "objects")}
    repo.change_password("a brand new passphrase", scrypt_n=FAST)
    assert {p.name: p.read_bytes() for p in raw_files(tmp_path / "repo" / "objects")} == objects  # nothing re-encrypted
    with pytest.raises(WrongPassword):
        Repository.open(repo.loc, password=PW)
    reopened = Repository.open(repo.loc, password="a brand new passphrase")
    out = tmp_path / "out"
    assert reopened.restore("latest", registry.open(str(out))).restored == 3
    with pytest.raises(RepositoryError, match="not encrypted"):
        Repository.init(registry.open(str(tmp_path / "plain")), password=None).change_password("whatever long")


def test_hostile_key_derivation_cost_is_refused(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path)
    config_path = tmp_path / "repo" / CONFIG_NAME
    config = json.loads(config_path.read_text())
    config["encryption"]["kdf"]["n"] = 2**30  # a tampered config must not make us allocate terabytes
    config_path.write_text(json.dumps(config))
    with pytest.raises(RepositoryError, match="unreasonable"):
        Repository.open(repo.loc, password=PW)


def test_missing_library_is_explained(registry: BackendRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "AVAILABLE", False)
    with pytest.raises(RepositoryError, match="pip install 'duplio\\[encrypt\\]'"):
        Repository.init(registry.open(str(tmp_path / "r")), password=PW)
    assert not (tmp_path / "r").exists()


def test_unencrypted_repositories_are_unchanged(registry: BackendRegistry, tmp_path: Path) -> None:
    repo = make(registry, tmp_path, password=None)
    config = json.loads((tmp_path / "repo" / CONFIG_NAME).read_text())
    assert config["version"] == 1 and "encryption" not in config and not repo.encrypted


# ---------------------------------------------------------------- tampering
def test_tampered_object_is_detected(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    victim = sorted(raw_files(tmp_path / "repo" / "objects"))[0]
    data = bytearray(victim.read_bytes())
    data[-1] ^= 0x01
    victim.write_bytes(bytes(data))
    report = Repository.open(repo.loc, password=PW).check(read_data=True)
    assert victim.name in report.corrupt and not report.ok


def test_objects_cannot_be_swapped(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    a, b = sorted(raw_files(tmp_path / "repo" / "objects"))[:2]
    a.write_bytes(b.read_bytes())  # a valid encrypted object, but stored under another id
    report = Repository.open(repo.loc, password=PW).check(read_data=True)
    assert a.name in report.corrupt


def test_plaintext_objects_are_rejected_in_an_encrypted_repository(registry: BackendRegistry, tmp_path: Path,
                                                                     source: Path) -> None:
    from duplio.backup.repository import encode_object

    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    victim = sorted(raw_files(tmp_path / "repo" / "objects"))[0]
    victim.write_bytes(encode_object(b"forged", compress=False))
    assert victim.name in Repository.open(repo.loc, password=PW).check(read_data=True).corrupt


def test_tampered_file_list_is_reported_not_trusted(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    tree = next((tmp_path / "repo" / "trees").iterdir())
    data = bytearray(tree.read_bytes())
    data[20] ^= 0xFF
    tree.write_bytes(bytes(data))
    report = Repository.open(repo.loc, password=PW).check()
    assert report.unreadable_trees and not report.ok
    with pytest.raises(RepositoryError, match="refusing to prune"):
        Repository.open(repo.loc, password=PW).prune()


def test_tampered_snapshot_summary_hides_the_snapshot(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    summary = next((tmp_path / "repo" / "snapshots").iterdir())
    summary.write_bytes(b"DPE1" + b"\0" * 60)
    assert Repository.open(repo.loc, password=PW).snapshots() == []


# ---------------------------------------------------------------- find / history / export
@pytest.fixture(params=[False, True], ids=["plain", "encrypted"])
def history_repo(request, registry: BackendRegistry, tmp_path: Path) -> tuple[Repository, Path]:  # type: ignore[no-untyped-def]
    src = tmp_path / "work"
    repo = make(registry, tmp_path, password=PW if request.param else None)
    write(src / "notes" / "todo.txt", b"v1", mtime=T0)
    write(src / "keep.txt", b"always", mtime=T0)
    repo.backup(registry.open(str(src)))
    write(src / "notes" / "todo.txt", b"v2 edited", mtime=T0 + 100)
    repo.backup(registry.open(str(src)))
    os.remove(src / "notes" / "todo.txt")
    write(src / "notes" / "other.md", b"another", mtime=T0 + 200)
    repo.backup(registry.open(str(src)))
    return repo, src


def test_find_across_snapshots(history_repo) -> None:  # type: ignore[no-untyped-def]
    repo, _ = history_repo
    hits = {h.path: h for h in repo.find("*.txt")}
    assert set(hits) == {"notes/todo.txt", "keep.txt"}
    todo = hits["notes/todo.txt"]
    assert len(todo.versions) == 2 and todo.snapshot_count == 2  # deleted since, but still findable
    assert len(hits["keep.txt"].versions) == 1 and hits["keep.txt"].snapshot_count == 3
    assert [h.path for h in repo.find("*.md")] == ["notes/other.md"]
    assert [h.path for h in repo.find("todo", regex=True)] == ["notes/todo.txt"]
    latest = repo.snapshots()[-1].id
    assert "notes/todo.txt" not in {h.path for h in repo.find("*.txt", snapshot=latest)}


def test_history_of_one_file(history_repo) -> None:  # type: ignore[no-untyped-def]
    repo, _ = history_repo
    found = repo.history("notes/todo.txt")
    assert found is not None and sorted(v.size for v in found.versions) == [2, 9]
    assert repo.history("never/existed.txt") is None


def test_export_formats(history_repo, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo, _ = history_repo
    first = repo.snapshots()[0].id
    zip_path, tar_path = tmp_path / "snap.zip", tmp_path / "snap.tar.gz"
    assert export_snapshot(repo, first, zip_path) == (2, len(b"v1") + len(b"always"))
    with zipfile.ZipFile(zip_path) as z:
        assert sorted(z.namelist()) == ["keep.txt", "notes/todo.txt"] and z.read("notes/todo.txt") == b"v1"
    assert export_snapshot(repo, first, tar_path, include=["notes"]) == (1, 2)
    with tarfile.open(tar_path) as t:
        assert t.getnames() == ["notes/todo.txt"] and t.extractfile("notes/todo.txt").read() == b"v1"  # type: ignore[union-attr]
    with pytest.raises(ConfigError, match="already exists"):
        export_snapshot(repo, first, zip_path)
    with pytest.raises(ConfigError, match="chosen by the file name"):
        export_snapshot(repo, first, tmp_path / "snap.rar")
    assert not [p for p in tmp_path.iterdir() if "duplio-tmp" in p.name]


def test_export_big_file_through_tar(registry: BackendRegistry, tmp_path: Path, source: Path) -> None:
    repo = make(registry, tmp_path)
    repo.backup(registry.open(str(source)))
    out = tmp_path / "all.tar"
    export_snapshot(repo, "latest", out)
    with tarfile.open(out) as t:
        assert t.extractfile("big.bin").read() == (source / "big.bin").read_bytes()  # type: ignore[union-attr]


# ---------------------------------------------------------------- CLI
def invoke(*args: str, env: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
    runner = CliRunner(env={"COLUMNS": "170", **(env or {})})
    return runner.invoke(cli, ["--no-color", *args], catch_exceptions=False, **kw)


def test_cli_encrypted_workflow(tmp_path: Path, source: Path) -> None:
    repo = str(tmp_path / "enc-repo")
    env = {"DUPLIO_REPO_PASSWORD": PW}
    init = invoke("backup", "init", repo, "--encrypt", env=env)
    assert init.exit_code == 0 and "Keep your password safe" in init.output
    run = invoke("backup", "run", str(source), repo, "--label", "docs", env=env)
    assert run.exit_code == 0, run.output
    assert b"TOP-SECRET" not in b"".join(p.read_bytes() for p in raw_files(Path(repo)))
    assert "docs" in invoke("backup", "snapshots", repo, env=env).output
    nopw = invoke("backup", "snapshots", repo)
    assert nopw.exit_code == 2 and "encrypted" in nopw.output
    wrong = invoke("backup", "snapshots", repo, env={"DUPLIO_REPO_PASSWORD": "definitely wrong"})
    assert wrong.exit_code == 2 and "wrong password" in wrong.output
    pwfile = tmp_path / "pw"
    pwfile.write_text(PW + "\n")
    via_file = invoke("backup", "--password-file", str(pwfile), "find", repo, "*.txt")
    assert via_file.exit_code == 0 and "secret.txt" in via_file.output
    out = tmp_path / "restored"
    restored = invoke("backup", "restore", repo, "latest", str(out), env=env)
    assert restored.exit_code == 0 and (out / "docs" / "secret.txt").read_bytes() == SECRET
    exported = invoke("backup", "export", repo, "latest", str(tmp_path / "x.zip"), env=env)
    assert exported.exit_code == 0 and "NOT encrypted" in exported.output
    assert invoke("backup", "passwd", repo, env=env).exit_code == 2  # needs a terminal


def test_cli_init_encrypt_needs_a_password_source(tmp_path: Path) -> None:
    result = invoke("backup", "init", str(tmp_path / "r"), "--encrypt")
    assert result.exit_code == 2 and "DUPLIO_REPO_PASSWORD" in result.output and not (tmp_path / "r").exists()


def test_cli_find_and_history(registry: BackendRegistry, tmp_path: Path) -> None:
    src = tmp_path / "w"
    write(src / "a" / "report.txt", b"one", mtime=T0)
    repo_path = tmp_path / "repo"
    repo = Repository.init(registry.open(str(repo_path)))
    repo.backup(registry.open(str(src)))
    write(src / "a" / "report.txt", b"two!", mtime=T0 + 5)
    repo.backup(registry.open(str(src)))
    found = invoke("backup", "find", str(repo_path), "report*")
    assert found.exit_code == 0 and "a/report.txt" in found.output
    assert "Nothing matches" in invoke("backup", "find", str(repo_path), "*.nope").output
    hist = invoke("backup", "history", str(repo_path), "a/report.txt")
    assert hist.exit_code == 0 and "restore" in hist.output
    assert invoke("backup", "history", str(repo_path), "no/such").exit_code == 2
