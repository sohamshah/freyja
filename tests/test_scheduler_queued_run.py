"""A run whose prompt waits behind the turn that started it is answered
"queued", then ends as any run does.

run_now from a tool call in the job's own session cannot wait for its
prompt: that runs after the calling turn ends. The call is answered with
the run as "queued", a status RunStatus did not list. The record then
stayed the dispatch of the prompt: the turn that ran it was no run's, so
the run had no cost and no output, and its sinks got nothing.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import get_args

import pytest

from bridge.scheduler.models import (
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
    RunStatus,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import (
    FileLock,
    job_run_lock_path,
    load_job,
    load_run,
    load_runs_for_job,
    save_job,
)
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from tests.test_scheduler_run_output import operator_session  # noqa: F401


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions)

    def emit_event(self, **_kw) -> None:
        pass


def _stub_session() -> SimpleNamespace:
    """A session whose queued prompts nothing runs."""
    return SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
        queued_messages=[],
    )


async def _run_now_from_a_tool_call(service, sess, job) -> tuple[RunRecord, str]:
    """fire_job as run_now calls it from a tool call of ``sess``'s own turn.
    Returns the run and its status as the call was answered."""
    from bridge.scheduler.runtime import fire_job

    async def tool_call():
        # The session's turn is the task that makes the tool call.
        sess.pending_task = asyncio.current_task()
        run = await fire_job(service, job)
        return run, run.status

    return await asyncio.ensure_future(tool_call())


async def _ended(job_id: str, run_id: str) -> RunRecord:
    for _ in range(100):
        saved = load_run(job_id, run_id)
        if saved.finished_at is not None:
            return saved
        await asyncio.sleep(0.02)
    raise AssertionError("the run did not end")


async def test_a_run_queued_behind_the_turn_that_started_it_has_a_known_status(monkeypatch):
    import bridge.freyja_bridge as fb

    sess = _stub_session()

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None, run_id=None):
        s.queued_messages.append((prompt, attachments, on_turn_start, run_id))
        return False

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)

    run, answered = await _run_now_from_a_tool_call(_Service({"s1": sess}), sess, job)

    assert len(sess.queued_messages) == 1
    assert answered == "queued"
    assert answered in get_args(RunStatus)
    await _ended(job.id, run.run_id)


async def test_a_queued_run_writes_its_end_to_the_events_log_and_tells_the_app(monkeypatch):
    # Every other run that ends does both: this one was saved "queued" and
    # its claim was the last the events log and the dashboard heard of it.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.persistence import read_recent_events

    sess = _stub_session()
    monkeypatch.setattr(fb, "_schedule_or_queue_turn", lambda *_a, **_kw: False)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)
    service = _Service({"s1": sess})
    told: list[dict] = []
    service.emit_event = lambda **kw: told.append(kw)

    run, _answered = await _run_now_from_a_tool_call(service, sess, job)
    saved = await _ended(job.id, run.run_id)

    # Its prompt never ran here: nothing runs the queue.
    ended = f"scheduler_run_{saved.status}"
    assert saved.status == "failed"
    assert [(e["subtype"], e["details"]["runId"]) for e in told] == [
        ("scheduler_run_claimed", run.run_id), ("scheduler_run_queued", run.run_id),
        (ended, run.run_id),
    ]
    logged = [e for e in read_recent_events() if e.get("run_id") == run.run_id]
    assert [e["type"] for e in logged] == ["scheduler_run_claimed", "scheduler_run_queued", ended]


async def test_a_queued_run_counts_as_a_fire(monkeypatch):
    # Its prompt runs once the calling turn ends, as a run's that waits
    # behind another turn does, and that run is counted when it ends.
    # Uncounted, the next run took the same fire number, and a job with
    # max_fires ran past it.
    import bridge.freyja_bridge as fb

    sess = _stub_session()
    monkeypatch.setattr(fb, "_schedule_or_queue_turn", lambda *_a, **_kw: False)
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"), max_fires=1)
    save_job(job)

    run, answered = await _run_now_from_a_tool_call(_Service({"s1": sess}), sess, job)
    await _ended(job.id, run.run_id)

    saved = load_job(job.id)
    assert answered == "queued" and run.fire_number == 1
    assert (saved.fire_count, saved.last_fire_at) == (1, run.started_at)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_max_fires", None)


async def test_a_queued_run_ends_with_the_cost_and_reply_of_its_prompts_turn_and_delivers_it(operator_session):
    # The turn that ran its prompt was no run's: the record kept "queued"
    # with no cost and no output, and its sinks got nothing.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"run the digest now": "It runs after this turn", "write the digest": "Here is the digest"}
    sess.costs = {"run the digest now": 5, "write the digest": 20}
    job = JobRecord(id="sched_digest", name="digest", prompt="write the digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()])
    save_job(job)
    answered: list[tuple[RunRecord, str]] = []

    async def run_now() -> None:  # The schedule tool's run_now, in the calling turn.
        run = await fire_job(_Service({"s1": sess}), job)
        answered.append((run, run.status))

    sess.tool_calls = {"run the digest now": run_now}
    fb._schedule_or_queue_turn(sess, "run the digest now")
    await asyncio.wait_for(asyncio.gather(sess.pending_task), 10)
    [(run, status)] = answered
    saved = await _ended(job.id, run.run_id)

    assert status == "queued"
    assert (saved.status, saved.output_text) == ("succeeded", "Here is the digest")
    assert saved.cost_usd == pytest.approx(0.20)
    assert [r.sink_kind for r in saved.delivery_reports] == ["noop"]
    assert load_job(job.id).fire_count == 1


async def test_a_loops_fire_due_while_its_run_is_queued_waits_for_that_run(monkeypatch):
    # The run lock was released as fire_job returned: the loop's fire, due
    # meanwhile, queued its prompt behind the same turn, and both runs put
    # the same loop tools in the session and took them out by name.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.service import SchedulerService

    sess = _stub_session()

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None, run_id=None):
        s.queued_messages.append((prompt, attachments, on_turn_start, run_id))
        return False

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    service = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    job = JobRecord(id="sched_loop", name="babysit the merge queue", prompt="check the PRs",
                    schedule=SelfPacedSchedule(min_delay_seconds=120, max_delay_seconds=600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)

    async def calling_turn():
        sess.pending_task = asyncio.current_task()
        run = await service.run_job_now(job.id)  # Its tool call.
        # The loop comes due while the turn goes on.
        due = load_job(job.id)
        due.next_fire_at = time.time() - 1
        save_job(due)
        await service._try_claim_and_fire(job.id)
        await asyncio.sleep(0.1)
        return run

    run = await asyncio.ensure_future(calling_turn())

    assert len(sess.queued_messages) == 1
    assert [r.run_id for r in load_runs_for_job(job.id)] == [run.run_id]
    assert load_job(job.id).waiting_slot is not None  # It fires once the run ends.
    await _ended(job.id, run.run_id)
    await asyncio.sleep(0)
    with FileLock(job_run_lock_path(job.id)) as free:
        assert free


async def test_a_queued_runs_prompt_is_queued_before_run_now_answers():
    # It was queued one step after the call returned. A calling turn that
    # ended by then had left no turn running, and the prompt started as a
    # new turn.
    sess = _stub_session()
    sess.id = "s1"
    sess.pending_config = None
    started: list[str] = []

    async def run_turn(content, *_a, **_kw):
        started.append(content)

    sess.run_turn = run_turn
    job = JobRecord(id="sched_digest", name="digest", prompt="write the digest",
                    schedule=IntervalSchedule(seconds=3600),
                    execution=ExistingSession(session_id="s1"))
    save_job(job)
    queued_as_answered: list[int] = []

    async def calling_turn():
        from bridge.scheduler.runtime import fire_job

        sess.pending_task = asyncio.current_task()
        run = await fire_job(_Service({"s1": sess}), job)
        queued_as_answered.append(len(sess.queued_messages))
        return run  # The turn ends as the call returns.

    run = await asyncio.ensure_future(calling_turn())
    await _ended(job.id, run.run_id)

    assert queued_as_answered == [1]
    assert started == []
    assert ["write the digest" in entry[0] for entry in sess.queued_messages] == [True]
