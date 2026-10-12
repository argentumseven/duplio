"""Entry point: ``duplio`` (console script) and ``python -m duplio``."""

from __future__ import annotations

import sys

from duplio.cli import (  # noqa: F401  (register commands)
    cmd_backup,
    cmd_diff,
    cmd_drive,
    cmd_merge,
    cmd_misc,
    cmd_plan,
    cmd_scan,
)
from duplio.cli.app import cli


def main(argv: list[str] | None = None) -> None:
    try:
        cli.main(args=argv, prog_name="duplio", standalone_mode=True)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
