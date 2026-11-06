"""A job that ran all its max_fires is not resumed until they are raised
or cleared.

Nothing checks max_fires when a run is claimed: the end of the run that
reaches it stops the job. Resumed as it was, the job fired once more and
stopped again. The stopped-state hints say to raise max fires first, so
the resume is refused until that is done.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    ExistingSession,
    IntervalSchedule,
    JobPatch,
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
    """A scheduler whose jobs' turns reply."""
    import bridge.freyja_bridge as fb
    import bridge.scheduler.sinks as sinks

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )

    async def turn(prompt: str) -> None:
        sess.session.transcript.append_message(Message(role="user", content=prompt))
        sess.session.transcript.append_message(Message(role="assistant", content="Done."))

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None):
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


async def _fire(service: SchedulerService, job_id: str) -> JobRecord:
    job = load_job(job_id)
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job_id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job_id)


async def _ran_all_its_fires(service: SchedulerService) -> JobRecord:
    save_job(JobRecord(
        id="sched_digest", name="digest", prompt="digest",
        schedule=IntervalSchedule(seconds=3600), max_fires=1,
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
    ))
    done = await _fire(service, "sched_digest")
    assert (done.enabled, done.status, done.fire_count) == (False, "disabled_max_fires", 1)
    return done


@pytest.mark.parametrize("resume", ["resume_job", "enabled=True"])
async def test_a_job_that_ran_all_its_max_fires_is_not_resumed(service, resume):
    done = await _ran_all_its_fires(service)

    with pytest.raises(ValueError, match="raise or clear max fires first"):
        if resume == "resume_job":
            await service.resume_job(done.id)
        else:
            await service.update_job(done.id, JobPatch(enabled=True))

    saved = load_job(done.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_max_fires", None)


@pytest.mark.parametrize("patch", [
    JobPatch(max_fires=2),
    JobPatch(clear_max_fires=True),
])
async def test_a_job_whose_max_fires_are_raised_or_cleared_resumes(service, patch):
    done = await _ran_all_its_fires(service)

    await service.update_job(done.id, patch)
    resumed = await service.resume_job(done.id)

    assert (resumed.enabled, resumed.status) == (True, "active")
    assert (await _fire(service, done.id)).fire_count == 2


async def test_raising_max_fires_and_enabling_in_one_update_resumes(service):
    done = await _ran_all_its_fires(service)

    updated = await service.update_job(done.id, JobPatch(enabled=True, max_fires=3))

    assert (updated.enabled, updated.status, updated.max_fires) == (True, "active", 3)


async def test_the_schedule_tool_and_slack_say_to_raise_or_clear_max_fires(service):
    from bridge.gateway.run import GatewayDaemon

    done = await _ran_all_its_fires(service)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "resume", "job_id": done.id})
    reply = await GatewayDaemon._scheduler_schedule_subcommand(
        None, f"resume {done.id}", service=service, creator=None,
    )

    assert result.is_error
    for said in (result.content, reply):
        assert "ran all 1 of its max fires" in said
        assert "raise or clear max fires first" in said
    assert not load_job(done.id).enabled


@pytest.mark.parametrize("max_fires", [2, 3])
async def test_lowering_max_fires_to_the_fires_it_ran_stops_the_job_at_once(service, max_fires):
    # Nothing checks max_fires when a run is claimed, so a job left active
    # with its cap at or below its fire count fired once more.
    save_job(JobRecord(
        id="sched_digest", name="digest", prompt="digest",
        schedule=IntervalSchedule(seconds=3600), fire_count=3,
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()],
    ))

    updated = await service.update_job("sched_digest", JobPatch(max_fires=max_fires))
    after = await _fire(service, "sched_digest")

    assert after.fire_count == 3
    assert (updated.enabled, updated.status, updated.next_fire_at) == (False, "disabled_max_fires", None)
