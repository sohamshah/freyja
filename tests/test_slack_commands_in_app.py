"""Commands the app sends for a Slack session run in the gateway.

The gateway runs a Slack session's thread and holds its kanban board,
model and goal. A command the app ran on a copy of the session changed
only that copy: a kanban card written by the copy's board went into the
same journal the gateway's board writes, with a card id the gateway's
board gave out again.
"""

from __future__ import annotations

import asyncio
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
    # The app holds a copy of the thread, in the kanban mode it was saved
    # in (a command such as compact or repair_session builds one).
    await state.ensure_session(SLACK)
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
    copy = await state.ensure_session(SLACK)
    assert copy.coordination_strategy == "kanban"
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


async def test_the_gateway_says_so_when_it_has_no_such_thread(app, monkeypatch):
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
    # Refused as the gateway refuses a model, for the session, so the app
    # takes back the model it showed.
    (refused,) = [e for e in events if e.get("subtype") == "model_change_refused"]
    assert refused["sessionId"] == SLACK and "Slack gateway" in refused["message"]
    assert refused["details"]["chatVisible"]


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


# ── mode ─────────────────────────────────────────────────────────────


async def test_a_mode_picked_in_the_app_for_a_slack_session_goes_to_the_gateway(app, monkeypatch):
    # Changed on a copy here, the app said the thread was in kanban mode
    # with autopilot on, for a board the copy does not have, while the
    # gateway ran the thread on as before.
    state, events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    _thread_saved("bus")
    await fb._handle_command(
        state,
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban",
         "model": "claude-opus-5-5", "reasoningLevel": "high"},
    )
    assert state.sessions == {}
    assert not [e for e in events if e.get("subtype") in ("coordination_strategy_changed", "kanban_autopilot_enabled")]
    # Only the mode: the app's model for the thread is not the gateway's.
    (cmd,) = _forwarded()
    assert cmd == {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban"}


async def test_a_mode_change_is_refused_when_no_gateway_runs(app, monkeypatch):
    state, events = app
    _gateway_running(monkeypatch, None)
    await fb._handle_command(
        state, {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban"},
    )
    assert state.sessions == {} and _forwarded() == []
    assert not [e for e in events if e.get("subtype") == "coordination_strategy_changed"]
    (refused,) = [e for e in events if e.get("subtype") == "coordination_strategy_refused"]
    assert refused["sessionId"] == SLACK and "Slack gateway" in refused["message"]


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """The gateway's bridge state, which runs the thread."""
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.talk_wake_hook_factory = lambda _sess: "hook"
    daemon = GatewayDaemon()
    daemon.state = state
    yield daemon, state, events
    for sess in state.sessions.values():
        sess._stop_kanban_dispatcher()


async def test_the_gateway_puts_the_thread_in_the_mode_picked_in_the_app(gateway):
    daemon, state, events = gateway
    _thread_saved("bus")
    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban"},
    )
    thread = state.sessions[SLACK]
    assert thread.coordination_strategy == "kanban"
    # The thread's own board, autopilot on as when the app enters kanban
    # mode, and the talk wake hook its workers' memos wake it through.
    assert thread.kanban_board is not None and thread.auto_dispatch_enabled
    assert thread.talk_wake_hook == "hook"
    assert thread.gateway_source.chat_id == "C1"
    (changed,) = [e for e in events if e.get("subtype") == "coordination_strategy_changed"]
    assert changed["sessionId"] == SLACK and changed["details"]["coordinationStrategy"] == "kanban"
    # The thread kept the model it was saved with.
    assert thread.model_id == "claude-sonnet-5-5"


async def test_the_gateway_builds_no_session_on_a_sub_agents_id_for_a_mode_change(gateway):
    daemon, state, events = gateway
    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SUB, "coordinationStrategy": "kanban"},
    )
    assert state.sessions == {}
    (refused,) = [e for e in events if e.get("subtype") == "coordination_strategy_refused"]
    assert refused["sessionId"] == SUB


