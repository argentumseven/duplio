from __future__ import annotations

import os
from pathlib import Path

import pytest

from duplio.compare.engine import compare
from duplio.compare.model import Mode, ScanOptions
from duplio.compare.session import load_session, save_session
from duplio.errors import ConfigError, SafetyError
from duplio.plan.executor import ApplyOptions, apply_plan
from duplio.plan.journal import list_journals, summarize
from duplio.plan.model import ActionType, Plan
from duplio.plan.planner import build_plan, build_similar_plan, decide
from duplio.plan.rules import looks_like_copy, parse_rules
from duplio.plan.undo import inspect_incomplete, list_quarantine, purge_quarantine, undo
from duplio.scan.filters import ScanFilter
from duplio.scan.inventory import RootRole, make_roots, scan
from duplio.vfs.location import BackendRegistry

from .conftest import rand_bytes, write


def scan_dirs(registry: BackendRegistry, *roots, mode: Mode = Mode.ALL, protect=None):  # type: ignore[no-untyped-def]
    locs = [(registry.open(str(p)), role) for p, role in roots]
    inv = scan(make_roots(locs), ScanFilter(protect=protect or []))
    return compare(inv, ScanOptions(mode=mode))


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    data = rand_bytes(250_000, 11)
    write(tmp_path / "main" / "IMG_0001.jpg", data, mtime=1_600_000_000)
    write(tmp_path / "main" / "Downloads" / "IMG_0001 (1).jpg", data, mtime=1_650_000_000)
    write(tmp_path / "phone" / "DCIM" / "IMG_0001.jpg", data, mtime=1_640_000_000)
    write(tmp_path / "main" / "doc.txt", "same text" * 50)
    write(tmp_path / "phone" / "doc - Copy.txt", "same text" * 50)
    return tmp_path


# ---------------------------------------------------------------------------
# Rules and planning
# ---------------------------------------------------------------------------


def test_copy_name_heuristic() -> None:
    for name in ("a - Copy.jpg", "a copy 2.jpg", "Copy of a.doc", "a (1).jpg", "x~", "y.bak"):
        assert looks_like_copy(name), name
    for name in ("a.jpg", "IMG_1234.jpg", "copyright.txt", "Track 01.mp3"):
        assert not looks_like_copy(name), name


def test_parse_rules_errors() -> None:
    with pytest.raises(ConfigError):
        parse_rules("frobnicate")
    with pytest.raises(ConfigError):
        parse_rules("prefer-root=0")
    with pytest.raises(ConfigError):
        parse_rules("prefer-path=")


def test_default_rules_keep_clean_original(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    removed = sorted(f"{i.victim.root}:{i.victim.rel}" for i in plan.items)
    kept = {f"{i.keeper.root}:{i.keeper.rel}" for i in plan.items}
    assert removed == ["0:Downloads/IMG_0001 (1).jpg", "1:DCIM/IMG_0001.jpg", "1:doc - Copy.txt"]
    assert kept == {"0:IMG_0001.jpg", "0:doc.txt"}
    assert plan.reclaim == 2 * 250_000 + 450


def test_rule_order_changes_keeper(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res, rules="prefer-root=2,oldest")
    img_keeper = next(i.keeper for i in plan.items if i.victim.rel.endswith(".jpg"))
    assert img_keeper.root == 1
    plan = build_plan(res, rules="newest")
    img_keeper = next(i.keeper for i in plan.items if i.victim.rel.endswith(".jpg"))
    assert img_keeper.rel == "Downloads/IMG_0001 (1).jpg"


def test_reference_and_protected_are_never_removed(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "phone", RootRole.NORMAL), (photos / "main", RootRole.REFERENCE), mode=Mode.REFERENCE)
    plan = build_plan(res, rules="prefer-root=1")  # even when rules prefer the non-reference copy
    assert {i.victim.root for i in plan.items} == {0}
    assert all(i.keeper.root == 1 for i in plan.items)
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), protect=["Downloads/**"])
    plan = build_plan(res, rules="newest")
    assert all(not i.victim.rel.startswith("Downloads/") for i in plan.items)


