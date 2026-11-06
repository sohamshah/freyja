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


# ── its mode ─────────────────────────────────────────────────────────
#
# A thread's coordination strategy is saved with its transcript, and
# nothing else tells the gateway what it was: no renderer sends it, as
# the app's does for its own sessions. Loaded as bus, a kanban thread had
# no board after a restart, so its cards stopped and the app's board
# commands were refused, and a goal thread lost its loop.


def _inbound(text: str = "how is it going?"):
    from bridge.gateway.platforms.base import IncomingMessage

    source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="1.0",
    )
    return IncomingMessage(source=source, text=text)


async def _load_by_message(state):
    from bridge.gateway.session_router import route as route_message

    _key, sess = await route_message(_inbound(), state)
    return sess


async def _load_by_talk(state):
    await fb.deliver_talk_message_locally(state, SLACK, _talk())
    return state.sessions[SLACK]


async def _load_for_a_memo(state):
    return await fb._cold_load_session(state, SLACK)


@pytest.mark.parametrize("load", [_load_by_message, _load_by_talk, _load_for_a_memo])
@pytest.mark.parametrize("strategy", ["kanban", "goal"])
async def test_a_thread_comes_back_in_the_mode_it_was_saved_in(gateway, load, strategy):
    state, _events = gateway
    _thread_saved(strategy)
    sess = await load(state)
    assert sess.coordination_strategy == strategy
    assert (sess.kanban_board is not None) == (strategy == "kanban")


async def test_a_mode_asked_for_on_a_thread_not_loaded_wins_over_the_saved_one(gateway):
    from bridge.gateway.session_router import route as route_message

    state, _events = gateway
    _thread_saved("kanban")
    _key, sess = await route_message(_inbound("--mode bus go"), state, default_strategy="bus")
    assert sess.coordination_strategy == "bus"


async def test_a_new_thread_starts_as_bus(gateway):
    state, _events = gateway
    sess = await _load_by_message(state)
    assert sess.coordination_strategy == "bus"


def _daemon(state):
    from bridge.gateway.run import GatewayDaemon

    daemon = GatewayDaemon()
    daemon.state = state
    state.talk_wake_hook_factory = lambda _sess: "hook"
    return daemon


async def test_a_kanban_command_for_a_thread_not_loaded_loads_it_and_runs_on_its_board(gateway):
    from bridge.kanban_journal import KanbanJournal, journal_path
    from bridge.tools.kanban_board import SessionKanbanBoard

    state, events = gateway
    _thread_saved("kanban")
    board = SessionKanbanBoard(journal=KanbanJournal(journal_path(SLACK)))
    card = await board.create(title="ship it", actor="parent")
    daemon = _daemon(state)

    await daemon._on_kanban_command(
        {"type": "kanban_operator_cancel", "sessionId": SLACK, "cardId": card.id},
    )

    sess = state.sessions[SLACK]
    assert (await sess.kanban_board.get(card.id)).status == "cancelled"
    rows = KanbanJournal(journal_path(SLACK)).read_all()
    assert (rows[-1]["kind"], rows[-1]["task"]["status"]) == ("update", "cancelled")
    # A worker's memo wakes the thread; its turn streams to the thread.
    assert sess.talk_wake_hook == "hook"
    assert not [e for e in events if e.get("subtype") == "control_command_skipped"]


async def test_a_kanban_command_for_a_sub_agent_builds_no_session_on_its_id(gateway):
    state, events = gateway
    daemon = _daemon(state)
    await daemon._on_kanban_command(
        {"type": "kanban_operator_cancel", "sessionId": "sub_19d8b53e6f0_1", "cardId": "c"},
    )
    assert state.sessions == {}
    (note,) = [e for e in events if e.get("subtype") == "control_command_skipped"]
    assert note["sessionId"] == "sub_19d8b53e6f0_1"
