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
    gateway = SimpleNamespace(sessions={SLACK: thread}, talk_wake_hook_factory=lambda _sess: hook)
    await fb._handle_talk_deliver_command(gateway, cmd)
    assert turns == [("check the build", hook)]


async def test_a_message_for_a_slack_session_is_refused_when_no_gateway_runs(app_bridge, monkeypatch):
    state, events, built = app_bridge
    _gateway_running(monkeypatch, None)
    await fb._handle_command(state, {"type": "send_message", "sessionId": SLACK, "content": "hi", "clientId": "m1"})
    assert built == [] and _forwarded() == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "Slack" in err["message"]


async def test_a_message_too_large_for_the_control_channel_is_refused(app_bridge, monkeypatch):
    # The gateway reads its control file a megabyte at a time, so a longer
    # line would never be read and would hold up every command after it.
    state, events, _built = app_bridge
    _gateway_running(monkeypatch, os.getpid() + 1)
    image = {"type": "image", "mimeType": "image/png", "dataBase64": "A" * (2 * 1024 * 1024)}
    await fb._handle_command(
        state, {"type": "send_message", "sessionId": SLACK, "content": "look", "attachments": [image]},
    )
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


async def test_the_gateway_still_saves_its_own_sessions(app_bridge, tmp_path):
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
