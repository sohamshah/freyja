"""Slack's /reset starts the thread's next message on a fresh conversation.

/reset dropped the thread's session from memory and nothing else. The
thread's next message loaded it again from its saved transcript, so the
conversation went on as if no reset had happened. A turn running at the
reset saved its question there too as it was cancelled.
"""

from __future__ import annotations

import asyncio

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp

KEY = "freyja:slack:T1:channel:C1:1700000000.000100"


async def _thread(monkeypatch, tmp_path, mode="goal"):
    """The gateway's state with a Slack thread loaded, on a model and in
    a mode of its own, one turn of which is saved."""
    from engine.types import Message

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session(KEY, model_id="claude-sonnet-5-5", coordination_strategy=mode)
    await sess.initialize()
    sess.session.transcript.append_message(Message(role="user", content="the old question"))
    sess.session.transcript.append_message(Message(role="assistant", content="the old answer"))
    sess._save_transcript()
    return state, sess


def _texts(sess) -> str:
    if sess.session is None:  # nothing loaded: an empty conversation
        return ""
    return " ".join(str(e.message.content) for e in sess.session.transcript.entries if e.message)


def _gateway(state):
    from bridge.gateway.run import GatewayDaemon

    gateway = GatewayDaemon()
    gateway.state = state
    return gateway


@pytest.mark.parametrize("turn_running", [False, True])
async def test_the_next_message_after_reset_starts_a_fresh_conversation(monkeypatch, tmp_path, turn_running):
    state, sess = await _thread(monkeypatch, tmp_path)
    if turn_running:
        asked = asyncio.Event()

        class _Runner:
            async def run(self, session, message, stream=True):
                session.add_user_message(message)
                asked.set()
                await asyncio.Event().wait()

        async def _initialize():
            return None

        sess.initialize = _initialize
        sess.runner = _Runner()
        sess.pending_task = asyncio.create_task(sess.run_turn("a new question"))
        await asyncio.wait_for(asked.wait(), 1)

    await _gateway(state)._reset_session(KEY)  # /reset
    if turn_running:
        await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), timeout=2)
    fresh = await state.ensure_session(KEY)  # the thread's next message

    assert fresh is not sess
    assert "old" not in _texts(fresh) and "a new question" not in _texts(fresh)
    # The thread keeps its model and mode.
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")
    # The old conversation stays on disk, where no load reads it.
    (aside,) = tp.SESSIONS_DIR.glob("*.transcript.json.reset-*")
    assert "the old answer" in aside.read_text()


async def test_reset_of_a_thread_the_gateway_has_not_loaded_since_it_started(monkeypatch, tmp_path):
    state, _sess = await _thread(monkeypatch, tmp_path)
    state.sessions.clear()  # the gateway restarted

    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)

    assert "old" not in _texts(fresh)
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")


# ── what the old conversation leaves under the thread's id ───────────


def _board(sess):
    return sess.kanban_board if sess.coordination_strategy == "kanban" else sess.task_board


@pytest.mark.parametrize("mode", ["kanban", "goal"])
async def test_the_conversation_after_reset_starts_on_an_empty_board(monkeypatch, tmp_path, mode):
    # The fresh conversation's first turn replayed the old one's kanban
    # (or task) journal, left under the thread's id.
    state, sess = await _thread(monkeypatch, tmp_path, mode)
    await _board(sess).create(title="the old card")

    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()  # its first turn

    assert await _board(fresh).list() == []
    (aside,) = tp.SESSIONS_DIR.glob("*.jsonl.reset-*")
    assert "the old card" in aside.read_text()


async def test_a_card_a_worker_the_reset_stopped_marks_stays_off_the_new_board(monkeypatch, tmp_path):
    # The worker marks its card as it ends (_mark_kanban_terminal), after
    # the reset: the row began a new journal, and the fresh board replayed
    # the card from it.
    state, sess = await _thread(monkeypatch, tmp_path, "kanban")
    card = await sess.kanban_board.create(title="the old card")
    await sess.kanban_board.update(card.id, actor="worker", status="running")

    await _gateway(state)._reset_session(KEY)
    await sess.kanban_board.update(card.id, actor="worker", status="review")
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()

    assert await fresh.kanban_board.list() == []


async def test_the_conversation_after_reset_has_no_goal_of_the_old_one(monkeypatch, tmp_path):
    # The goal file stayed under the thread's id: once the fresh
    # conversation had a turn saved, a load brought the old goal back.
    from bridge.tools.goal_loop import GoalState
    from engine.types import Message

    state, sess = await _thread(monkeypatch, tmp_path)
    sess.goal_state = GoalState(goal="ship the old thing")
    sess._save_goal_state()

    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()
    fresh.session.transcript.append_message(Message(role="user", content="a new question"))
    fresh._save_transcript()
    state.sessions.clear()  # the gateway restarted
    again = await state.ensure_session(KEY)

    assert "a new question" in _texts(again)
    assert again.goal_state is None
    (aside,) = tp.SESSIONS_DIR.glob("*.goal.json.reset-*")
    assert "ship the old thing" in aside.read_text()


