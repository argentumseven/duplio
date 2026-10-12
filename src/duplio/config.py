"""User configuration and saved job profiles (TOML).

Example ``config.toml``::

    [general]
    action = "quarantine"
    keep = ["has-hardlinks", "not-copy-named", "root-order", "oldest", "shortest-path"]
    verify = "hash"
    cache = true

    [scan]
    exclude = ["*.tmp"]
    protect = ["**/Lightroom/**"]
    skip_hidden = false

    [profiles.phone-photos]
    type = "ingest"
    source = "adb:/sdcard/DCIM"
    dest = "~/Pictures/Library"

    [profiles.documents-to-nas]
    type = "backup"
    source = "~/Documents"
    repo = "smb://me@nas/backups/duplio"
    keep_daily = 7
    keep_weekly = 4
    keep_monthly = 12
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duplio.errors import ConfigError
from duplio.plan.model import ActionType
from duplio.plan.rules import DEFAULT_RULES, parse_rules
from duplio.util.appdirs import AppDirs, get_app_dirs
from duplio.util.atomic import atomic_write_text
from duplio.util.fmt import parse_size

PROFILE_TYPES = ("dedupe", "compare", "backup", "mirror", "ingest")

PROFILE_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "dedupe": {"roots": list, "reference": list, "mode": str, "action": str, "keep": list, "verify": str},
    "compare": {"source": str, "against": list, "copy_missing_to": str},
    "backup": {
        "source": str, "repo": str, "label": str, "tags": list, "exclude": list,
        "keep_last": int, "keep_hourly": int, "keep_daily": int, "keep_weekly": int,
        "keep_monthly": int, "keep_yearly": int, "keep_within": str, "prune": bool,
    },
    "mirror": {"source": str, "dest": str, "delete": bool, "checksum": bool, "keep_versions": str, "exclude": list},
    "ingest": {"source": str, "dest": str, "layout": str, "media_only": bool, "free_source": bool, "device": str},
}  # fmt: skip


@dataclass
class Settings:
    action: ActionType = ActionType.QUARANTINE
    keep: list[str] = field(default_factory=lambda: list(DEFAULT_RULES))
    verify: str = "hash"
    cache: bool = True
    exclude: list[str] = field(default_factory=list)
    protect: list[str] = field(default_factory=list)
    min_size: int = 1
    skip_hidden: bool = False
    one_file_system: bool = True
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None


def _expect(value: Any, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        name = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise ConfigError(f"{where} must be a {name}")
    if isinstance(value, list) and not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def validate_profile(name: str, profile: dict[str, Any]) -> dict[str, Any]:
    kind = profile.get("type")
    if kind not in PROFILE_TYPES:
        raise ConfigError(f"profile {name!r}: type must be one of {', '.join(PROFILE_TYPES)}")
    allowed = PROFILE_FIELDS[kind]
    for key, value in profile.items():
        if key == "type":
            continue
        if key not in allowed:
            raise ConfigError(f"profile {name!r}: unknown setting {key!r} for a {kind} profile")
        _expect(value, allowed[key], f"profile {name!r}: {key}")
    required = {"dedupe": ["roots"], "compare": ["source", "against"], "backup": ["source", "repo"],
                "mirror": ["source", "dest"], "ingest": ["source", "dest"]}[kind]  # fmt: skip
    for key in required:
        if key not in profile:
            raise ConfigError(f"profile {name!r}: missing required setting {key!r}")
    if kind == "dedupe":
        if "action" in profile:
            try:
                ActionType(profile["action"])
            except ValueError as exc:
                raise ConfigError(f"profile {name!r}: unknown action {profile['action']!r}") from exc
        if "keep" in profile:
            parse_rules(profile["keep"])
    return profile


def load_settings(dirs: AppDirs | None = None, path: Path | None = None) -> Settings:
    dirs = dirs or get_app_dirs()
    path = path or dirs.config_file
    settings = Settings(path=path)
    if not path.exists():
        return settings
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    general = data.get("general", {})
    scan = data.get("scan", {})
    if "action" in general:
        try:
            settings.action = ActionType(_expect(general["action"], str, "general.action"))
        except ValueError as exc:
            raise ConfigError(f"general.action: unknown action {general['action']!r}") from exc
    if "keep" in general:
        settings.keep = list(_expect(general["keep"], list, "general.keep"))
        parse_rules(settings.keep)
    if "verify" in general:
        settings.verify = _expect(general["verify"], str, "general.verify")
        if settings.verify not in ("hash", "bytes"):
            raise ConfigError("general.verify must be 'hash' or 'bytes'")
    if "cache" in general:
        settings.cache = bool(_expect(general["cache"], bool, "general.cache"))
    settings.exclude = list(_expect(scan.get("exclude", []), list, "scan.exclude"))
    settings.protect = list(_expect(scan.get("protect", []), list, "scan.protect"))
    if "min_size" in scan:
        raw = scan["min_size"]
        settings.min_size = raw if isinstance(raw, int) and not isinstance(raw, bool) else parse_size(str(raw))
    settings.skip_hidden = bool(scan.get("skip_hidden", False))
    settings.one_file_system = bool(scan.get("one_file_system", True))
    for name, profile in data.get("profiles", {}).items():
        if not isinstance(profile, dict):
            raise ConfigError(f"profile {name!r} must be a table")
        settings.profiles[name] = validate_profile(name, dict(profile))
    return settings


# ---------------------------------------------------------------------------
# Minimal TOML writer (the standard library only reads TOML)
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to TOML")


def _toml_key(key: str) -> str:
    return key if key and all(c.isalnum() or c in "-_" for c in key) else _toml_value(key)


def dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def table(prefix: str, body: dict[str, Any]) -> None:
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        if prefix and (scalars or not tables):
            lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        if prefix and scalars:
            lines.append("")
        for key, value in tables.items():
            table(f"{prefix}.{_toml_key(key)}" if prefix else _toml_key(key), value)

    table("", data)
    return "\n".join(lines).rstrip() + "\n"


def save_settings(settings: Settings, dirs: AppDirs | None = None) -> Path:
    dirs = dirs or get_app_dirs()
    path = settings.path or dirs.config_file
    data: dict[str, Any] = {
        "general": {
            "action": settings.action.value,
            "keep": settings.keep,
            "verify": settings.verify,
            "cache": settings.cache,
        },
        "scan": {
            "exclude": settings.exclude,
            "protect": settings.protect,
            "min_size": settings.min_size,
            "skip_hidden": settings.skip_hidden,
            "one_file_system": settings.one_file_system,
        },
        "profiles": settings.profiles,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "# Duplio configuration — see 'duplio config --help'\n\n" + dump_toml(data))
    return path