def _mode_changes(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("subtype") == "coordination_strategy_changed"]


async def test_a_mode_picked_during_a_running_turn_is_made_when_the_turn_ends(gateway):
    # The change waits for the turn. The app was told the thread was set to
    # the mode it was still in, and autopilot stayed off once kanban came.
    daemon, state, events = gateway
    _thread_saved("bus")
    thread = await state.ensure_session(SLACK)
    turn = asyncio.get_running_loop().create_future()
    thread.pending_task = turn

    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban"},
    )
    assert thread.coordination_strategy == "bus" and not thread.auto_dispatch_enabled
    (held,) = _mode_changes(events)
    assert held["details"]["coordinationStrategy"] == "kanban"
    assert "when the running turn ends" in held["message"]

    events.clear()
    turn.set_result(None)
    await fb._drain_after_turn(thread)
    assert thread.coordination_strategy == "kanban"
    assert thread.kanban_board is not None and thread.auto_dispatch_enabled
    # Reported as the mode, not as a model change.
    reports = [e for e in events if e.get("subtype") in ("config_applied", "coordination_strategy_changed")]
    assert [(e["subtype"], e["message"]) for e in reports] == [
        ("coordination_strategy_changed", "Coordination strategy set to Kanban"),
    ]


async def test_slash_mode_during_a_running_turn_says_the_mode_waits_for_the_turn(gateway):
    from bridge.gateway.platforms.base import IncomingMessage, MessageSource, Platform

    daemon, state, _events = gateway
    _thread_saved("bus")
    thread = await state.ensure_session(SLACK)
    thread.pending_task = asyncio.get_running_loop().create_future()
    sent: list[str] = []

    class _Adapter:
        async def send(self, _chat_id, text, **_kw):
            sent.append(text)

    source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="1.0",
    )
    await daemon._handle_mode_command(
        IncomingMessage(
            source=source, text="/mode kanban", is_slash_command=True, slash_command_name="mode",
            slash_command_args="kanban",
        ),
        _Adapter(),
    )
    assert thread.coordination_strategy == "bus"
    (text,) = sent
    assert "`kanban`" in text and "turn" in text


# ── saved when made, and kept over a restart ─────────────────────────


def _saved() -> dict:
    return tp.load_transcript(SLACK)["metadata"]


async def _after_a_restart(tmp_path):
    """The thread as a gateway started again loads it."""
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    return await state.ensure_session(SLACK)


async def test_a_mode_picked_in_the_app_is_kept_over_a_gateway_restart_before_the_next_turn(gateway, tmp_path):
    # Saved only when the thread's next turn ended, the change was lost
    # when the gateway restarted first.
    daemon, state, _events = gateway
    _thread_saved("bus")
    await state.ensure_session(SLACK)
    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "goal"},
    )
    assert _saved()["coordination_strategy"] == "goal"
    assert (await _after_a_restart(tmp_path)).coordination_strategy == "goal"


async def test_a_model_picked_in_the_app_is_saved_when_it_is_made(gateway):
    daemon, state, _events = gateway
    _thread_saved("bus")
    await state.ensure_session(SLACK)
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    assert _saved()["model_id"] == "claude-opus-5-5"


async def test_a_change_to_a_thread_not_loaded_since_the_gateway_started_is_saved(gateway):
    daemon, _state, _events = gateway
    _thread_saved("bus")
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    assert _saved()["model_id"] == "claude-opus-5-5"
    # The mode it was saved with stays.
    assert _saved()["coordination_strategy"] == "bus"


async def test_a_change_held_for_a_running_turn_is_saved_when_the_turn_ends(gateway):
    daemon, state, _events = gateway
    _thread_saved("bus")
    thread = await state.ensure_session(SLACK)
    turn = asyncio.get_running_loop().create_future()
    thread.pending_task = turn
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "goal"},
    )
    turn.set_result(None)
    await fb._drain_after_turn(thread)
    assert (_saved()["model_id"], _saved()["coordination_strategy"]) == ("claude-opus-5-5", "goal")


