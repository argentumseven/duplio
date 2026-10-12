"""Device-aware parallel I/O.

A global thread pool is the wrong tool when one root is an NVMe drive and the
other a phone on MTP: the phone tolerates exactly one request at a time, while
the SSD wants several. Jobs are therefore queued per *device* and each device
gets its own small set of workers sized from its capabilities. All devices
progress concurrently and none can starve another.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from duplio.events import CancelToken


@dataclass
class Job:
    device: str
    limit: int
    run: Callable[[], None]


def run_jobs(jobs: list[Job], *, cancel: CancelToken | None = None) -> None:
    """Run jobs with per-device concurrency limits; re-raise the first crash."""
    if not jobs:
        return
    queues: dict[str, list[Job]] = defaultdict(list)
    limits: dict[str, int] = {}
    for job in jobs:
        queues[job.device].append(job)
        limits[job.device] = max(1, min(limits.get(job.device, job.limit), job.limit))

    lock = threading.Lock()
    positions = dict.fromkeys(queues, 0)
    failure: list[BaseException] = []

    def consume(device: str) -> None:
        queue = queues[device]
        while True:
            if failure or (cancel is not None and cancel.cancelled):
                return
            with lock:
                pos = positions[device]
                if pos >= len(queue):
                    return
                positions[device] = pos + 1
            try:
                queue[pos].run()
            except BaseException as exc:  # crash in worker: stop everything
                with lock:
                    failure.append(exc)
                return

    threads = []
    for device, queue in queues.items():
        for n in range(min(limits[device], len(queue))):
            t = threading.Thread(target=consume, args=(device,), name=f"duplio-io-{device}-{n}", daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    if failure:
        raise failure[0]
    if cancel is not None:
        cancel.check()