async def test_a_message_the_old_conversation_left_unread_does_not_wake_the_new_one(monkeypatch, tmp_path):
    # The inbox watch took it into the fresh conversation and woke it.
    from bridge.inbox import InboxMessage, new_message_id

    state, sess = await _thread(monkeypatch, tmp_path)
    sess.inbox.push(InboxMessage(
        id=new_message_id(), from_session="sub_x", from_label="worker", from_role="agent",
        content="the old report",
    ))

    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)
    woke: list[str] = []
    fresh.wake_for_inbox = lambda: woke.append(fresh.id) or "woke"
    fb._take_in_queued_inbox_messages(state)

    assert fresh.inbox.unread == [] and woke == []
    (aside,) = tp.SESSIONS_DIR.glob("*.inbox.json.reset-*")
    assert "the old report" in aside.read_text()


async def test_the_conversation_after_reset_holds_none_of_the_old_ones_writes_or_working_memory(monkeypatch, tmp_path):
    # The thread's project directory stays under its id: the write
    # reminder listed the old conversation's writes (its action ledger,
    # and its artifact manifest) as this chat's, and its working memory
    # came back.
    import json

    state, sess = await _thread(monkeypatch, tmp_path)
    old_file = str(tmp_path / "old_report.md")
    sess.session_ledger.record_effect(kind="file_create", operation="create", summary="created old_report.md", path=old_file)
    sess.artifact_store.record_file(
        tmp_path / "old_notes.md", creator_id=sess.id, creator_label="lead", operation="write", source="write_file",
    )
    sess.working_memory.set_overview(summary="the old summary")
    project = sess.project_output_dir

    await _gateway(state)._reset_session(KEY)
    await asyncio.sleep(0.01)
    fresh = await state.ensure_session(KEY)

    assert fresh.session_ledger.effects() == []
    assert fresh._build_write_ledger_reminder() is None
    assert fresh.working_memory.overview() is None
    # The ledger and manifest stay whole on disk; the working memory is set aside.
    assert old_file in (project / "action_ledger.jsonl").read_text()
    assert "old_notes.md" in (project / "manifest.jsonl").read_text()
    (aside,) = project.glob("working_memory.json.reset-*")
    assert json.loads(aside.read_text())["overview"]["summary"] == "the old summary"
    # A write of the new conversation is its own.
    fresh.session_ledger.record_effect(kind="file_create", operation="create", summary="created new.md", path=str(tmp_path / "new.md"))
    reopened = fb.SessionLedger(session_id=KEY, project_dir=project)
    reopened.ensure()
    assert [r["summary"] for r in reopened.effects()] == ["created new.md"]


def _subagent(parent: str, sub_id: str) -> str:
    """A sub-agent of ``parent`` that ran and ended, as its files hold it."""
    import time

    tp.save_subagent_state(sub_id, {
        "sessionId": sub_id, "parentSessionId": parent, "agentType": "general",
        "label": sub_id, "task": "a task", "transcript": {"entries": []},
        "savedAt": int(time.time() * 1000),
    })
    return sub_id


@pytest.mark.parametrize("reset", ["/reset", "new_session"])
async def test_the_conversation_after_reset_lists_none_of_the_old_sub_agents(monkeypatch, tmp_path, reset):
    # The old sub-agents' files still name the thread as their parent: the
    # fresh conversation's list showed them as its archived children.
    import json

    monkeypatch.setattr(fb, "_SUBAGENT_PARENTS", {})
    state, _sess = await _thread(monkeypatch, tmp_path)
    old = _subagent(KEY, "sub_old_1")

    if reset == "/reset":
        await _gateway(state)._reset_session(KEY)
    else:  # the app's new session under a loaded id
        await fb._handle_command(state, {"type": "new_session", "sessionId": KEY})
    await asyncio.sleep(0.01)
    new = _subagent(KEY, "sub_new_1")  # one the fresh conversation started
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()
    listing = fresh.tool_registry._tools["list_agent_sessions"]
    out = json.loads((await listing.execute("c1", {})).content)

    assert [(s["id"], s["relationship"]) for s in out["sessions"]] == [(new, "child")]
    assert tp.load_subagent_state(old)["parentSessionId"] == KEY  # its own files stay


async def test_a_talk_to_an_old_sub_agent_does_not_wake_it_under_the_fresh_conversation(monkeypatch, tmp_path):
    # It was resumed under the conversation now under the thread's id,
    # became its child, and its report woke that conversation.
    from bridge.inbox import InboxMessage, new_message_id

    monkeypatch.setattr(fb, "_SUBAGENT_PARENTS", {})
    state, _sess = await _thread(monkeypatch, tmp_path)
    old = _subagent(KEY, "sub_old_1")
    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()
    resumed: list[str] = []

    async def _resume(sidecar, **_kwargs):
        resumed.append(sidecar["sessionId"])
        return sidecar["sessionId"]

    monkeypatch.setattr(fresh.tool_registry._tools["sub_agent"], "resume_archived", _resume)
    msg = InboxMessage(
        id=new_message_id(), from_session="operator", from_label="operator", from_role="operator",
        content="one more thing",
    )

    assert await fb._wake_archived_subagent(state, old, msg) is False
    assert resumed == []
    # The message waits in its own inbox file.
    assert [m["content"] for m in tp.load_inbox_state(old)["unread"]] == ["one more thing"]


