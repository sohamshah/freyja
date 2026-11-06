"""A job resumed after it was stopped for failing gets three tries again.

The stop counted the job's failed runs at the head of its history, and
resuming it left them there: one more failed run made three failed runs
in a row, and the resumed job was stopped again after a single failure.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import CronSchedule, JobPatch, JobRecord, NoopSinkSpec, RunRecord
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

REAL_ERROR = "agent turn failed: the report's sheet is gone"


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose every run fails for a real reason."""
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    svc.fired = 0

    async def fire(_service, job, **kw):
        svc.fired += 1
        run = RunRecord(run_id=f"run_{svc.fired}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), status="failed", error=REAL_ERROR)
        save_run(run)
        await asyncio.sleep(0.01)  # runs are ordered by file mtime
        return run

    import bridge.scheduler.sinks as sinks

    async def deliver_all(*_a, **_kw):
        return []

    monkeypatch.setattr(runtime, "fire_job", fire)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    return svc


async def _run_when_due(service: SchedulerService, job_id: str) -> JobRecord:
    job = load_job(job_id)
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job_id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job_id)


async def _stopped_for_failing(service: SchedulerService) -> JobRecord:
    job = JobRecord(id="sched_report", name="daily report",
                    schedule=CronSchedule(expression="0 6 * * *"), prompt="report",
                    sinks=[NoopSinkSpec()])
    save_job(job)
    for _ in range(3):
        stopped = await _run_when_due(service, job.id)
    assert (stopped.enabled, stopped.status) == (False, "disabled_error")
    return stopped


async def _resume(service: SchedulerService, job_id: str, how: str) -> None:
    if how == "resume_job":
        await service.resume_job(job_id)
    else:
        await service.update_job(job_id, JobPatch(enabled=True))


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_resumed_job_is_not_stopped_by_its_next_failure(service, how):
    job = await _stopped_for_failing(service)
    await _resume(service, job.id, how)

    saved = await _run_when_due(service, job.id)
    saved = await _run_when_due(service, job.id)

    assert (saved.enabled, saved.status) == (True, "active")


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_resumed_job_is_stopped_after_three_more_failures(service, how):
    job = await _stopped_for_failing(service)
    await _resume(service, job.id, how)

    for _ in range(3):
        saved = await _run_when_due(service, job.id)

    assert (saved.enabled, saved.status) == (False, "disabled_error")
