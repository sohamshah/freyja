"""A scheduler that started without the owner lock takes it when it frees.

Only the owner-lock holder runs the tick loop. A process that lost the
race at start() stayed CRUD-only for life: when the owner exited and
nothing restarted it (the gateway LaunchAgent unloaded, the desktop
bridge that owned the lock quit), no process fired jobs until one of
them restarted.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
import bridge.scheduler.service as service_mod
from bridge.scheduler.models import (
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import FileLock, load_job, owner_lock_path, save_job
from bridge.scheduler.service import SchedulerService


async def test_a_crud_only_scheduler_takes_over_when_the_owner_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.05, raising=False)
    fired: list[str] = []

    async def fake_fire(_service, job, **_kw):
        fired.append(job.id)
        return RunRecord(run_id="run_1", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    owner = FileLock(owner_lock_path())
    assert owner.acquire()
    service = SchedulerService(SimpleNamespace())
    await service.start()
    assert not service._is_owner

    due = time.time() + 0.3
    job = JobRecord(
        id="sched_due",
        name="reminder",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(due, timezone.utc).isoformat()),
        prompt="ping",
        sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = due
    save_job(job)
    owner.release()

    for _ in range(60):
        if fired:
            break
        await asyncio.sleep(0.05)

    assert service._is_owner
    assert fired == ["sched_due"]
    await service.stop()
    # stop() also ends the retry, and gives the lock back.
    peer = FileLock(owner_lock_path())
    assert peer.acquire()
    peer.release()


async def test_stop_ends_the_retry_of_a_crud_only_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.05, raising=False)
    owner = FileLock(owner_lock_path())
    assert owner.acquire()
    service = SchedulerService(SimpleNamespace())
    await service.start()
    await service.stop()
    owner.release()

    await asyncio.sleep(0.2)

    assert not service._is_owner
    peer = FileLock(owner_lock_path())
    assert peer.acquire()
    peer.release()


@pytest.mark.parametrize(
    "schedule",
    [
        pytest.param(IntervalSchedule(seconds=3600), id="interval"),
        pytest.param(SelfPacedSchedule(min_delay_seconds=60, max_delay_seconds=3600), id="self-paced"),
    ],
)
async def test_a_new_owner_keeps_the_next_fire_of_a_job_not_yet_due(tmp_path, monkeypatch, schedule):
    # The owner boot recomputed every job's next fire from now: an hourly
    # job due in 6 minutes moved to an hour from now, and a self-paced job
    # lost the delay its agent chose. Ownership changes hands on every
    # restart and takeover, so such a job could be pushed out forever.
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    now = time.time()
    job = JobRecord(id="sched_not_due", name="hourly check", schedule=schedule,
                    prompt="check", sinks=[NoopSinkSpec()])
    job.last_fire_at = now - 3240
    job.next_fire_at = now + 360
    save_job(job)
    service = SchedulerService(SimpleNamespace())

    await service.start()

    assert service._is_owner
    assert load_job(job.id).next_fire_at == job.next_fire_at
    await service.stop()
