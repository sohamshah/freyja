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


# ─── wake-on-push ─────────────────────────────────────────────────────


def test_wake_for_inbox_schedules_a_turn_only_when_idle(monkeypatch):
    from bridge import freyja_bridge as fb

    calls: list = []

    def _fake_schedule(sess, content, attachments=None, on_turn_start=None):
        calls.append((sess, content, on_turn_start))
        return True

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", _fake_schedule)

    idle = SimpleNamespace(
        id="idle-root",
        inbox=SessionInbox(session_id="idle-root"),
        pending_task=None,
        talk_wake_hook=None,
    )
    idle.inbox.push(_msg())
    status = fb._BridgeSession.wake_for_inbox(idle)
    assert "woke" in status
    assert len(calls) == 1
    assert calls[0][1] == fb.TALK_WAKE_PROMPT

    busy = SimpleNamespace(
        id="busy-root",
        inbox=SessionInbox(session_id="busy-root"),
        pending_task=_LiveTask(),
        talk_wake_hook=None,
    )
    busy.inbox.push(_msg())
    status = fb._BridgeSession.wake_for_inbox(busy)
    assert "busy" in status
    assert len(calls) == 1, "a busy session must not schedule a wake turn"


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

    # The id without its "freyja:" prefix is normalized before forwarding.
    bare = GW_ID[len("freyja:") :]
    result = await tool.execute("c2", {"to": bare, "content": "ping"})
    assert "forwarded to the gateway daemon" in result.content
    cmd = json.loads(
        (tmp_path / "control" / "commands.jsonl").read_text().splitlines()[-1]
    )
    assert cmd["to"] == GW_ID and cmd["message"]["content"] == "ping"


async def test_gateway_id_goes_to_its_inbox_file_when_no_daemon(monkeypatch, tmp_path):
    """No gateway runs: the thread's inbox file is its mailbox, even with a
    copy of the thread loaded here. The copy saves and runs nothing (see
    _BridgeSession._held_elsewhere), so a message pushed into it was lost
    while the sender was told it was delivered. The gateway reads the file
    when it next loads the thread."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: None)

    wake_log: list = []
    copy = _fake_root(GW_ID, wake_log=wake_log)
    router = _router({GW_ID: copy, "caller": _fake_root("caller")})
    tool = TalkTool(router, _ctx())

    result = await tool.execute("c1", {"to": GW_ID, "content": "hi"})
    assert "sidecar" in result.content
    assert copy.inbox.unread == [] and wake_log == []

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    thread = fb._BridgeSession(
        GW_ID, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    assert thread._restore_inbox()
    assert [m.content for m in thread.inbox.unread] == ["hi"]


def test_a_talk_that_saw_no_gateway_goes_to_one_that_started_before_it_was_queued(monkeypatch, tmp_path):
    """The sender saw no gateway, then stalled while a whole gateway started
    and loaded the thread. Written to the inbox file then, the message
    missed the gateway's load, and the gateway's next save of the inbox
    dropped it."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp
    from bridge.gateway.control_channel import commands_path

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    thread = fb._BridgeSession(
        GW_ID, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    gateway: list[int] = []

    def _owner() -> int | None:
        if gateway:
            return gateway[0]
        # The stall: a gateway writes its PID, then loads the thread.
        gateway.append(os.getpid() + 1)
        thread._restore_inbox()
        return None

    monkeypatch.setattr(tt, "_gateway_owner_pid", _owner)
    status = _router({}).forward_cross_process(GW_ID, _msg("hi"), live_local=False, archived_state=None)
    thread._save_inbox()

    lines = commands_path().read_text().splitlines() if commands_path().exists() else []
    assert [json.loads(line)["message"]["content"] for line in lines] == ["hi"]
    assert "forwarded to the gateway daemon" in status


def _queue_in_another_process(monkeypatch, sid: str, content: str):
    """Start queueing ``content`` to ``sid``'s inbox file on a thread, as
    another process would, and return once it has read the file and is
    about to put its copy back. Join the returned thread to finish it."""
    import threading
    import time
    from pathlib import Path

    replace = Path.replace
    putting_back = threading.Event()

    def _replace_slowly(self, target):
        if threading.current_thread().name == "queue" and str(target).endswith(".inbox.json"):
            putting_back.set()
            time.sleep(0.3)
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace_slowly)
    writer = threading.Thread(target=queue_to_inbox_sidecar, args=(sid, _msg(content)), name="queue")
    writer.start()
    assert putting_back.wait(5)
    return writer


