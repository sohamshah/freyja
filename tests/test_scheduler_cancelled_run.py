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


def _session_with_a_long_turn(monkeypatch):
    """A session whose turn writes half a report, then works on; and the
    deliveries made."""
    import bridge.freyja_bridge as fb
    import bridge.scheduler.runtime as runtime

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=0.0,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    sess.started = asyncio.Event()

    async def turn(prompt: str) -> None:
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=prompt))
        transcript.append_message(Message(role="assistant", content="Half of the report"))
        sess.started.set()
        await asyncio.sleep(60)

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None, run_id=None):
        on_turn_start()
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
    return sess, delivered


def _report_job() -> JobRecord:
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    return job


async def test_a_cancelled_run_is_not_delivered(monkeypatch):
    from bridge.scheduler.runtime import fire_job

    sess, delivered = _session_with_a_long_turn(monkeypatch)
    job = _report_job()

    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(sess.started.wait(), 5)
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "cancelled"
    assert delivered == []
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"
    assert time.time() - run.started_at < 10


async def test_a_run_whose_queued_prompt_the_emergency_stop_dropped_is_cancelled(monkeypatch, tmp_path):
    """A fire into a session that is busy waits in its queue. The emergency
    stop drops that queue: the run never ran, and the last reply in the
    transcript is the operator's stopped turn, not this run's output."""
    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)  # this is the app's bridge
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1")
    await sess.initialize()
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=content))
        transcript.append_message(Message(role="assistant", content=f"Half an answer to {content}"))
        if content == "the operator's question":
            await asyncio.sleep(60)

    sess.run_turn = run_turn
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    fb._schedule_or_queue_turn(sess, "the operator's question")
    job = _report_job()
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    for _ in range(200):
        if sess.queued_messages:
            break
        await asyncio.sleep(0)
    await fb._handle_command(state, {"type": "computer.emergency_stop", "reason": "user"})
    run = await asyncio.wait_for(fire, 10)

    assert ran == ["the operator's question"]
    assert delivered == []
    assert run.status == "cancelled"
    assert "emergency stop" in run.error
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"


async def _a_run_queued_behind_the_operators_turn(monkeypatch, tmp_path, *, timeout=None):
    """A fire into a session whose turn the operator started waits in its
    queue. Returns the session, the turns that ran, the fire's job, its
    task and the deliveries made."""
    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)  # this is the app's bridge
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1")
    await sess.initialize()
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=content))
        transcript.append_message(Message(role="assistant", content=f"Half an answer to {content}"))
        if content == "the operator's question":
            await asyncio.sleep(60)

    sess.run_turn = run_turn
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    fb._schedule_or_queue_turn(sess, "the operator's question")
    job = _report_job()
    job.timeout_seconds = timeout
    save_job(job)
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    for _ in range(200):
        if sess.queued_messages:
            break
        await asyncio.sleep(0)
    return sess, ran, job, fire, delivered


async def test_a_queued_run_that_is_cancelled_leaves_the_operators_turn_running(monkeypatch, tmp_path):
    """A run cancelled while its prompt still waits in the queue stopped
    whatever turn was running, the operator's, and its prompt stayed
    queued and ran after it. Only the run's prompt goes."""
    sess, ran, job, fire, delivered = await _a_run_queued_behind_the_operators_turn(monkeypatch, tmp_path)
    operators_turn = sess.pending_task
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    run = await asyncio.wait_for(fire, 10)

    assert not operators_turn.done()
    assert sess.queued_messages == []
    assert run.status == "cancelled"
    assert run.output_text == "" and delivered == []
    operators_turn.cancel()
    await asyncio.gather(operators_turn, return_exceptions=True)
    assert ran == ["the operator's question"]


async def test_a_queued_run_that_times_out_leaves_the_operators_turn_running(monkeypatch, tmp_path):
    sess, ran, job, fire, delivered = await _a_run_queued_behind_the_operators_turn(
        monkeypatch, tmp_path, timeout=0.2,
    )
    operators_turn = sess.pending_task
    run = await asyncio.wait_for(fire, 10)

    assert not operators_turn.done()
    assert sess.queued_messages == []
    assert run.status == "timed_out"
    assert run.output_text == "" and delivered == []
    operators_turn.cancel()
    await asyncio.gather(operators_turn, return_exceptions=True)
    assert ran == ["the operator's question"]


