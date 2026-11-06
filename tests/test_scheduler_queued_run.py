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


async def test_a_queued_run_writes_its_end_to_the_events_log_and_tells_the_app(monkeypatch):
    # Every other run that ends does both: this one was saved "queued" and
    # its claim was the last the events log and the dashboard heard of it.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.persistence import read_recent_events
    from bridge.scheduler.runtime import fire_job

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    monkeypatch.setattr(fb, "_schedule_or_queue_turn", lambda *_a, **_kw: False)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)
    service = _Service({"s1": sess})
    told: list[dict] = []
    service.emit_event = lambda **kw: told.append(kw)

    async def run_now_from_a_tool_call():
        sess.pending_task = asyncio.current_task()
        return await fire_job(service, job)

    run = await asyncio.ensure_future(run_now_from_a_tool_call())

    assert [(e["subtype"], e["details"]["runId"]) for e in told] == [
        ("scheduler_run_claimed", run.run_id), ("scheduler_run_queued", run.run_id),
    ]
    logged = [e for e in read_recent_events() if e.get("run_id") == run.run_id]
    assert [e["type"] for e in logged] == ["scheduler_run_claimed", "scheduler_run_queued"]


async def test_a_queued_run_counts_as_a_fire(monkeypatch):
    # Its prompt runs once the calling turn ends, as a run's that waits
    # behind another turn does, and that run is counted when it ends.
    # Uncounted, the next run took the same fire number, and a job with
    # max_fires ran past it.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.persistence import load_job
    from bridge.scheduler.runtime import fire_job

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    monkeypatch.setattr(fb, "_schedule_or_queue_turn", lambda *_a, **_kw: False)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"), max_fires=1)
    save_job(job)

    async def run_now_from_a_tool_call():
        sess.pending_task = asyncio.current_task()
        return await fire_job(_Service({"s1": sess}), job)

    run = await asyncio.ensure_future(run_now_from_a_tool_call())

    saved = load_job(job.id)
    assert run.status == "queued" and run.fire_number == 1
    assert (saved.fire_count, saved.last_fire_at) == (1, run.started_at)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_max_fires", None)