def test_keep_overrides_and_invariants(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    group = next(g for g in res.groups if g.size == 250_000)
    keep_all = {m.id for m in group.members}
    plan = build_plan(res, group_ids=[group.id], keep_overrides={group.id: keep_all})
    assert plan.items == []
    phone = next(m for m in group.members if m.root == 1)
    plan = build_plan(res, group_ids=[group.id], keep_overrides={group.id: {phone.id}})
    assert {i.keeper.path for i in plan.items} == {phone.path}
    assert len(plan.items) == 2
    with pytest.raises(ConfigError):
        build_plan(res, group_ids=[group.id], keep_overrides={group.id: {999999}})


def test_decide_always_keeps_one(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    for group in res.groups:
        d = decide(group, res.inventory.roots, parse_rules(None), ActionType.QUARANTINE, keep_ids=[])
        assert len(d.keep) >= 1 and not ({m.id for m in d.keep} & {m.id for m in d.remove})


def test_hardlink_plan_requires_same_filesystem(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res, action=ActionType.HARDLINK)
    assert plan.items  # same tmp filesystem
    assert all(i.op is ActionType.HARDLINK for i in plan.items)


def test_plan_roundtrip(tmp_path: Path, registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    path = plan.save(tmp_path / "plan.json")
    again = Plan.load(path)
    assert again.to_dict() == plan.to_dict()
    gz = plan.save(tmp_path / "plan.json.gz")
    assert Plan.load(gz).to_dict() == plan.to_dict()


def test_session_roundtrip(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    path = save_session(res, dirs)
    loaded = load_session(path, registry)
    assert [sorted(m.path for m in g.members) for g in loaded.groups] == [sorted(m.path for m in g.members) for g in res.groups]
    assert build_plan(loaded).to_dict()["items"] == build_plan(res).to_dict()["items"]


def test_unique_mode_plans_nothing(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "phone", RootRole.NORMAL), (photos / "main", RootRole.REFERENCE), mode=Mode.UNIQUE)
    assert build_plan(res).items == []


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_quarantine_apply_and_undo(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    report = apply_plan(plan, registry, dirs=dirs)
    assert report.count("done") == 3 and report.reclaimed == plan.reclaim
    assert not (photos / "phone" / "DCIM" / "IMG_0001.jpg").exists()
    assert (photos / "main" / "IMG_0001.jpg").exists()
    q = list((photos / "phone" / ".duplio-quarantine").rglob("IMG_0001.jpg"))
    assert len(q) == 1 and q[0].read_bytes() == (photos / "main" / "IMG_0001.jpg").read_bytes()
    assert list((photos / "phone" / ".duplio-quarantine").rglob("MANIFEST.json"))
    assert list_quarantine(dirs)[0].files == 3
    # Quarantined files are excluded from later scans.
    again = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    assert again.groups == []
    u = undo(None, registry, dirs=dirs)
    assert u.count("restored") == 3
    assert (photos / "phone" / "DCIM" / "IMG_0001.jpg").exists()
    assert summarize(list_journals(dirs.journals)[0]).count("undone") == 3
    # A second undo has nothing left to do.
    assert undo(None, registry, dirs=dirs).count("restored") == 0


def test_changed_victim_is_skipped(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    target = photos / "phone" / "DCIM" / "IMG_0001.jpg"
    os.utime(target, (1_700_000_000, 1_700_000_000))  # touched after the scan
    report = apply_plan(plan, registry, dirs=dirs)
    skipped = [o for o in report.outcomes if o.status == "skipped"]
    assert [o.item.victim.path for o in skipped] == [str(target)]
    assert target.exists()


def test_same_size_same_mtime_content_change_is_caught(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    target = photos / "phone" / "DCIM" / "IMG_0001.jpg"
    data = bytearray(target.read_bytes())
    data[1000] ^= 1
    st = target.stat()
    target.write_bytes(bytes(data))
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # disguise the edit
    report = apply_plan(plan, registry, dirs=dirs)
    outcome = next(o for o in report.outcomes if o.item.victim.path == str(target))
    assert outcome.status == "skipped" and "content changed" in outcome.message
    assert target.exists()


def test_changed_keeper_blocks_group(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    keeper = photos / "main" / "IMG_0001.jpg"
    keeper.write_bytes(rand_bytes(250_000, 99))  # same size, different content
    report = apply_plan(plan, registry, dirs=dirs)
    jpg = [o for o in report.outcomes if o.item.victim.rel.endswith(".jpg")]
    assert jpg and all(o.status == "failed" for o in jpg)
    assert (photos / "phone" / "DCIM" / "IMG_0001.jpg").exists()
    assert (photos / "main" / "Downloads" / "IMG_0001 (1).jpg").exists()


def test_missing_keeper_blocks_group(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    (photos / "main" / "doc.txt").unlink()
    report = apply_plan(plan, registry, dirs=dirs)
    doc = next(o for o in report.outcomes if o.item.victim.rel == "doc - Copy.txt")
    assert doc.status == "failed"
    assert (photos / "phone" / "doc - Copy.txt").exists()


def test_bytes_verification_mode(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    report = apply_plan(plan, registry, dirs=dirs, options=ApplyOptions(verify="bytes"))
    assert report.count("done") == 3


def test_dry_run_changes_nothing(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res, action=ActionType.DELETE)
    before = sorted(str(p) for p in photos.rglob("*"))
    report = apply_plan(plan, registry, dirs=dirs, options=ApplyOptions(dry_run=True))
    assert report.count("would-do") == 3
    assert sorted(str(p) for p in photos.rglob("*")) == before
    assert list_journals(dirs.journals) == []


def test_delete_requires_confirmation(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res, action=ActionType.DELETE)
    with pytest.raises(SafetyError):
        apply_plan(plan, registry, dirs=dirs)
    report = apply_plan(plan, registry, dirs=dirs, options=ApplyOptions(confirm_delete=True))
    assert report.count("done") == 3
    assert not (photos / "phone" / "doc - Copy.txt").exists()
    assert (photos / "main" / "doc.txt").exists()
    lines = undo(None, registry, dirs=dirs).lines
    assert {line.status for line in lines} == {"not-undoable"}


def test_alias_paths_are_never_both_acted_on(registry: BackendRegistry, tmp_path: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    """A plan whose victim and keeper are two names for one file must not destroy it."""
    write(tmp_path / "real" / "only.bin", rand_bytes(5000, 3))
    os.symlink(tmp_path / "real", tmp_path / "linkdir")
    res = scan_dirs(registry, (tmp_path / "real", RootRole.NORMAL))
    # Fabricate a malicious/buggy plan: "keep" the file through the symlinked path.
    from duplio.plan.model import FileRef, PlanItem, RootSpec

    info = (tmp_path / "real" / "only.bin").stat()
    victim = FileRef(0, str(tmp_path / "real" / "only.bin"), "only.bin", info.st_size, info.st_mtime_ns)
    keeper = FileRef(1, str(tmp_path / "linkdir" / "only.bin"), "only.bin", info.st_size, info.st_mtime_ns)
    from duplio.hashing.core import hash_file
    from duplio.vfs.local import LocalBackend

    digest = hash_file(LocalBackend(), victim.path, "blake2b")
    for action in (ActionType.QUARANTINE, ActionType.DELETE):
        plan = Plan(action=action, roots=[RootSpec(0, str(tmp_path / "real"), "normal"), RootSpec(1, str(tmp_path / "linkdir"), "normal")],
                    items=[PlanItem(1, 1, action, victim, keeper, digest, "blake2b", 3, 5000)], session_id=res.session_id)
        report = apply_plan(plan, registry, dirs=dirs, options=ApplyOptions(confirm_delete=True))
        assert report.outcomes[0].status == "skipped"
        assert (tmp_path / "real" / "only.bin").exists()


def test_hardlink_apply_and_undo(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res, action=ActionType.HARDLINK)
    report = apply_plan(plan, registry, dirs=dirs)
    assert report.count("done") == 3
    keeper = photos / "main" / "IMG_0001.jpg"
    assert keeper.stat().st_nlink == 3
    victim = photos / "phone" / "DCIM" / "IMG_0001.jpg"
    assert victim.read_bytes() == keeper.read_bytes()
    u = undo(None, registry, dirs=dirs)
    assert u.count("restored") == 3
    assert keeper.stat().st_nlink == 1
    assert victim.stat().st_ino != keeper.stat().st_ino
    assert victim.stat().st_mtime_ns == 1_640_000_000 * 10**9  # original timestamp restored


def test_purge_quarantine(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    apply_plan(build_plan(res), registry, dirs=dirs)
    with pytest.raises(SafetyError):
        purge_quarantine(registry, dirs=dirs)
    files, nbytes, errors = purge_quarantine(registry, dirs=dirs, older_than="0s", dry_run=True)
    assert files == 3 and not errors
    assert list((photos / "phone" / ".duplio-quarantine").rglob("*.jpg"))
    files, nbytes, errors = purge_quarantine(registry, dirs=dirs, older_than="0s")
    assert files == 3 and nbytes == 2 * 250_000 + 450
    assert not list((photos / "phone" / ".duplio-quarantine").rglob("*.jpg"))
    lines = undo(None, registry, dirs=dirs).lines
    assert all(line.status == "failed" for line in lines)  # purged files cannot come back


def test_quarantine_fallback_when_rename_impossible(registry: BackendRegistry, photos: Path, dirs,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Storage that cannot rename (MTP) gets copy → verify → delete into the local quarantine."""
    from duplio.errors import UnsupportedOperation
    from duplio.vfs.local import LocalBackend

    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL), (photos / "phone", RootRole.NORMAL))
    plan = build_plan(res)
    real_rename = LocalBackend.rename
    phone = str(photos / "phone")

    def no_rename(self, src, dst):  # type: ignore[no-untyped-def]
        if src.startswith(phone):
            raise UnsupportedOperation("no rename on this device")
        return real_rename(self, src, dst)

    monkeypatch.setattr(LocalBackend, "rename", no_rename)
    report = apply_plan(plan, registry, dirs=dirs)
    assert report.count("done") == 3
    fallback = [o for o in report.outcomes if o.dst and o.dst.startswith(str(dirs.quarantine))]
    assert len(fallback) == 2
    assert all(Path(o.dst).exists() for o in fallback)  # type: ignore[arg-type]
    monkeypatch.setattr(LocalBackend, "rename", real_rename)
    assert undo(None, registry, dirs=dirs).count("restored") == 3
    assert (photos / "phone" / "DCIM" / "IMG_0001.jpg").exists()


def test_interrupted_operation_is_detected(registry: BackendRegistry, photos: Path, dirs) -> None:  # type: ignore[no-untyped-def]
    from duplio.plan.journal import Journal

    with Journal(dirs.journals / "20200101T000000000Z-dead.jsonl", header={"apply_id": "x", "action": "quarantine"}) as j:
        j.intent(op="quarantine", src_root=str(photos / "phone"), src=str(photos / "phone" / "doc - Copy.txt"))
    findings = inspect_incomplete(registry, dirs)
    assert len(findings) == 1 and "nothing happened" in findings[0][2]


def test_similar_plan_only_reversible(registry: BackendRegistry, photos: Path) -> None:
    res = scan_dirs(registry, (photos / "main", RootRole.NORMAL))
    with pytest.raises(ConfigError):
        build_similar_plan(res, {}, action=ActionType.DELETE)
