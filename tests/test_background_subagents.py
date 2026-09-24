"""Background sub-agents, completion memos, and follow-up routing (bridge).

The contract:

* ``sub_agent`` / ``computer_use`` never block the parent turn. A child
  the parent's MODEL spawned reports back with an inbox memo when it
  reaches a terminal state; the memo slides into a running turn or wakes
  an idle parent (debounced), except when the operator stopped the child
  (queued, no wake) or the parent killed it (nothing).
* ``subagents`` has no blocking wait; a legacy wait returns at once.
* Operator follow-ups sent mid-turn ride the inbox: injected at the next
  boundary with a framing header, cut in with force, promoted to a fresh
  turn when the turn they aimed at already ended, handed back on stop.
* Stopping the turn leaves background children alone; stopping the
  children leaves the turn alone.
* Job-style callers (scheduler, voice) wait for quiescence — the turn,
  the children, and the wake turns their memos trigger.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from bridge import freyja_bridge as fb
from bridge.inbox import (
    KIND_FOLLOWUP,
    KIND_MEMO,
    InboxMessage,
    SessionInbox,
    new_message_id,
)
from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState
from bridge.tools.sub_agent_tool import build_subagent_memo
from bridge.tools.subagents_tool import SubAgentsTool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every write off the real ~/.freyja: inbox sidecars go to
    tmp, and spawn telemetry goes nowhere. The telemetry module is
    stubbed in sys.modules rather than patched, because it freezes
    Path.home() at import and another test relies on importing it fresh."""
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda ev: events.append(ev))
    return events


@pytest.fixture
def events(_isolate):
    return _isolate


def _record(registry: SubAgentRegistry, sid: str, *, label="researcher", notify=True):
    rec = registry.register(id=sid, label=label, task="dig into the logs", mode="background")
    rec.agent_type_name = "explore"
    rec.notify_parent = notify
    return rec


# ─── memo content ─────────────────────────────────────────────────────


def test_memo_for_finished_child_carries_report_and_siblings():
    reg = SubAgentRegistry()
    done = _record(reg, "sub_a", label="log digger")
    other = _record(reg, "sub_b", label="metrics")
    done.artifact_path = "/tmp/out/sub_a.md"
    done.created_files = ["/tmp/out/sub_a.md", "/tmp/out/chart.png"]
    done.tools_called = 7
    reg.mark_done("sub_a", "Found the leak in pool.py:88.", SubAgentState.DONE)

    memo = build_subagent_memo(done, still_running=[other])

    assert memo.kind == KIND_MEMO
    assert memo.from_session == "sub_a"
    assert memo.content.startswith("[sub-agent memo · log digger · id sub_a · done ·")
    assert "Found the leak in pool.py:88." in memo.content
    assert "Full output: /tmp/out/sub_a.md" in memo.content
    assert "`/tmp/out/chart.png`" in memo.content
    assert "Still running: metrics (sub_b)" in memo.content
    assert "No reply is needed" in memo.content
    assert memo.meta["state"] == "done" and memo.meta["stillRunning"] == 1
    # Memos carry their own header — no talk attribution line.
    assert memo.as_user_block() == memo.content.strip()


def test_memo_for_failed_and_stopped_children():
    reg = SubAgentRegistry()
    failed = _record(reg, "sub_f")
    reg.mark_done("sub_f", "Error: provider exploded", SubAgentState.FAILED)
    stopped = _record(reg, "sub_s")
    stopped.cancel_origin = "operator"
    reg.mark_done("sub_s", "Cancelled", SubAgentState.CANCELLED)

    f = build_subagent_memo(failed, still_running=[])
    s = build_subagent_memo(stopped, still_running=[])

    assert "FAILED" in f.content and "provider exploded" in f.content
    assert "was stopped by the operator" in s.content
    assert "Don't assume the task is done" in s.content
    assert "No other sub-agents of yours are running." in s.content


# ─── subagents tool: nothing blocks ───────────────────────────────────


async def test_subagents_wait_is_gone_and_legacy_calls_return_immediately():
    reg = SubAgentRegistry()
    _record(reg, "sub_a")  # still running forever
    tool = SubAgentsTool(reg)

    assert tool.definition.parameters["properties"]["action"]["enum"] == [
        "list", "status", "result", "kill",
    ]
    for action in ("wait", "wait_all", "status"):
        res = await asyncio.wait_for(
            tool.execute("c", {"action": action, "id": "sub_a"}), timeout=1,
        )
        assert res.is_error is False
        assert "memo" in res.content


