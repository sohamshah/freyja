"""A job can be edited, paused or removed while it runs.

A run held its job's lock from claim to end, and every edit takes that
lock without waiting: editing a job while it ran failed with "job ... is
locked by a peer; retry shortly", and retrying kept failing for as long
as the run went on (minutes, or an hour for a long job). The run needs
the lock only to write the job's record at its start and end; what it
must hold for its whole length is that no second run of the job starts.
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
from bridge.scheduler.persistence import load_job, load_runs_for_job, save_job
from bridge.scheduler.service import SchedulerService
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message


@pytest.fixture
def running_job(monkeypatch):
    """A due job whose run, started by the owner's tick, goes on until
    ``finish`` is set."""
    import bridge.freyja_bridge as fb

    started = asyncio.Event()
    finish = asyncio.Event()
    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )

    async def turn(prompt: str, run_id: str) -> None:
        fb.emit({"type": "turn_start", "sessionId": "s1", "turnId": "turn-1", "runId": run_id})
        sess.session.transcript.append_message(Message(role="user", content=prompt))
        started.set()
        await finish.wait()
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
    service = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)

    async def start() -> JobRecord:
        await service.start()
        job = JobRecord(id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
                        prompt="the old prompt", execution=ExistingSession(session_id="s1"),
                        sinks=[NoopSinkSpec()])
        job.next_fire_at = time.time() - 1
        save_job(job)
        await asyncio.wait_for(started.wait(), 5)
        return job

    async def end() -> None:
        finish.set()
        for _ in range(100):
            runs = load_runs_for_job("sched_digest", limit=1)
            if runs and runs[0].finished_at:
                break
            await asyncio.sleep(0.05)
        await service.stop()

    return SimpleNamespace(service=service, start=start, end=end)


async def test_an_edit_made_while_the_job_runs_is_kept_for_the_next_run(running_job):
    service = running_job.service
    job = await running_job.start()

    await service.update_job(job.id, JobPatch(prompt="the new prompt", name="morning digest"))
    await running_job.end()

    saved = load_job(job.id)
    assert (saved.prompt, saved.name) == ("the new prompt", "morning digest")
    assert saved.fire_count == 1 and saved.last_fire_at is not None
    # The run went on with the prompt it started with.
    assert load_runs_for_job(job.id, limit=1)[0].prompt == "the old prompt"


async def test_a_job_paused_while_it_runs_stays_paused(running_job):
    service = running_job.service
    job = await running_job.start()

    await service.pause_job(job.id)
    await running_job.end()

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (False, "paused")


async def test_a_job_removed_while_it_runs_stays_removed(running_job):
    service = running_job.service
    job = await running_job.start()

    assert await service.remove_job(job.id)
    await running_job.end()

    assert load_job(job.id) is None


async def test_run_now_while_the_job_runs_says_it_is_running(running_job):
    service = running_job.service
    job = await running_job.start()

    try:
        with pytest.raises(RuntimeError, match="is running"):
            await service.run_job_now(job.id)
    finally:
        await running_job.end()
