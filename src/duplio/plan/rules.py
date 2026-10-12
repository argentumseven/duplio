"""Which copy to keep.

Rules form an ordered chain, like a multi-column sort: the first rule that
tells two copies apart decides, later rules only break ties, and the file path
is the final, deterministic tie-breaker. Hard constraints come before any
rule: copies in reference locations, protected paths and read-only storage
can never be chosen for removal.

Rule syntax (comma-separated)::

    not-copy-named         prefer names without "copy", "(1)", "~", ".bak"...
    has-hardlinks          prefer the copy that other hard links point to
    root-order             prefer locations in the order they were given
    prefer-root=N          prefer location number N (1-based)
    avoid-root=N           prefer anything but location N
    prefer-path=GLOB       prefer copies whose relative path matches GLOB
    avoid-path=GLOB        prefer copies whose relative path does not match GLOB
    prefer-kind=KIND       prefer storage of a kind: local, network, device, removable
    oldest / newest        modification time
    shallowest / deepest   folder depth
    shortest-path / longest-path
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from duplio.errors import ConfigError
from duplio.scan.filters import compile_glob
from duplio.scan.inventory import FileEntry, Root

DEFAULT_RULES = ("has-hardlinks", "not-copy-named", "root-order", "oldest", "shortest-path")

_COPY_MARKERS = re.compile(
    r"""(
        \bcopy\b | \bkopie\b | \bcopie\b | \bcopia\b | コピー |   # "copy" in a few languages
        \(\d+\)(?=\.[^.]*$|$) |                                  # "name (2).jpg"
        ~\d*(?=\.[^.]*$|$) |                                     # "file~.txt", "file~1"
        \.(?:bak|old|orig|tmp)$ |
        \bduplicate\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_copy(name: str) -> bool:
    """Heuristic: does a file *name* look like a copy of another file?"""
    return bool(_COPY_MARKERS.search(name))


@dataclass(frozen=True)
class RuleContext:
    roots: Sequence[Root]
    aliases: dict[int, list[FileEntry]]


Scorer = Callable[[FileEntry, RuleContext], tuple[float | str, ...] | float | int | str]


@dataclass(frozen=True)
class Rule:
    spec: str
    description: str
    score: Scorer  # lower is better


def _name(e: FileEntry) -> str:
    return e.rel.rsplit("/", 1)[-1]


def _root_number(arg: str) -> int:
    try:
        number = int(arg)
    except ValueError as exc:
        raise ConfigError(f"location number expected, got {arg!r}") from exc
    if number < 1:
        raise ConfigError("location numbers start at 1")
    return number - 1


def parse_rule(spec: str) -> Rule:
    spec = spec.strip()
    name, _, arg = spec.partition("=")
    name = name.strip().lower()
    arg = arg.strip()
    if name == "not-copy-named":
        return Rule(spec, "prefer names that don't look like copies", lambda e, c: int(looks_like_copy(_name(e))))
    if name == "has-hardlinks":
        return Rule(spec, "prefer the copy with hard links", lambda e, c: -len(c.aliases.get(e.id, ())))
    if name == "root-order":
        return Rule(spec, "prefer earlier locations", lambda e, c: e.root)
    if name in ("prefer-root", "avoid-root"):
        idx = _root_number(arg)
        sign = 0 if name == "prefer-root" else 1
        return Rule(
            spec,
            f"{'prefer' if sign == 0 else 'avoid'} location {idx + 1}",
            lambda e, c: (0 if e.root == idx else 1) ^ sign,
        )
    if name in ("prefer-path", "avoid-path"):
        if not arg:
            raise ConfigError(f"{name} needs a glob, e.g. {name}=**/Camera/**")
        rx = compile_glob(arg)
        sign = 0 if name == "prefer-path" else 1
        return Rule(spec, f"{name.split('-')[0]} paths matching {arg}", lambda e, c: (0 if rx.match(e.rel) else 1) ^ sign)
    if name == "prefer-kind":
        kind = arg.lower()
        if kind not in {"local", "rotational", "removable", "network", "device"}:
            raise ConfigError(f"unknown storage kind {arg!r}")
        return Rule(spec, f"prefer {kind} storage", lambda e, c: 0 if c.roots[e.root].caps.kind == kind else 1)
    if name == "oldest":
        return Rule(spec, "prefer the oldest modification time", lambda e, c: e.mtime_ns)
    if name == "newest":
        return Rule(spec, "prefer the newest modification time", lambda e, c: -e.mtime_ns)
    if name == "shallowest":
        return Rule(spec, "prefer shallow folders", lambda e, c: e.rel.count("/"))
    if name == "deepest":
        return Rule(spec, "prefer deep folders", lambda e, c: -e.rel.count("/"))
    if name == "shortest-path":
        return Rule(spec, "prefer short paths", lambda e, c: len(e.rel))
    if name == "longest-path":
        return Rule(spec, "prefer long paths", lambda e, c: -len(e.rel))
    raise ConfigError(f"unknown keep rule {spec!r} (see 'duplio rules')")


def parse_rules(specs: Sequence[str] | str | None) -> list[Rule]:
    if specs is None:
        specs = DEFAULT_RULES
    if isinstance(specs, str):
        specs = [s for s in specs.split(",") if s.strip()]
    return [parse_rule(s) for s in specs]


def rank(members: Sequence[FileEntry], rules: Sequence[Rule], ctx: RuleContext) -> list[FileEntry]:
    """Members sorted best-to-keep first."""

    def key(e: FileEntry) -> tuple[object, ...]:
        return (*(r.score(e, ctx) for r in rules), e.root, e.rel)

    return sorted(members, key=key)


RULE_HELP = [
    ("not-copy-named", "Prefer names without copy markers such as 'copy', '(1)', '~' or '.bak'."),
    ("has-hardlinks", "Prefer the copy that hard links already point to."),
    ("root-order", "Prefer locations in the order you listed them."),
    ("prefer-root=N / avoid-root=N", "Prefer (or avoid) location number N."),
    ("prefer-path=GLOB / avoid-path=GLOB", "Prefer (or avoid) relative paths matching a glob."),
    ("prefer-kind=KIND", "Prefer storage kind: local, rotational, removable, network, device."),
    ("oldest / newest", "Prefer the oldest (or newest) modification time."),
    ("shallowest / deepest", "Prefer files higher (or deeper) in the folder tree."),
    ("shortest-path / longest-path", "Prefer shorter (or longer) relative paths."),
]
