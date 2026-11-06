"""Each change a scheduler makes by itself to a job's next fire or state
sends a job event (scheduler_job_<what>), as a job made, changed, paused,
resumed or stopped does.

The dashboard and the sidebar read the job list back on these events
only. A slot a daily budget skipped moved the job to the next day, a
failed run's retry moved its next fire minutes ahead, and a boot ran again
a job only passing failures had stopped, all with no job event: the
dashboard went on showing the next fire or the state from before. A run's
own end event comes before its retry is set.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    BudgetSpec,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RetryPolicy,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

REAL_ERROR = "agent turn failed: the report's sheet is gone"
CONNECTION_ERROR = "the turn failed before the model replied — unknown: Connection error."


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose fires fail with ``service.error``, and that keeps
    the events it sends."""
    import bridge.scheduler.sinks as sinks

    svc = SchedulerService(SimpleNamespace())
    svc.told = []
    svc.error = REAL_ERROR
    monkeypatch.setattr(svc, "emit_event", lambda **kw: svc.told.append(kw))

    async def fire(_service, job, **kw):
        run = RunRecord(run_id=f"run_{time.time_ns()}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), status="failed", error=svc.error)
        run.attempt = kw.get("attempt", 0)
        save_run(run)
        await asyncio.sleep(0.01)  # runs are ordered by file mtime
        return run

    async def deliver_all(*_a, **_kw):
        return []

    monkeypatch.setattr(runtime, "fire_job", fire)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    return svc


def _job(**fields) -> JobRecord:
    job = JobRecord(id="sched_report", name="daily report", prompt="report",
                    schedule=IntervalSchedule(seconds=86400), sinks=[NoopSinkSpec()],
                    **fields)
    job.next_fire_at = time.time() - 1
    save_job(job)
    return job


async def _when_due(service: SchedulerService, job: JobRecord) -> JobRecord:
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job.id)


def _job_events(service: SchedulerService, job: JobRecord) -> list[dict]:
    return [
        e for e in service.told
        if e["subtype"].startswith("scheduler_job_") and e["details"].get("jobId") == job.id
    ]


async def test_a_slot_the_daily_budget_skips_sends_a_job_event(service):
    job = _job(budget=BudgetSpec(max_cost_usd_per_day=0.60, on_exceeded="abort_run"))
    save_run(RunRecord(run_id="run_spent", job_id=job.id, job_name=job.name,
                       started_at=time.time() - 600, finished_at=time.time() - 595,
                       status="succeeded", cost_usd=0.70))

    saved = await _when_due(service, job)

    (told,) = _job_events(service, job)
    assert told["subtype"] == "scheduler_job_skipped_over_budget"
    assert told["details"]["nextFireAt"] == saved.next_fire_at


async def test_a_retry_under_the_retry_policy_sends_a_job_event_after_the_run_ends(service):
    job = _job(retry_policy=RetryPolicy(max_retries=1, backoff_seconds=600))

    saved = await _when_due(service, job)

    (told,) = _job_events(service, job)
    assert told["subtype"] == "scheduler_job_retrying"
    assert told["details"]["nextFireAt"] == saved.next_fire_at == pytest.approx(time.time() + 600, abs=5)


async def test_a_retry_after_a_passing_failure_sends_a_job_event(service):
    service.error = CONNECTION_ERROR
    job = _job()

    saved = await _when_due(service, job)

    (told,) = _job_events(service, job)
    assert told["subtype"] == "scheduler_job_retrying"
    assert told["details"]["nextFireAt"] == saved.next_fire_at == pytest.approx(time.time() + 120, abs=5)


async def test_a_failed_run_that_is_not_retried_sends_no_job_event(service):
    job = _job()

    await _when_due(service, job)

    assert _job_events(service, job) == []


async def test_a_job_a_boot_runs_again_after_passing_failures_sends_a_job_event(monkeypatch):
    job = JobRecord(id="sched_briefer", name="Morning briefing", prompt="brief me",
                    schedule=IntervalSchedule(seconds=86400), sinks=[NoopSinkSpec()])
    for n in (1, 2, 3):
        save_run(RunRecord(run_id=f"run_{n}", job_id=job.id, job_name=job.name,
                           started_at=time.time(), status="failed", error=CONNECTION_ERROR))
        time.sleep(0.01)  # runs are ordered by file mtime
    job.enabled, job.status, job.next_fire_at = False, "disabled_error", None
    save_job(job)
    owner = SchedulerService(SimpleNamespace())
    told: list[dict] = []
    monkeypatch.setattr(owner, "emit_event", lambda **kw: told.append(kw))

    await owner.start()
    await owner.stop()

    assert (load_job(job.id).enabled, load_job(job.id).status) == (True, "active")
    assert [(e["subtype"], e["details"]["jobId"]) for e in told if e["subtype"].startswith("scheduler_job_")] == [
        ("scheduler_job_resumed_after_passing_failures", job.id),
    ]
