"""Turn comparison results into a plan, enforcing every safety invariant.

Invariants (checked here *and* again by the executor):

1. Every group keeps at least one copy; a kept copy is never also removed.
2. Copies in reference locations, protected paths or read-only storage are
   never removed.
3. Only groups proven identical (``CERTAIN``) get content actions. Similar
   images can be quarantined or trashed only when explicitly hand-picked.
4. Hard links and clones are only planned where they are physically possible
   (same local filesystem, supporting storage).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from duplio.compare.model import Confidence, DuplicateGroup, Mode, ScanResult, SimilarGroup
from duplio.errors import ConfigError
from duplio.plan.model import ActionType, FileRef, Plan, PlanItem, RootSpec
from duplio.plan.rules import Rule, RuleContext, parse_rules, rank
from duplio.scan.inventory import FileEntry, Root
from duplio.vfs.base import MediaKind
from duplio.vfs.local import LocalBackend


def file_ref(e: FileEntry) -> FileRef:
    return FileRef(root=e.root, path=e.path, rel=e.rel, size=e.size, mtime_ns=e.mtime_ns)


def root_specs(roots: Sequence[Root]) -> list[RootSpec]:
    return [RootSpec(r.index, r.location.uri, r.role.value, r.label) for r in roots]


@dataclass
class GroupDecision:
    keep: list[FileEntry]
    remove: list[FileEntry]
    locked: list[FileEntry]  # kept because they may not be touched
    reasons: dict[int, str]


def removal_block_reason(entry: FileEntry, roots: Sequence[Root], action: ActionType) -> str | None:
    """Why ``entry`` may not be removed/replaced, or ``None`` if it may."""
    root = roots[entry.root]
    if root.is_reference:
        return "reference location"
    if entry.protected:
        return "protected path"
    caps = root.caps
    if not caps.can_delete:
        return "read-only storage"
    if action is ActionType.TRASH and not caps.can_trash:
        return "no system trash on this storage"
    if action in (ActionType.HARDLINK, ActionType.REFLINK) and not isinstance(root.backend, LocalBackend):
        return f"{action.value} needs a local filesystem"
    if action is ActionType.HARDLINK and not caps.can_hardlink:
        return "this filesystem has no hard links"
    if action is ActionType.REFLINK and not caps.can_reflink:
        return "this filesystem cannot clone files"
    return None


def decide(
    group: DuplicateGroup,
    roots: Sequence[Root],
    rules: Sequence[Rule],
    action: ActionType,
    *,
    keep_ids: Iterable[int] | None = None,
) -> GroupDecision:
    ctx = RuleContext(roots=roots, aliases=group.aliases)
    reasons: dict[int, str] = {}
    locked: list[FileEntry] = []
    for m in group.members:
        reason = removal_block_reason(m, roots, action)
        if reason:
            reasons[m.id] = reason
            locked.append(m)
    ranked = rank(group.members, rules, ctx)
    keep_set = {m.id for m in locked}
    if keep_ids is not None:
        chosen = set(keep_ids)
        unknown = chosen - {m.id for m in group.members}
        if unknown:
            raise ConfigError(f"group {group.id}: unknown files selected to keep")
        keep_set |= chosen
    else:
        has_reference = any(roots[m.root].is_reference for m in group.members)
        if not has_reference:
            keep_set.add(ranked[0].id)  # the best-ranked copy is always kept
    if not keep_set:
        keep_set.add(ranked[0].id)
    keep = [m for m in ranked if m.id in keep_set]
    remove = [m for m in ranked if m.id not in keep_set]
    return GroupDecision(keep=keep, remove=remove, locked=locked, reasons=reasons)


def _same_device(a: FileEntry, b: FileEntry, roots: Sequence[Root]) -> bool:
    ra, rb = roots[a.root], roots[b.root]
    if not (isinstance(ra.backend, LocalBackend) and isinstance(rb.backend, LocalBackend)):
        return False
    return a.inode is not None and b.inode is not None and a.inode[0] == b.inode[0]


def _pick_keeper(victim: FileEntry, keep: list[FileEntry], roots: Sequence[Root], action: ActionType) -> FileEntry | None:
    if action in (ActionType.HARDLINK, ActionType.REFLINK):
        for k in keep:
            if _same_device(victim, k, roots):
                return k
        return None
    # Prefer a keeper on cheap, local storage for the final verification read.
    order = {MediaKind.LOCAL: 0, MediaKind.ROTATIONAL: 1, MediaKind.REMOVABLE: 2, MediaKind.NETWORK: 3}
    return min(keep, key=lambda k: (order.get(roots[k.root].caps.kind, 4), keep.index(k)))


def build_plan(
    result: ScanResult,
    *,
    action: ActionType = ActionType.QUARANTINE,
    rules: Sequence[str] | str | None = None,
    verify: str = "hash",
    group_ids: Iterable[int] | None = None,
    keep_overrides: Mapping[int, Iterable[int]] | None = None,
) -> Plan:
    """Plan actions for the certain duplicate groups of a scan."""
    roots = result.inventory.roots
    parsed = parse_rules(rules)
    plan = Plan(
        action=action,
        roots=root_specs(roots),
        session_id=result.session_id,
        verify=verify,
        rules=[r.spec for r in parsed],
    )
    if result.options.mode is Mode.UNIQUE:
        plan.notes.append("Unique-mode scans list files that are missing elsewhere; nothing is planned for removal.")
        return plan
    wanted = set(group_ids) if group_ids is not None else None
    overrides = dict(keep_overrides or {})
    next_id = 1
    for group in result.groups:
        if wanted is not None and group.id not in wanted:
            continue
        if group.confidence is not Confidence.CERTAIN:
            plan.skipped_groups += 1
            continue
        decision = decide(group, roots, parsed, action, keep_ids=overrides.get(group.id))
        if not decision.remove:
            plan.skipped_groups += 1
            continue
        for victim in decision.remove:
            keeper = _pick_keeper(victim, decision.keep, roots, action)
            if keeper is None:
                plan.notes.append(
                    f"group {group.id}: {roots[victim.root].backend.display(victim.path)} is not on the same "
                    f"filesystem as any kept copy, so it cannot be replaced by a {action.value}"
                )
                continue
            # Hard-link aliases of a removed copy are removed too, or no space is freed.
            names = [victim, *group.aliases.get(victim.id, [])]
            removable = [n for n in names if removal_block_reason(n, roots, action) is None]
            frees = len(removable) == len(names) and victim.nlink <= len(names)
            for n, name in enumerate(removable):
                plan.items.append(
                    PlanItem(
                        id=next_id,
                        group_id=group.id,
                        op=action,
                        victim=file_ref(name),
                        keeper=file_ref(keeper),
                        digest=group.digest,
                        algorithm=group.algorithm,
                        confidence=int(group.confidence),
                        reclaim=group.size if (frees and n == 0) else 0,
                        note="" if name is victim else "hard link of a removed copy",
                    )
                )
                next_id += 1
    _assert_invariants(plan, result)
    return plan


def build_similar_plan(
    result: ScanResult,
    picks: Mapping[int, Iterable[int]],
    *,
    action: ActionType = ActionType.QUARANTINE,
) -> Plan:
    """Plan for hand-picked members of *similar* image groups.

    ``picks`` maps a similar-group id to the entry ids to remove. Only
    reversible actions are allowed because the files are not identical.
    """
    if action not in (ActionType.QUARANTINE, ActionType.TRASH):
        raise ConfigError("similar (non-identical) images can only be quarantined or trashed")
    roots = result.inventory.roots
    plan = Plan(action=action, roots=root_specs(roots), session_id=result.session_id, verify="metadata")
    groups: dict[int, SimilarGroup] = {g.id: g for g in result.similar}
    next_id = 1
    for gid, entry_ids in picks.items():
        group = groups[gid]
        remove_ids = set(entry_ids)
        keep = [m for m in group.members if m.id not in remove_ids]
        if not keep:
            raise ConfigError(f"similar group {gid}: keep at least one image")
        for m in group.members:
            if m.id not in remove_ids:
                continue
            reason = removal_block_reason(m, roots, action)
            if reason:
                plan.notes.append(f"{roots[m.root].backend.display(m.path)}: {reason}")
                continue
            plan.items.append(
                PlanItem(
                    id=next_id,
                    group_id=-gid,
                    op=action,
                    victim=file_ref(m),
                    keeper=file_ref(keep[0]),
                    digest=None,
                    algorithm=None,
                    confidence=int(Confidence.SIMILAR),
                    reclaim=m.size,
                    note="similar image (not identical)",
                )
            )
            next_id += 1
    return plan


def _assert_invariants(plan: Plan, result: ScanResult) -> None:
    """Defence in depth: a planner bug must never produce an unsafe plan."""
    roots = result.inventory.roots
    victims_by_group: dict[int, set[tuple[int, str]]] = {}
    for item in plan.items:
        victims_by_group.setdefault(item.group_id, set()).add((item.victim.root, item.victim.path))
        if roots[item.victim.root].is_reference:
            raise AssertionError("planner targeted a reference location")
        if (item.victim.root, item.victim.path) == (item.keeper.root, item.keeper.path):
            raise AssertionError("planner chose a file as its own keeper")
    for item in plan.items:
        if (item.keeper.root, item.keeper.path) in victims_by_group[item.group_id]:
            raise AssertionError("planner removes a copy it also keeps")
    for group in result.groups:
        victims = victims_by_group.get(group.id)
        if victims and all((m.root, m.path) in victims for m in group.members):
            raise AssertionError("planner would remove every copy of a file")
