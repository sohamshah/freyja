"""Inter-agent talk() delivery semantics.

Covers the 2026-08-13 failure where a rewrite brief sent to the idle
Slack session ``freyja:slack:…:1786645306.443119`` from the desktop
bridge (a) landed in a non-authoritative desktop mirror instead of the
gateway daemon and (b) sat undelivered for 2h14m because nothing wakes
an idle root session when its inbox is pushed.

  · wake-on-push: delivering to an idle live root schedules a turn
  · cross-process routing: gateway-shaped ids forward to the daemon's
    control channel instead of a local mirror
  · reply hygiene: wait_for_reply consumes the reply (no double
    delivery) and the injected header carries the message id
  · reporting: wait_for_reply results include the delivery status
  · list_agent_sessions query/limit filtering
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from bridge.inbox import InboxMessage, SessionInbox, new_message_id
from bridge.tools import talk_tool as tt
from bridge.tools.talk_tool import (
    ListAgentSessionsTool,
    TalkRouter,
    TalkRouterContext,
    TalkTool,
    queue_to_inbox_sidecar,
)


# ─── helpers ──────────────────────────────────────────────────────────


def _msg(content: str = "hello", reply_to: str | None = None) -> InboxMessage:
    return InboxMessage(
        id=new_message_id(),
        from_session="sender",
        from_label="sender",
        from_role="agent",
        content=content,
        reply_to=reply_to,
    )


class _LiveTask:
    def done(self) -> bool:
        return False


def _fake_root(session_id: str, *, wake_log: list | None = None) -> SimpleNamespace:
    """A _BridgeSession-shaped stub with a real inbox."""
    sess = SimpleNamespace(
        id=session_id,
        title=session_id,
        parent_session_id=None,
        subagent_registry=None,
        subagent_record=None,
        agent_type=None,
        inbox=SessionInbox(session_id=session_id),
        pending_task=None,
    )

    def _wake() -> str:
        if wake_log is not None:
            wake_log.append(session_id)
        return "recipient was idle — woke it to process the message now"

    sess.wake_for_inbox = _wake
    return sess


def _router(sessions: dict) -> TalkRouter:
    return TalkRouter(
        bridge_state=SimpleNamespace(sessions=sessions),
        get_running_sessions=lambda: dict(sessions),
        resolve_archived_sub=lambda _sid: None,
        wake_archived_sub=lambda _sid, _msg: None,
    )


def _ctx(caller: str = "caller") -> TalkRouterContext:
    return TalkRouterContext(
        caller_session_id=caller,
        caller_label=caller,
        caller_role="agent",
        parent_session_id=None,
    )


# ─── inbox primitives ─────────────────────────────────────────────────


def test_take_reply_removes_message_and_marks_delivered():
    inbox = SessionInbox(session_id="s1")
    reply = _msg("the answer", reply_to="source123")
    inbox.push(_msg("unrelated"))
    inbox.push(reply)

    got = inbox.take_reply("source123")
    assert got is reply
    assert got.delivered_at is not None
    # The unrelated message stays queued; the reply is gone from unread
    # so the next drain can't double-deliver it.
    assert [m.content for m in inbox.unread] == ["unrelated"]
    assert reply in inbox.delivered
    assert inbox.take_reply("source123") is None


def test_attribution_header_carries_full_message_id():
    m = _msg("ping")
    header = m.attribution_prefix()
    # The recipient can only construct talk(reply_to=…) if the full id
    # is visible in the injected block.
    assert f"id {m.id}" in header


# ─── wake-on-push ─────────────────────────────────────────────────────


def test_wake_for_inbox_schedules_turn_when_idle(monkeypatch):
    from bridge import freyja_bridge as fb

    calls: list = []

    def _fake_schedule(sess, content, attachments=None, on_turn_start=None):
        calls.append((sess, content, on_turn_start))
        return True

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", _fake_schedule)

    stub = SimpleNamespace(
        id="idle-root",
        inbox=SessionInbox(session_id="idle-root"),
        pending_task=None,
        talk_wake_hook=None,
    )
    stub.inbox.push(_msg())

    status = fb._BridgeSession.wake_for_inbox(stub)
    assert "woke" in status
    assert len(calls) == 1
    assert calls[0][1] == fb.TALK_WAKE_PROMPT


def test_wake_for_inbox_noop_when_busy(monkeypatch):
    from bridge import freyja_bridge as fb

    def _explode(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("busy session must not schedule a wake turn")

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", _explode)

    stub = SimpleNamespace(
        id="busy-root",
        inbox=SessionInbox(session_id="busy-root"),
        pending_task=_LiveTask(),
        talk_wake_hook=None,
    )
    stub.inbox.push(_msg())

    status = fb._BridgeSession.wake_for_inbox(stub)
    assert "busy" in status


async def test_deliver_to_live_root_pushes_and_wakes():
    wake_log: list = []
    root = _fake_root("root-a", wake_log=wake_log)
    router = _router({"root-a": root})

    status = await router.deliver("root-a", root, None, None, _msg("brief"))
    assert "delivered to root session" in status
    assert "woke" in status
    assert wake_log == ["root-a"]
    assert [m.content for m in root.inbox.unread] == ["brief"]


# ─── cross-process routing ────────────────────────────────────────────


GW_ID = "freyja:slack:T04X:channel:C0AJ:1786645306.443119"


async def test_gateway_id_forwards_to_daemon_not_local_mirror(
    monkeypatch, tmp_path
):
    """The exact 2026-08-13 shape: a live LOCAL mirror of a Slack
    session exists, but another process owns the gateway lock — the
    message must go over the control channel, not into the mirror."""
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: os.getpid() + 1)

    mirror = _fake_root(GW_ID)
    caller = _fake_root("caller")
    router = _router({GW_ID: mirror, "caller": caller})
    tool = TalkTool(router, _ctx())

    result = await tool.execute("c1", {"to": GW_ID, "content": "rewrite it"})
    assert "forwarded to the gateway daemon" in result.content
    # Mirror untouched — no split-brain double delivery.
    assert mirror.inbox.unread == []

    lines = (tmp_path / "control" / "commands.jsonl").read_text().splitlines()
    cmd = json.loads(lines[-1])
    assert cmd["type"] == "talk_deliver"
    assert cmd["to"] == GW_ID
    assert cmd["message"]["content"] == "rewrite it"


async def test_gateway_id_without_prefix_is_normalized(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: os.getpid() + 1)

    router = _router({"caller": _fake_root("caller")})
    tool = TalkTool(router, _ctx())

    bare = GW_ID[len("freyja:") :]
    result = await tool.execute("c1", {"to": bare, "content": "ping"})
    assert "forwarded to the gateway daemon" in result.content
    cmd = json.loads(
        (tmp_path / "control" / "commands.jsonl").read_text().splitlines()[-1]
    )
    assert cmd["to"] == GW_ID


async def test_gateway_id_delivers_locally_when_no_daemon(monkeypatch):
    """Desktop-only deployment: no gateway lock holder → the live local
    session is the best (only) surface."""
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)

    wake_log: list = []
    sess = _fake_root(GW_ID, wake_log=wake_log)
    router = _router({GW_ID: sess, "caller": _fake_root("caller")})
    tool = TalkTool(router, _ctx())

    result = await tool.execute("c1", {"to": GW_ID, "content": "hi"})
    assert "delivered to root session" in result.content
    assert wake_log == [GW_ID]


def test_sidecar_queue_roundtrip(monkeypatch, tmp_path):
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    m = _msg("offline brief")
    status = queue_to_inbox_sidecar("some-session", m)
    assert "queued to inbox sidecar" in status

    data = json.loads((tmp_path / "some-session.inbox.json").read_text())
    assert [u["content"] for u in data["unread"]] == ["offline brief"]
    # And it round-trips through the restore path.
    restored = SessionInbox.from_dict(data)
    assert restored is not None
    assert restored.unread[0].content == "offline brief"


async def test_typo_id_stays_unresolved_not_sidecar(monkeypatch, tmp_path):
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)

    router = _router({"caller": _fake_root("caller")})
    tool = TalkTool(router, _ctx())

    result = await tool.execute(
        "c1", {"to": "session-doesnotexist", "content": "hello?"}
    )
    assert "unresolved" in result.content
    # No orphan sidecar for a session that will never run.
    assert not (tmp_path / "session-doesnotexist.inbox.json").exists()


async def test_closed_desktop_session_gets_sidecar_delivery(
    monkeypatch, tmp_path
):
    """A session that exists on disk but isn't loaded anywhere gets its
    message queued for its next load instead of a hard failure."""
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)
    (tmp_path / "session-closed.transcript.json").write_text("{}")

    router = _router({"caller": _fake_root("caller")})
    tool = TalkTool(router, _ctx())

    result = await tool.execute(
        "c1", {"to": "session-closed", "content": "for later"}
    )
    assert "sidecar" in result.content
    data = json.loads((tmp_path / "session-closed.inbox.json").read_text())
    assert [u["content"] for u in data["unread"]] == ["for later"]


# ─── wait_for_reply ───────────────────────────────────────────────────


async def test_wait_for_reply_returns_and_consumes_reply(monkeypatch):
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)

    recipient = _fake_root("worker")
    caller = _fake_root("caller")
    router = _router({"worker": recipient, "caller": caller})
    tool = TalkTool(router, _ctx("caller"))

    async def _respond() -> None:
        # Wait for the outbound message to land, then reply to it.
        for _ in range(100):
            if recipient.inbox.unread:
                break
            await asyncio.sleep(0.01)
        sent = recipient.inbox.unread[0]
        caller.inbox.push(_msg("done!", reply_to=sent.id))

    responder = asyncio.create_task(_respond())
    result = await tool.execute(
        "c1",
        {
            "to": "worker",
            "content": "do the thing",
            "wait_for_reply": True,
            "reply_timeout_s": 5,
        },
    )
    await responder
    assert "Reply from" in result.content
    assert "done!" in result.content
    # Consumed — the caller's next drain must not re-inject the reply.
    assert caller.inbox.unread == []


async def test_wait_for_reply_timeout_reports_delivery_status(monkeypatch):
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)

    recipient = _fake_root("worker")
    caller = _fake_root("caller")
    router = _router({"worker": recipient, "caller": caller})
    tool = TalkTool(router, _ctx("caller"))

    result = await tool.execute(
        "c1",
        {
            "to": "worker",
            "content": "do the thing",
            "wait_for_reply": True,
            "reply_timeout_s": 1,
        },
    )
    # The old result hid the delivery outcome entirely ("Sent … proceed
    # without."), which read as success. It must now carry the status.
    assert "delivered to root session" in result.content
    assert "No reply within" in result.content
    assert "do NOT assume" in result.content


# ─── control-channel end-to-end ───────────────────────────────────────


async def test_control_channel_roundtrip_delivers_and_wakes(
    monkeypatch, tmp_path
):
    """Writer process appends talk_deliver → reader tails it → handler
    pushes into the live session's inbox and wakes it."""
    from bridge import freyja_bridge as fb
    from bridge.gateway.control_channel import (
        ControlChannelReader,
        append_command,
    )

    cmd_file = tmp_path / "cmds.jsonl"
    off_file = tmp_path / "cmds.offset"

    wake_log: list = []
    sess = _fake_root(GW_ID, wake_log=wake_log)
    state = SimpleNamespace(sessions={GW_ID: sess}, talk_wake_hook_factory=None)

    reader = ControlChannelReader(commands_file=cmd_file, offset_file=off_file)
    reader.register(
        "talk_deliver", lambda cmd: fb._handle_talk_deliver_command(state, cmd)
    )
    await reader.start()
    try:
        append_command(
            {
                "type": "talk_deliver",
                "to": GW_ID,
                "message": _msg("cross-process brief").to_dict(),
            },
            path=cmd_file,
        )
        for _ in range(100):
            if sess.inbox.unread:
                break
            await asyncio.sleep(0.05)
    finally:
        await reader.stop()

    assert [m.content for m in sess.inbox.unread] == ["cross-process brief"]
    assert wake_log == [GW_ID]


