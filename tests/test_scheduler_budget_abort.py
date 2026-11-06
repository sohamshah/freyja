"""A run aborted for going over its budget is a budget abort, not a
timeout, and is never retried.

A run over its budget whose on_exceeded is abort_run ended as timed_out.
A retry_policy whose `on` is empty or holds "timeout" retries a timed-out
run, so the job ran again within minutes and spent its budget again; a
one-shot reminder was even enabled again for it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    BudgetSpec,
    CronSchedule,
    ExistingSession,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RetryPolicy,
)
from bridge.scheduler.persistence import load_job, load_runs_for_job, save_job
from bridge.scheduler.service import SchedulerService
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose job's turn costs $0.50."""
    import bridge.freyja_bridge as fb

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )

    async def turn(prompt: str, run_id: str) -> None:
        sess.session.transcript.append_message(Message(role="user", content=prompt))
        fb.emit({"type": "turn_start", "sessionId": "s1", "turnId": "turn-1", "runId": run_id})
        fb.emit({"type": "llm_call_metric", "sessionId": "s1", "turnId": "turn-1", "costUsd": 0.50})
        sess.session.transcript.append_message(Message(role="assistant", content="Done."))
        fb.emit({"type": "turn_complete", "sessionId": "s1", "turnId": "turn-1"})

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None, run_id=None):
        on_turn_start()
        s.pending_task = asyncio.ensure_future(turn(prompt, run_id))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    svc = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    return svc


async def _over_budget(service: SchedulerService, **fields) -> JobRecord:
    """A daily job capped at $0.10 a run fires once and spends $0.50."""
    fields.setdefault("schedule", CronSchedule(expression="0 6 * * *"))
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest",
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
        budget=BudgetSpec(max_cost_usd_per_run=0.10, on_exceeded="abort_run"),
        **fields,
    )
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job.id)


async def test_a_run_over_its_budget_is_recorded_as_a_budget_abort(service):
    saved = await _over_budget(service)

    [run] = load_runs_for_job(saved.id)
    assert run.status == "budget_exceeded"
    assert "budget exceeded" in run.error
    assert run.delivery_reports == []


@pytest.mark.parametrize("on", [[], ["timeout"], ["agent_error", "sink_error", "timeout"]])
async def test_a_run_over_its_budget_is_not_retried(service, on):
    saved = await _over_budget(
        service, retry_policy=RetryPolicy(max_retries=3, backoff_seconds=60, on=on),
    )

    assert (saved.enabled, saved.status) == (True, "active")
    assert saved.retry_attempt == 0
    assert saved.next_fire_at > time.time() + 3600  # its next slot, not a retry


async def test_a_one_shot_over_its_budget_is_not_enabled_again_to_retry(service):
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    saved = await _over_budget(
        service, schedule=OnceSchedule(at_iso=at),
        retry_policy=RetryPolicy(max_retries=3, backoff_seconds=60),
    )

    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_max_fires", None)


async def test_a_run_over_its_budget_on_the_last_fire_it_may_have_says_it_was_stopped(service, monkeypatch):
    # Counted as the last of its max_fires, it was saved "disabled_max_fires"
    # over the stop: the dashboard said the job had run all its fires, and
    # the notice that it was stopped over its budget was never posted.
    told: list[str] = []
    monkeypatch.setattr(service, "emit_event", lambda **kw: told.append(kw["subtype"]))
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest",
        schedule=CronSchedule(expression="0 6 * * *"),
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
        budget=BudgetSpec(max_cost_usd_per_run=0.10, on_exceeded="abort_job"),
        max_fires=1,
    )
    job.next_fire_at = time.time() - 1
    save_job(job)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_budget_exhausted", None)
    assert saved.fire_count == 1
    assert "scheduler_job_stopped_over_budget" in told
