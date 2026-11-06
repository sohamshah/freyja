"""A job's misfire_policy decides what happens to slots it missed.

Nothing read misfire_policy. When a process took the owner lock it
recomputed every job's next fire from now, so a slot that came due while
no scheduler ran (the Mac off, every process restarting) was dropped
whatever the policy said, and a late tick (after sleep) fired once
whatever the policy said.

  · skip (the default) keeps that behavior, including the one run after
    sleep, so existing jobs behave as before;
  · fire_once fires one run for the missed slots;
  · fire_all fires one run per missed slot.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    CronSchedule,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job
from bridge.scheduler.service import SchedulerService

HOUR = 3600


@pytest.fixture
def fired(tmp_path, monkeypatch) -> list[str]:
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    out: list[str] = []

    async def fake_fire(_service, job):
        out.append(job.id)
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


def _missed_daily(policy: str) -> JobRecord:
    """A daily 09:00 job whose last slot passed an hour ago, unfired."""
    now = time.time()
    job = JobRecord(
        id=f"sched_{policy}",
        name="daily digest",
        schedule=CronSchedule(expression="0 9 * * *"),
        prompt="digest",
        sinks=[NoopSinkSpec()],
        misfire_policy=policy,
    )
    job.next_fire_at = now - HOUR
    job.last_fire_at = now - HOUR - 24 * HOUR
    save_job(job)
    return job


async def _run_owner(seconds: float) -> None:
    service = SchedulerService(SimpleNamespace())
    await service.start()
    assert service._is_owner
    await asyncio.sleep(seconds)
    await service.stop()


async def test_skip_drops_a_slot_missed_while_no_scheduler_ran(fired):
    job = _missed_daily("skip")

    await _run_owner(0.5)

    assert fired == []
    assert load_job(job.id).next_fire_at > time.time()


async def test_fire_once_fires_a_slot_missed_while_no_scheduler_ran(fired):
    job = _missed_daily("fire_once")

    await _run_owner(0.5)

    assert fired == [job.id]
    assert load_job(job.id).next_fire_at > time.time()


def _missed_hourly(policy: str) -> JobRecord:
    """An hourly job whose slots at now-2.5h, now-1.5h and now-0.5h all
    passed unfired."""
    now = time.time()
    job = JobRecord(
        id=f"sched_hourly_{policy}",
        name="hourly check",
        schedule=IntervalSchedule(seconds=HOUR),
        prompt="check",
        sinks=[NoopSinkSpec()],
        misfire_policy=policy,
    )
    job.last_fire_at = now - 3.5 * HOUR
    job.next_fire_at = job.last_fire_at + HOUR
    save_job(job)
    return job


async def test_fire_all_fires_each_slot_missed_while_no_scheduler_ran(fired):
    job = _missed_hourly("fire_all")

    await _run_owner(1.0)

    assert fired == [job.id] * 3
    assert load_job(job.id).next_fire_at == pytest.approx(time.time() + 0.5 * HOUR, abs=2)


@pytest.mark.parametrize(("policy", "runs"), [("skip", 1), ("fire_once", 1), ("fire_all", 3)])
async def test_a_late_tick_after_sleep_follows_the_policy(fired, policy, runs):
    # The owner was running, but the Mac slept through three slots: the
    # tick finds the job long overdue.
    service = SchedulerService(SimpleNamespace())
    await service.start()
    job = _missed_hourly(policy)

    await asyncio.sleep(1.0)
    await service.stop()

    assert fired == [job.id] * runs
    assert load_job(job.id).next_fire_at > time.time()