async def test_talk_deliver_to_unloaded_session_queues_sidecar(monkeypatch, tmp_path):
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    # Not the gateway owner → no cold ensure; no live session or
    # sub-agent → the message must survive via the inbox sidecar.
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    state = SimpleNamespace(sessions={}, talk_wake_hook_factory=None)

    status = await fb.deliver_talk_message_locally(state, GW_ID, _msg("later"))
    assert "sidecar" in status
    data = json.loads((tmp_path / f"{GW_ID}.inbox.json").read_text())
    assert [u["content"] for u in data["unread"]] == ["later"]


# ─── list_agent_sessions filters ──────────────────────────────────────


async def test_list_sessions_query_and_limit(monkeypatch, tmp_path):
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    sessions = {
        f"root-{i}": _fake_root(f"root-{i}") for i in range(5)
    }
    sessions[GW_ID] = _fake_root(GW_ID)
    router = _router(sessions)
    tool = ListAgentSessionsTool(router, _ctx())

    out = json.loads(
        (
            await tool.execute(
                "c1", {"connected": False, "query": "slack", "limit": 10}
            )
        ).content
    )
    assert out["count"] == 1
    assert out["sessions"][0]["id"] == GW_ID

    out = json.loads(
        (await tool.execute("c2", {"connected": False, "limit": 3})).content
    )
    assert out["count"] == 6
    assert out["shown"] == 3
    assert "note" in out
