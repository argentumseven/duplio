"""Commands and notifications around a saved job (``duplio run``)."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from rich.text import Text

from duplio.cli.context import AppContext
from duplio.cli.render import t
from duplio.notify import Notification, send, should_notify
from duplio.util.text import printable

HOOK_TIMEOUT_S = 6 * 3600


def status_for(code: int) -> str:
    """Map a command's exit status to the words used in hooks and notifications."""
    return {0: "ok", 1: "problems", 3: "skipped", 130: "cancelled"}.get(code, "failed")


def run_hook_commands(app: AppContext, label: str, commands: list[str], env: dict[str, str]) -> int:
    """Run shell commands in order; stop at the first failure and return its exit status (0 if all passed)."""
    merged = {**os.environ, **env}
    for command in commands:
        app.console.print(Text.assemble((f"{label}: ", "dim"), t(command)))
        try:
            result = subprocess.run(command, shell=True, env=merged, capture_output=True, text=True,
                                    timeout=HOOK_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            app.console.print(Text(f"{label} command timed out: {printable(command)}", style="red"))
            return 124
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            app.console.print(Text(f"{label} command failed (exit {result.returncode}): {printable(command)}", style="red"))
            for line in tail:
                app.console.print(t(f"  {line}", "dim"))
            return result.returncode
    return 0


def notify_outcome(app: AppContext, name: str, prof: dict[str, Any], status: str, code: int) -> None:
    targets = list(prof.get("notify", []))
    if not targets or not should_notify(str(prof.get("notify_on", "failure")), status):
        return
    messages = {
        "ok": "finished without problems",
        "problems": "finished, but some files had problems (see the log)",
        "failed": f"failed (exit status {code})",
        "cancelled": "was cancelled",
        "skipped": "was skipped",
    }
    note = Notification(title=f"Duplio: {name}", status=status, message=f"The job '{name}' {messages[status]}.",
                        details={"profile": name, "type": prof.get("type", ""), "exit_status": code})
    for label, error in send(targets, note, smtp=app.settings.notify_smtp):
        if error:
            app.console.print(Text(f"Could not notify {label}: {error}", style="yellow"))
