"""retry_policy.max_retries is enforced, and each slot gets its retries.

The retry read the run's attempt with getattr(run, "_attempts", 0), which
nothing set, so every failed run was "attempt 1": a job retried after
every failure, always after backoff_seconds, however many retries it
allowed, until the third failed run in a row stopped the job. A job that
allowed three retries never had its third; one that allowed two was
stopped after one bad slot.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    CronSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RetryPolicy,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

REAL_ERROR = "agent turn failed: the report's sheet is gone"
CONNECTION_ERROR = "the turn failed before the model replied — unknown: Connection error."


@pytest.fixture
def service(monkeypatch):
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    svc.attempts = []
    svc.errors = []

    async def fire(_service, job, **kw):
        svc.attempts.append(kw.get("attempt"))
        error = svc.errors.pop(0) if svc.errors else REAL_ERROR
        run = RunRecord(run_id=f"run_{len(svc.attempts)}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), status="failed", error=error)
        run.attempt = kw.get("attempt", 0)
        save_run(run)
        await asyncio.sleep(0.01)  # runs are ordered by file mtime
        return run

    import bridge.scheduler.sinks as sinks

    async def deliver_all(*_a, **_kw):
        return []

    monkeypatch.setattr(runtime, "fire_job", fire)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    return svc


def _report(**fields) -> JobRecord:
    fields.setdefault("retry_policy", RetryPolicy(max_retries=2, backoff_seconds=60))
    job = JobRecord(id="sched_report", name="daily report",
                    schedule=CronSchedule(expression="0 6 * * *"), prompt="report",
                    sinks=[NoopSinkSpec()], **fields)
    job.next_fire_at = time.time() - 1
    save_job(job)
    return job


async def _run_when_due(service: SchedulerService, job_id: str) -> JobRecord:
    """Its next fire comes: the tick claims and runs it to the end."""
    job = load_job(job_id)
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job_id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job_id)


def _next_slot(job: JobRecord) -> bool:
    return job.next_fire_at > time.time() + 3600


async def test_a_failed_slot_is_retried_max_retries_times_with_growing_waits(service):
    job = _report()

    after_run = await _run_when_due(service, job.id)
    after_first_retry = await _run_when_due(service, job.id)
    after_second_retry = await _run_when_due(service, job.id)

    assert after_run.next_fire_at == pytest.approx(time.time() + 60, abs=5)
    assert after_first_retry.next_fire_at == pytest.approx(time.time() + 120, abs=5)
    assert _next_slot(after_second_retry)
    assert service.attempts == [0, 1, 2]


async def test_a_slot_whose_retries_failed_does_not_stop_the_job(service):
    job = _report(retry_policy=RetryPolicy(max_retries=3, backoff_seconds=60))

    for _ in range(4):
        saved = await _run_when_due(service, job.id)

    assert (saved.enabled, saved.status) == (True, "active")
    assert _next_slot(saved)
    assert service.attempts == [0, 1, 2, 3]


async def test_the_next_slot_gets_its_own_retries(service):
    job = _report(retry_policy=RetryPolicy(max_retries=1, backoff_seconds=60))
    await _run_when_due(service, job.id)
    await _run_when_due(service, job.id)

    saved = await _run_when_due(service, job.id)

    assert saved.next_fire_at == pytest.approx(time.time() + 60, abs=5)
    assert service.attempts == [0, 1, 0]


async def test_a_job_whose_last_three_slots_failed_with_their_retries_is_stopped(service):
    job = _report(retry_policy=RetryPolicy(max_retries=1, backoff_seconds=60))

    for _ in range(6):
        saved = await _run_when_due(service, job.id)

    assert service.attempts == [0, 1, 0, 1, 0, 1]
    assert (saved.enabled, saved.status) == (False, "disabled_error")


async def test_a_passing_failure_does_not_use_up_a_retry(service):
    job = _report(retry_policy=RetryPolicy(max_retries=1, backoff_seconds=60))
    service.errors = [REAL_ERROR, CONNECTION_ERROR, REAL_ERROR]

    await _run_when_due(service, job.id)
    after_passing = await _run_when_due(service, job.id)
    saved = await _run_when_due(service, job.id)

    # The connection error is retried by its own rule, 2 minutes later.
    assert after_passing.next_fire_at == pytest.approx(time.time() + 120, abs=5)
    assert service.attempts == [0, 1, 1]
    assert _next_slot(saved)


async def test_a_one_shot_that_failed_is_retried(service):
    job = JobRecord(id="sched_once", name="reminder", schedule=OnceSchedule(at_iso="2026-01-01T09:00:00+00:00"),
                    prompt="ping", sinks=[NoopSinkSpec()],
                    retry_policy=RetryPolicy(max_retries=1, backoff_seconds=60))
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (True, "active")
    assert saved.next_fire_at == pytest.approx(time.time() + 60, abs=5)


async def test_a_job_paused_during_its_run_is_not_retried(service, monkeypatch):
    job = _report()

    async def fire_then_pause(_service, fired, **kw):
        await service.pause_job(fired.id)
        run = RunRecord(run_id="run_paused", job_id=fired.id, job_name=fired.name,
                        started_at=time.time(), status="failed", error=REAL_ERROR)
        save_run(run)
        return run

    monkeypatch.setattr(runtime, "fire_job", fire_then_pause)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (False, "paused")


async def test_the_run_record_says_which_retry_it_is(monkeypatch):
    from bridge.scheduler.models import ExistingSession
    from bridge.scheduler.persistence import load_run

    class _Service:
        state = SimpleNamespace(sessions={})

        def emit_event(self, **_kw):
            pass

    job = _report(execution=ExistingSession(session_id=""))

    run = await runtime.fire_job(_Service(), job, attempt=2)

    assert load_run(job.id, run.run_id).attempt == 2


# ─── the stop notice counts every run that failed ─────────────────────


@pytest.mark.parametrize("retry_policy, runs, said", [
    (RetryPolicy(max_retries=1, backoff_seconds=60), 6,
     "its last 6 runs failed, 3 of them retries under its retry policy."),
    (None, 3, "its last 3 runs failed."),
])
async def test_the_stop_notice_says_how_many_runs_failed(service, monkeypatch, retry_policy, runs, said):
    notices: list[str] = []
    monkeypatch.setattr(service, "emit_event", lambda **kw: notices.append(kw["message"]))
    job = _report(retry_policy=retry_policy)

    for _ in range(runs):
        saved = await _run_when_due(service, job.id)

    assert saved.status == "disabled_error"
    assert f"I stopped the scheduled job *daily report*: {said} Last error: {REAL_ERROR}" in notices


# ─── many retries do not hide the failing slots ───────────────────────


@pytest.mark.parametrize("max_retries", [7, 8, 10])
async def test_a_job_with_many_retries_is_stopped_after_three_failed_slots(service, max_retries):
    job = _report(retry_policy=RetryPolicy(max_retries=max_retries, backoff_seconds=60))

    for _ in range(3 * (max_retries + 1)):
        saved = await _run_when_due(service, job.id)

    assert (saved.enabled, saved.status) == (False, "disabled_error")


async def test_passing_failures_between_slots_do_not_hide_the_first_one(service):
    job = _report(retry_policy=RetryPolicy(max_retries=10, backoff_seconds=60))
    service.errors = [CONNECTION_ERROR] * 20

    for _ in range(20 + 3 * 11):
        saved = await _run_when_due(service, job.id)

    assert (saved.enabled, saved.status) == (False, "disabled_error")


async def test_a_job_saved_with_more_retries_than_allowed_gets_ten(service):
    job = _report(retry_policy=RetryPolicy(max_retries=11, backoff_seconds=60))

    for _ in range(3 * 11):
        saved = await _run_when_due(service, job.id)

    assert service.attempts == list(range(11)) * 3
    assert (saved.enabled, saved.status) == (False, "disabled_error")


@pytest.mark.parametrize("max_retries", [11, -1])
async def test_max_retries_out_of_bounds_is_refused(service, max_retries):
    from bridge.scheduler.models import JobPatch

    job = _report(retry_policy=None)
    too_many = RetryPolicy(max_retries=max_retries)

    with pytest.raises(ValueError, match=rf"retry_policy.max_retries is {max_retries}: use 0 to 10"):
        await service.update_job(job.id, JobPatch(retry_policy=too_many))
    with pytest.raises(ValueError, match="use 0 to 10 retries per slot"):
        await service.create_job(JobRecord(id="", name="x", prompt="x", retry_policy=too_many))
    assert load_job(job.id).retry_policy is None
