"""A one-shot whose last allowed run fails says so where it reports.

A one-shot has one slot. Its run failed for a real reason (not a passing
one, which is retried), and with no retry_policy, or with its retries
spent, the job was used up (disabled_max_fires). The stop notice comes
only after three failed runs in a row, which a one-shot never has: the
reminder never ran and nobody was told.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    CreatorRef,
    DesktopSinkSpec,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RetryPolicy,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

REAL_ERROR = "agent turn failed: Error code: 400 - messages.3: tool_use ids must be unique"
CONNECTION_ERROR = "the turn failed before the model replied — unknown: Connection error."
DUE = time.time() - 60


@pytest.fixture
def service(monkeypatch):
    posted: list[tuple[list, str]] = []
    told: list[dict] = []

    async def deliver_all(job, run, output, ctx):
        posted.append((list(job.sinks), output.text))
        return []

    import bridge.scheduler.sinks as sinks

    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    svc = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(svc, "emit_event", lambda **kw: told.append(kw))
    svc.posted = posted
    svc.told = told
    return svc


def _fails(monkeypatch, *, status: str = "failed", error: str | None = REAL_ERROR) -> list[int]:
    attempts: list[int] = []

    async def fire(_service, job, *, attempt=0, **_kw):
        attempts.append(attempt)
        run = RunRecord(run_id=f"run_{len(attempts)}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), finished_at=time.time(), status=status,
                        attempt=attempt, error=error)
        save_run(run)
        time.sleep(0.01)  # runs are ordered by file mtime
        return run

    monkeypatch.setattr(runtime, "fire_job", fire)
    return attempts


def _reminder(**fields) -> JobRecord:
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        creator=CreatorRef(surface="desktop", session_id="session-19f0desk"),
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        sinks=[NoopSinkSpec()], **fields,
    )
    job.next_fire_at = DUE
    save_job(job)
    return job


async def _claim(service: SchedulerService, job: JobRecord) -> None:
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())


async def test_a_one_shot_whose_only_run_failed_says_so(service, monkeypatch):
    _fails(monkeypatch)
    job = _reminder()

    await _claim(service, job)

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (False, "disabled_max_fires")
    [(sinks, notice)] = service.posted
    # Its sink is noop: the notice goes to the session it was made in.
    assert DesktopSinkSpec(session_id="session-19f0desk") in sinks
    assert "call the dentist" in notice and "did not complete" in notice
    assert REAL_ERROR in notice
    assert "resume" in notice
    assert [e["subtype"] for e in service.told if "one_shot" in e["subtype"]] == ["scheduler_one_shot_failed"]


async def test_a_one_shot_says_so_once_its_retries_are_spent(service, monkeypatch):
    attempts = _fails(monkeypatch)
    job = _reminder(retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0))

    await _claim(service, job)
    assert service.posted == []  # retried, not done yet
    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.retry_attempt) == (True, "active", 1)

    await _claim(service, job)

    assert attempts == [0, 1]
    [(_, notice)] = service.posted
    assert "did not complete" in notice


async def test_a_one_shot_that_failed_for_a_passing_reason_is_retried_without_a_notice(service, monkeypatch):
    _fails(monkeypatch, error=CONNECTION_ERROR)
    job = _reminder()

    await _claim(service, job)

    assert service.posted == []
    assert load_job(job.id).status == "active"


@pytest.mark.parametrize("status, error", [("succeeded", None), ("cancelled", "cancel_requested by peer process or user")])
async def test_a_one_shot_that_ran_or_was_cancelled_says_nothing(service, monkeypatch, status, error):
    _fails(monkeypatch, status=status, error=error)
    job = _reminder()

    await _claim(service, job)

    assert service.posted == []


async def test_a_recurring_job_is_still_told_of_only_after_three_failures(service, monkeypatch):
    _fails(monkeypatch)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), sinks=[NoopSinkSpec()])
    job.next_fire_at = DUE
    save_job(job)

    await _claim(service, job)

    assert service.posted == []
