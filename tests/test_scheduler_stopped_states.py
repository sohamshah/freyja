"""A job that stopped for a reason other than a pause says why, and how
to run it again.

The dashboard labels every disabled job "paused", so a job stopped for
failing, one that ran all its max_fires, a one-shot that ran and a loop
that ended itself all read "paused". The schedule tool and Slack showed
the raw status (disabled_error, disabled_max_fires), which says nothing
of what runs the job again: resume alone does not run a one-shot whose
time has passed, and runs a job with max_fires used up only once.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    CronSchedule,
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.runtime import _apply_loop_decision
from bridge.scheduler.service import SchedulerService
from bridge.tools.schedule_tool import LoopDecision, ScheduleTool
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose jobs' turns reply and cost nothing."""
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
        sess.session.transcript.append_message(Message(role="assistant", content="Done."))

    def schedule_turn(s, prompt, attachments=None):
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    async def deliver_all(job, run, output, ctx):
        return []

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    svc = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    return svc


async def _fire(service: SchedulerService, job: JobRecord) -> JobRecord:
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job.id)


def _job(**fields) -> JobRecord:
    fields.setdefault("schedule", IntervalSchedule(seconds=3600))
    return JobRecord(
        id="sched_digest", name="digest", prompt="digest",
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
        **fields,
    )


async def _stopped_for_failing(service: SchedulerService) -> JobRecord:
    job = _job(schedule=CronSchedule(expression="0 1 * * *"))
    save_job(job)
    for n in (1, 2, 3):
        run = RunRecord(run_id=f"run_{n}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), status="failed",
                        error="agent turn failed: Error code: 400 - bad request")
        save_run(run)
        time.sleep(0.01)  # runs are ordered by file mtime
        await service._stop_if_failing(job, run)
    return load_job(job.id)


async def _ran_all_its_fires(service: SchedulerService) -> JobRecord:
    return await _fire(service, _job(max_fires=1))


async def _one_shot_that_ran(service: SchedulerService) -> JobRecord:
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    return await _fire(service, _job(schedule=OnceSchedule(at_iso=at)))


async def _loop_that_ended(service: SchedulerService) -> JobRecord:
    job = _job(schedule=SelfPacedSchedule())
    _apply_loop_decision(service, job, LoopDecision(kind="complete", reason="merged"))
    save_job(job)
    return load_job(job.id)


STOPS = {
    "failing": (_stopped_for_failing, "disabled_error"),
    "max fires": (_ran_all_its_fires, "disabled_max_fires"),
    "one-shot": (_one_shot_that_ran, "disabled_max_fires"),
    "loop": (_loop_that_ended, "disabled_max_fires"),
}


async def _stop(service, which: str) -> JobRecord:
    how, status = STOPS[which]
    saved = await how(service)
    assert (saved.enabled, saved.status) == (False, status)
    return saved


@pytest.mark.parametrize("which, says", [
    ("failing", ["kept failing", "action='runs'", "action='resume'"]),
    ("max fires", ["all 1 of its max_fires", "raise max_fires", "action='resume'"]),
    ("one-shot", ["no more times", "new time", "action='resume'"]),
    ("loop", ["loop ended itself", "action='resume'"]),
])
async def test_the_schedule_tool_says_why_the_job_stopped_and_how_to_run_it_again(service, which, says):
    saved = await _stop(service, which)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "get", "job_id": saved.id})

    stopped = json.loads(result.content)["job"]["stopped"]
    for words in says:
        assert words in stopped


@pytest.mark.parametrize("which, says", [
    ("failing", ["stopped for failing", "/freyja schedule runs sched_digest"]),
    ("max fires", ["done", "all 1 of its max fires", "raise max fires"]),
    ("one-shot", ["done", "no more times", "new time"]),
    ("loop", ["done", "loop ended itself"]),
])
async def test_slack_says_why_the_job_stopped_and_how_to_run_it_again(service, which, says):
    from bridge.gateway.run import _format_job_detail, _format_jobs_table

    saved = await _stop(service, which)

    for text in (_format_job_detail(saved), _format_jobs_table([saved])):
        for words in [*says, "/freyja schedule resume sched_digest"]:
            assert words in text
        assert saved.status not in text


async def test_a_paused_job_has_no_stopped_note(service):
    job = _job()
    save_job(job)
    paused = await service.pause_job(job.id)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "get", "job_id": paused.id})

    assert "stopped" not in json.loads(result.content)["job"]