async def test_a_run_that_times_out_after_its_turn_leaves_the_operators_next_turn_running(monkeypatch, tmp_path):
    """The run waits for its sub-agents after its turn ends. A turn the
    operator started meanwhile is not the run's to stop."""
    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1")
    await sess.initialize()
    child = SimpleNamespace(id="sub_1", label="researcher")
    children: list = []
    sess._running_background_work = lambda: list(children)
    stopped_children: list = []
    monkeypatch.setattr(fb, "_stop_background_work", lambda s, exclude=(): stopped_children.append(s))
    turn_no = 0

    async def run_turn(content, _attachments=None, **_kw):
        nonlocal turn_no
        turn_no += 1
        sess.current_turn_id = f"turn-{turn_no}"
        fb.emit({"type": "turn_start", "sessionId": sess.id, "turnId": sess.current_turn_id,
                 "runId": _kw.get("run_id")})
        if content == "the operator's next question":
            await asyncio.sleep(60)
        else:
            children.append(child)  # the run's sub-agent works on

    sess.run_turn = run_turn
    monkeypatch.setattr(runtime, "deliver_all", lambda *a: asyncio.sleep(0, []))
    job = _report_job()
    job.timeout_seconds = 0.5
    save_job(job)
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    for _ in range(200):
        if children and sess.pending_task.done():
            break
        await asyncio.sleep(0.01)
    fb._schedule_or_queue_turn(sess, "the operator's next question")
    operators_turn = sess.pending_task
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "timed_out"
    assert stopped_children == [sess]
    assert not operators_turn.done()
    operators_turn.cancel()
    await asyncio.gather(operators_turn, return_exceptions=True)


async def test_a_run_asked_to_stop_while_its_session_loads_stops(monkeypatch):
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    sess, delivered = _session_with_a_long_turn(monkeypatch)
    loading = asyncio.Event()
    loaded = asyncio.Event()

    async def resolve(_service, _job):
        loading.set()
        await loaded.wait()
        return sess, "s1", False

    monkeypatch.setattr(runtime, "_resolve_execution_session", resolve)
    job = _report_job()

    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(loading.wait(), 5)
    request_run_cancellation(job.id, load_runs_for_job(job.id, limit=1)[0].run_id)
    loaded.set()
    run = await asyncio.wait_for(fire, 10)

    assert run.status == "cancelled"
    assert delivered == []


# ─── a run asked to stop stays in its job's history ───────────────────


def _running_run(job_id: str = "sched_report"):
    from bridge.scheduler.models import RunRecord
    from bridge.scheduler.persistence import save_run

    run = RunRecord(run_id="run_live", job_id=job_id, job_name="report",
                    started_at=time.time(), status="running")
    save_run(run)
    return run


def test_a_run_asked_to_stop_is_still_listed():
    from bridge.scheduler.persistence import load_recent_runs_global, load_run

    run = _running_run()
    assert request_run_cancellation(run.job_id, run.run_id)

    assert [r.run_id for r in load_runs_for_job(run.job_id)] == [run.run_id]
    assert [r.run_id for r in load_recent_runs_global()] == [run.run_id]
    assert load_run(run.job_id, run.run_id) is not None


def test_every_key_a_run_record_is_saved_with_reads_back():
    import dataclasses
    import json

    from bridge.scheduler.models import DeliveryReport, RunRecord
    from bridge.scheduler.persistence import load_run, runs_dir, save_run

    run = _running_run()
    run.delivery_reports = [DeliveryReport(sink_index=0, sink_kind="noop", success=True,
                                           delivered_at=time.time())]
    save_run(run)
    request_run_cancellation(run.job_id, run.run_id)

    on_disk = json.loads((runs_dir(run.job_id) / f"{run.run_id}.json").read_text())
    loaded = load_run(run.job_id, run.run_id)
    assert set(on_disk) <= {f.name for f in dataclasses.fields(RunRecord)}
    assert loaded.to_dict() == {**run.to_dict(), "cancel_requested": True}


