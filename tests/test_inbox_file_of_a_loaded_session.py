"""A message another process queues in the inbox file of a session that the
owner already has loaded reaches that session and is never written over.

A talk for a scheduled job's session from a process that does not hold it
goes to the session's inbox file (_scheduler_runs_elsewhere). The tick owner
keeps the session loaded between runs, read the file once when it loaded it,
and saved its own copy over the file at every inbox change, so the message
was neither read nor kept.
"""

from __future__ import annotations

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.inbox import InboxMessage
from bridge.tools.talk_tool import queue_to_inbox_sidecar

SCHED = "scheduler:sched_19f0"


def _msg(content: str, *, sender: str = "session-1") -> InboxMessage:
    return InboxMessage(
        id=f"t-{content}", from_session=sender, from_label="lead", from_role="agent", content=content,
    )


def _owner(monkeypatch, tmp_path, sid: str = SCHED):
    """The tick owner, with the job's session loaded and idle."""
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.scheduler._is_owner = True  # noqa: SLF001
    sess = fb._BridgeSession(
        sid, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess._restore_inbox()  # noqa: SLF001
    state.sessions[sid] = sess
    return state, sess, events


def _file(sid: str = SCHED) -> list[str]:
    return [m["content"] for m in (tp.load_inbox_state(sid) or {}).get("unread", [])]


def test_the_owners_inbox_save_keeps_a_message_another_process_queued(monkeypatch, tmp_path):
    state, sess, _ = _owner(monkeypatch, tmp_path)
    sess.inbox.push(_msg("from the owner"))

    queue_to_inbox_sidecar(SCHED, _msg("from the app"))
    sess.inbox.push(_msg("from the owner again"))

    assert sorted(_file()) == ["from the app", "from the owner", "from the owner again"]


def test_what_the_owner_took_in_or_withdrew_stays_out_of_the_file(monkeypatch, tmp_path):
    state, sess, _ = _owner(monkeypatch, tmp_path)
    sess.inbox.push(_msg("read"))
    sess.inbox.push(_msg("withdrawn"))
    queue_to_inbox_sidecar(SCHED, _msg("queued"))

    sess.inbox.take_first_kind("talk")
    sess.inbox.remove("t-withdrawn")

    assert _file() == ["queued"]


def test_a_loaded_owner_takes_in_a_queued_message_and_wakes(monkeypatch, tmp_path):
    state, sess, events = _owner(monkeypatch, tmp_path)
    woke: list[str] = []
    monkeypatch.setattr(sess, "wake_for_inbox", lambda: woke.append(sess.id) or "woke")

    fb._take_in_queued_inbox_messages(state)
    assert woke == []

    queue_to_inbox_sidecar(SCHED, _msg("from the app"))
    fb._take_in_queued_inbox_messages(state)
    fb._take_in_queued_inbox_messages(state)

    assert [m.content for m in sess.inbox.unread] == ["from the app"]
    assert woke == [SCHED]
    assert _file() == ["from the app"]
    assert [e["action"] for e in events if e.get("type") == "inbox_event"] == ["enqueued"]


def test_a_loaded_owner_takes_in_a_queued_stop_notice_without_waking(monkeypatch, tmp_path):
    # The app's copy sends the notice of work its stop ended here. A stop
    # asks for quiet: the notice rides into the next turn, as one a
    # stop in this process leaves.
    state, sess, _ = _owner(monkeypatch, tmp_path)
    woke: list[str] = []
    monkeypatch.setattr(sess, "wake_for_inbox", lambda: woke.append(sess.id) or "woke")
    notice = _msg("[background work stopped by the operator]")
    notice.meta = {"stoppedBatch": True, "count": 1}

    queue_to_inbox_sidecar(SCHED, notice)
    fb._take_in_queued_inbox_messages(state)

    assert [m.content for m in sess.inbox.unread] == ["[background work stopped by the operator]"]
    assert woke == []


def test_a_copy_held_elsewhere_takes_in_nothing(monkeypatch, tmp_path):
    # The app's copy of a job's session that the tick owner runs: the file
    # is the owner's mailbox.
    state, sess, _ = _owner(monkeypatch, tmp_path)
    state.scheduler._is_owner = False  # noqa: SLF001
    queue_to_inbox_sidecar(SCHED, _msg("for the owner"))

    fb._take_in_queued_inbox_messages(state)

    assert sess.inbox.unread == []
    assert _file() == ["for the owner"]


def test_a_running_sub_agent_takes_in_a_queued_message(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from bridge.inbox import SessionInbox

    state, sess, _ = _owner(monkeypatch, tmp_path)
    rec = SimpleNamespace(id="sub_19f0sched_1", inbox=SessionInbox(session_id="sub_19f0sched_1"), state=None)
    sess.subagent_registry = SimpleNamespace(list_all=lambda: [rec], get=lambda sid: rec if sid == rec.id else None)
    monkeypatch.setattr(fb, "_root_session_id", lambda sid: SCHED)

    queue_to_inbox_sidecar(rec.id, _msg("for the worker"))
    fb._take_in_queued_inbox_messages(state)

    assert [m.content for m in rec.inbox.unread] == ["for the worker"]


@pytest.mark.parametrize("start_kwargs", [{}, {"listen": False}], ids=["gateway", "scheduler daemon"])
async def test_the_gateway_and_the_scheduler_daemon_watch_their_sessions_inbox_files(monkeypatch, start_kwargs):
    # Either can own the scheduler's tick. The app's bridge starts the
    # same watch in _main.
    from tests.test_scheduler_slack_sink import _start_gateway

    started: list = []
    monkeypatch.setattr(fb, "_start_inbox_watch", started.append)
    daemon = await _start_gateway(monkeypatch, **start_kwargs)

    assert started == [daemon.state]