async def test_kanban_autopilot_comes_back_after_a_gateway_restart(gateway, tmp_path):
    # Only the app's renderer sent a session's autopilot back to a bridge
    # (with switch_session). Nothing sends the gateway a thread's, so a
    # kanban thread came back with its board and no autopilot.
    daemon, state, _events = gateway
    _thread_saved("kanban")
    await daemon._on_kanban_command({"type": "kanban_autopilot", "sessionId": SLACK, "enabled": True})
    assert state.sessions[SLACK].auto_dispatch_enabled
    thread = await _after_a_restart(tmp_path)
    try:
        assert thread.coordination_strategy == "kanban" and thread.auto_dispatch_enabled
    finally:
        thread._stop_kanban_dispatcher()


async def test_kanban_autopilot_turned_off_stays_off_after_a_gateway_restart(gateway, tmp_path):
    daemon, state, _events = gateway
    _thread_saved("bus")
    await daemon._on_set_coordination_strategy(
        {"type": "set_coordination_strategy", "sessionId": SLACK, "coordinationStrategy": "kanban"},
    )
    assert state.sessions[SLACK].auto_dispatch_enabled
    await daemon._on_kanban_command({"type": "kanban_autopilot", "sessionId": SLACK, "enabled": False})
    thread = await _after_a_restart(tmp_path)
    assert thread.coordination_strategy == "kanban" and not thread.auto_dispatch_enabled


async def test_a_model_change_leaves_a_kanban_threads_autopilot_on(gateway):
    # The change rebuilds the session (reset, then a load from disk), and
    # the reset turned autopilot off.
    daemon, state, _events = gateway
    _thread_saved("kanban")
    await daemon._on_kanban_command({"type": "kanban_autopilot", "sessionId": SLACK, "enabled": True})
    await daemon._on_set_model({"type": "set_model", "sessionId": SLACK, "model": "claude-opus-5-5"})
    thread = state.sessions[SLACK]
    assert thread.model_id == "claude-opus-5-5" and thread.auto_dispatch_enabled


async def test_a_copy_of_the_thread_in_the_app_does_not_turn_autopilot_on(app):
    state, _events = app
    _thread_saved("kanban")
    data = tp.load_transcript(SLACK)
    data["metadata"]["auto_dispatch_enabled"] = True
    tp.save_transcript(SLACK, data)
    copy = await state.ensure_session(SLACK)
    assert not copy.auto_dispatch_enabled and copy._kanban_dispatcher_task is None


# ── goal ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["set", "pause", "resume", "clear", "stop", "done", "status", "recalibrate_judge"])
async def test_a_goal_command_for_a_slack_session_is_refused_in_the_app(app, monkeypatch, action):
    # Armed on a copy, the goal could never run (a copy runs no turn), the
    # copy saves no goal file, and arming it started a judge calibration
    # (a model call) here.
    state, events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    _thread_saved("goal")
    await fb._handle_command(
        state,
        {"type": "goal_control", "sessionId": SLACK, "action": action, "goal": "ship the release",
         "model": "claude-sonnet-5-5", "coordinationStrategy": "goal"},
    )
    assert state.sessions == {}
    assert not [e for e in events if str(e.get("subtype") or "").startswith("goal_")]
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"] and "goal" in err["message"]
    # A notice, not an error that ends the thread's live turn in the app.
    assert "sessionId" not in err


# ── stop agents, and a sub-agent's Stop ──────────────────────────────


SUB = "sub_19d8b53e6f0_1"


async def test_stop_agents_on_a_slack_session_goes_to_the_gateway(app, monkeypatch):
    state, _events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(state, {"type": "force_cancel", "sessionId": SLACK, "scope": "subagents"})
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["sessionId"], cmd["scope"]) == ("cancel_turn", SLACK, "subagents")