async def test_subagents_result_and_kill():
    reg = SubAgentRegistry()
    rec = _record(reg, "sub_a")
    tool = SubAgentsTool(reg)

    running = await tool.execute("c", {"action": "result", "id": "sub_a"})
    assert "still running" in running.content

    killed = await tool.execute("c", {"action": "kill", "id": "sub_a"})
    assert "Kill signal sent" in killed.content
    assert rec.cancel_event.is_set()
    assert rec.cancel_origin == "parent"  # its memo is suppressed

    reg.mark_done("sub_a", "x" * 25_000, SubAgentState.DONE)
    rec.artifact_path = "/tmp/a.md"
    full = await tool.execute("c", {"action": "result", "id": "sub_a"})
    assert '"truncated": true' in full.content
    assert "/tmp/a.md" in full.content


# ─── sub_agent returns at once; memo on terminal ──────────────────────


def _sub_tool(monkeypatch, *, on_terminal, run_child):
    from bridge.tools import sub_agent_tool as sat
    from bridge.tools.agent_types import ModelResolution
    from bridge.tools.base import ToolRegistry

    monkeypatch.setattr(
        sat,
        "resolve_model_choice",
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
        parent_session_id="parent-1",
        on_child_terminal=on_terminal,
    )
    return sat.SubAgentTool(spec), spec


async def test_sub_agent_returns_immediately_and_memos_on_finish(monkeypatch):
    release = asyncio.Event()
    finished: list = []

    async def fake_run_child(self, record):
        await release.wait()
        self._spec.registry.mark_done(record.id, "all done", SubAgentState.DONE)
        return "all done"

    tool, spec = _sub_tool(monkeypatch, on_terminal=finished.append, run_child=fake_run_child)

    result = await asyncio.wait_for(
        tool.execute("call-1", {"label": "scan", "task": "scan the repo", "mode": "foreground"}),
        timeout=1,
    )
    assert "launched in the background" in result.content
    assert "don't wait" in result.content
    (rec,) = spec.registry.list_all()
    assert rec.mode == "background" and rec.notify_parent is True
    assert finished == []

    release.set()
    for _ in range(50):
        if finished:
            break
        await asyncio.sleep(0.01)
    assert finished == [rec]


async def test_dispatcher_spawns_do_not_memo_the_parent(monkeypatch):
    finished: list = []

    async def fake_run_child(self, record):
        self._spec.registry.mark_done(record.id, "ok", SubAgentState.DONE)
        return "ok"

    tool, _spec = _sub_tool(monkeypatch, on_terminal=finished.append, run_child=fake_run_child)
    await tool.execute("auto-1", {"label": "w", "task": "t"}, notify_parent=False)
    await asyncio.sleep(0.05)
    assert finished == []


# ─── bridge session: drain, wake, follow-ups, memos ───────────────────


class _FakeRunner:
    def __init__(self, active=True, note=None):
        self.turn_active = active
        self.interrupts = 0
        self._note = note

    def request_interrupt(self):
        if not self.turn_active:
            return False
        self.interrupts += 1
        return True

    def consume_interrupt_note(self):
        note, self._note = self._note, None
        return note


def _session(**overrides):
    sess = fb._BridgeSession.__new__(fb._BridgeSession)
    sess.id = "root-1"
    sess.inbox = SessionInbox(session_id="root-1")
    sess.pending_task = None
    sess.talk_wake_hook = None
    sess.runtime = "native"
    sess.runner = _FakeRunner()
    sess.model_id = "fake-model"
    sess.subagent_registry = SubAgentRegistry()
    sess.queued_messages = []
    for k, v in overrides.items():
        setattr(sess, k, v)
    return sess


class _LiveTask:
    def done(self):
        return False


def _followup(text, *, force=False, client_id=None):
    return InboxMessage(
        id=new_message_id(), from_session="operator", from_label="operator",
        from_role="operator", content=text, force=force, kind=KIND_FOLLOWUP,
        client_id=client_id,
    )


async def test_drain_frames_midturn_followup_and_reports_where_it_landed(events):
    from engine.session import Session

    sess = _session(runner=_FakeRunner(note={"phase": "tools", "partial_chars": 0, "tools": ["bash"]}))
    session = Session.create(system_prompt="t")
    sess.inbox.push(_followup("use postgres instead", force=True, client_id="msg-9"))
    reg = SubAgentRegistry()
    rec = _record(reg, "sub_a")
    reg.mark_done("sub_a", "report", SubAgentState.DONE)
    sess.inbox.push(build_subagent_memo(rec, still_running=[]))

    await sess._drain_inbox_into_session(session, 3)

    msgs = session.get_messages()
    assert msgs[0].content.startswith("<system-reminder>The operator sent this follow-up")
    assert "(bash) were stopped" in msgs[0].content
    assert msgs[0].content.endswith("use postgres instead")
    assert msgs[1].content.startswith("[sub-agent memo · researcher")
    (injected,) = [e for e in events if e["type"] == "inbox_injected"]
    assert injected["midTurn"] is True
    assert [i["kind"] for i in injected["items"]] == ["followup", "memo"]
    assert injected["items"][0]["clientId"] == "msg-9"
    assert injected["items"][0]["content"] == "use postgres instead"
    assert injected["items"][1]["content"] is None
    assert not sess.inbox.has_unread()