def test_a_message_queued_while_the_gateway_loads_the_thread_is_kept(monkeypatch, tmp_path):
    """No gateway ran, so a talk to the thread went to its inbox file. The
    gateway started and loaded the thread while that write was between its
    read and its write: the gateway's copy of the inbox lacked the message,
    and its next save of the inbox dropped it."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    tp.save_inbox_state(GW_ID, {"sessionId": GW_ID, "unread": [], "delivered": []})
    writer = _queue_in_another_process(monkeypatch, GW_ID, "hi")

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    thread = fb._BridgeSession(
        GW_ID, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    thread._restore_inbox()
    writer.join()
    thread._save_inbox()

    assert [m.content for m in thread.inbox.unread] == ["hi"]
    assert [u["content"] for u in tp.load_inbox_state(GW_ID)["unread"]] == ["hi"]


def test_two_messages_queued_at_once_are_both_kept(monkeypatch, tmp_path):
    # The app and the scheduler daemon can each queue one while no gateway
    # runs.
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    writer = _queue_in_another_process(monkeypatch, GW_ID, "from the app")
    queue_to_inbox_sidecar(GW_ID, _msg("from the daemon"))
    writer.join()

    assert sorted(u["content"] for u in tp.load_inbox_state(GW_ID)["unread"]) == ["from the app", "from the daemon"]


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
    # A clock that jumps a second per reading: the deadline has passed on its first check, so the wait loop returns on its own without a real second going by.
    ticks = iter(range(10_000))
    monkeypatch.setattr(tt, "time", SimpleNamespace(monotonic=lambda: float(next(ticks))))

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
    # The reader of the gateway's channel is the gateway.
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)

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


async def test_talk_to_closed_desktop_root_cold_loads_and_wakes(monkeypatch, tmp_path):
    """2026-10-06: a closed desktop session only got an inbox file and was
    never run, while the sender waited out its reply timeout."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    sid = "session-closed1"
    (tmp_path / f"{sid}.transcript.json").write_text("{}")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(fb, "_resolve_archived_subagent", lambda s: None)

    pushed: list = []
    woke: list = []
    sess = SimpleNamespace(
        inbox=SimpleNamespace(push=pushed.append),
        wake_for_inbox=lambda: woke.append(sid) or "woken",
    )

    class _State(SimpleNamespace):
        async def ensure_session(self, session_id, **kw):
            self.sessions[session_id] = sess
            return sess

    state = _State(sessions={}, talk_wake_hook_factory=None)
    status = await fb.deliver_talk_message_locally(state, sid, _msg("hello"))
    assert status == "woken"
    assert [m.content for m in pushed] == ["hello"]
    assert woke == [sid]


def _scheduler_daemon(monkeypatch, tmp_path) -> tuple[TalkTool, list[str]]:
    """A talk tool in the scheduler daemon: neither the gateway nor the
    app's bridge. Records every session the daemon would load."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: os.getpid() + 1)
    built: list[str] = []

    async def ensure_session(session_id, **_kw):
        built.append(session_id)
        raise AssertionError(f"the scheduler daemon loaded a copy of {session_id}")

    state = SimpleNamespace(
        sessions={}, active_session_id=None, ensure_session=ensure_session, talk_wake_hook_factory=None,
    )
    router = TalkRouter(
        bridge_state=state, get_running_sessions=dict,
        resolve_archived_sub=fb._resolve_archived_subagent,
        wake_archived_sub=lambda sid, _msg: built.append(sid),
    )
    return TalkTool(router, _ctx()), built


def _closed_desktop_session(tmp_path, sid: str) -> None:
    from bridge import transcript_persistence as tp

    (tmp_path / f"{sid}.transcript.json").write_text("{}")
    if sid.startswith("sub_"):
        tp.save_subagent_state(sid, {"sessionId": sid, "parentSessionId": "session-1", "transcript": {}})


@pytest.mark.parametrize("sid", ["session-closed2", "sub_19f0desk_2"])
async def test_a_talk_from_the_scheduler_daemon_to_a_closed_desktop_session_loads_no_copy(monkeypatch, tmp_path, sid):
    """The daemon loaded the session (or re-woke the sub-agent under its
    parent, loaded there) and ran the message as a turn, while the app's
    bridge loads and saves the same files. With no app running, the
    message waits in the session's inbox file."""
    from bridge import transcript_persistence as tp

    tool, built = _scheduler_daemon(monkeypatch, tmp_path)
    _closed_desktop_session(tmp_path, sid)

    await tool.execute("c1", {"to": sid, "content": "the report is ready"})

    assert built == []
    assert [u["content"] for u in tp.load_inbox_state(sid)["unread"]] == ["the report is ready"]


