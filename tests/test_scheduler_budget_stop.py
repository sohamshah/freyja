"""A job stopped for going over its budget says so, and has no next fire.

A run over a budget whose on_exceeded is abort_job set the job
disabled_budget_exhausted and kept the next fire the claim had set. The
tick skips such a job, so the time was never used, but the dashboard,
which labels every disabled job "paused", showed a paused job due at
that time, and the schedule tool and Slack showed the time with a status
that did not say how to run the job again.

Nobody was told of the stop either: the run delivers nothing, and unlike
a job stopped for failing, no notice went to the job's sinks or to the
conversation it was made in.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    BudgetSpec,
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
)
from bridge.scheduler.persistence import load_job, save_job
from bridge.scheduler.service import SchedulerService
from bridge.tools.schedule_tool import ScheduleTool
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


async def _over_budget(service: SchedulerService) -> JobRecord:
    """An hourly job capped at $0.10 a run fires once and spends $0.50."""
    job = JobRecord(
        id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
        prompt="digest", execution=ExistingSession(session_id="s1"),
        sinks=[NoopSinkSpec()],
        budget=BudgetSpec(max_cost_usd_per_run=0.10, on_exceeded="abort_job"),
    )
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job.id)


async def test_a_job_stopped_over_budget_has_no_next_fire(service):
    saved = await _over_budget(service)

    assert (saved.enabled, saved.status) == (False, "disabled_budget_exhausted")
    assert saved.next_fire_at is None


async def test_the_schedule_tool_says_the_job_went_over_budget_and_how_to_resume(service):
    saved = await _over_budget(service)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "get", "job_id": saved.id})

    job = json.loads(result.content)["job"]
    assert job["next_fire_at"] is None
    assert "over budget" in job["stopped"]
    assert "action='resume'" in job["stopped"]


async def test_slack_says_the_job_went_over_budget_and_how_to_resume(service):
    from bridge.gateway.run import _format_job_detail, _format_jobs_table

    saved = await _over_budget(service)

    for text in (_format_job_detail(saved), _format_jobs_table([saved])):
        assert "over budget" in text
        assert f"/freyja schedule resume {saved.id}" in text
        assert "disabled_budget_exhausted" not in text
    assert "next: —" in _format_jobs_table([saved])


async def test_the_job_s_sinks_and_the_conversation_it_came_from_are_told_it_stopped(service, monkeypatch):
    import bridge.scheduler.sinks as sinks
    from bridge.scheduler.models import CreatorRef, DesktopSinkSpec

    posted: list[tuple[list, str]] = []

    async def deliver_all(job, run, output, ctx):
        posted.append((list(job.sinks), output.text))
        return []

    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    job = JobRecord(
        id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
        prompt="digest", execution=ExistingSession(session_id="s1"),
        creator=CreatorRef(surface="desktop", session_id="session-abc"),
        sinks=[NoopSinkSpec()],
        budget=BudgetSpec(max_cost_usd_per_run=0.10, on_exceeded="abort_job"),
    )
    job.next_fire_at = time.time() - 1
    save_job(job)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    [(told, text)] = posted
    assert told == [NoopSinkSpec(), DesktopSinkSpec(session_id="session-abc")]
    assert "digest" in text
    assert "went over its budget (it cost $0.50, the budget is $0.10 a run)" in text
    assert "resume" in text