async def test_a_talk_to_an_old_sub_agent_says_its_conversation_was_reset(monkeypatch, tmp_path):
    # The talk said the re-wake failed because no session could host it,
    # and that it would read the message when it next runs: nothing runs
    # it again.
    monkeypatch.setattr(fb, "_SUBAGENT_PARENTS", {})
    state, _sess = await _thread(monkeypatch, tmp_path)
    old = _subagent(KEY, "sub_old_1")
    await _gateway(state)._reset_session(KEY)
    fresh = await state.ensure_session(KEY)
    await fresh.initialize()

    out = await fresh.tool_registry._tools["talk"].execute(
        "c1", {"to": old, "content": "one more thing", "wait_for_reply": True, "reply_timeout_s": 1},
    )

    assert "reset" in out.content and "inbox" in out.content
    assert "when it next runs" not in out.content
    assert [m["content"] for m in tp.load_inbox_state(old)["unread"]] == ["one more thing"]


@pytest.mark.parametrize("loaded", [True, False])
async def test_reset_parses_no_transcript_on_the_gateways_loop(monkeypatch, tmp_path, loaded):
    # /reset read the thread's settings from its saved transcript on the
    # gateway's loop: a long DM's file held every thread up while it was
    # parsed.
    import json
    import threading

    from bridge.gateway.platforms.base import IncomingMessage
    from bridge.gateway.session_router import source_from_session_key

    state, _sess = await _thread(monkeypatch, tmp_path)
    if not loaded:
        state.sessions.clear()  # the gateway restarted
    parsed_on: list[int] = []

    class _Json:
        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, *args, **kwargs):
            parsed_on.append(threading.get_ident())
            return json.loads(*args, **kwargs)

    class _Adapter:
        async def send(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(tp, "json", _Json())
    reset = IncomingMessage(
        source=source_from_session_key(KEY), text="/reset",
        is_slash_command=True, slash_command_name="reset",
    )
    assert await _gateway(state)._handle_slash_in_gateway(reset, _Adapter())
    loop_parsed = threading.get_ident() in parsed_on
    monkeypatch.setattr(tp, "json", json)
    fresh = await state.ensure_session(KEY)  # the thread's next message

    assert not loop_parsed
    assert "old" not in _texts(fresh)
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")


async def test_reset_of_a_thread_with_no_message_yet_keeps_its_settings_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    await state.ensure_session(KEY, model_id="claude-sonnet-5-5")  # /model before its first message

    await _gateway(state)._reset_session(KEY)

    assert list(tp.SESSIONS_DIR.glob("*.reset-*")) == []
    assert tp.load_transcript(KEY)["metadata"]["model_id"] == "claude-sonnet-5-5"


async def test_the_settings_kept_off_the_loop_go_over_no_conversation_saved_meanwhile(monkeypatch, tmp_path):
    # The thread's next message came in while the old file was parsed,
    # and its turn saved first.
    state, _sess = await _thread(monkeypatch, tmp_path)
    aside = tp.set_transcript_aside(KEY)
    tp.save_transcript(KEY, {
        "version": 1, "session_id": KEY, "metadata": {"model_id": "kimi-k3-fast"},
        "transcript": {"entries": [{"id": "n1"}], "head_id": "n1"},
    })

    assert tp.keep_settings(KEY, aside) == aside

    assert tp.load_transcript(KEY)["transcript"]["entries"] == [{"id": "n1"}]


async def test_a_message_while_reset_reads_the_settings_starts_on_them(monkeypatch, tmp_path):
    # The thread's next message came in while /reset read its model and
    # mode off the loop: it found no file, and its session started on the
    # gateway's default model and mode.
    import threading

    state, _sess = await _thread(monkeypatch, tmp_path)
    reading, read = threading.Event(), threading.Event()
    keep_settings = tp.keep_settings

    def _slow_keep_settings(key, aside):
        reading.set()
        read.wait(5)
        return keep_settings(key, aside)

    monkeypatch.setattr(tp, "keep_settings", _slow_keep_settings)
    reset = asyncio.create_task(_gateway(state)._reset_session(KEY))  # /reset
    await asyncio.to_thread(reading.wait, 5)
    message = asyncio.create_task(state.ensure_session(KEY))  # the thread's next message
    await asyncio.sleep(0.05)
    read.set()
    await reset
    fresh = await message

    assert "old" not in _texts(fresh)
    assert (fresh.model_id, fresh.coordination_strategy) == ("claude-sonnet-5-5", "goal")