async def test_a_talk_from_the_scheduler_daemon_to_a_desktop_session_goes_to_the_app_while_it_runs(
    monkeypatch, tmp_path
):
    import fcntl

    from bridge import transcript_persistence as tp
    from bridge.gateway import pid as gp
    from bridge.gateway.control_channel import desktop_commands_path

    tool, built = _scheduler_daemon(monkeypatch, tmp_path)
    sid = "session-closed3"
    _closed_desktop_session(tmp_path, sid)
    with gp.desktop_lock_path().open("a") as app:  # the app's bridge, in its own process
        fcntl.flock(app.fileno(), fcntl.LOCK_EX)
        await tool.execute("c1", {"to": sid, "content": "the report is ready"})

    assert built == [] and tp.load_inbox_state(sid) is None
    (line,) = desktop_commands_path().read_text().splitlines()
    cmd = json.loads(line)
    assert (cmd["type"], cmd["to"], cmd["message"]["content"]) == ("talk_deliver", sid, "the report is ready")


def _gateway(monkeypatch, tmp_path) -> tuple[TalkTool, list[str]]:
    """A talk tool in the gateway, which holds the gateway lock. Records
    every session it would load."""
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    monkeypatch.setattr(tt, "_holds_gateway_lock", lambda: True)
    built: list[str] = []

    async def ensure_session(session_id, **_kw):
        built.append(session_id)
        raise AssertionError(f"the gateway loaded a copy of {session_id}")

    state = SimpleNamespace(
        sessions={}, active_session_id=None, ensure_session=ensure_session, talk_wake_hook_factory=None,
    )
    router = TalkRouter(
        bridge_state=state, get_running_sessions=dict,
        resolve_archived_sub=fb._resolve_archived_subagent,
        wake_archived_sub=lambda sid, _msg: built.append(sid),
    )
    return TalkTool(router, _ctx()), built


@pytest.mark.parametrize("sid", ["session-closed5", "sub_19f0desk_5"])
async def test_a_talk_from_the_gateway_to_a_desktop_session_waits_in_its_inbox_file_while_no_app_runs(
    monkeypatch, tmp_path, sid
):
    """The gateway queued it on the app's control channel whatever the app
    did. The app replayed it on its next start, which loaded and woke the
    session, or dropped it on its first start; and the sender waited out
    its whole reply timeout."""
    from bridge import transcript_persistence as tp
    from bridge.gateway.control_channel import desktop_commands_path

    tool, built = _gateway(monkeypatch, tmp_path)
    _closed_desktop_session(tmp_path, sid)

    result = await tool.execute("c1", {"to": sid, "content": "the deploy is done", "wait_for_reply": True})

    assert built == []
    assert not desktop_commands_path().exists() or desktop_commands_path().read_text() == ""
    assert [u["content"] for u in tp.load_inbox_state(sid)["unread"]] == ["the deploy is done"]
    assert "not waiting for a reply" in result.content


async def test_a_talk_from_the_gateway_to_a_desktop_session_goes_to_the_app_while_it_runs(monkeypatch, tmp_path):
    import fcntl

    from bridge import transcript_persistence as tp
    from bridge.gateway import pid as gp
    from bridge.gateway.control_channel import desktop_commands_path

    tool, built = _gateway(monkeypatch, tmp_path)
    sid = "session-closed6"
    _closed_desktop_session(tmp_path, sid)
    with gp.desktop_lock_path().open("a") as app:  # the app's bridge, in its own process
        fcntl.flock(app.fileno(), fcntl.LOCK_EX)
        await tool.execute("c1", {"to": sid, "content": "the deploy is done"})

    assert built == [] and tp.load_inbox_state(sid) is None
    (line,) = desktop_commands_path().read_text().splitlines()
    assert json.loads(line)["to"] == sid


async def test_a_talk_from_the_gateway_to_no_saved_session_is_unresolved_while_no_app_runs(monkeypatch, tmp_path):
    from bridge import transcript_persistence as tp

    tool, _built = _gateway(monkeypatch, tmp_path)

    result = await tool.execute("c1", {"to": "session-typo", "content": "hi"})

    assert "unresolved" in result.content and tp.load_inbox_state("session-typo") is None


