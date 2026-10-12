"""Safe handling of file names that are not well-behaved text.

Real file names are arbitrary bytes on Linux/macOS and arbitrary UTF-16 on
Windows and SMB. Python represents undecodable bytes as lone surrogates
(``'caf\\udce9.txt'`` for latin-1 ``café.txt``). Those strings are valid paths
but cannot be encoded as UTF-8, which breaks JSON files, SQLite, terminals and
logs. This module keeps them *lossless* where data is stored and *harmless*
where data is displayed:

* :func:`json_dumps` always escapes (``\\udce9``), so persisted files are plain
  ASCII and decode back to the exact same path.
* :func:`fs_bytes` / :func:`fs_str` give a reversible byte form for databases.
* :func:`printable` makes any name safe to show: undecodable bytes become
  ``\\xe9``, control characters (including terminal escape sequences) and bidi
  overrides become visible escapes, so a hostile name can neither crash the
  display nor rewrite your terminal, nor visually spoof another name.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# Characters that must never reach a terminal or report verbatim.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f"  # C0 and C1 controls, including ESC
    "\ud800-\udfff"  # lone surrogates (undecodable bytes)
    "  "  # line / paragraph separators
    "؜‎‏‪-‮⁦-⁩]"  # bidirectional controls (name spoofing)
)
_UNSAFE_KEEP_NEWLINE = re.compile(_UNSAFE.pattern.replace("\x00-\x1f", "\x00-\x09\x0b-\x1f"))


def _escape(match: re.Match[str]) -> str:
    ch = match.group()
    code = ord(ch)
    if 0xDC80 <= code <= 0xDCFF:  # a byte that was not valid UTF-8
        return f"\\x{code - 0xDC00:02x}"
    if code == 0x0A:
        return "\\n"
    if code == 0x0D:
        return "\\r"
    if code == 0x09:
        return "\\t"
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}"


def printable(text: str, *, keep_newlines: bool = False) -> str:
    """``text`` with every character that is unsafe to display made visible.

    Ordinary text, including emoji, CJK and combining marks, is untouched. With
    ``keep_newlines`` real newlines survive, for multi-line messages.
    """
    if text.isprintable() and text.isascii():
        return text
    pattern = _UNSAFE_KEEP_NEWLINE if keep_newlines else _UNSAFE
    return pattern.sub(_escape, text)


def has_unsafe(text: str) -> bool:
    return bool(_UNSAFE.search(text))


def fs_bytes(text: str) -> bytes:
    """Reversible bytes for a path (undecodable bytes round-trip exactly)."""
    return text.encode("utf-8", "surrogateescape")


def fs_str(data: bytes | str) -> str:
    return data if isinstance(data, str) else data.decode("utf-8", "surrogateescape")


def json_dumps(obj: Any, **kwargs: Any) -> str:
    """JSON that survives any file name: pure ASCII, lone surrogates escaped."""
    kwargs["ensure_ascii"] = True
    return json.dumps(obj, **kwargs)


def nfc(text: str) -> str:
    """Canonical Unicode composition, for comparing names across file systems.

    macOS, some SMB servers and many archives store ``é`` decomposed (``e`` +
    U+0301); Linux and Windows usually store it composed. They are the same
    name to a human and must be to a comparison.
    """
    return unicodedata.normalize("NFC", text)


def name_key(name: str, *, fold: bool = False) -> str:
    """Comparison key for a file name: NFC, optionally case-folded."""
    key = nfc(name)
    return key.casefold() if fold else key


def temp_name(name: str, suffix: str, *, max_bytes: int = 96) -> str:
    """A hidden temporary sibling name that cannot exceed file-system limits.

    Most file systems cap a single name at 255 bytes, so ``.<name>.tmp-xyz`` for
    a long ``name`` would fail. The temp name only needs to be unique and
    recognisable, so the original is truncated on a character boundary.
    """
    raw = fs_bytes(name)
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        name = raw.decode("utf-8", "ignore")
    return f".{name}{suffix}"
