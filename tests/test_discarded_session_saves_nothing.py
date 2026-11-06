"""A session deleted with its files, replaced by a new session with its id,
or reset by Slack's /reset saves no inbox or settings file again.

dcd6133 stopped such a session's transcript saves (_discarded). Its inbox
still saved on every change (_on_inbox_change -> _save_inbox): a message
pushed into it by whatever still held it, or a follow-up its stop handed
back, wrote the id's inbox file, which the next session with that id reads.
"""

from __future__ import annotations

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.inbox import InboxMessage, new_message_id

KEY = "freyja:slack:T1:channel:C1:1700000000.000100"


def _talk(content: str) -> InboxMessage:
    return InboxMessage(
        id=new_message_id(), from_session="sub_x", from_label="worker", from_role="agent", content=content,
    )


def _unread(sid: str) -> list[str]:
    return [m["content"] for m in (tp.load_inbox_state(sid) or {}).get("unread", [])]


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    return fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")


async def test_a_deleted_session_writes_no_inbox_file(state):
    sess = await state.ensure_session("session-desktop")
    sess._save_transcript()
    await fb._handle_command(state, {"type": "delete_session", "sessionId": sess.id})

    sess.inbox.push(_talk("tests pass"))

    assert not tp._inbox_path(sess.id).exists()


async def test_a_replaced_session_writes_nothing_into_the_new_sessions_inbox_file(state):
    old = await state.ensure_session("session-desktop")
    await fb._handle_command(state, {"type": "new_session", "sessionId": old.id})
    new = state.sessions[old.id]
    new.inbox.push(_talk("for the new session"))

    old.inbox.push(_talk("for the old session"))

    assert _unread(old.id) == ["for the new session"]


async def _reset_thread(state):
    from bridge.gateway.run import GatewayDaemon

    sess = await state.ensure_session(KEY, model_id="claude-sonnet-5-5")
    gateway = GatewayDaemon()
    gateway.state = state
    gateway._reset_session(KEY)  # /reset
    return sess


async def test_a_thread_reset_by_slack_writes_no_inbox_file(state):
    sess = await _reset_thread(state)

    sess.inbox.push(_talk("tests pass"))

    assert not tp._inbox_path(KEY).exists()


async def test_a_thread_reset_by_slack_writes_no_settings_file(state):
    # The file is the next session's with the thread's id: the old model
    # went over the one that session was given before its first message.
    sess = await _reset_thread(state)
    await state.ensure_session(KEY, model_id="kimi-k3-fast")

    sess._save_settings_alone()

    assert tp.load_transcript(KEY)["metadata"]["model_id"] == "kimi-k3-fast"
