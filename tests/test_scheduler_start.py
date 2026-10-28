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

from types import SimpleNamespace

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
