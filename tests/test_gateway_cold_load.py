"""A Slack thread the gateway loads from disk is set up as a Slack thread.

The gateway loads a thread it has not run since it started for more than
its messages: a talk() message, a sub-agent's memo, an app command, or
/mode, /model and /goal on the thread. The thread's first initialize
applies the Slack tool filter and registers send_attachment from its
gateway source, so a load that gave it none left it without either for
as long as the gateway ran.
"""

from __future__ import annotations

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.gateway.platforms.base import MessageSource, Platform
from bridge.inbox import InboxMessage

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """The gateway's bridge state: this process holds the gateway lock."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    # A woken thread would start a turn: a model call.
    monkeypatch.setattr(fb._BridgeSession, "wake_for_inbox", lambda self: "woken")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    yield state, events
    for sess in state.sessions.values():
        sess._stop_kanban_dispatcher()


def _thread_saved(strategy: str = "bus") -> None:
    from engine.session import Session

    session = Session.create(system_prompt="t")
    session.add_user_message("plan the release")
    session.add_assistant_message("on it")
    data = session.serialize_transcript()
    data.setdefault("metadata", {}).update(
        model_id="claude-sonnet-5-5", reasoning_level="auto", coordination_strategy=strategy,
    )
    tp.save_transcript(SLACK, data)


def _filter_on() -> None:
    from bridge.gateway.config import config_path

    config_path().write_text("slack:\n  enable_tool_filter: true\n")


def _tools(sess) -> set[str]:
    return set(sess.tool_registry._tools)  # noqa: SLF001


def _talk(content: str = "the build is green") -> InboxMessage:
    return InboxMessage(
        id="m1", from_session="desktop-1", from_label="desktop", from_role="agent", content=content,
    )


async def test_a_thread_a_talk_message_loads_has_its_slack_source_and_filter(gateway):
    state, _events = gateway
    _thread_saved()
    _filter_on()
    await fb.deliver_talk_message_locally(state, SLACK, _talk())
    sess = state.sessions[SLACK]
    source = getattr(sess, "gateway_source", None)
    assert source is not None and (source.chat_id, source.thread_id) == ("C1", "1.0")
    assert "send_attachment" in _tools(sess)
    assert "bash" not in _tools(sess)


async def test_a_thread_loaded_for_a_sub_agents_memo_has_its_slack_source(gateway):
    state, _events = gateway
    _thread_saved()
    sess = await fb._cold_load_session(state, SLACK)
    assert getattr(sess, "gateway_source", None) is not None and sess.gateway_source.chat_id == "C1"
    assert "send_attachment" in _tools(sess)


async def test_slash_mode_on_a_thread_not_loaded_gives_it_its_slack_source(gateway):
    from bridge.gateway.platforms.base import IncomingMessage
    from bridge.gateway.run import GatewayDaemon

    state, _events = gateway
    _thread_saved()

    class _Adapter:
        async def send(self, *_a, **_kw):
            return None

    daemon = GatewayDaemon()
    daemon.state = state
    source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="1.0",
    )
    message = IncomingMessage(
        source=source, text="/mode kanban", is_slash_command=True, slash_command_name="mode",
        slash_command_args="kanban",
    )
    await daemon._handle_mode_command(message, _Adapter())
    sess = state.sessions[SLACK]
    assert getattr(sess, "gateway_source", None) is not None and sess.gateway_source.chat_id == "C1"
    assert "send_attachment" in _tools(sess)


async def test_a_copy_of_the_thread_in_the_app_gets_no_slack_source(gateway, monkeypatch):
    # The app's copy runs no turn; it has nothing to post to Slack.
    state, _events = gateway
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    _thread_saved()
    sess = await state.ensure_session(SLACK)
    assert getattr(sess, "gateway_source", None) is None
