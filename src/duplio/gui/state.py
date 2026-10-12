"""What the window remembers between runs: recent folders, thumbnail size, window size. Never passwords."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

MAX_RECENT = 12


class GuiState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = loaded

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def recent(self) -> list[str]:
        value = self.data.get("recent", [])
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    def remember(self, location: str) -> None:
        location = location.strip()
        if not location:
            return
        items = [location] + [r for r in self.recent if r != location]
        self.data["recent"] = items[:MAX_RECENT]

    def save(self) -> None:
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
