"""Tell someone how a job went: webhook, command or e-mail.

Targets are plain strings, usable in a profile's ``notify`` list or on the command line:

``webhook:https://hooks.example.com/…``   POST a JSON document (Slack, Discord, ntfy, Home Assistant, …)
``command:/path/to/script``               run a command; the message arrives in environment variables
``email:you@example.com``                 send a message through the SMTP server set in ``[notify.smtp]``

Notification problems never fail a backup: a dead webhook is reported as a warning and the job's own
result stands. Webhook addresses often contain a secret token, so only the host name is ever printed.
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

from duplio import __version__
from duplio.errors import ConfigError
from duplio.util.fmt import iso_now

STATUSES = ("ok", "problems", "failed", "skipped", "cancelled")
NOTIFY_ON = ("failure", "problems", "always", "never")
TIMEOUT_S = 10.0


@dataclass
class Notification:
    title: str
    status: str  # one of STATUSES
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        text = f"[{self.status.upper()}] {self.title}: {self.message}"
        return {
            "text": text,  # Slack and compatible
            "content": text,  # Discord and compatible
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "host": socket.gethostname(),
            "time": iso_now(),
            "duplio": __version__,
            "details": self.details,
        }


def split_target(target: str) -> tuple[str, str]:
    """``'webhook:https://x'`` -> ``('webhook', 'https://x')``; a bare http(s) URL is a webhook."""
    target = target.strip()
    if target.lower().startswith(("http://", "https://")):
        return "webhook", target
    kind, sep, value = target.partition(":")
    kind = kind.lower()
    if not sep or kind not in ("webhook", "command", "email") or not value.strip():
        raise ConfigError(
            f"not a notification target: {target!r} (use webhook:URL, command:PROGRAM or email:ADDRESS)"
        )
    return kind, value.strip()


def validate_target(target: str) -> str:
    kind, value = split_target(target)
    if kind == "webhook":
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"webhook targets need an http:// or https:// address, got {value!r}")
    elif kind == "email" and ("@" not in value or " " in value):
        raise ConfigError(f"not an e-mail address: {value!r}")
    return target


def describe_target(target: str) -> str:
    """A label that is safe to print (no tokens from URLs, no command arguments)."""
    kind, value = split_target(target)
    if kind == "webhook":
        return f"webhook {urlsplit(value).netloc}"
    if kind == "command":
        return f"command {value.split()[0] if value.split() else value}"
    return f"e-mail {value}"


def should_notify(policy: str, status: str) -> bool:
    if policy == "never" or status == "skipped":
        return False
    if policy == "always":
        return True
    if policy == "problems":
        return status in ("problems", "failed", "cancelled")
    return status in ("failed",)  # "failure"


def _post(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": f"duplio/{__version__}"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        response.read(1024)


def _run_command(command: str, note: Notification) -> None:
    env = dict(os.environ)
    env.update(DUPLIO_TITLE=note.title, DUPLIO_STATUS=note.status, DUPLIO_MESSAGE=note.message,
               DUPLIO_HOST=socket.gethostname())
    result = subprocess.run(command, shell=True, env=env, capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"exit status {result.returncode}")


def _email(address: str, note: Notification, smtp: dict[str, Any] | None) -> None:
    if not smtp or not smtp.get("host"):
        raise ConfigError("e-mail notifications need an SMTP server: add a [notify.smtp] section to the configuration")
    message = EmailMessage()
    message["Subject"] = f"[Duplio {note.status}] {note.title}"
    message["From"] = smtp.get("sender") or smtp.get("user") or f"duplio@{socket.gethostname()}"
    message["To"] = address
    lines = [note.message, "", f"Host: {socket.gethostname()}", f"Time: {iso_now()}"]
    lines.extend(f"{k}: {v}" for k, v in note.details.items())
    message.set_content("\n".join(lines))
    port = int(smtp.get("port", 587))
    use_ssl = bool(smtp.get("ssl", port == 465))
    client = (smtplib.SMTP_SSL if use_ssl else smtplib.SMTP)(str(smtp["host"]), port, timeout=TIMEOUT_S)
    with client:
        if not use_ssl and smtp.get("starttls", True):
            client.starttls()
        if smtp.get("user"):
            password = os.environ.get(str(smtp.get("password_env", "DUPLIO_SMTP_PASSWORD")), "")
            client.login(str(smtp["user"]), password)
        client.send_message(message)


def send(targets: list[str], note: Notification, *, smtp: dict[str, Any] | None = None) -> list[tuple[str, str | None]]:
    """Deliver to every target; returns ``(label, error or None)`` per target and never raises."""
    results: list[tuple[str, str | None]] = []
    for target in targets:
        try:
            kind, value = split_target(target)
            label = describe_target(target)
        except ConfigError as exc:
            results.append((target.split(":")[0], str(exc)))
            continue
        try:
            if kind == "webhook":
                _post(value, note.payload())
            elif kind == "command":
                _run_command(value, note)
            else:
                _email(value, note, smtp)
        except Exception as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            results.append((label, f"{type(exc).__name__}: {reason}"))
        else:
            results.append((label, None))
    return results
