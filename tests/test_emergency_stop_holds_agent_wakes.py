"""After the emergency stop, agents' messages do not wake a session.

After a stop, a message an agent sent during the stopped turn wakes the
session about two seconds later. After the emergency stop it did the same,
and an agent's message arriving later woke the idle session too. Such
messages now wait unread in the inbox, and the chat says so, until the
operator sends something.
"""

from __future__ import annotations

import asyncio

import pytest

import bridge.freyja_bridge as fb
from bridge.inbox import InboxMessage, new_message_id


def _agent_message(text: str) -> InboxMessage:
    return InboxMessage(
        id=new_message_id(), from_session="sub_x", from_label="worker",
        from_role="agent", content=text,
    )


async def _until(done) -> None:
    for _ in range(200):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def _stopped_session(monkeypatch, tmp_path):
    """A session whose turn got an agent's message, then the emergency stop.
    Returns it, the turns run, and the events emitted."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "TALK_WAKE_AFTER_STOP_S", 0.05)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("session-desktop")
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        if content == "first":
            await asyncio.sleep(60)
        sess.inbox.drain_injectable()  # its steps take in what waits

    sess.run_turn = run_turn
    fb._schedule_or_queue_turn(sess, "first")
    await _until(lambda: ran)
    sess.inbox.push(_agent_message("tests pass"))
    await fb._handle_command(state, {"type": "computer.emergency_stop", "reason": "user"})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    await asyncio.sleep(0.2)  # past the wake a normal stop makes
    return state, sess, ran, events


async def test_an_agents_message_from_the_stopped_turn_waits_for_the_operator(monkeypatch, tmp_path):
    state, sess, ran, events = await _stopped_session(monkeypatch, tmp_path)
    assert ran == ["first"]
    assert [m.content for m in sess.inbox.unread] == ["tests pass"]
    (note,) = [e for e in events if e.get("subtype") == "agent_messages_held"]
    assert note["sessionId"] == sess.id
    assert note["details"] == {"count": 1, "chatVisible": True}

    # The operator sends something: the message goes with that turn.
    await fb._handle_command(state, {"type": "send_message", "sessionId": sess.id, "content": "go on"})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert ran == ["first", "go on"]
    assert sess.inbox.unread == []


async def test_an_agents_message_after_the_emergency_stop_does_not_wake_the_session(monkeypatch, tmp_path):
    state, sess, ran, events = await _stopped_session(monkeypatch, tmp_path)
    sess.inbox.push(_agent_message("one more thing"))
    status = sess.wake_for_inbox()
    assert "emergency stop" in status
    await asyncio.sleep(0.1)
    assert ran == ["first"]
    assert len([e for e in events if e.get("subtype") == "agent_messages_held"]) == 2

    # Once the operator has sent something, agents wake it again.
    await fb._handle_command(state, {"type": "send_message", "sessionId": sess.id, "content": "go on"})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    sess.inbox.push(_agent_message("and another"))
    sess.wake_for_inbox()
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert ran[:2] == ["first", "go on"] and len(ran) == 3 and ran[2] in fb.SYNTHETIC_WAKE_PROMPTS


async def test_after_a_normal_stop_an_agents_message_still_wakes_the_session(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "TALK_WAKE_AFTER_STOP_S", 0.05)
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
    await _until(lambda: ran)
    sess.inbox.push(_agent_message("tests pass"))
    await fb._handle_command(state, {"type": "cancel", "sessionId": sess.id, "scope": "turn"})
    await _until(lambda: len(ran) == 2)
    assert ran[1] in fb.SYNTHETIC_WAKE_PROMPTS


@pytest.mark.parametrize(
    "command",
    [
        {"type": "edit_user_message", "messageOrdinal": 0, "content": "first, again"},
        {"type": "rerun_user_message", "messageOrdinal": 0},
    ],
)
async def test_an_edit_or_rerun_ends_the_hold_as_a_message_does(monkeypatch, tmp_path, command):
    """The operator acting: what waited goes with the turn, and agents wake
    the session again."""
    state, sess, ran, events = await _stopped_session(monkeypatch, tmp_path)
    await sess.initialize()
    sess.session.add_user_message("first")
    await fb._handle_command(state, {**command, "sessionId": sess.id})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert len(ran) == 2 and sess.inbox.unread == []

    sess.inbox.push(_agent_message("and another"))
    status = sess.wake_for_inbox()
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    assert "emergency stop" not in status
    assert len(ran) == 3 and ran[2] in fb.SYNTHETIC_WAKE_PROMPTS
