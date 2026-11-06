"""Commands the app sends for a Slack session run in the gateway.

The gateway runs a Slack session's thread and holds its kanban board,
model and goal. A command the app ran on a copy of the session changed
only that copy: a kanban card written by the copy's board went into the
same journal the gateway's board writes, with a card id the gateway's
board gave out again.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.kanban_journal import KanbanJournal, journal_path
from bridge.tools.kanban_board import SessionKanbanBoard

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def app(monkeypatch, tmp_path):
    """The app's bridge: a real bridge state, in a process that does not
    hold the gateway lock. Records what it emits."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    yield state, events
    for sess in state.sessions.values():
        sess._stop_kanban_dispatcher()


def _gateway_running(monkeypatch, pid: int | None) -> None:
    import bridge.gateway.pid as gp

    monkeypatch.setattr(gp, "get_running_pid", lambda: pid)


def _forwarded() -> list[dict]:
    from bridge.gateway.control_channel import commands_path

    path = commands_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _thread_saved(strategy: str = "kanban") -> None:
    """The gateway's saved transcript of the thread."""
    from engine.session import Session

    session = Session.create(system_prompt="t")
    session.add_user_message("plan the release")
    session.add_assistant_message("on it")
    data = session.serialize_transcript()
    data.setdefault("metadata", {}).update(
        model_id="claude-sonnet-5-5", reasoning_level="auto", coordination_strategy=strategy,
    )
    tp.save_transcript(SLACK, data)


def _journal() -> list[dict]:
    return KanbanJournal(journal_path(SLACK)).read_all()


# ── kanban ───────────────────────────────────────────────────────────


async def test_a_card_added_in_the_app_is_written_by_the_gateways_board_alone(app, monkeypatch):
    state, _events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    _thread_saved()
    board = SessionKanbanBoard(journal=KanbanJournal(journal_path(SLACK)))
    await board.create(title="from the thread", actor="parent")
    # The app holds a copy of the thread in kanban mode (the mode menu
    # builds one).
    await fb._handle_command(
        state,
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban",
         "model": "claude-sonnet-5-5"},
    )
    await fb._handle_command(state, {"type": "kanban_operator_create", "sessionId": SLACK, "title": "from the app"})
    await board.create(title="next in the thread", actor="parent")

    created = [row["task"]["id"] for row in _journal() if row["kind"] == "create"]
    assert created == ["card_001", "card_002"]
    assert [row["kind"] for row in _journal()] == ["create", "create"]
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["sessionId"], cmd["title"]) == ("kanban_operator_create", SLACK, "from the app")


async def test_a_copy_in_the_app_has_no_board_to_write_or_dispatch_from(app, monkeypatch):
    state, _events = app
    _thread_saved()
    board = SessionKanbanBoard(journal=KanbanJournal(journal_path(SLACK)))
    await board.create(title="from the thread", actor="parent")
    await fb._handle_command(
        state,
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban",
         "model": "claude-sonnet-5-5"},
    )
    copy = state.sessions[SLACK]
    assert copy.kanban_board is None
    await copy._kanban_tick(source="test", force=True)
    assert [row["kind"] for row in _journal()] == ["create"]


@pytest.mark.parametrize(
    "cmd",
    [
        {"type": "kanban_autopilot", "enabled": True},
        {"type": "kanban_operator_create", "title": "t"},
        {"type": "kanban_operator_cancel", "cardId": "card_001"},
        {"type": "kanban_operator_unblock", "cardId": "card_001"},
        {"type": "kanban_operator_force_dispatch", "cardId": "card_001"},
        {"type": "kanban_operator_reassign", "cardId": "card_001", "assignee": "code"},
    ],
)
async def test_every_kanban_command_for_a_slack_session_goes_to_the_gateway(app, monkeypatch, cmd):
    state, _events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(state, {**cmd, "sessionId": SLACK})
    assert state.sessions == {}
    (sent,) = _forwarded()
    assert sent == {**cmd, "sessionId": SLACK}


async def test_a_kanban_command_is_refused_when_no_gateway_runs(app, monkeypatch):
    state, events = app
    _gateway_running(monkeypatch, None)
    await fb._handle_command(state, {"type": "kanban_operator_force_dispatch", "sessionId": SLACK, "cardId": "card_001"})
    assert state.sessions == {} and _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack gateway" in err["message"]


def _gateway_with(monkeypatch, sess):
    """The gateway's end: it holds the lock, and ``sess`` (when given) is
    the thread it has loaded."""
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)

    async def ensure_session(sid, **_kw):
        return sess

    daemon = GatewayDaemon()
    daemon.state = SimpleNamespace(
        sessions={SLACK: sess} if sess is not None else {}, active_session_id=None,
        ensure_session=ensure_session, talk_wake_hook_factory=lambda _sess: "hook",
    )
    return daemon


