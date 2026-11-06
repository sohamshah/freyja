"""Slack's /reset starts the thread's next message on a fresh conversation.

/reset dropped the thread's session from memory and nothing else. The
thread's next message loaded it again from its saved transcript, so the
conversation went on as if no reset had happened. A turn running at the
reset saved its question there too as it was cancelled.
"""

from __future__ import annotations

import asyncio

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp

KEY = "freyja:slack:T1:channel:C1:1700000000.000100"


async def _thread(monkeypatch, tmp_path):
    """The gateway's state with a Slack thread loaded, on a model and in
    a mode of its own, one turn of which is saved."""
    from engine.types import Message

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session(KEY, model_id="claude-sonnet-5-5", coordination_strategy="goal")
    await sess.initialize()
    sess.session.transcript.append_message(Message(role="user", content="the old question"))
    sess.session.transcript.append_message(Message(role="assistant", content="the old answer"))
    sess._save_transcript()
    return state, sess


def _texts(sess) -> str:
    if sess.session is None:  # nothing loaded: an empty conversation
        return ""
    return " ".join(str(e.message.content) for e in sess.session.transcript.entries if e.message)


def _gateway(state):
    from bridge.gateway.run import GatewayDaemon

    gateway = GatewayDaemon()
    gateway.state = state
    return gateway


@pytest.mark.parametrize("turn_running", [False, True])
async def test_the_next_message_after_reset_starts_a_fresh_conversation(monkeypatch, tmp_path, turn_running):
    state, sess = await _thread(monkeypatch, tmp_path)
    if turn_running:
        asked = asyncio.Event()

        class _Runner:
            async def run(self, session, message, stream=True):
                session.add_user_message(message)
                asked.set()
                await asyncio.Event().wait()

        async def _initialize():
            return None

        sess.initialize = _initialize
        sess.runner = _Runner()
        sess.pending_task = asyncio.create_task(sess.run_turn("a new question"))
        await asyncio.wait_for(asked.wait(), 1)

    _gateway(state)._reset_session(KEY)  # /reset
    if turn_running:
        await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    fresh = await state.ensure_session(KEY)  # the thread's next message

    assert fresh is not sess
    assert "old" not in _texts(fresh) and "a new question" not in _texts(fresh)
    # The thread keeps its model and mode.
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")
    # The old conversation stays on disk, where no load reads it.
    (aside,) = tp.SESSIONS_DIR.glob("*.transcript.json.reset-*")
    assert "the old answer" in aside.read_text()


async def test_reset_of_a_thread_the_gateway_has_not_loaded_since_it_started(monkeypatch, tmp_path):
    state, _sess = await _thread(monkeypatch, tmp_path)
    state.sessions.clear()  # the gateway restarted

    _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)

    assert "old" not in _texts(fresh)
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")
