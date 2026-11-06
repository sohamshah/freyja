"""A cancelled run stays cancelled and delivers nothing.

The run was marked "cancelled" when its cancel flag was seen, then went
on to delivery like a finished one: its partial answer went to the
job's sinks, and the delivery outcome overwrote the status, so the
dashboard listed it as succeeded.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_runs_for_job, request_run_cancellation, save_job
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions)

    def emit_event(self, **_kw) -> None:
        pass


async def test_a_cancelled_run_is_not_delivered(monkeypatch):
    import bridge.freyja_bridge as fb
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    started = asyncio.Event()

    async def turn(prompt: str) -> None:
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=prompt))
        transcript.append_message(Message(role="assistant", content="Half of the report"))
        started.set()
        await asyncio.sleep(60)

    def schedule_turn(s, prompt, attachments=None):
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    monkeypatch.setattr(fb, "_force_cancel_session", lambda s, scope: s.pending_task.cancel())
    monkeypatch.setattr(fb, "_stop_background_work", lambda s, exclude: None)
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)

    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(started.wait(), 5)
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "cancelled"
    assert delivered == []
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"
    assert time.time() - run.started_at < 10