async def test_drain_at_first_step_has_no_midturn_framing():
    from engine.session import Session

    sess = _session()
    session = Session.create(system_prompt="t")
    sess.inbox.push(_followup("and add tests"))
    await sess._drain_inbox_into_session(session, 1)
    assert session.get_messages()[0].content == "and add tests"


def test_push_followup_interrupts_only_when_forced(events):
    sess = _session(pending_task=_LiveTask())
    assert sess.accepts_followups() is True

    sess.push_operator_followup("soft", None, client_id="a")
    assert sess.runner.interrupts == 0
    sess.push_operator_followup("now!", None, force=True, client_id="b")
    assert sess.runner.interrupts == 1
    queued = [e for e in events if e["type"] == "followup_queued"]
    assert [(e["clientId"], e["force"], e["interrupting"]) for e in queued] == [
        ("a", False, False), ("b", True, True),
    ]
    assert sess._has_pending_inbox() is True
    assert sess._operator_input_pending() is True

    assert sess.inject_followup_now("a") is True
    assert sess.runner.interrupts == 2


def test_harness_runtimes_keep_the_queue():
    sess = _session(pending_task=_LiveTask(), runtime="claude_code")
    assert sess.accepts_followups() is False


def test_withdraw_all_followups_hands_text_back(events):
    sess = _session()
    sess.inbox.push(_followup("first", client_id="a"))
    sess.inbox.push(InboxMessage(
        id="t1", from_session="x", from_label="x", from_role="agent", content="talk",
    ))
    assert sess.withdraw_all_followups(restore=True) == 1
    (w,) = [e for e in events if e["type"] == "followup_withdrawn"]
    assert w["clientId"] == "a" and w["restore"] is True and w["content"] == "first"
    assert [m.content for m in sess.inbox.unread] == ["talk"]


def test_wake_prompt_matches_what_is_waiting(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        fb, "_schedule_or_queue_turn",
        lambda sess, content, attachments=None, on_turn_start=None: calls.append(
            (content, attachments)
        ) or True,
    )
    reg = SubAgentRegistry()
    rec = _record(reg, "sub_a")
    reg.mark_done("sub_a", "r", SubAgentState.DONE)

    memo_only = _session()
    memo_only.inbox.push(build_subagent_memo(rec))
    assert "woke" in memo_only.wake_for_inbox()
    assert calls[-1][0] == fb.MEMO_WAKE_PROMPT

    mixed = _session()
    mixed.inbox.push(build_subagent_memo(rec))
    mixed.inbox.push(InboxMessage(id="t", from_session="x", from_label="x", from_role="agent", content="hi"))
    mixed.wake_for_inbox()
    assert calls[-1][0] == fb.INBOX_WAKE_PROMPT


def test_wake_promotes_leftover_followups_to_a_real_turn(monkeypatch, events):
    calls: list = []
    monkeypatch.setattr(
        fb, "_schedule_or_queue_turn",
        lambda sess, content, attachments=None, on_turn_start=None: calls.append(content) or True,
    )
    sess = _session()
    sess.inbox.push(_followup("part one", client_id="a"))
    sess.inbox.push(_followup("part two", client_id="b"))
    sess.wake_for_inbox()
    assert calls == ["part one\n\npart two"]
    (promoted,) = [e for e in events if e["type"] == "followups_promoted"]
    assert [i["clientId"] for i in promoted["items"]] == ["a", "b"]
    assert not sess.inbox.has_unread()


