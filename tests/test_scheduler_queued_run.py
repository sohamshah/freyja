"""A run whose prompt waits behind the turn that started it has a status
RunStatus lists.

run_now from a tool call in the job's own session cannot wait for its
prompt: that runs after the calling turn ends. The run is recorded as
"queued", the dispatch of the prompt, which RunStatus did not list.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import get_args

from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, RunStatus
from bridge.scheduler.persistence import load_run, save_job
from engine.session import TranscriptManager
from engine.tools import ToolRegistry


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions)

    def emit_event(self, **_kw) -> None:
        pass


async def test_a_run_queued_behind_the_turn_that_started_it_has_a_known_status(monkeypatch):
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
        queued_messages=[],
    )

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None):
        s.queued_messages.append((prompt, attachments, on_turn_start))
        return False

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)

    async def run_now_from_a_tool_call():
        # The session's turn is the task that makes the tool call.
        sess.pending_task = asyncio.current_task()
        return await fire_job(_Service({"s1": sess}), job)

    run = await asyncio.ensure_future(run_now_from_a_tool_call())

    assert len(sess.queued_messages) == 1
    assert load_run(job.id, run.run_id).status == run.status == "queued"
    assert run.status in get_args(RunStatus)
