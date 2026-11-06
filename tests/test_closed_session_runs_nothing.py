"""A deleted or replaced session runs nothing more.

Deleting a session cancelled its running turn, but the turn task then
drained the session's queue as after any stop, and ran each queued turn
on the deleted session. A new session made with the id of a loaded one
did the same with the old one's queue.

An agent's message woke a deleted or replaced session that something
still held (wake_for_inbox), such as a scheduled run's nudge
(wait_until_quiescent), and a turn ran on it.
"""

from __future__ import annotations

import asyncio

import pytest

import bridge.freyja_bridge as fb
from bridge.inbox import InboxMessage, new_message_id


async def _until(done) -> None:
    for _ in range(200):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def _busy_session(monkeypatch, tmp_path):
    """A session with a turn running and two turns queued behind it.
    Returns the bridge's state, the session and the turns run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        if content == "first":
            await asyncio.sleep(60)

    sess.run_turn = run_turn
    fb._schedule_or_queue_turn(sess, "first")
    fb._schedule_or_queue_turn(sess, "second")
    fb._schedule_or_queue_turn(sess, "third")
    await _until(lambda: ran)
    return state, sess, ran


async def test_a_deleted_session_runs_none_of_its_queued_turns(monkeypatch, tmp_path):
    state, sess, ran = await _busy_session(monkeypatch, tmp_path)
    await fb._handle_command(state, {"type": "delete_session", "sessionId": sess.id})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)

    assert ran == ["first"]
    assert sess.queued_messages == []


async def test_a_replaced_session_runs_none_of_its_queued_turns(monkeypatch, tmp_path):
    state, sess, ran = await _busy_session(monkeypatch, tmp_path)
    await fb._handle_command(state, {"type": "new_session", "sessionId": sess.id})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)

    assert ran == ["first"]
    assert sess.queued_messages == []
    assert state.sessions[sess.id] is not sess


async def test_the_gateways_reset_session_runs_none_of_its_queued_turns(monkeypatch, tmp_path):
    from bridge.gateway.run import GatewayDaemon

    state, sess, ran = await _busy_session(monkeypatch, tmp_path)
    gateway = GatewayDaemon()
    gateway.state = state
    gateway._reset_session(sess.id)  # /reset
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)

    assert ran == ["first"]
    assert sess.queued_messages == []


async def test_a_run_whose_queued_prompt_went_with_its_deleted_session_is_cancelled(monkeypatch, tmp_path):
    """Its prompt never ran: the last reply in the transcript is the
    operator's stopped turn, not this run's output."""
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from types import SimpleNamespace

    from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
    from bridge.scheduler.persistence import save_job
    from bridge.scheduler.runtime import fire_job
    from engine.types import Message

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
        await asyncio.sleep(60)

    sess.run_turn = run_turn
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    fb._schedule_or_queue_turn(sess, "the operator's question")
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    service = SimpleNamespace(state=SimpleNamespace(permission_tier="low", sessions={"s1": sess}),
                              emit_event=lambda **_kw: None)
    fire = asyncio.create_task(fire_job(service, job))
    await _until(lambda: sess.queued_messages)
    await fb._handle_command(state, {"type": "delete_session", "sessionId": "s1"})
    run = await asyncio.wait_for(fire, 10)

    assert ran == ["the operator's question"]
    assert delivered == []
    assert run.status == "cancelled"
    assert "deleted" in run.error


@pytest.mark.parametrize("closing", ["delete_session", "new_session"])
async def test_a_closed_session_is_not_woken(monkeypatch, tmp_path, closing):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)

    sess.run_turn = run_turn
    await fb._handle_command(state, {"type": closing, "sessionId": sess.id})
    sess.inbox.push(InboxMessage(
        id=new_message_id(), from_session="sub_x", from_label="worker",
        from_role="agent", content="tests pass",
    ))
    status = sess.wake_for_inbox()
    await asyncio.sleep(0.05)

    assert ran == []
    assert sess.pending_task is None
    assert "closed" in status