async def test_child_terminal_memo_wakes_unless_stopped_or_killed(monkeypatch):
    monkeypatch.setattr(fb, "MEMO_WAKE_DEBOUNCE_S", 0)
    woke: list = []
    sess = _session()
    monkeypatch.setattr(sess, "wake_for_inbox", lambda: woke.append(1) or "woke")
    reg = sess.subagent_registry

    killed = _record(reg, "sub_k")
    killed.cancel_origin = "parent"
    reg.mark_done("sub_k", "Cancelled", SubAgentState.CANCELLED)
    await sess._on_child_terminal(killed)
    assert not sess.inbox.has_unread()

    stopped = _record(reg, "sub_s")
    stopped.cancel_origin = "operator"
    reg.mark_done("sub_s", "Cancelled", SubAgentState.CANCELLED)
    await sess._on_child_terminal(stopped)
    assert len(sess.inbox.unread) == 1
    await asyncio.sleep(0.01)
    assert woke == []

    done = _record(reg, "sub_d")
    reg.mark_done("sub_d", "report", SubAgentState.DONE)
    await sess._on_child_terminal(done)
    await asyncio.sleep(0.01)
    assert woke == [1]
    assert all(m.kind == KIND_MEMO for m in sess.inbox.unread)


async def test_after_turn_drain_runs_followups_then_memos(monkeypatch):
    sess = _session(pending_config=None)
    ran: list = []

    async def fake_run_turn(content, attachments=None, **_):
        ran.append(content)
        # The turn's first boundary drains whatever is left.
        sess.inbox.drain()

    sess.run_turn = fake_run_turn
    sess.inbox.push(_followup("follow up A"))
    reg = SubAgentRegistry()
    rec = _record(reg, "sub_a")
    reg.mark_done("sub_a", "r", SubAgentState.DONE)
    sess.inbox.push(build_subagent_memo(rec))

    await fb._drain_after_turn(sess)
    assert ran == ["follow up A"]  # the memo rode into that same turn

    sess.inbox.push(build_subagent_memo(rec))
    await fb._drain_after_turn(sess)
    assert ran[-1] == fb.MEMO_WAKE_PROMPT


async def test_after_turn_drain_stops_after_operator_stop_and_on_stuck_inbox():
    sess = _session(pending_config=None)
    ran: list = []

    async def stuck_run_turn(content, attachments=None, **_):
        ran.append(content)  # never reaches a boundary: nothing drained

    sess.run_turn = stuck_run_turn
    reg = SubAgentRegistry()
    rec = _record(reg, "sub_a")
    reg.mark_done("sub_a", "r", SubAgentState.DONE)
    sess.inbox.push(build_subagent_memo(rec))

    await fb._drain_after_turn(sess, cancelled=True)
    assert ran == []
    await asyncio.wait_for(fb._drain_after_turn(sess), timeout=2)
    assert ran == [fb.MEMO_WAKE_PROMPT]  # one attempt, then it gives up


class _CancellableTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def _cancel_session():
    sess = _session()
    sess.computer_cancel = asyncio.Event()
    sess.harness_adapter = None
    sess.pending_task = _CancellableTask()
    rec = _record(sess.subagent_registry, "sub_r")
    return sess, rec


def test_stopping_the_turn_leaves_background_children_running():
    sess, rec = _cancel_session()
    sess.inbox.push(_followup("pending", client_id="p"))
    fb._force_cancel_session(sess, scope="turn")
    assert sess.pending_task.cancelled is True
    assert not rec.cancel_event.is_set()
    assert not sess.inbox.has_unread()  # follow-up handed back


def test_stopping_children_leaves_the_turn_and_suppresses_wakes():
    sess, rec = _cancel_session()
    fb._force_cancel_session(sess, scope="subagents")
    assert sess.pending_task.cancelled is False
    assert rec.cancel_event.is_set()
    assert rec.cancel_origin == "operator"


def test_background_work_reminder_lists_running_children():
    sess = _session()
    assert sess._build_background_work_reminder() is None
    _record(sess.subagent_registry, "sub_a", label="scanner")
    comp = _record(sess.subagent_registry, "comp_1", label="computer: fill form")
    comp.agent_type_name = "computer"
    _record(sess.subagent_registry, "sub_x", label="judge", notify=False)
    text = sess._build_background_work_reminder()
    assert "Background sub-agents still working (2)" in text
    assert "scanner" in text and "judge" not in text
    assert "driving the screen" in text


async def test_wait_until_quiescent_covers_children_and_wake_turns():
    sess = _session()
    rec = _record(sess.subagent_registry, "sub_a")
    first = asyncio.create_task(asyncio.sleep(0.05))
    sess.pending_task = first

    async def finish_child_then_wake():
        await asyncio.sleep(0.15)
        sess.subagent_registry.mark_done("sub_a", "r", SubAgentState.DONE)
        sess.inbox.push(build_subagent_memo(rec))
        await asyncio.sleep(0.05)
        sess.inbox.drain()
        sess.pending_task = asyncio.create_task(asyncio.sleep(0.1))

    driver = asyncio.create_task(finish_child_then_wake())
    last = await asyncio.wait_for(fb.wait_until_quiescent(sess, poll_s=0.02), timeout=3)
    await driver
    assert last is sess.pending_task and last is not first
    assert last.done()


