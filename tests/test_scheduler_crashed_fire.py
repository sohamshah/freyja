"""A run whose fire raises is finished as failed, with the error.

fire_job saves its run as "running" before it composes the prompt and
starts the turn. When something there raised, the code that caught the
crash only logged it: the run said "running" in the dashboard until the
next scheduler start marked it interrupted, for as long as the process
lived.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RetryPolicy,
)
from bridge.scheduler.persistence import load_job, load_run, load_runs_for_job, save_job
from bridge.scheduler.service import SchedulerService
from engine.session import TranscriptManager
from engine.tools import ToolRegistry

CRASH = "the skill file is unreadable"


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose fires crash after their run says "running"."""
    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )

    def crash(*_a, **_kw):
        raise RuntimeError(CRASH)

    monkeypatch.setattr(runtime, "_compose_fire_prompt", crash)
    svc = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    svc.emitted = []
    monkeypatch.setattr(svc, "emit_event", lambda **kw: svc.emitted.append(kw))
    return svc


def _job() -> JobRecord:
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest", schedule=IntervalSchedule(seconds=3600),
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = time.time() - 1
    save_job(job)
    return job


def _finished_as_failed(service: SchedulerService, job_id: str, run_id: str) -> None:
    run = load_run(job_id, run_id)
    assert run.status == "failed"
    assert CRASH in run.error
    assert run.finished_at is not None
    # The dashboard hears how it ended.
    assert any(
        e["subtype"] == "scheduler_run_failed" and e["details"]["runId"] == run_id
        for e in service.emitted
    )


async def test_a_scheduled_fire_that_raises_is_finished_as_failed(service):
    job = _job()

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    (run,) = load_runs_for_job(job.id)
    _finished_as_failed(service, job.id, run.run_id)


async def test_a_run_started_now_that_raises_is_finished_as_failed(service):
    job = _job()

    started = await service.start_job_now(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    _finished_as_failed(service, job.id, started.run_id)


async def test_a_run_now_that_raises_is_finished_as_failed_and_says_why(service):
    job = _job()

    with pytest.raises(RuntimeError, match=CRASH):
        await service.run_job_now(job.id)

    (run,) = load_runs_for_job(job.id)
    _finished_as_failed(service, job.id, run.run_id)


# A crashed fire ends as a failed run does: it counts as a fire, its retry
# policy applies, and a one-shot it used up says it did not complete. It
# skipped all of that: a job with max_fires fired past it, a retry_policy
# never retried, and a reminder whose fire crashed was used up in silence.


async def test_a_scheduled_fire_that_raises_counts_as_a_fire(service):
    job = _job()
    job.max_fires = 1
    save_job(job)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    saved = load_job(job.id)
    (run,) = load_runs_for_job(job.id)
    assert saved.fire_count == 1
    assert saved.last_fire_at == run.started_at
    assert (saved.enabled, saved.status) == (False, "disabled_max_fires")


async def test_a_run_now_that_raises_counts_as_a_fire(service):
    job = _job()

    with pytest.raises(RuntimeError, match=CRASH):
        await service.run_job_now(job.id)

    assert load_job(job.id).fire_count == 1


async def test_a_scheduled_fire_that_raises_is_retried_by_its_retry_policy(service):
    job = _job()
    job.retry_policy = RetryPolicy(max_retries=1, backoff_seconds=600)
    save_job(job)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    saved = load_job(job.id)
    assert saved.retry_attempt == 1
    assert saved.next_fire_at == pytest.approx(time.time() + 600, abs=5)


async def test_a_one_shot_whose_fire_raises_says_it_did_not_complete(service):
    due = time.time() - 60
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(due, timezone.utc).isoformat()),
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = due
    save_job(job)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    (notice,) = [e for e in service.emitted if e["subtype"] == "scheduler_one_shot_failed"]
    assert "did not complete" in notice["message"] and CRASH in notice["message"]