async def test_the_gateway_adds_the_card_on_its_board_and_dispatches_it(app, monkeypatch):
    from tests.test_kanban_dispatcher import _make_session, _StubSubAgentTool

    _state, events = app
    board = SessionKanbanBoard(journal=KanbanJournal(journal_path(SLACK)))
    sub_tool = _StubSubAgentTool()
    sess = _make_session(board=board, sub_tool=sub_tool)
    sess.id = SLACK
    sess.talk_wake_hook = None
    sess._emit_kanban_replay = lambda *_a: None
    daemon = _gateway_with(monkeypatch, sess)

    await daemon._on_kanban_command(
        {"type": "kanban_operator_create", "sessionId": SLACK, "title": "from the app", "assignee": "code"},
    )

    (card,) = await board.list()
    assert (card.title, card.created_by) == ("from the app", "operator")
    assert [row["task"]["id"] for row in _journal() if row["kind"] == "create"] == [card.id]
    # Dispatched by the gateway, whose sub-agents run beside the thread;
    # its memo wakes the thread through the talk wake hook.
    assert [call["agent_type"] for call in sub_tool.calls] == ["code"]
    assert sess.talk_wake_hook == "hook"
    assert any(e.get("subtype") == "kanban_task_created" and e["sessionId"] == SLACK for e in events)


async def test_the_gateway_says_so_when_the_thread_is_not_loaded(app, monkeypatch):
    _state, events = app
    daemon = _gateway_with(monkeypatch, None)
    await daemon._on_kanban_command({"type": "kanban_operator_create", "sessionId": SLACK, "title": "t"})
    (note,) = [e for e in events if e.get("subtype") == "control_command_skipped"]
    assert note["sessionId"] == SLACK and note["details"]["chatVisible"]


# ── model ────────────────────────────────────────────────────────────


async def test_a_model_picked_in_the_app_for_a_slack_session_goes_to_the_gateway(app, monkeypatch):
    state, events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(
        state,
        {"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5", "reasoningLevel": "high",
         "coordinationStrategy": "bus"},
    )
    # No copy changed, and the app does not say the model changed: the
    # gateway does, once it has.
    assert state.sessions == {}
    assert not [e for e in events if e.get("subtype") == "model_changed"]
    (cmd,) = _forwarded()
    assert cmd == {"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5", "reasoningLevel": "high"}


async def test_a_model_change_is_refused_when_no_gateway_runs(app, monkeypatch):
    state, events = app
    _gateway_running(monkeypatch, None)
    await fb._handle_command(state, {"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    assert state.sessions == {} and _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack gateway" in err["message"]


def _gateway_state(monkeypatch, *, turn_running: bool = False):
    """The gateway's bridge state, holding the thread: ensure_session as
    the real one changes a model, or parks the change while a turn runs."""
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    thread = SimpleNamespace(id=SLACK, model_id="claude-sonnet-5-5", reasoning_level="auto", pending_config=None)
    calls: list[dict] = []

    async def ensure_session(session_id, **kw):
        calls.append({"session_id": session_id, **kw})
        if turn_running:
            thread.pending_config = {"model_id": kw["model_id"], "reasoning_level": kw.get("reasoning_level")}
        else:
            thread.model_id = kw["model_id"]
            thread.reasoning_level = kw.get("reasoning_level") or "auto"
        return thread

    daemon = GatewayDaemon()
    daemon.state = SimpleNamespace(sessions={SLACK: thread}, ensure_session=ensure_session)
    return daemon, thread, calls


async def test_the_gateway_changes_the_threads_model_as_slash_model_does(app, monkeypatch):
    _state, events = app
    daemon, thread, calls = _gateway_state(monkeypatch)
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5", "reasoningLevel": "high"})
    (call,) = calls
    assert (call["session_id"], call["model_id"], call["reasoning_level"]) == (SLACK, "claude-opus-5-5", "high")
    # A thread loaded here for the first time gets its Slack source, so its
    # first initialize applies the Slack tool filter.
    assert call["gateway_source"].chat_id == "C1"
    assert thread.model_id == "claude-opus-5-5"
    (changed,) = [e for e in events if e.get("subtype") == "model_changed"]
    assert changed["sessionId"] == SLACK
    assert changed["details"] == {"model": "claude-opus-5-5", "reasoningLevel": "high"}


async def test_the_gateway_says_a_model_change_waits_for_the_running_turn(app, monkeypatch):
    _state, events = app
    daemon, thread, _calls = _gateway_state(monkeypatch, turn_running=True)
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    assert thread.model_id == "claude-sonnet-5-5"
    (changed,) = [e for e in events if e.get("subtype") == "model_changed"]
    assert changed["details"]["model"] == "claude-opus-5-5"
    assert "turn" in changed["message"]


async def test_the_gateway_refuses_a_model_it_does_not_know(app, monkeypatch):
    _state, events = app
    daemon, thread, calls = _gateway_state(monkeypatch)
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-9-9"})
    assert calls == [] and thread.model_id == "claude-sonnet-5-5"
    assert not [e for e in events if e.get("subtype") == "model_changed"]
    (note,) = [e for e in events if e.get("subtype") == "model_change_refused"]
    assert note["sessionId"] == SLACK and "claude-opus-9-9" in note["message"]