async def test_the_apps_first_start_reads_the_commands_sent_once_it_holds_its_lock(monkeypatch):
    """With no offset file yet, the reader skipped to the end of the file
    when it started. Other processes send to the app as soon as it holds
    its lock, which it takes long before its reader starts."""
    from bridge import freyja_bridge as fb
    from bridge.gateway import pid as gp
    from bridge.gateway.control_channel import append_command, desktop_commands_path

    monkeypatch.setattr(gp, "_desktop_lock", None)
    append_command({"type": "talk_deliver", "to": "session-old", "message": {}}, path=desktop_commands_path())
    delivered: list[str] = []

    async def _deliver(_state, cmd):
        delivered.append(cmd["to"])

    monkeypatch.setattr(fb, "_handle_talk_deliver_command", _deliver)
    state = SimpleNamespace()
    fb._take_desktop_lock(state)
    reader = None
    try:
        assert tt._desktop_bridge_runs()
        append_command({"type": "talk_deliver", "to": "session-7", "message": {}}, path=desktop_commands_path())
        reader = await fb._start_desktop_control_reader(state)
        for _ in range(40):
            if delivered:
                break
            await asyncio.sleep(0.05)
    finally:
        if reader is not None:
            await reader.stop()
        gp._desktop_lock.close()

    assert delivered == ["session-7"]


async def test_a_restarted_apps_bridge_reads_the_commands_only_once_the_old_one_let_go(monkeypatch):
    """The new bridge started its reader while the old one still held the
    lock, and both read the same file from the same offset file: a talk
    was delivered twice. Waiting must skip nothing: what came while the
    old one held the lock and had not read is read after."""
    import fcntl

    from bridge import freyja_bridge as fb
    from bridge.gateway import pid as gp
    from bridge.gateway.control_channel import (
        append_command,
        desktop_commands_offset_path,
        desktop_commands_path,
    )

    monkeypatch.setattr(gp, "_desktop_lock", None)
    append_command({"type": "talk_deliver", "to": "session-read", "message": {}}, path=desktop_commands_path())
    # The old bridge read that one, and saved its offset.
    desktop_commands_offset_path().write_text(str(desktop_commands_path().stat().st_size))
    delivered: list[str] = []

    async def _deliver(_state, cmd):
        delivered.append(cmd["to"])

    monkeypatch.setattr(fb, "_handle_talk_deliver_command", _deliver)
    state = SimpleNamespace()
    old = gp.desktop_lock_path().open("a")  # the old bridge, in its own process
    fcntl.flock(old.fileno(), fcntl.LOCK_EX)
    reader = None
    try:
        fb._take_desktop_lock(state)
        reader = await fb._start_desktop_control_reader(state)
        append_command({"type": "talk_deliver", "to": "session-8", "message": {}}, path=desktop_commands_path())
        await asyncio.sleep(0.6)
        assert delivered == []  # the old one may still read it

        old.close()  # the old bridge exits
        for _ in range(60):
            if delivered:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
    finally:
        old.close()
        if reader is not None:
            await reader.stop()
        task = getattr(state, "_desktop_reader_start_task", None)
        if task is not None:
            task.cancel()
        if gp._desktop_lock is not None:
            gp._desktop_lock.close()

    assert delivered == ["session-8"]


async def test_the_apps_bridge_delivers_a_talk_to_its_own_closed_session_itself(monkeypatch, tmp_path):
    from bridge.gateway import pid as gp

    monkeypatch.setattr(gp, "_desktop_lock", object())
    assert _router({}).forward_cross_process("session-closed4", _msg(), live_local=False, archived_state=None) is None


async def test_talk_to_closed_subagent_is_not_cold_loaded_as_a_root(monkeypatch, tmp_path):
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    sid = "sub_abc_1"
    (tmp_path / f"{sid}.transcript.json").write_text("{}")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(fb, "_resolve_archived_subagent", lambda s: {"parentSessionId": "p"})
    assert fb._is_closed_root_session(sid) is False


async def test_talk_does_not_wait_for_a_reply_from_a_session_that_is_not_running(
    monkeypatch, tmp_path
):
    from bridge import freyja_bridge as fb
    from bridge import transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    sid = "freyja:slack:T:channel:C:1.1"
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    router = _router({})
    tool = TalkTool(router, _ctx())
    (tmp_path / f"{sid}.transcript.json").write_text("{}")

    async def boom(**kw):  # would block for reply_timeout_s
        raise AssertionError("must not wait")

    monkeypatch.setattr(tool, "_await_reply", boom)
    out = await tool.execute(
        "c1",
        {"to": sid, "content": "hi", "wait_for_reply": True, "reply_timeout_s": 180},
    )
    assert "not waiting for a reply" in out.content
    assert "NOT seen" in out.content


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
