"""File names that the destination cannot store, and safe replacements.

Copying from Linux to an SMB share, a Windows or exFAT disk, a NAS or an Android
phone fails (or silently mangles names) when a name contains ``:`` ``?`` ``*``
and similar, ends with a dot or space, is a reserved device name such as ``CON``,
or is too long. Duplio checks names *before* copying, reports every problem, and
can rewrite names deterministically. Rewrites never make two files collide
silently: the caller de-duplicates the results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_NAME_MAX_BYTES = 255  # ext4, XFS, btrfs, APFS (UTF-8 bytes)
_NAME_MAX_UNITS = 255  # NTFS, exFAT, SMB (UTF-16 code units)


@dataclass(frozen=True)
class NameIssue:
    component: str
    problem: str

    def __str__(self) -> str:
        return f"{self.component!r}: {self.problem}"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _too_long(component: str, windows: bool) -> bool:
    if windows:
        return _utf16_len(component) > _NAME_MAX_UNITS
    return len(component.encode("utf-8", "surrogateescape")) > _NAME_MAX_BYTES


def component_issues(component: str, *, windows: bool) -> list[str]:
    problems: list[str] = []
    if component in ("", ".", ".."):
        return ["empty or relative component"]
    if "\x00" in component:
        problems.append("contains a NUL character")
    if _too_long(component, windows):
        problems.append("name is longer than 255 characters")
    if windows:
        bad = sorted(set(_WINDOWS_ILLEGAL.findall(component)) - {"\x00"})
        if bad:
            shown = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in bad)
            problems.append(f"contains characters not allowed here ({shown})")
        if component.endswith((" ", ".")):
            problems.append("ends with a space or dot")
        stem = component.split(".", 1)[0].rstrip(" ").upper()
        if stem in _RESERVED:
            problems.append("is a reserved device name")
    return problems


def rel_issues(rel: str, *, windows: bool) -> list[NameIssue]:
    """Every problem with a ``/``-separated relative path on the destination."""
    out: list[NameIssue] = []
    for component in rel.split("/"):
        out.extend(NameIssue(component, p) for p in component_issues(component, windows=windows))
    return out


def _shorten(component: str, windows: bool) -> str:
    """Trim to the limit, keeping the extension when there is one."""
    if not _too_long(component, windows):
        return component
    stem, dot, ext = component.rpartition(".")
    if not dot or not stem or len(ext) > 16:
        stem, ext = component, ""
    else:
        ext = "." + ext
    while stem and _too_long(stem + ext, windows):
        stem = stem[:-1]
    return (stem or "_") + ext


def fix_component(component: str, *, windows: bool) -> str:
    fixed = component.replace("\x00", "_")
    if windows:
        fixed = _WINDOWS_ILLEGAL.sub("_", fixed)
        fixed = fixed.rstrip(" .") or "_"
        if fixed.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
            fixed = "_" + fixed
    return _shorten(fixed, windows)


def fix_rel(rel: str, *, windows: bool) -> str:
    """The relative path with each problematic component replaced; unchanged if it is fine."""
    return "/".join(fix_component(c, windows=windows) if component_issues(c, windows=windows) else c
                    for c in rel.split("/"))
