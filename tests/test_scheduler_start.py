"""SchedulerService.start() runs once per process.

The scheduler daemon started its scheduler twice: GatewayDaemon.start()
starts it, and the headless boot started it again. The second start()
opened owner.lock again and tried to flock it. A flock belongs to the
open file description, so the process's own first lock refused the
second one: the service dropped to CRUD-only and its tick loop stopped,
while the first, now unreferenced descriptor kept the lock from every
other process. Whenever the daemon won the lock, no process fired jobs
until it restarted.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import bridge.scheduler.service as service_mod
from bridge.scheduler.persistence import FileLock, owner_lock_path
from bridge.scheduler.service import SchedulerService


async def test_a_second_start_keeps_the_owner_and_its_tick_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    service = SchedulerService(SimpleNamespace())
    await service.start()
    assert service._is_owner
    loop_task = service._loop_task

    await service.start()

    assert service._is_owner
    assert service._loop_task is loop_task
    assert not loop_task.done()

    await service.stop()
    # stop() gives the lock back, so a peer can take over the tick.
    peer = FileLock(owner_lock_path())
    assert peer.acquire()
    peer.release()


async def test_start_after_stop_runs_the_tick_loop_again(tmp_path, monkeypatch):
    # stop() set _stopped and start() never cleared it, so a restarted
    # service's tick loop saw it and exited before its first tick.
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(service_mod, "_TICK_CAP_SECONDS", 0.02)
    service = SchedulerService(SimpleNamespace())
    await service.start()
    await service.stop()
    ticks = 0
    real_tick = service._tick

    async def counting_tick() -> None:
        nonlocal ticks
        ticks += 1
        await real_tick()

    monkeypatch.setattr(service, "_tick", counting_tick)

    await service.start()
    deadline = time.monotonic() + 3
    while ticks < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)

    assert service._is_owner
    assert not service._loop_task.done()
    assert ticks >= 1
    await service.stop()
