"""Bandwidth limiting, so a backup does not saturate a home network or a NAS.

A token bucket shared by every worker thread: ``limiter.acquire(n)`` blocks just
long enough to keep the long-run rate at or below the limit. Waiting is
cancellable and polls the cancel token, so Ctrl-C is never held up by a sleep.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from duplio.errors import ConfigError
from duplio.events import CancelToken
from duplio.util.fmt import parse_size

_RATE_RE = re.compile(r"^\s*(?P<size>[0-9.]+\s*[a-zA-Z]*)\s*(?:/\s*s(?:ec)?)?\s*$", re.IGNORECASE)


def parse_rate(text: str | None) -> int | None:
    """``'5MB/s'``, ``'800k'``, ``'2 MiB/s'`` -> bytes per second; ``None``/``'0'``/``'off'`` -> unlimited."""
    if text is None:
        return None
    cleaned = text.strip().lower()
    if cleaned in ("", "0", "off", "none", "unlimited"):
        return None
    match = _RATE_RE.match(cleaned)
    if not match:
        raise ConfigError(f"not a bandwidth limit: {text!r} (examples: 5MB/s, 800k, 2MiB/s)")
    try:
        rate = parse_size(match.group("size").replace(" ", ""))
    except ValueError as exc:
        raise ConfigError(f"not a bandwidth limit: {text!r}") from exc
    return rate or None


class RateLimiter:
    def __init__(
        self,
        bytes_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if bytes_per_second <= 0:
            raise ConfigError("the bandwidth limit must be positive")
        self.rate = float(bytes_per_second)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._available = self.rate  # a one-second burst is allowed
        self._last = clock()

    def acquire(self, nbytes: int, cancel: CancelToken | None = None) -> None:
        """Account for ``nbytes`` about to be transferred; sleeps while over budget."""
        if nbytes <= 0:
            return
        with self._lock:
            now = self._clock()
            self._available = min(self.rate, self._available + (now - self._last) * self.rate)
            self._last = now
            self._available -= nbytes
            deficit = -self._available
        if deficit <= 0:
            return
        remaining = deficit / self.rate
        while remaining > 0:
            if cancel is not None:
                cancel.check()
            step = min(remaining, 0.25)
            self._sleep(step)
            remaining -= step


def limiter_for(rate: int | None) -> RateLimiter | None:
    return RateLimiter(rate) if rate else None
