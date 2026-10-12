"""Crash and error injection points, for testing that an interrupted run never loses data.

Code that changes something durable calls :func:`crashpoint` just before and just after the change. Normally that
is a single ``None`` check. A test (or ``DUPLIO_CRASH_AT=<n>`` / ``DUPLIO_ERROR_AT=<n>`` in the environment) can make
the n-th point

* **crash**: the process ends at once, with no cleanup of any kind (``os._exit``), like a power cut or ``kill -9`` —
  whatever the operating system already holds is what is left on disk; or
* **fail**: raise ``OSError(EIO)`` there, which models both "the write failed" (a point before a change) and "it
  worked but the acknowledgement was lost", as with a network share that times out (a point after it).

The same scenario is run once to count its points and then once per point, which checks every place a run can be
interrupted rather than the few places someone thought of (the approach of ALICE and CrashMonkey for storage
systems, applied here to Duplio's own steps).
"""

from __future__ import annotations

import errno
import os
import threading
from dataclasses import dataclass, field

CRASH_EXIT = 137  # what a killed process reports (128 + SIGKILL)


@dataclass
class _Plan:
    crash_at: int | None = None
    error_at: int | None = None
    names: list[str] | None = None
    count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_plan: _Plan | None = None


def configure(*, crash_at: int | None = None, error_at: int | None = None, record: list[str] | None = None) -> None:
    """Arm the points. ``record`` collects the name of every point reached, in order."""
    global _plan
    _plan = _Plan(crash_at, error_at, record)


def reset() -> None:
    global _plan
    _plan = None


def hits() -> int:
    return 0 if _plan is None else _plan.count


def crashpoint(name: str, *, can_fail: bool = True) -> None:
    """A place where the run can be killed. ``can_fail=False`` marks a step whose contract is "never raises"."""
    plan = _plan
    if plan is None:
        return
    if threading.current_thread().name.startswith("duplio-lock-heartbeat"):
        return  # a timer-driven thread would make the numbering depend on timing
    with plan.lock:
        plan.count += 1
        number = plan.count
        if plan.names is not None:
            plan.names.append(name)
    if plan.crash_at == number:
        os._exit(CRASH_EXIT)
    if plan.error_at == number and can_fail:
        raise OSError(errno.EIO, f"injected I/O error at {name} (#{number})")


def torn_write(name: str, handle, data: str) -> None:  # type: ignore[no-untyped-def]
    """A write that a crash can cut short: if the process is killed at this point, only the first half of ``data``
    reaches the file (as when power is lost part-way through a write)."""
    plan = _plan
    if plan is None or threading.current_thread().name.startswith("duplio-lock-heartbeat"):
        return
    with plan.lock:
        plan.count += 1
        number = plan.count
        if plan.names is not None:
            plan.names.append(name)
    if plan.crash_at == number:
        handle.write(data[: max(1, len(data) // 2)])
        handle.flush()
        os.fsync(handle.fileno())
        os._exit(CRASH_EXIT)


def _from_environment() -> None:
    crash, error = os.environ.get("DUPLIO_CRASH_AT"), os.environ.get("DUPLIO_ERROR_AT")
    if crash or error:
        configure(crash_at=int(crash) if crash else None, error_at=int(error) if error else None)


_from_environment()