async def test_stop_in_the_pane_of_a_sub_agent_the_gateway_runs_goes_to_the_gateway(app, monkeypatch):
    # The app's main process marks it: it saw the sub-agent on the
    # gateway's log, and the bridge cannot tell from its id.
    state, _events = app
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(
        state, {"type": "force_cancel", "sessionId": SUB, "scope": "turn", "turnId": "t1", "gatewayOwned": True},
    )
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["sessionId"], cmd["scope"]) == ("cancel_turn", SUB, "turn")


async def test_a_stop_for_gateway_work_says_so_when_no_gateway_runs(app, monkeypatch):
    state, events = app
    _gateway_running(monkeypatch, None)
    await fb._handle_command(state, {"type": "force_cancel", "sessionId": SLACK, "scope": "subagents"})
    assert _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack gateway" in err["message"]


def _gateway_thread_with_subagent(monkeypatch):
    """The gateway's end: the thread, its turn running, and a sub-agent it
    started running beside it."""
    import asyncio
    import threading

    from bridge.gateway.run import GatewayDaemon
    from bridge.tools.sub_agent_registry import SubAgentState

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    rec = SimpleNamespace(
        id=SUB, state=SubAgentState.RUNNING, cancel_event=threading.Event(), cancel_origin=None,
        asyncio_cancel=None, loop=None,
    )
    registry = SimpleNamespace(get=lambda sid: rec if sid == SUB else None)
    task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    thread = SimpleNamespace(
        id=SLACK, pending_task=task, current_turn_id="turn-3", _active_stream_consumer=None,
        subagent_registry=registry,
    )
    daemon = GatewayDaemon()
    daemon.state = SimpleNamespace(sessions={SLACK: thread})
    return daemon, thread, task, rec


async def test_the_gateway_stops_the_threads_sub_agents_and_leaves_its_turn(app, monkeypatch):
    _state, events = app
    daemon, thread, task, _rec = _gateway_thread_with_subagent(monkeypatch)
    stopped: list[tuple] = []
    monkeypatch.setattr(fb, "_force_cancel_session", lambda sess, *, scope: stopped.append((sess, scope)) or 2)
    await daemon._on_cancel_turn({"type": "cancel_turn", "sessionId": SLACK, "scope": "subagents", "kind": "force_cancel"})
    # The path the app's bridge takes for its own sessions' Stop agents.
    assert stopped == [(thread, "subagents")]
    assert not task.done()
    (note,) = [e for e in events if e.get("subtype") == "turn_cancelled"]
    assert (note["sessionId"], note["details"]["scope"], note["details"]["fired"]) == (SLACK, "subagents", 2)
    task.cancel()


async def test_the_gateway_stops_a_sub_agent_from_its_pane(app, monkeypatch):
    _state, _events = app
    daemon, _thread, task, rec = _gateway_thread_with_subagent(monkeypatch)
    await daemon._on_cancel_turn({"type": "cancel_turn", "sessionId": SUB, "scope": "turn", "kind": "force_cancel"})
    assert rec.cancel_event.is_set() and rec.cancel_origin == "operator"
    # The thread's own turn goes on.
    assert not task.done()
    task.cancel()


async def test_the_gateway_builds_no_session_on_a_sub_agents_id_for_a_model_change(app, monkeypatch):
    # The model menu sends the id of the pane in view, which may be a
    # sub-agent under the thread; a session built on that id would be a
    # second, parentless copy of the sub-agent, saving over its files.
    _state, events = app
    daemon, _thread, calls = _gateway_state(monkeypatch)
    await daemon._on_set_model({"type": "set_model", "sessionId": SUB, "model": "claude-opus-5-5"})
    assert calls == []
    (note,) = [e for e in events if e.get("subtype") == "model_change_refused"]
    assert note["sessionId"] == SUB
