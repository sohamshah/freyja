"""A job's max_cost_usd_per_day holds its runs once it has spent that
much today.

The schedule tool offers budget.max_cost_usd_per_day, and BudgetSpec
saved it, but nothing read it: a job capped at $0.60 a day ran every
slot whatever it had spent.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dt_time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    BudgetSpec,
    CreatorRef,
    DeliveryReport,
    DesktopSinkSpec,
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, load_runs_for_job, save_job, save_run
from bridge.scheduler.service import SchedulerService
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message

START_OF_TODAY = datetime.combine(date.today(), dt_time.min).timestamp()


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose job's turn costs $0.50, and that keeps what it
    posts."""
    import bridge.freyja_bridge as fb
    import bridge.scheduler.sinks as sinks

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )

    async def turn(prompt: str) -> None:
        sess.session.transcript.append_message(Message(role="user", content=prompt))
        fb.emit({"type": "turn_start", "sessionId": "s1", "turnId": "turn-1"})
        fb.emit({"type": "llm_call_metric", "sessionId": "s1", "turnId": "turn-1", "costUsd": 0.50})
        sess.session.transcript.append_message(Message(role="assistant", content="Done."))

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None):
        on_turn_start()
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    posted: list[tuple[list, str]] = []

    async def deliver_all(job, run, output, ctx):
        posted.append((list(job.sinks), output.text))
        return [DeliveryReport(sink_index=i, sink_kind=s.kind, success=True, delivered_at=time.time())
                for i, s in enumerate(job.sinks)]

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    svc = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    svc.posted = posted
    return svc


def _job(on_exceeded: str = "abort_run") -> JobRecord:
    job = JobRecord(
        id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
        prompt="digest", execution=ExistingSession(session_id="s1"),
        creator=CreatorRef(surface="desktop", session_id="session-abc"),
        sinks=[NoopSinkSpec()],
        budget=BudgetSpec(max_cost_usd_per_day=0.60, on_exceeded=on_exceeded),
    )
    save_job(job)
    return job


def _spent(job: JobRecord, cost: float, *, at: float, n: int = 1) -> None:
    save_run(RunRecord(run_id=f"run_spent_{n}", job_id=job.id, job_name=job.name,
                       started_at=at, finished_at=at + 5, status="succeeded", cost_usd=cost))
    time.sleep(0.01)  # runs are ordered by file mtime


async def _when_due(service: SchedulerService, job: JobRecord) -> JobRecord:
    fresh = load_job(job.id)
    fresh.next_fire_at = time.time() - 1
    save_job(fresh)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job.id)


async def test_a_job_that_spent_its_daily_budget_skips_its_slot(service):
    job = _job("abort_run")
    _spent(job, 0.40, at=time.time() - 600, n=1)
    _spent(job, 0.30, at=time.time() - 300, n=2)

    saved = await _when_due(service, job)

    assert [r.run_id for r in load_runs_for_job(job.id)] == ["run_spent_2", "run_spent_1"]
    assert (saved.enabled, saved.status) == (True, "active")
    assert saved.next_fire_at > time.time() + 3000  # its next slot
    assert service.posted == []


async def test_a_job_under_its_daily_budget_runs(service):
    job = _job("abort_run")
    _spent(job, 0.50, at=time.time() - 600)

    await _when_due(service, job)

    newest = load_runs_for_job(job.id)[0]
    assert (newest.status, newest.cost_usd) == ("succeeded", 0.50)


async def test_what_it_spent_yesterday_does_not_count(service):
    job = _job("abort_run")
    _spent(job, 5.00, at=START_OF_TODAY - 60)

    await _when_due(service, job)

    assert load_runs_for_job(job.id)[0].status == "succeeded"


async def test_a_job_that_spent_its_daily_budget_is_stopped_and_says_so(service):
    job = _job("abort_job")
    _spent(job, 0.70, at=time.time() - 600)

    saved = await _when_due(service, job)

    assert (saved.enabled, saved.status, saved.next_fire_at) == (
        False, "disabled_budget_exhausted", None,
    )
    newest = load_runs_for_job(job.id)[0]
    assert newest.status == "budget_exceeded"
    assert "$0.70" in newest.error and "$0.60" in newest.error
    [(told, text)] = service.posted
    assert DesktopSinkSpec(session_id="session-abc") in told
    assert "digest" in text
    assert "spent $0.70 today, the budget is $0.60 a day" in text
