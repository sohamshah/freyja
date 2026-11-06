"""A Slack session opened in the app never writes the gateway's files.

The gateway runs a Slack session's thread and saves its transcript,
inbox and goal sidecars. The app's bridge loaded its own copy when the
app opened one (switch_session: its logs show it restoring Slack
transcripts of 51 and 92 entries), and a message typed for it ran a turn
on that copy. Every turn, compaction and save then wrote the copy over
the gateway's files, and the gateway's next save undid it or lost its
own newer entries. Messages typed for the session go to the gateway,
whose talk wake hook streams the reply into the thread.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.inbox import KIND_FOLLOWUP, KIND_QUEUED, InboxMessage, SessionInbox

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def app_bridge(monkeypatch, tmp_path):
    """The app's bridge: it does not hold the gateway lock, and its state
    has no gateway runner. Records what it emits and every session it
    would build."""
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    built: list[str] = []

    async def ensure_session(sid, **_kw):
        built.append(sid)
        raise AssertionError(f"the app's bridge built a copy of {sid}")

    state = SimpleNamespace(
        sessions={}, active_session_id=None, ensure_session=ensure_session, gateway_runner=None,
    )
    state.get = lambda sid: state.sessions.get(sid) if sid else None
    return state, events, built


def _gateway_running(monkeypatch, pid: int | None) -> None:
    import bridge.gateway.pid as gp

    monkeypatch.setattr(gp, "get_running_pid", lambda: pid)


def _forwarded() -> list[dict]:
    from bridge.gateway.control_channel import commands_path

    path = commands_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_opening_a_slack_session_in_the_app_loads_no_copy(app_bridge):
    state, _events, built = app_bridge
    await fb._handle_command(state, {"type": "switch_session", "sessionId": SLACK, "model": "claude-opus-5-5"})
    assert built == []
    assert state.sessions == {}


@pytest.mark.parametrize("after_turn, kind", [(False, KIND_FOLLOWUP), (True, KIND_QUEUED)])
async def test_a_message_typed_for_a_slack_session_goes_to_the_gateway(app_bridge, monkeypatch, after_turn, kind):
    state, _events, built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(
        state,
        {
            "type": "send_message", "sessionId": SLACK, "content": "check the build",
            "clientId": "msg-7", "afterTurn": after_turn, "model": "claude-opus-5-5",
        },
    )
    assert built == []
    (cmd,) = _forwarded()
    assert cmd["type"] == "talk_deliver" and cmd["to"] == SLACK
    msg = cmd["message"]
    assert (msg["content"], msg["fromRole"], msg["kind"], msg["clientId"]) == (
        "check the build", "operator", kind, "msg-7",
    )


async def test_the_gateway_runs_the_forwarded_message_with_its_slack_hook(app_bridge, monkeypatch):
    # The gateway's end of the route: the talk wake hook is what builds the
    # Slack consumer that posts the turn's reply into the thread.
    state, _events, _built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(
        state, {"type": "send_message", "sessionId": SLACK, "content": "check the build", "clientId": "msg-7"},
    )
    (cmd,) = _forwarded()

    turns: list[tuple] = []
    monkeypatch.setattr(
        fb, "_schedule_or_queue_turn",
        lambda sess, content, attachments=None, on_turn_start=None: turns.append((content, on_turn_start)) or True,
    )
    hook = object()
    thread = SimpleNamespace(
        id=SLACK, inbox=SessionInbox(session_id=SLACK), pending_task=None, talk_wake_hook=None,
    )
    thread.wake_for_inbox = lambda: fb._BridgeSession.wake_for_inbox(thread)
    thread.accepts_followups = lambda: fb._BridgeSession.accepts_followups(thread)
    gateway = SimpleNamespace(sessions={SLACK: thread}, talk_wake_hook_factory=lambda _sess: hook)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    await fb._handle_talk_deliver_command(gateway, cmd)
    assert turns == [("check the build", hook)]


async def test_a_message_for_a_slack_session_is_refused_when_no_gateway_runs(app_bridge, monkeypatch):
    state, events, built = app_bridge
    _gateway_running(monkeypatch, None)
    await fb._handle_command(state, {"type": "send_message", "sessionId": SLACK, "content": "hi", "clientId": "m1"})
    assert built == [] and _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"]
    # The app takes the message's bubble back out and returns its text.
    (refused,) = [e for e in events if e["type"] == "send_refused"]
    assert (refused["sessionId"], refused["clientId"]) == (SLACK, "m1")


async def test_a_message_too_large_for_the_control_channel_is_refused(app_bridge, monkeypatch):
    # The gateway reads its control file a megabyte at a time and skips a
    # longer line, so the message would never run.
    state, events, _built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    image = {"type": "image", "mimeType": "image/png", "dataBase64": "A" * (2 * 1024 * 1024)}
    await fb._handle_command(
        state,
        {"type": "send_message", "sessionId": SLACK, "content": "look", "attachments": [image], "clientId": "m2"},
    )
    assert _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"]
    (refused,) = [e for e in events if e["type"] == "send_refused"]
    assert (refused["sessionId"], refused["clientId"]) == (SLACK, "m2")


# ── Stop pressed in the app stops the gateway's turn ─────────────────


def _gateway_thread(turn_id: str):
    """The gateway's end: its session for the thread, with a turn running
    and the Slack consumer streaming it."""
    import asyncio

    from bridge.gateway.run import GatewayDaemon

    aborts: list[dict] = []
    consumer = SimpleNamespace(finalized=False, abort=lambda **kw: aborts.append(kw))
    task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    thread = SimpleNamespace(id=SLACK, pending_task=task, current_turn_id=turn_id, _active_stream_consumer=consumer)
    daemon = GatewayDaemon()
    daemon.state = SimpleNamespace(sessions={SLACK: thread})
    return daemon, task, aborts


async def _stop(state, turn_id: str) -> None:
    await fb._handle_command(state, {"type": "force_cancel", "sessionId": SLACK, "scope": "turn", "turnId": turn_id})


async def test_stop_pressed_in_the_app_stops_the_gateways_turn(app_bridge, monkeypatch):
    import asyncio

    state, events, built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    await _stop(state, "turn-3")
    assert built == []
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["sessionId"], cmd["turnId"]) == ("cancel_turn", SLACK, "turn-3")

    daemon, task, aborts = _gateway_thread("turn-3")
    await daemon._on_cancel_turn(cmd)
    await asyncio.sleep(0)
    assert task.cancelled()
    # As /stop does: Slack has not sealed the thread's message, so the
    # consumer seals it.
    assert aborts == [{"stopped_by_user": False}]
    # The app hears of it as it does for its own sessions.
    (note,) = [e for e in events if e.get("subtype") == "turn_cancelled"]
    assert note["sessionId"] == SLACK


async def test_a_stop_for_a_turn_that_already_ended_leaves_the_gateways_new_turn_running(app_bridge, monkeypatch):
    # A new message in the thread may have started another turn by then.
    state, events, _built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    await _stop(state, "turn-3")
    (cmd,) = _forwarded()
    daemon, task, aborts = _gateway_thread("turn-4")
    await daemon._on_cancel_turn(cmd)
    assert not task.done() and aborts == []
    (stale,) = [e for e in events if e.get("subtype") == "turn_cancel_stale"]
    assert stale["sessionId"] == SLACK
    task.cancel()


async def test_a_cut_in_typed_in_the_app_cuts_into_the_gateways_running_turn(app_bridge, monkeypatch):
    # ⌃↵: the app's own follow-ups cut the running step short; pushed into
    # the thread's inbox as talk, this one waited for the step to end.
    import asyncio

    state, events, _built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    await fb._handle_command(
        state,
        {
            "type": "send_message", "sessionId": SLACK, "content": "use the other branch",
            "clientId": "m9", "followup": True, "force": True,
        },
    )
    (cmd,) = _forwarded()

    interrupts: list[int] = []
    yields: list[bool] = []
    task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    thread = SimpleNamespace(
        id=SLACK, inbox=SessionInbox(session_id=SLACK), pending_task=task, runtime="native", talk_wake_hook=None,
        runner=SimpleNamespace(request_interrupt=lambda: interrupts.append(1) or True),
        _request_tool_yield=lambda *, hard: yields.append(hard),
    )
    for name in ("accepts_followups", "push_operator_followup", "interrupt_for_inbox", "wake_for_inbox"):
        setattr(thread, name, getattr(fb._BridgeSession, name).__get__(thread))
    gateway = SimpleNamespace(sessions={SLACK: thread}, talk_wake_hook_factory=lambda _sess: None)
    await fb._handle_talk_deliver_command(gateway, cmd)
    task.cancel()

    assert interrupts == [1] and yields == [True]
    (msg,) = thread.inbox.unread
    assert (msg.content, msg.kind, msg.force, msg.client_id) == ("use the other branch", KIND_FOLLOWUP, True, "m9")
    # The app's pending bubble learns it is cutting in, as for its own.
    (queued,) = [e for e in events if e["type"] == "followup_queued"]
    assert (queued["clientId"], queued["force"], queued["interrupting"]) == ("m9", True, True)


async def test_stop_says_so_when_no_gateway_runs(app_bridge, monkeypatch):
    state, events, _built = app_bridge
    _gateway_running(monkeypatch, None)
    await _stop(state, "turn-3")
    assert _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"]


# ── a copy built by any other command writes nothing ─────────────────


def _copy(tmp_path, *, gateway_runner=None):
    """A bridge session for the Slack thread over a real engine session, in
    a process whose state has (or has no) gateway runner."""
    from engine.session import Session

    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.gateway_runner = gateway_runner
    sess = fb._BridgeSession(
        SLACK, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.session = Session.create(system_prompt="t")
    sess.session.add_user_message("from the app")
    return sess


def _gateway_saved() -> None:
    tp.save_transcript(SLACK, {
        "version": 1, "session_id": SLACK, "metadata": {},
        "transcript": {"entries": [{"id": "g1"}], "head_id": "g1"},
    })


async def test_a_copy_in_the_app_saves_nothing_over_the_gateways_files(app_bridge, tmp_path):
    from bridge.tools.goal_loop import GoalState

    _gateway_saved()
    sess = _copy(tmp_path)
    sess.inbox.push(InboxMessage(id="x", from_session="a", from_label="a", from_role="agent", content="later"))
    sess.goal_state = GoalState(goal="ship it")
    sess._save_transcript()
    sess._save_goal_state()
    assert [e["id"] for e in tp.load_transcript(SLACK)["transcript"]["entries"]] == ["g1"]
    assert tp.load_inbox_state(SLACK) is None
    assert tp.load_goal_state(SLACK) is None


async def test_the_gateway_still_saves_its_own_sessions(app_bridge, monkeypatch, tmp_path):
    # The gateway is the process holding the gateway lock.
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    _gateway_saved()
    sess = _copy(tmp_path, gateway_runner=object())
    sess._save_transcript()
    entries = tp.load_transcript(SLACK)["transcript"]["entries"]
    assert len(entries) == 1 and entries[0]["id"] != "g1"


async def test_a_copy_in_the_app_runs_no_turn(app_bridge, tmp_path):
    _state, events, _built = app_bridge
    sess = _copy(tmp_path)
    assert fb._schedule_or_queue_turn(sess, "go", None) is False
    assert sess.pending_task is None
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"]


async def test_a_copy_in_the_app_is_not_compacted(app_bridge, monkeypatch, tmp_path):
    from engine.compaction import SummaryCompaction

    _state, events, _built = app_bridge
    sess = _copy(tmp_path)
    sess.provider = object()

    async def _initialize():
        return None

    sess.initialize = _initialize
    compacted: list[int] = []
    monkeypatch.setattr(SummaryCompaction, "compact", lambda *_a, **_k: compacted.append(1))
    await sess.force_compact()
    assert compacted == []
    (skip,) = [e for e in events if e.get("subtype") == "compaction_skipped"]
    assert "Slack" in skip["message"]


async def test_a_copy_in_the_app_is_not_repaired(app_bridge, tmp_path):
    # /repair saves the history it fixed and keeps a backup of the old one.
    _state, events, _built = app_bridge
    _gateway_saved()
    sess = _copy(tmp_path)
    await sess.repair_history()
    assert [e["id"] for e in tp.load_transcript(SLACK)["transcript"]["entries"]] == ["g1"]
    (report,) = [e for e in events if e.get("subtype") == "session_repaired"]
    assert "Slack" in report["message"]
    assert not list((tmp_path / "sessions").glob("*repair*"))


async def test_a_copy_restored_in_the_app_is_not_compacted(app_bridge, monkeypatch, tmp_path):
    # A restore compaction saves what it made; on a copy that was the
    # gateway's transcript, summarized by the app, written over its file.
    from engine.compaction import CompactionResult, SummaryCompaction
    from engine.providers import APIUsage, ProviderResponse
    from engine.runner import AsyncAgentRunner
    from engine.types import AgentConfig

    _gateway_saved()
    sess = _copy(tmp_path)
    session = sess.session
    session.restore_transcript = lambda _data: None
    session.estimate_tokens = lambda: 900_000

    class _Provider:
        name, model_id, context_window = "anthropic", "claude-sonnet-5-5", 1_000_000

        async def stream_to_response(self, **_kw):
            return ProviderResponse(content="", stop_reason="end_turn", usage=APIUsage(input_tokens=1))

    provider = _Provider()

    async def _initialize():
        sess.session, sess.provider = session, provider
        sess.runner = AsyncAgentRunner(provider, config=AgentConfig())

    sess.initialize = _initialize
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None
    compacted: list[int] = []

    def _compact(*_a, **_k):
        compacted.append(1)
        return CompactionResult(success=True, summary="s", tokens_before=900_000, tokens_after=5_000)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    assert await sess.try_restore_transcript()
    assert compacted == []
    assert [e["id"] for e in tp.load_transcript(SLACK)["transcript"]["entries"]] == ["g1"]


# ── delete in the app hides; the gateway's files stay ────────────────


async def test_deleting_a_slack_session_in_the_app_keeps_its_files(app_bridge, tmp_path):
    # The gateway runs the thread and saves these. The user never deletes
    # sessions; the app only stops showing it.
    _state, _events, _built = app_bridge
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    _gateway_saved()
    tp.save_inbox_state(SLACK, {"sessionId": SLACK, "unread": [], "delivered": []})
    sub = "sub_abc_1"
    tp.save_transcript(sub, {"version": 1, "session_id": sub, "metadata": {"parent_session_id": SLACK}})

    await fb._handle_command(
        state, {"type": "delete_session", "sessionId": SLACK, "cascadeSessionIds": [sub]},
    )

    assert [e["id"] for e in tp.load_transcript(SLACK)["transcript"]["entries"]] == ["g1"]
    assert tp.load_inbox_state(SLACK) is not None
    assert tp.load_transcript(sub) is not None


async def test_deleting_a_desktop_session_still_removes_its_transcript(app_bridge, tmp_path):
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    tp.save_transcript("session-1", {"version": 1, "session_id": "session-1", "metadata": {}})

    await fb._handle_command(state, {"type": "delete_session", "sessionId": "session-1"})

    assert tp.load_transcript("session-1") is None


# ── a Slack thread's sub-agents are the gateway's too ────────────────

# The gateway runs a Slack thread's sub-agents and saves their files, as
# for the thread. The app knows one by its saved files, which name its
# parent.
SLACK_SUB = "sub_19f0slack_1"


def _gateway_sub_agent_saved(sub: str = SLACK_SUB, parent: str = SLACK) -> None:
    transcript = {
        "version": 1, "session_id": sub, "metadata": {"parent_session_id": parent},
        "transcript": {"entries": [{"id": "g1"}], "head_id": "g1"},
    }
    tp.save_transcript(sub, transcript)
    tp.save_subagent_state(sub, {
        "sessionId": sub, "parentSessionId": parent, "agentType": "general",
        "model": "claude-sonnet-5-5", "task": "t", "label": "worker", "transcript": transcript,
    })


def _app_root_that_could_host(state) -> list[str]:
    """A root session the app has loaded, whose sub_agent tool would
    resume a sub-agent here. Records each one it resumes."""
    resumed: list[str] = []

    async def resume_archived(sidecar, **_kw):
        resumed.append(sidecar["sessionId"])
        return sidecar["sessionId"]

    tool = SimpleNamespace(resume_archived=resume_archived)
    state.sessions["session-1"] = SimpleNamespace(
        id="session-1", tool_registry=SimpleNamespace(_tools={"sub_agent": tool}),
    )
    return resumed


async def test_a_message_typed_for_a_slack_sub_agent_goes_to_the_gateway(app_bridge, monkeypatch):
    # Resumed here, it ran under the app's root as a copy, and saved its
    # transcript and sidecar over the gateway's.
    state, _events, built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    _gateway_sub_agent_saved()
    resumed = _app_root_that_could_host(state)

    await fb._handle_command(
        state, {"type": "send_message", "sessionId": SLACK_SUB, "content": "also check X", "clientId": "m5"},
    )

    assert resumed == [] and built == []
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["to"], cmd["message"]["content"]) == ("talk_deliver", SLACK_SUB, "also check X")


async def test_a_message_for_a_slack_sub_agent_waits_in_its_inbox_file_while_no_gateway_runs(app_bridge, monkeypatch):
    state, _events, _built = app_bridge
    _gateway_running(monkeypatch, None)
    _gateway_sub_agent_saved()
    resumed = _app_root_that_could_host(state)

    await fb._handle_command(state, {"type": "send_message", "sessionId": SLACK_SUB, "content": "also check X"})

    assert resumed == [] and _forwarded() == []
    assert [m["content"] for m in tp.load_inbox_state(SLACK_SUB)["unread"]] == ["also check X"]


async def test_a_message_for_a_slack_sub_agent_goes_to_a_gateway_that_started_before_it_was_queued(
    app_bridge, monkeypatch
):
    # It saw no gateway, then stalled while a whole gateway started and
    # loaded the sub-agent's inbox file. Written there then, the message
    # was dropped by the gateway's next save of that file.
    import bridge.gateway.pid as gp

    state, _events, _built = app_bridge
    _gateway_sub_agent_saved()
    _app_root_that_could_host(state)
    gateway: list[dict | None] = []

    def _running_pid() -> int | None:
        if gateway:
            return os.getpid() + 1
        gateway.append(tp.load_inbox_state(SLACK_SUB))
        return None

    monkeypatch.setattr(gp, "get_running_pid", _running_pid)
    await fb._handle_command(state, {"type": "send_message", "sessionId": SLACK_SUB, "content": "also check X"})
    tp.save_inbox_state(SLACK_SUB, gateway[0] or {"sessionId": SLACK_SUB, "unread": [], "delivered": []})

    assert [cmd["message"]["content"] for cmd in _forwarded()] == ["also check X"]


def _gateway_sub_agent_running(sub: str = SLACK_SUB, parent: str = SLACK) -> None:
    """A first run still going in the gateway: its transcript is saved,
    and its sidecar is not yet."""
    tp.save_transcript(sub, {
        "version": 1, "session_id": sub, "metadata": {"parent_session_id": parent},
        "transcript": {"entries": [{"id": "g1"}], "head_id": "g1"},
    })


def _talk(content: str) -> InboxMessage:
    return InboxMessage(id=f"t-{content}", from_session="session-1", from_label="lead", from_role="agent", content=content)


async def test_a_talk_to_a_running_slack_sub_agent_goes_to_the_gateway(app_bridge, monkeypatch):
    # It counted as a closed root session: the app loaded a root copy of
    # it, which saves nothing, and pushed the message into that copy.
    state, _events, built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    _gateway_sub_agent_running()

    await fb.deliver_talk_message_locally(state, SLACK_SUB, _talk("status?"))

    assert built == []
    (cmd,) = _forwarded()
    assert (cmd["type"], cmd["to"], cmd["message"]["content"]) == ("talk_deliver", SLACK_SUB, "status?")


def test_a_sub_agent_whose_first_run_is_going_is_not_a_closed_root_session(app_bridge):
    _gateway_sub_agent_running()
    _gateway_sub_agent_running("sub_19f0desk_1", parent="session-1")

    assert not fb._is_closed_root_session(SLACK_SUB)
    assert not fb._is_closed_root_session("sub_19f0desk_1")


async def test_a_copy_of_a_slack_sub_agent_saves_nothing_over_its_files(app_bridge, tmp_path):
    # compact, set_model, goal_control and the like build a root session
    # on the id they are given, here a Slack thread's sub-agent.
    from engine.session import Session

    _gateway_sub_agent_saved()
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    copy = fb._BridgeSession(
        SLACK_SUB, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    copy.session = Session.create(system_prompt="t")
    copy.session.add_user_message("from the app")

    copy._save_transcript()
    assert [e["id"] for e in tp.load_transcript(SLACK_SUB)["transcript"]["entries"]] == ["g1"]


def test_a_sub_agent_under_a_slack_sub_agent_is_the_gateways(app_bridge):
    _gateway_sub_agent_saved()
    _gateway_sub_agent_saved("sub_19f0slack_2", parent=SLACK_SUB)
    _gateway_sub_agent_saved("sub_19f0desk_1", parent="session-1")

    assert fb._gateway_holds("sub_19f0slack_2")
    assert not fb._gateway_holds("sub_19f0desk_1")


def test_the_gateway_holds_none_of_its_own_sub_agents(app_bridge, monkeypatch):
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    _gateway_sub_agent_saved()

    assert not fb._gateway_holds(SLACK_SUB)
