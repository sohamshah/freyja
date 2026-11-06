"""A due job that cannot fire yet does not make the tick loop spin.

The loop slept until the earliest next fire, at least 0.05 s. A job that
stays due because it cannot fire yet (its lock is held while its last
run goes on) kept that earliest time in the past, so the loop woke
twenty times a second, parsing every job file twice each time, until
the run ended.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from bridge.scheduler.models import IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import FileLock, job_lock_path, save_job
from bridge.scheduler.service import SchedulerService


async def test_a_due_job_that_cannot_fire_is_looked_at_once_a_second():
    job = JobRecord(id="sched_busy", name="digest", schedule=IntervalSchedule(seconds=60),
                    prompt="digest", sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() - 1
    save_job(job)
    held = FileLock(job_lock_path(job.id))
    assert held.acquire()
    service = SchedulerService(SimpleNamespace())
    ticks = 0
    tick = service._tick

    async def counted_tick():
        nonlocal ticks
        ticks += 1
        await tick()

    service._tick = counted_tick
    try:
        await service.start()
        await asyncio.sleep(1.5)
    finally:
        await service.stop()
        held.release()

    assert ticks <= 3
