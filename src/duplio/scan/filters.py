"""Include / exclude / protect rules.

Patterns use gitignore-like globs, matched case-insensitively against the
POSIX path relative to the scan root:

* ``*.tmp`` — no slash: matches the name at any depth
* ``Android/data`` — contains a slash: anchored to the scan root
* ``**/cache/**`` — ``**`` spans any number of directories

Three lists, three meanings:

* **exclude** — never scanned (system junk, Duplio's own folders, VCS data).
* **include** — if given, only matching files are scanned.
* **protect** — scanned and usable as the copy to keep, but never removed or
  replaced. Defaults cover app bundles, photo-library packages, Android app
  data and software environments, where deleting a "duplicate" breaks things.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache

# Never scanned. Duplio's own bookkeeping comes first.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".duplio-quarantine",
    ".duplio-versions",
    ".duplio",
    "*.duplio-tmp-*",
    ".duplio-del-*",
    ".duplio-link-*",
    # Operating-system bookkeeping and recycle bins
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Trash-*",
    ".Trashes",
    ".Trash",
    ".Spotlight-V100",
    ".fseventsd",
    ".DocumentRevisions-V100",
    ".TemporaryItems",
    "lost+found",
    ".DS_Store",
    "Thumbs.db",
    "ehthumbs.db",
    "desktop.ini",
    "._*",
    # NAS housekeeping (Synology, QNAP, NetApp)
    "@eaDir",
    "#recycle",
    "#snapshot",
    "@Recycle",
    ".snapshot",
    # Android: thumbnail caches, MediaStore trash and pending writes
    ".thumbnails",
    ".Thumbnails",
    ".trashed-*",
    ".pending-*",
)

# Version-control internals: skipped when deduplicating (removing a "duplicate"
# object inside .git corrupts the repository) but included in backups.
VCS_EXCLUDES: tuple[str, ...] = (".git", ".hg", ".svn", ".bzr", "_darcs", ".fossil")

# Scanned, may be kept, never removed.
DEFAULT_PROTECT: tuple[str, ...] = (
    "**/*.photoslibrary/**",
    "**/*.photolibrary/**",
    "**/*.aplibrary/**",
    "**/*.musiclibrary/**",
    "**/*.tvlibrary/**",
    "**/*.lrdata/**",
    "**/*.lrlibrary/**",
    "**/*.fcpbundle/**",
    "**/*.app/**",
    "**/*.framework/**",
    "**/*.bundle/**",
    "**/*.pkg/**",
    "**/Android/data/**",
    "**/Android/obb/**",
    "**/node_modules/**",
    "**/site-packages/**",
    "**/.venv/**",
    "**/venv/**",
    "**/Program Files/**",
    "**/Program Files (x86)/**",
    "**/AppData/**",
    "**/Library/Application Support/**",
)


@lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-style glob into a regex over POSIX relative paths."""
    pat = pattern.strip().replace("\\", "/")
    if pat.startswith("/"):
        pat = pat[1:]
        anchored = True
    else:
        anchored = "/" in pat.rstrip("/")
    pat = pat.rstrip("/")
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("/**", i) and i + 3 == len(pat):
            out.append("(?:/.*)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pat.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    regex = f"^{body}$" if anchored else f"^(?:.*/)?{body}$"
    return re.compile(regex, re.IGNORECASE | re.DOTALL)


class GlobSet:
    """A set of globs; a path matches if it or any of its ancestors matches."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = tuple(p for p in patterns if p and p.strip())
        self._compiled = [compile_glob(p) for p in self.patterns]

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def matches(self, rel: str) -> bool:
        return any(rx.match(rel) for rx in self._compiled)

    def matches_path_or_parent(self, rel: str) -> bool:
        if not self._compiled:
            return False
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            if self.matches("/".join(parts[:i])):
                return True
        return False


TYPE_GROUPS: dict[str, frozenset[str]] = {
    "images": frozenset({"jpg", "jpeg", "jpe", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif", "svg",
                         "dng", "raw", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef", "psd", "ico"}),
    "video": frozenset({"mp4", "m4v", "mov", "3gp", "3g2", "mkv", "webm", "avi", "mts", "m2ts", "wmv", "flv", "mpg",
                        "mpeg", "ts", "vob"}),
    "audio": frozenset({"mp3", "m4a", "aac", "ogg", "opus", "flac", "wav", "wma", "aiff", "aif", "alac", "mid", "midi"}),
    "documents": frozenset({"pdf", "doc", "docx", "odt", "rtf", "txt", "md", "xls", "xlsx", "ods", "csv", "ppt",
                            "pptx", "odp", "epub", "mobi", "pages", "numbers", "key", "tex"}),
    "archives": frozenset({"zip", "7z", "rar", "tar", "gz", "bz2", "xz", "zst", "tgz", "iso", "dmg", "img"}),
    "code": frozenset({"py", "js", "ts", "tsx", "jsx", "java", "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php",
                       "swift", "kt", "sh", "bash", "ps1", "sql", "html", "css", "scss", "json", "yaml", "yml", "toml",
                       "xml"}),
}  # fmt: skip


def resolve_types(kinds: Iterable[str]) -> frozenset[str]:
    """Extensions for names like ``images`` or ``.pdf``; raises ValueError for anything unknown."""
    out: set[str] = set()
    for kind in kinds:
        key = kind.strip().lower()
        if not key:
            continue
        if key in TYPE_GROUPS:
            out |= TYPE_GROUPS[key]
        elif key.lstrip(".").isalnum():
            out.add(key.lstrip("."))
        else:
            raise ValueError(f"unknown file type {kind!r}; use {', '.join(TYPE_GROUPS)} or an extension such as .pdf")
    return frozenset(out)


@dataclass
class ScanFilter:
    """Implements :class:`duplio.vfs.base.WalkFilter` plus protection checks."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    protect: list[str] = field(default_factory=list)
    min_size: int = 1
    max_size: int | None = None
    skip_hidden: bool = False
    use_default_excludes: bool = True
    use_default_protect: bool = True
    exclude_vcs: bool = True
    types: list[str] = field(default_factory=list)  # kinds ("images") or extensions (".pdf"); empty = everything
    modified_after_ns: int | None = None  # only files modified at or after this instant
    modified_before_ns: int | None = None  # only files modified at or before this instant

    def __post_init__(self) -> None:
        excludes = list(self.exclude)
        if self.use_default_excludes:
            excludes = list(DEFAULT_EXCLUDES) + excludes
        if self.exclude_vcs:
            excludes = list(VCS_EXCLUDES) + excludes
        protects = list(self.protect)
        if self.use_default_protect:
            protects = list(DEFAULT_PROTECT) + protects
        self._exclude = GlobSet(excludes)
        self._include = GlobSet(self.include)
        self._protect = GlobSet(protects)
        self._types = resolve_types(self.types)

    # WalkFilter protocol
    def skip_dir(self, rel: str, name: str) -> bool:
        if self.skip_hidden and name.startswith("."):
            return True
        return self._exclude.matches(rel)

    def skip_file(self, rel: str, name: str, size: int) -> bool:
        if size < self.min_size:
            return True
        if self.max_size is not None and size > self.max_size:
            return True
        if self.skip_hidden and name.startswith("."):
            return True
        if self._exclude.matches(rel):
            return True
        if self._types and extension(name) not in self._types:
            return True
        return bool(self._include) and not self._include.matches(rel)

    @property
    def uses_time(self) -> bool:
        return self.modified_after_ns is not None or self.modified_before_ns is not None

    def skip_by_time(self, mtime_ns: int) -> bool:
        """Age filter; applied after the walk because the walk only offers name and size."""
        if self.modified_after_ns is not None and mtime_ns < self.modified_after_ns:
            return True
        return self.modified_before_ns is not None and mtime_ns > self.modified_before_ns

    def is_protected(self, rel: str) -> bool:
        return self._protect.matches_path_or_parent(rel)

    def to_dict(self) -> dict[str, object]:
        return {
            "include": list(self.include),
            "exclude": list(self.exclude),
            "protect": list(self.protect),
            "min_size": self.min_size,
            "max_size": self.max_size,
            "skip_hidden": self.skip_hidden,
            "use_default_excludes": self.use_default_excludes,
            "use_default_protect": self.use_default_protect,
            "exclude_vcs": self.exclude_vcs,
            "types": list(self.types),
            "modified_after_ns": self.modified_after_ns,
            "modified_before_ns": self.modified_before_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ScanFilter:
        return cls(
            include=list(data.get("include", [])),  # type: ignore[call-overload]
            exclude=list(data.get("exclude", [])),  # type: ignore[call-overload]
            protect=list(data.get("protect", [])),  # type: ignore[call-overload]
            min_size=int(data.get("min_size", 1)),  # type: ignore[call-overload]
            max_size=data.get("max_size"),  # type: ignore[arg-type]
            skip_hidden=bool(data.get("skip_hidden", False)),
            use_default_excludes=bool(data.get("use_default_excludes", True)),
            use_default_protect=bool(data.get("use_default_protect", True)),
            exclude_vcs=bool(data.get("exclude_vcs", True)),
            types=list(data.get("types", [])),  # type: ignore[call-overload]
            modified_after_ns=data.get("modified_after_ns"),  # type: ignore[arg-type]
            modified_before_ns=data.get("modified_before_ns"),  # type: ignore[arg-type]
        )

    @classmethod
    def for_backup(cls, *, include: list[str] | None = None, exclude: list[str] | None = None) -> ScanFilter:
        """Backups keep everything except junk: empty files and VCS data included."""
        return cls(
            include=list(include or []),
            exclude=list(exclude or []),
            min_size=0,
            use_default_protect=False,
            exclude_vcs=False,
        )


MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {
        # photos
        "jpg", "jpeg", "jpe", "png", "gif", "webp", "heic", "heif", "avif", "bmp", "tif", "tiff",
        "dng", "raw", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef",
        # videos
        "mp4", "m4v", "mov", "3gp", "3g2", "mkv", "webm", "avi", "mts", "m2ts", "wmv",
        # motion-photo sidecars and live photos
        "mp", "livp",
    }
)  # fmt: skip

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "jpe", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif"}
)


def extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 else ""
