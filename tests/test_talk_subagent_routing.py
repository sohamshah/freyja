"""Talking to sub-agents: the parent-child link must survive.

Covers what broke in real sessions:

* 2026-10-07 15:54 — opening a RUNNING sub-agent's pane (switch_session
  with its id) built a second, parentless root session from a UI text
  summary. The operator's messages went to the copy, its
  talk("parent") was unresolved, and both copies edited the same files.
* 2026-10-07 15:24 — opening a FINISHED sub-agent's pane loaded it as a
  root session, so the parent's talk() reached a parentless copy that
  would never memo the parent.
* 2026-09-30 — every re-wake of an archived sub-agent replayed all the
  messages earlier runs had already read.
* 2026-10-06 — talk(to=<message id>) was unresolved; the sender's
  session id was not in the header.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from bridge import freyja_bridge as fb
from bridge.inbox import KIND_FOLLOWUP, InboxMessage, SessionInbox, new_message_id
from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState
from bridge.tools.talk_tool import TalkRouter, TalkRouterContext, TalkTool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda ev: events.append(ev))
    return events


@pytest.fixture
def events(_isolate):
    return _isolate


def _sessions_dir():
    import bridge.transcript_persistence as tp

    return tp.SESSIONS_DIR


class _State(SimpleNamespace):
    """_BridgeState-shaped stub; records every ensure_session call."""

    def __init__(self, sessions):
        super().__init__(
            sessions=sessions,
            active_session_id=next(iter(sessions), None),
            ensured=[],
            talk_wake_hook_factory=None,
        )

    def get(self, session_id):
        return self.sessions.get(session_id) if session_id else None

    async def ensure_session(self, session_id, **_kw):
        self.ensured.append(session_id)
        sess = SimpleNamespace(id=session_id, inbox=SessionInbox(session_id=session_id))
        self.sessions[session_id] = sess
        self.active_session_id = session_id
        return sess


def _parent_with_child(child_id="sub_x_1", *, state=SubAgentState.RUNNING):
    reg = SubAgentRegistry()
    rec = reg.register(id=child_id, label="builder", task="build it", mode="background")
    rec.parent_session_id = "session-parent"
    rec.inbox = SessionInbox(session_id=child_id)
    rec.state = state
    parent = SimpleNamespace(
        id="session-parent",
        title="parent",
        parent_session_id=None,
        subagent_registry=reg,
        inbox=SessionInbox(session_id="session-parent"),
        tool_registry=None,
    )
    return parent, rec


# ─── the pane of a sub-agent never becomes a root session ─────────────


async def test_opening_a_running_subagent_pane_does_not_copy_it():
    parent, rec = _parent_with_child()
    state = _State({"session-parent": parent})
    await fb._handle_command(state, {"type": "switch_session", "sessionId": rec.id})
    assert state.ensured == []
    assert rec.id not in state.sessions
    assert state.active_session_id == "session-parent"


async def test_opening_a_finished_subagent_pane_does_not_load_it_as_a_root():
    (_sessions_dir() / "sub_old_1.subagent.json").write_text(
        json.dumps({"sessionId": "sub_old_1", "parentSessionId": "session-parent"})
    )
    (_sessions_dir() / "sub_old_1.transcript.json").write_text("{}")
    parent, _ = _parent_with_child()
    state = _State({"session-parent": parent})
    await fb._handle_command(state, {"type": "switch_session", "sessionId": "sub_old_1"})
    assert state.ensured == []


async def test_typing_into_a_running_subagent_pane_reaches_that_subagent(events):
    parent, rec = _parent_with_child()
    state = _State({"session-parent": parent})
    await fb._handle_command(
        state,
        {
            "type": "send_message",
            "sessionId": rec.id,
            "content": "you got the spec update right?",
            "clientId": "c1",
            "followup": True,
        },
    )
    assert state.ensured == []
    (msg,) = rec.inbox.unread
    assert msg.from_role == "operator" and msg.kind == KIND_FOLLOWUP
    assert msg.client_id == "c1"
    assert any(e.get("type") == "followup_queued" and e["sessionId"] == rec.id for e in events)


async def test_typing_into_a_finished_subagent_pane_rewakes_it(monkeypatch):
    (_sessions_dir() / "sub_x_1.subagent.json").write_text(
        json.dumps({"sessionId": "sub_x_1", "parentSessionId": "session-parent"})
    )
    parent, rec = _parent_with_child(state=SubAgentState.DONE)
    state = _State({"session-parent": parent})
    woken: list = []

    async def _wake(st, sid, msg, **_kw):
        woken.append((sid, msg.from_role, msg.content))
        return True

    monkeypatch.setattr(fb, "_wake_archived_subagent", _wake)
    await fb._handle_command(
        state, {"type": "send_message", "sessionId": rec.id, "content": "one more pass"}
    )
    assert state.ensured == []
    assert woken == [("sub_x_1", "operator", "one more pass")]


async def test_stop_in_a_subagent_pane_stops_that_subagent():
    parent, rec = _parent_with_child()
    state = _State({"session-parent": parent})
    await fb._handle_command(state, {"type": "force_cancel", "sessionId": rec.id})
    assert rec.cancel_event.is_set()
    assert rec.cancel_origin == "operator"


async def test_list_subagents_for_a_subagent_pane_reports_its_children(events):
    parent, rec = _parent_with_child()
    grandchild = parent.subagent_registry.register(
        id="sub_x_2", label="helper", task="t", mode="background"
    )
    grandchild.parent_session_id = rec.id
    state = _State({"session-parent": parent})
    await fb._handle_command(state, {"type": "list_subagents", "sessionId": rec.id})
    (snap,) = [e for e in events if e.get("type") == "subagents_snapshot"]
    assert snap["runningIds"] == ["sub_x_2"]


# ─── talk routing ─────────────────────────────────────────────────────


def _router(sessions):
    return TalkRouter(
        bridge_state=SimpleNamespace(sessions=sessions),
        get_running_sessions=lambda: dict(sessions),
        resolve_archived_sub=lambda _sid: None,
        wake_archived_sub=lambda _sid, _msg: None,
    )


def _ctx(caller="session-parent", parent=None):
    return TalkRouterContext(
        caller_session_id=caller,
        caller_label=caller,
        caller_role="agent",
        parent_session_id=parent,
    )


def test_talk_prefers_the_running_subagent_over_a_stray_root_copy():
    parent, rec = _parent_with_child()
    stray = SimpleNamespace(
        id=rec.id, title=rec.id, parent_session_id=None,
        subagent_registry=None, inbox=SessionInbox(session_id=rec.id),
    )
    router = _router({"session-parent": parent, rec.id: stray})
    sid, live_root, sub, _ = router.resolve_ref(rec.id, _ctx())
    assert sid == rec.id and live_root is None and sub is rec


async def test_deliver_locally_prefers_the_running_subagent(monkeypatch):
    parent, rec = _parent_with_child()
    stray = SimpleNamespace(id=rec.id, inbox=SessionInbox(session_id=rec.id))
    state = _State({"session-parent": parent, rec.id: stray})
    status = await fb.deliver_talk_message_locally(
        state, rec.id, InboxMessage(
            id=new_message_id(), from_session="x", from_label="x",
            from_role="agent", content="hi",
        ),
    )
    assert status == "delivered to sub-agent"
    assert [m.content for m in rec.inbox.unread] == ["hi"]
    assert stray.inbox.unread == []


async def test_talk_to_a_message_id_goes_to_the_agent_that_wrote_it():
    parent, rec = _parent_with_child()
    # The parent read a message from its child earlier.
    incoming = InboxMessage(
        id="aa11bb22cc33dd44", from_session=rec.id, from_label="builder",
        from_role="agent", content="which cursor order?",
    )
    parent.inbox.push(incoming)
    parent.inbox.drain()
    tool = TalkTool(_router({"session-parent": parent}), _ctx())
    out = await tool.execute("c1", {"to": "aa11bb22cc33dd44", "content": "id DESC"})
    assert "unresolved" not in out.content
    assert [m.content for m in rec.inbox.unread] == ["id DESC"]


async def test_reused_label_reaches_the_running_respawn():
    parent, failed = _parent_with_child("sub_a_1", state=SubAgentState.FAILED)
    respawn = parent.subagent_registry.register(
        id="sub_a_2", label="builder", task="t", mode="background"
    )
    respawn.inbox = SessionInbox(session_id="sub_a_2")
    tool = TalkTool(_router({"session-parent": parent}), _ctx())
    out = await tool.execute("c1", {"to": "builder", "content": "addendum"})
    assert "sub_a_2" in out.content
    assert [m.content for m in respawn.inbox.unread] == ["addendum"]


async def test_ambiguous_label_error_names_the_candidates():
    parent, _ = _parent_with_child("sub_a_1", state=SubAgentState.DONE)
    parent.subagent_registry.register(
        id="sub_a_2", label="builder", task="t", mode="background"
    ).state = SubAgentState.DONE
    tool = TalkTool(_router({"session-parent": parent}), _ctx())
    out = await tool.execute("c1", {"to": "builder", "content": "x"})
    assert "matches more than one" in out.content
    assert "sub_a_1" in out.content and "sub_a_2" in out.content


def test_header_names_the_sender_session():
    m = InboxMessage(
        id="m1", from_session="sub_x_1", from_label="builder",
        from_role="agent", content="hi",
    )
    header = m.attribution_prefix()
    assert "session sub_x_1" in header
    assert "msg id m1" in header


# ─── re-wake: no replays, no strays ───────────────────────────────────


def _sub_tool(monkeypatch, run_child):
    from bridge.tools import sub_agent_tool as sat
    from bridge.tools.agent_types import ModelResolution
    from bridge.tools.base import ToolRegistry

    monkeypatch.setattr(
        sat, "resolve_model_choice",
        lambda agent_type, parent_model: ModelResolution(
            model="fake-model", policy="test", candidates=("fake-model",),
        ),
    )
    monkeypatch.setattr(sat.SubAgentTool, "_run_child", run_child)

    async def _emit(_ev):
        return None

    spec = sat.SubAgentSpec(
        parent_workspace="/tmp",
        parent_model="fake-model",
        build_provider=lambda *a, **k: None,
        parent_registry=ToolRegistry(),
        registry=SubAgentRegistry(),
        emit_event=_emit,
        parent_session_id="session-parent",
        on_child_terminal=None,
    )
    return sat.SubAgentTool(spec), spec


async def test_rewake_does_not_replay_messages_an_earlier_run_read(monkeypatch):
    from bridge.transcript_persistence import save_inbox_state

    seen_at_start: list[list[str]] = []

    async def fake_run_child(self, record):
        seen_at_start.append([m.id for m in record.inbox.drain()])
        self._spec.registry.mark_done(record.id, "ok", SubAgentState.DONE)
        return "ok"

    tool, spec = _sub_tool(monkeypatch, fake_run_child)
    old = InboxMessage(id="aaaa1111", from_session="p", from_label="p", from_role="agent", content="old")
    new = InboxMessage(id="bbbb2222", from_session="p", from_label="p", from_role="agent", content="new")
    # A sidecar from before the drain wrote its state back: the old
    # message is still "unread" though the transcript shows it was read.
    save_inbox_state("sub_x_1", {"sessionId": "sub_x_1", "unread": [old.to_dict(), new.to_dict()], "delivered": []})
    sidecar = {
        "sessionId": "sub_x_1",
        "parentSessionId": "session-parent",
        "agentType": "general",
        "label": "builder",
        "task": "build it",
        "transcript": {"entries": [{"message": {"role": "user", "content": old.as_user_block()}}]},
    }
    assert await tool.resume_archived(sidecar) == "sub_x_1"
    for _ in range(50):
        if seen_at_start:
            break
        await asyncio.sleep(0.01)
    assert seen_at_start == [["bbbb2222"]]


async def test_message_landing_as_a_child_finishes_starts_a_new_run(monkeypatch):
    runs: list[str] = []

    async def fake_run_child(self, record):
        runs.append(record.id)
        record.inbox.drain()
        if len(runs) == 1:
            # Arrives after the runner's last inbox check.
            record.inbox.push(InboxMessage(
                id="late1", from_session="p", from_label="p",
                from_role="agent", content="late addendum",
            ))
        self._spec.registry.mark_done(record.id, "ok", SubAgentState.DONE)
        return "ok"

    tool, spec = _sub_tool(monkeypatch, fake_run_child)
    (_sessions_dir() / "sub_x_1.subagent.json").write_text(json.dumps({
        "sessionId": "sub_x_1", "parentSessionId": "session-parent",
        "agentType": "general", "label": "builder", "task": "build it",
        "transcript": {"entries": []},
    }))
    await tool.resume_archived(json.loads((_sessions_dir() / "sub_x_1.subagent.json").read_text()))
    for _ in range(100):
        if len(runs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert runs == ["sub_x_1", "sub_x_1"]


async def test_wake_of_a_subagent_that_is_running_again_uses_its_live_inbox():
    parent, rec = _parent_with_child()
    state = _State({"session-parent": parent})
    msg = InboxMessage(id="m2", from_session="p", from_label="p", from_role="agent", content="more")
    assert await fb._wake_archived_subagent(state, rec.id, msg) is True
    assert [m.id for m in rec.inbox.unread] == ["m2"]