def test_a_run_record_from_a_newer_scheduler_still_reads():
    from bridge.scheduler.models import RunRecord

    run = RunRecord.from_dict({**_running_run().to_dict(), "a_later_field": 1})

    assert run.run_id == "run_live"


# ─── cancel_run stops a run of its own process at once ────────────────


async def test_cancel_run_stops_a_run_of_this_process_at_once(monkeypatch):
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.persistence import load_run
    from bridge.scheduler.service import SchedulerService

    sess, delivered = _session_with_a_long_turn(monkeypatch)
    # Left to the cancel flag, the run would stop at its next look at it.
    monkeypatch.setattr(runtime, "_CANCEL_POLL_INTERVAL", 30.0)
    service = SchedulerService(SimpleNamespace(permission_tier="low", sessions={"s1": sess}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    job = _report_job()
    job.next_fire_at = time.time() - 1
    save_job(job)
    await service._try_claim_and_fire(job.id)
    await asyncio.wait_for(sess.started.wait(), 5)
    run_id = load_runs_for_job(job.id, limit=1)[0].run_id

    assert await service.cancel_run(run_id)
    await asyncio.wait_for(asyncio.gather(*service._in_flight_runs.values()), 5)

    run = load_run(job.id, run_id)
    assert (run.status, run.cancel_requested) == ("cancelled", True)
    assert run.finished_at is not None
    assert delivered == []


# ─── the operator's stop of a run's turn cancels the run ──────────────


async def _a_run_whose_turn_writes_half_a_report(monkeypatch, tmp_path):
    """A fire into the operator's session, run by the real run_turn: its
    turn writes half a report, then works on. Returns the bridge's state,
    the session, the fire's job, its task and the deliveries made."""
    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)  # this is the app's bridge
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1")
    await sess.initialize()
    writing = asyncio.Event()

    class _Runner:
        usage = SimpleNamespace(input=0, output=0, cache_read=0, cache_write=0,
                                effective_context_tokens=lambda: 0)

        async def run(self, session, message, stream=True):
            session.add_user_message(message)
            if "[Scheduled run" not in str(message):
                session.add_assistant_message("The build is green")
                return SimpleNamespace(success=True, response="The build is green", stop_reason="end_turn")
            session.add_assistant_message("Half of the report")
            writing.set()
            await asyncio.sleep(60)

    sess.runner = _Runner()
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    job = _report_job()
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), job))
    await asyncio.wait_for(writing.wait(), 5)
    return state, sess, job, fire, delivered


async def test_a_run_whose_turn_the_operator_stops_is_cancelled_and_not_delivered(monkeypatch, tmp_path):
    """Stopped, its turn ended, and the run went on as a finished one: its
    partial answer went to the job's sinks, as the emergency stop's did
    before 8cc6623."""
    import bridge.freyja_bridge as fb

    state, sess, job, fire, delivered = await _a_run_whose_turn_writes_half_a_report(monkeypatch, tmp_path)
    await fb._handle_command(state, {"type": "cancel", "sessionId": "s1", "scope": "turn"})
    run = await asyncio.wait_for(fire, 10)

    assert delivered == []
    assert run.status == "cancelled"
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"


async def test_a_turn_queued_behind_the_stopped_run_still_runs(monkeypatch, tmp_path):
    """A normal stop lets the queue run: only the run is over."""
    import bridge.freyja_bridge as fb

    state, sess, job, fire, delivered = await _a_run_whose_turn_writes_half_a_report(monkeypatch, tmp_path)
    fb._schedule_or_queue_turn(sess, "how is the build?")
    await fb._handle_command(state, {"type": "cancel", "sessionId": "s1", "scope": "turn"})
    run = await asyncio.wait_for(fire, 10)
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=5)

    assert run.status == "cancelled" and delivered == []
    assert sess.session.transcript.entries[-1].message.content == "The build is green"