async def test_wait_until_quiescent_accepts_a_bare_turn_holder():
    task = asyncio.create_task(asyncio.sleep(0.01))
    holder = SimpleNamespace(pending_task=task)
    assert await asyncio.wait_for(fb.wait_until_quiescent(holder, poll_s=0.01), timeout=1) is task


# ─── talk(force) interrupts instead of killing ────────────────────────


async def test_talk_force_interrupts_root_and_child():
    from bridge.tools.talk_tool import TalkRouter

    router = TalkRouter.__new__(TalkRouter)
    root = _session()
    root.interrupt_for_inbox = lambda: root.runner.request_interrupt()
    root.wake_for_inbox = lambda: "recipient busy"
    msg = InboxMessage(id="m", from_session="x", from_label="x", from_role="agent", content="stop", force=True)
    await router.deliver("root-1", root, None, None, msg)
    assert root.runner.interrupts == 1

    reg = SubAgentRegistry()
    child = _record(reg, "sub_c")
    child.inbox = SessionInbox(session_id="sub_c")
    hits: list = []
    child.request_interrupt = lambda: hits.append(1)
    child.loop = asyncio.get_running_loop()
    status = await router.deliver("sub_c", None, child, None, msg)
    await asyncio.sleep(0)
    assert status == "delivered to sub-agent"
    assert hits == [1]
    assert not child.cancel_event.is_set()  # force no longer kills


# ─── gateway: same-thread follow-ups slide in ─────────────────────────


def _src(**kw):
    base = dict(chat_id="C1", thread_id="111.1", user_id="U1", message_id="111.1")
    base.update(kw)
    return SimpleNamespace(**base)


def test_gateway_slides_in_only_same_thread_same_sender():
    from bridge.gateway.run import _can_slide_into_running_turn

    sess = _session(pending_task=_LiveTask(), gateway_source=_src())
    assert _can_slide_into_running_turn(sess, _src(message_id="222.2")) is True
    # the double-fired copy of the in-flight mention
    assert _can_slide_into_running_turn(sess, _src()) is False
    assert _can_slide_into_running_turn(sess, _src(message_id="3", thread_id="999.9")) is False
    assert _can_slide_into_running_turn(sess, _src(message_id="3", user_id="U2")) is False
    # top-level DM message: anchors its own reply
    assert _can_slide_into_running_turn(sess, _src(message_id="3", thread_id=None)) is False
    idle_runner = _session(
        pending_task=_LiveTask(), gateway_source=_src(), runner=_FakeRunner(active=False),
    )
    assert _can_slide_into_running_turn(idle_runner, _src(message_id="3")) is False


# ─── computer_use: the screen lease ───────────────────────────────────


async def test_screen_lease_blocks_parent_mutations_only():
    from bridge.tools import computer_use_tool as cut
    from engine.tools import ToolDefinition, ToolResult

    class _Stub:
        def __init__(self, name):
            self._name = name

        @property
        def definition(self):
            return ToolDefinition(name=self._name, description="", summary="")

        async def execute(self, call_id, arguments):
            return ToolResult(call_id=call_id, content="ran", is_error=False)

    click = cut.ScreenLeasedTool(_Stub("click"))
    shot = cut.ScreenLeasedTool(_Stub("screenshot"))
    assert (await click.execute("c", {})).content == "ran"

    cut._SCREEN_DRIVERS["comp_1"] = "computer: book flight"
    try:
        blocked = await click.execute("c", {})
        assert blocked.is_error and "computer: book flight" in blocked.content
        assert (await shot.execute("c", {})).content == "ran"
    finally:
        cut._SCREEN_DRIVERS.pop("comp_1", None)


# ─── scheduler: timeout stops the whole job ───────────────────────────


async def test_scheduler_abort_hook_runs_on_timeout(monkeypatch):
    from bridge.scheduler import runtime as rt
    import bridge.scheduler.persistence as persistence

    monkeypatch.setattr(persistence, "read_run_cancel_requested", lambda *_a: False)
    aborted = threading.Event()
    never = asyncio.ensure_future(asyncio.Event().wait())
    outcome = await rt._await_pending_with_cancel_poll(
        never, job_id="j", run_id="r", timeout_seconds=0.2, on_abort=aborted.set,
    )
    assert outcome == "timed_out"
    assert aborted.is_set()
