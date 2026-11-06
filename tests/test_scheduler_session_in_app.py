"""A scheduled job's session in the app never writes the tick owner's files.

The process that owns the scheduler's tick (the scheduler daemon, or the
gateway) loads, runs and saves a job's ``scheduler:`` session when the job
fires. Since d5b0fa1 the app only views one when it opens it. Its other
commands (compact, repair, a model or mode change, a goal, an edit) and a
talk, or a message to a sub-agent under the job's session, still built a
copy in the app, and the copy ran turns and saved over the owner's files.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.transcript_persistence as tp
from bridge.inbox import InboxMessage
from bridge.scheduler.service import SchedulerService

SCHED = "scheduler:sched_19f0"
SCHED_SUB = "sub_19f0sched_1"


@pytest.fixture
def app_bridge(monkeypatch, tmp_path):
    """The app's bridge while another process owns the scheduler's tick.
    Records what it emits and every session it would build."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(fb, "_SUBAGENT_PARENTS", {})
    monkeypatch.setattr(gp, "_desktop_lock", object())  # this process is the app's bridge
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    built: list[str] = []

    async def ensure_session(sid, **_kw):
        built.append(sid)
        raise AssertionError(f"the app's bridge built a copy of {sid}")

    state = SimpleNamespace(
        sessions={}, active_session_id=None, ensure_session=ensure_session,
        gateway_runner=None, talk_wake_hook_factory=None, permission_tier="low",
    )
    state.get = lambda sid: state.sessions.get(sid) if sid else None
    state.scheduler = SchedulerService(state)  # not started: it does not own the tick
    return state, events, built


def _owner_saved(sid: str = SCHED, parent: str | None = None) -> None:
    """What the tick owner saved for ``sid``: one entry."""
    meta = {"parent_session_id": parent} if parent else {}
    transcript = {
        "version": 1, "session_id": sid, "metadata": meta,
        "transcript": {"entries": [{"id": "o1"}], "head_id": "o1"},
    }
    tp.save_transcript(sid, transcript)
    if parent:
        tp.save_subagent_state(sid, {
            "sessionId": sid, "parentSessionId": parent, "agentType": "general",
            "model": "claude-sonnet-5-5", "task": "t", "label": "worker", "transcript": transcript,
        })


def _saved_entries(sid: str = SCHED) -> list[str]:
    return [e["id"] for e in tp.load_transcript(sid)["transcript"]["entries"]]


def _talk(content: str) -> InboxMessage:
    return InboxMessage(
        id=f"t-{content}", from_session="session-1", from_label="lead", from_role="agent", content=content,
    )


def _inbox_file(sid: str = SCHED) -> list[str]:
    return [m["content"] for m in (tp.load_inbox_state(sid) or {}).get("unread", [])]


# ── commands ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd, subtype",
    [
        ({"type": "compact"}, "compaction_skipped"),
        ({"type": "repair_session"}, "session_repaired"),
        ({"type": "set_model", "model": "claude-opus-5-5", "requestId": "r1"}, "model_change_refused"),
        (
            {"type": "set_coordination_strategy", "coordinationStrategy": "kanban", "requestId": "r1"},
            "coordination_strategy_refused",
        ),
        ({"type": "kanban_autopilot", "enabled": True}, "control_command_skipped"),
    ],
)
async def test_a_command_for_a_scheduled_jobs_session_builds_no_copy_in_the_app(app_bridge, cmd, subtype):
    state, events, built = app_bridge

    await fb._handle_command(state, {**cmd, "sessionId": SCHED})

    assert built == [] and state.sessions == {}
    (refused,) = [e for e in events if e.get("subtype") == subtype]
    assert refused["sessionId"] == SCHED and "scheduled job" in refused["message"]
    if "requestId" in cmd:
        # The renderer takes back the change it showed.
        assert refused["details"]["requestId"] == "r1"


async def test_a_goal_for_a_scheduled_jobs_session_builds_no_copy_in_the_app(app_bridge):
    state, events, built = app_bridge

    await fb._handle_command(state, {"type": "goal_control", "action": "set", "goal": "ship it", "sessionId": SCHED})

    assert built == []
    (err,) = [e for e in events if e["type"] == "error"]
    assert "scheduled job" in err["message"]


def _stale_copy(state, tmp_path):
    """A copy the app loaded while it still owned the tick, before it
    handed the tick to a LaunchAgent. It ran no turn since."""
    from engine.session import Session

    sess = fb._BridgeSession(
        SCHED, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.session = Session.create(system_prompt="t")
    sess.session.add_user_message("the job's prompt")
    turns: list[str] = []

    async def run_turn(content, *_a, **_kw):
        turns.append(content)

    sess.run_turn = run_turn
    state.sessions[SCHED] = sess
    return sess, turns


@pytest.mark.parametrize(
    "cmd",
    [
        {"type": "edit_user_message", "content": "do it differently"},
        {"type": "rerun_user_message"},
        {"type": "delete_messages_from"},
    ],
)
async def test_an_edit_of_a_scheduled_jobs_session_in_the_app_is_refused(app_bridge, tmp_path, cmd):
    state, events, _built = app_bridge
    _owner_saved()
    _sess, turns = _stale_copy(state, tmp_path)

    await fb._handle_command(state, {**cmd, "sessionId": SCHED, "messageOrdinal": 0, "requestId": "q1"})

    assert turns == [] and _saved_entries() == ["o1"]
    (refused,) = [e for e in events if e["type"] == "rewind_refused"]
    assert refused["requestId"] == "q1"
    (err,) = [e for e in events if e["type"] == "error"]
    assert "scheduled job" in err["message"]


async def test_a_pin_in_a_scheduled_jobs_session_in_the_app_is_taken_back(app_bridge, tmp_path):
    state, events, _built = app_bridge
    _owner_saved()
    _stale_copy(state, tmp_path)

    await fb._handle_command(
        state, {"type": "toggle_entry_pin", "sessionId": SCHED, "messageOrdinal": 0, "pinned": True},
    )

    assert _saved_entries() == ["o1"]
    (undo,) = [e for e in events if e["type"] == "entry_pin_changed"]
    assert undo["pinned"] is False
    (err,) = [e for e in events if e["type"] == "error"]
    assert "scheduled job" in err["message"]


# ── a copy writes nothing ────────────────────────────────────────────


def _copy(tmp_path, *, owns_tick: bool, sid: str = SCHED):
    """A bridge session for the job's session over a real engine session,
    in a process whose scheduler owns (or does not own) the tick."""
    from engine.session import Session

    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.scheduler._is_owner = owns_tick
    sess = fb._BridgeSession(
        sid, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.session = Session.create(system_prompt="t")
    sess.session.add_user_message("from the app")
    return sess


async def test_a_copy_of_a_scheduled_jobs_session_saves_nothing_over_the_owners_files(app_bridge, tmp_path):
    from bridge.tools.goal_loop import GoalState

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    sess.inbox.push(InboxMessage(id="x", from_session="a", from_label="a", from_role="agent", content="later"))
    sess.goal_state = GoalState(goal="ship it")
    sess._save_transcript()
    sess._save_goal_state()

    assert _saved_entries() == ["o1"]
    assert tp.load_inbox_state(SCHED) is None
    assert tp.load_goal_state(SCHED) is None


async def test_the_tick_owner_still_saves_its_job_sessions(app_bridge, tmp_path):
    _owner_saved()
    _copy(tmp_path, owns_tick=True)._save_transcript()

    assert _saved_entries() != ["o1"]


async def test_a_copy_of_a_scheduled_jobs_session_runs_no_turn_and_is_not_compacted_or_repaired(
    app_bridge, monkeypatch, tmp_path
):
    from engine.compaction import SummaryCompaction

    _state, events, _built = app_bridge
    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    compacted: list[int] = []
    monkeypatch.setattr(SummaryCompaction, "compact", lambda *_a, **_k: compacted.append(1))

    assert fb._schedule_or_queue_turn(sess, "go", None) is False
    await sess.force_compact()
    await sess.repair_history()

    assert sess.pending_task is None and compacted == []
    assert _saved_entries() == ["o1"]
    assert not list((tmp_path / "sessions").glob("*repair*"))
    said = [
        e["message"] for e in events
        if e["type"] == "error" or e.get("subtype") in ("compaction_skipped", "session_repaired")
    ]
    assert len(said) == 3 and all("scheduled job" in m for m in said)


async def test_a_run_that_goes_on_after_the_app_handed_the_tick_away_still_saves_its_session(
    app_bridge, monkeypatch, tmp_path
):
    # The app owned the tick and fired the job. A LaunchAgent then took
    # the tick while the run went on: the run is still this process's,
    # and its saves are the job's history. Once it ends, the copy left
    # here is not.
    from bridge.scheduler.models import JobRecord, PersistentJobSession
    from bridge.scheduler.persistence import FileLock, job_run_lock_path

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=True)
    service = sess.state.scheduler
    job = JobRecord(id="sched_19f0", name="digest", execution=PersistentJobSession(session_id=SCHED))

    async def _run(_job, _slot, _attempt=0):
        service._is_owner = False  # handed to the LaunchAgent
        sess._save_transcript()

    monkeypatch.setattr(service, "_fire_under_lock", _run)
    lock = FileLock(job_run_lock_path(job.id))
    assert lock.acquire()
    await service._fire_with_concurrency(job, lock, None)
    saved = _saved_entries()
    assert saved != ["o1"]

    sess.session.add_user_message("after the run")
    sess._save_transcript()
    assert _saved_entries() == saved


def _app_owns_the_tick(service, monkeypatch) -> None:
    """``service``, the app's, owns the tick, and a LaunchAgent that runs a
    scheduler is installed and running: the app hands the tick to it."""
    import bridge.scheduler.service as service_mod
    from bridge.scheduler.persistence import FileLock, owner_lock_path

    service._owner_lock = FileLock(owner_lock_path())
    assert service._owner_lock.acquire()
    service._is_owner = True
    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(service_mod, "always_on_scheduler_installed", lambda: True)
    monkeypatch.setattr(service_mod, "always_on_scheduler_running", lambda: True)


async def test_the_app_hands_the_tick_over_once_a_turn_on_a_job_session_that_is_not_a_run_ends(
    app_bridge, monkeypatch, tmp_path
):
    # A talk woke the job's session in the app, which owned the tick. The
    # turn holds no run lock: handed over under it, the LaunchAgent could
    # run the job on its own copy meanwhile, and the turn's saves were
    # dropped.
    import asyncio

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=True)
    state = sess.state
    state.sessions[SCHED] = sess
    service = state.scheduler
    _app_owns_the_tick(service, monkeypatch)
    turn_ends = asyncio.Event()

    async def talk_wake():
        await turn_ends.wait()
        sess._save_transcript()

    sess.pending_task = asyncio.create_task(talk_wake())
    hand_over = asyncio.create_task(service._hand_over_to_launch_agent())
    await asyncio.sleep(0.1)
    assert service._is_owner and not hand_over.done()

    turn_ends.set()
    await asyncio.wait_for(hand_over, 1)
    assert not service._is_owner
    assert _saved_entries() != ["o1"]


async def test_a_run_of_the_job_going_on_in_the_app_does_not_hold_the_tick(app_bridge, monkeypatch, tmp_path):
    # Its run lock keeps the new owner from firing the job again, and its
    # saves stay the job's (holds_session).
    import asyncio

    sess = _copy(tmp_path, owns_tick=True)
    state = sess.state
    state.sessions[SCHED] = sess
    service = state.scheduler
    _app_owns_the_tick(service, monkeypatch)
    service._sessions_in_runs[SCHED] += 1
    sess.pending_task = asyncio.create_task(asyncio.Event().wait())

    await asyncio.wait_for(service._hand_over_to_launch_agent(), 1)

    assert not service._is_owner
    sess.pending_task.cancel()


async def test_background_work_left_under_a_job_session_after_the_handover_reports_to_its_inbox_file(
    app_bridge, monkeypatch, tmp_path
):
    # A run in the app outlived its wait on a sub-agent, a shell command
    # and a monitor, and the app then handed the tick over. Their memos
    # reached the app's copy, which saves no inbox and runs no turn: the
    # LaunchAgent that runs the job never saw them.
    import asyncio
    import time

    from bridge.tools.background_shell import BackgroundCommand
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    monkeypatch.setattr(fb, "MEMO_WAKE_DEBOUNCE_S", 0)
    _owner_saved()
    sess = _copy(tmp_path, owns_tick=True)
    state = sess.state
    state.sessions[SCHED] = sess
    sess.subagent_registry = SubAgentRegistry()
    child = sess.subagent_registry.register(SCHED_SUB, "worker", "t", "general")
    child.notify_parent = True
    log = tmp_path / "bg.log"
    log.write_text("built\n")
    shell, monitor = (
        BackgroundCommand(
            id=f"bg_{kind}", command="make", summary=kind, pid=1, output_path=str(log),
            started_at=time.time(), backgrounded_at=time.time(), reason="cut_in", kind=kind,
        )
        for kind in ("bash", "monitor")
    )
    sess.background_commands.update({shell.id: shell, monitor.id: monitor})
    _app_owns_the_tick(state.scheduler, monkeypatch)
    await asyncio.wait_for(state.scheduler._hand_over_to_launch_agent(), 1)

    sess.subagent_registry.mark_done(SCHED_SUB, "the report", SubAgentState.DONE)
    await sess._on_child_terminal(child)
    shell.state, shell.exit_code = "exited", 0
    await sess._on_background_exit(shell)
    await sess._on_monitor_event(monitor, "a line it waited for")
    monitor.state, monitor.exit_code = "exited", 0
    await sess._on_background_exit(monitor)
    await asyncio.sleep(0.01)

    memos = _inbox_file()
    assert len(memos) == 4
    assert "the report" in memos[0] and "a line it waited for" in memos[2]
    assert sess.inbox.unread == []


async def test_a_stop_in_the_app_on_a_job_session_copy_tells_the_session_through_its_inbox_file(
    app_bridge, monkeypatch, tmp_path
):
    # A run in the app left a sub-agent and a shell command under its copy
    # of the job's session, and the tick went to a LaunchAgent. The stop
    # pressed in the app put its notice in the copy's inbox, which a copy
    # never saves: the session never learned the work was stopped.
    import time

    from bridge.tools.background_shell import BackgroundCommand
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    state = sess.state
    state.sessions[SCHED] = sess
    sess.subagent_registry = SubAgentRegistry()
    children = [
        sess.subagent_registry.register(f"{SCHED_SUB[:-1]}{n}", f"worker {n}", "t", "general")
        for n in (1, 2)
    ]
    for child in children:
        child.notify_parent = True
    shell = BackgroundCommand(
        id="bg_bash", command="make", summary="build", pid=1, output_path=str(tmp_path / "bg.log"),
        started_at=time.time(), backgrounded_at=time.time(), reason="cut_in",
    )
    sess.background_commands[shell.id] = shell

    await fb._handle_command(state, {"type": "cancel", "sessionId": SCHED})
    for child in children:
        sess.subagent_registry.mark_done(child.id, "Cancelled", SubAgentState.CANCELLED)
        await sess._on_child_terminal(child)
    await sess._on_background_exit(shell)

    # One notice for the stop, as an inbox here folds them: each piece of
    # work's own notice flooded the session's file.
    (notice,) = _inbox_file()
    assert notice.startswith("[background work stopped by the operator]")
    assert all(f"{w} (" in notice for w in ("worker 1", "worker 2", "build"))
    assert sess.inbox.unread == []


async def test_a_stop_in_the_app_on_a_job_session_copy_clears_its_monitors_events_from_the_inbox_file(
    app_bridge, tmp_path
):
    # The stop dropped the stopped monitor's events waiting in the copy's
    # inbox, which a copy never fills: the copy sent each to the session's
    # inbox file, and there they stayed, for the owner's next turn.
    import time

    from bridge.tools.background_shell import BackgroundCommand

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    sess.state.sessions[SCHED] = sess
    stopped, spared = (
        BackgroundCommand(
            id=f"bg_{name}", command="tail -f log", summary=name, pid=1, output_path=str(tmp_path / "bg.log"),
            started_at=time.time(), backgrounded_at=time.time(), reason="cut_in", kind="monitor",
        )
        for name in ("deploy watch", "build watch")
    )
    await sess._on_monitor_event(stopped, "deploy 1 of 3 done")
    await sess._on_monitor_event(spared, "build green")

    stopped.cancel_origin, stopped.state = "operator", "stopped"
    await sess._on_background_exit(stopped)

    queued = _inbox_file()
    assert not any("deploy 1 of 3 done" in m for m in queued)
    assert any("build green" in m for m in queued)
    assert any(m.startswith("[background work stopped by the operator]") for m in queued)


async def test_a_stops_notice_folded_after_the_owner_took_it_in_replaces_the_one_it_took(
    app_bridge, monkeypatch, tmp_path
):
    # Folded in the file, the next stopped child's line was lost: the owner
    # never reads a message it took in again, and saved its own over it.
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    copy = _copy(tmp_path, owns_tick=False)
    copy.state.sessions[SCHED] = copy
    copy.subagent_registry = SubAgentRegistry()
    owner = _copy(tmp_path, owns_tick=True)
    owner.state.sessions[SCHED] = owner
    monkeypatch.setattr(owner, "wake_for_inbox", lambda: "woke")
    children = [copy.subagent_registry.register(f"{SCHED_SUB[:-1]}{n}", f"worker {n}", "t", "general") for n in (1, 2)]
    for child in children:
        child.notify_parent = True
        child.cancel_origin = "operator"

    for child in children:
        copy.subagent_registry.mark_done(child.id, "Cancelled", SubAgentState.CANCELLED)
        await copy._on_child_terminal(child)
        fb._take_in_queued_inbox_messages(owner.state)

    (notice,) = owner.inbox.unread
    assert "worker 1 (" in notice.content and "worker 2 (" in notice.content
    assert _inbox_file() == [notice.content]


FRESH = "scheduler:sched_19f0.ephemeral"


async def test_work_left_under_a_copy_of_a_fresh_session_jobs_session_reports_to_no_other_run(
    app_bridge, tmp_path
):
    # A run now in the app of a job that starts a new session each run
    # left a sub-agent, and the run ended. Routed to the session's inbox
    # file, its memo reached the tick owner's copy, which holds another
    # run's transcript, or the first turn of the job's next run.
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    sess = _copy(tmp_path, owns_tick=False, sid=FRESH)
    sess.state.sessions[FRESH] = sess
    sess.subagent_registry = SubAgentRegistry()
    child = sess.subagent_registry.register("sub_19f0fresh_1", "worker", "t", "general")
    child.notify_parent = True

    sess.subagent_registry.mark_done(child.id, "the report", SubAgentState.DONE)
    await sess._on_child_terminal(child)

    assert _inbox_file(FRESH) == []
    assert sess.inbox.unread == []


async def test_a_fresh_session_jobs_next_run_gets_no_memo_of_work_an_earlier_run_left(
    app_bridge, monkeypatch, tmp_path
):
    # reset() keeps the inbox, and carries the sub-agents still running:
    # a memo an earlier run's work left unread went with the next run's
    # first turn, and one such work sent later slid into that run.
    from bridge.inbox import KIND_MEMO
    from bridge.scheduler.models import CronSchedule, JobRecord, NewSession, NoopSinkSpec
    from bridge.scheduler.runtime import _resolve_execution_session
    from bridge.tools.sub_agent_registry import SubAgentState

    monkeypatch.setattr(fb, "MEMO_WAKE_DEBOUNCE_S", 3600)
    sess = _copy(tmp_path, owns_tick=True, sid=FRESH)
    state = sess.state
    state.sessions[FRESH] = sess
    await sess.initialize()
    earlier = sess.subagent_registry.register("sub_19f0fresh_1", "worker", "t", "general")
    earlier.notify_parent = True
    sess.inbox.push(InboxMessage(
        id="m1", from_session="sub_19f0fresh_0", from_label="scout", from_role="agent",
        content="the scout's report", kind=KIND_MEMO,
    ))

    async def ensure_session(_sid, **_kw):
        return sess

    state.ensure_session = ensure_session
    job = JobRecord(id="sched_19f0", name="digest", schedule=CronSchedule(expression="0 6 * * *"),
                    prompt="digest", execution=NewSession(), sinks=[NoopSinkSpec()])
    await _resolve_execution_session(SimpleNamespace(state=state), job)
    assert sess.inbox.unread == []

    sess.subagent_registry.mark_done(earlier.id, "the worker's report", SubAgentState.DONE)
    await sess._on_child_terminal(earlier)
    assert sess.inbox.unread == []
    # Work the run itself starts still reports to it.
    own = sess.subagent_registry.register("sub_19f0fresh_2", "checker", "t", "general")
    own.notify_parent = True
    sess.subagent_registry.mark_done(own.id, "the checker's report", SubAgentState.DONE)
    await sess._on_child_terminal(own)
    assert [m.from_session for m in sess.inbox.unread] == [own.id]
    assert _inbox_file(FRESH) == [sess.inbox.unread[0].content]


async def test_work_a_fresh_session_jobs_run_left_wakes_no_turn_after_the_run(
    app_bridge, monkeypatch, tmp_path
):
    # The run delivered with a sub-agent still working. Before the next
    # run began, the sub-agent's memo woke a turn on the ended run's
    # transcript: no run counted its cost, and its reply went nowhere.
    import asyncio

    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.models import CronSchedule, JobRecord, NewSession, NoopSinkSpec
    from bridge.scheduler.persistence import save_job
    from bridge.scheduler.runtime import fire_job
    from bridge.tools.sub_agent_registry import SubAgentState
    from engine.types import Message

    monkeypatch.setattr(fb, "MEMO_WAKE_DEBOUNCE_S", 0)
    monkeypatch.setattr(runtime, "BACKGROUND_WORK_WAIT_CAP_S", 0.1)
    sess = _copy(tmp_path, owns_tick=True, sid=FRESH)
    state = sess.state
    state.sessions[FRESH] = sess
    await sess.initialize()
    ran: list[str] = []
    left: list = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        sess.session.transcript.append_message(Message(role="assistant", content="the digest"))
        if not left:
            left.append(sess.subagent_registry.register("sub_19f0fresh_1", "worker", "t", "general"))
            left[0].notify_parent = True

    async def ensure_session(_sid, **_kw):
        return sess

    sess.run_turn = run_turn
    state.ensure_session = ensure_session
    job = JobRecord(id="sched_19f0", name="digest", schedule=CronSchedule(expression="0 6 * * *"),
                    prompt="digest", execution=NewSession(), sinks=[NoopSinkSpec()])
    save_job(job)
    run = await fire_job(SimpleNamespace(state=state, emit_event=lambda **_kw: None), job)
    assert run.status == "succeeded" and len(ran) == 1

    sess.subagent_registry.mark_done(left[0].id, "the worker's report", SubAgentState.DONE)
    await sess._on_child_terminal(left[0])
    await asyncio.sleep(0.05)

    assert ran[1:] == []  # no turn after the run
    assert sess.inbox.unread == [] and _inbox_file(FRESH) == []


async def test_a_fresh_session_jobs_next_run_gets_no_talk_an_earlier_runs_sub_agent_left_unread(
    app_bridge, tmp_path
):
    # begin_fresh_run dropped the memos left unread, and not a sub-agent's
    # talk to the session: it went with the next run's first turn.
    from bridge.inbox import KIND_FOLLOWUP
    from bridge.scheduler.models import CronSchedule, JobRecord, NewSession, NoopSinkSpec
    from bridge.scheduler.runtime import _resolve_execution_session

    sess = _copy(tmp_path, owns_tick=True, sid=FRESH)
    state = sess.state
    state.sessions[FRESH] = sess
    await sess.initialize()
    sess.inbox.push(InboxMessage(
        id="t1", from_session="sub_19f0fresh_0", from_label="scout", from_role="agent",
        content="one more finding",
    ))
    sess.inbox.push(InboxMessage(
        id="o1", from_session="operator", from_label="operator", from_role="operator",
        content="and the weather", kind=KIND_FOLLOWUP,
    ))

    async def ensure_session(_sid, **_kw):
        return sess

    state.ensure_session = ensure_session
    job = JobRecord(id="sched_19f0", name="digest", schedule=CronSchedule(expression="0 6 * * *"),
                    prompt="digest", execution=NewSession(), sinks=[NoopSinkSpec()])
    await _resolve_execution_session(SimpleNamespace(state=state), job)

    assert [m.id for m in sess.inbox.unread] == ["o1"]


def _talk_from(sess, sub_id: str):
    """The talk tool of sub-agent ``sub_id``, whose parent is ``sess``."""
    from bridge.tools.talk_tool import TalkRouter, TalkRouterContext, TalkTool

    state = sess.state
    router = TalkRouter(
        bridge_state=state,
        get_running_sessions=lambda: dict(state.sessions),
        resolve_archived_sub=lambda _sid: None,
        wake_archived_sub=lambda _sid, _msg: None,
    )
    ctx = TalkRouterContext(
        caller_session_id=sub_id, caller_label="worker", caller_role="agent", parent_session_id=sess.id,
    )
    return TalkTool(router, ctx)


async def test_a_sub_agent_an_earlier_run_left_cannot_talk_into_a_fresh_session_jobs_next_run(
    app_bridge, tmp_path
):
    from bridge.scheduler.models import CronSchedule, JobRecord, NewSession, NoopSinkSpec
    from bridge.scheduler.runtime import _resolve_execution_session

    sess = _copy(tmp_path, owns_tick=True, sid=FRESH)
    state = sess.state
    state.sessions[FRESH] = sess
    await sess.initialize()
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)

    async def ensure_session(_sid, **_kw):
        return sess

    sess.run_turn = run_turn
    state.ensure_session = ensure_session
    earlier = sess.subagent_registry.register("sub_19f0fresh_1", "worker", "t", "general")
    earlier.notify_parent = True
    job = JobRecord(id="sched_19f0", name="digest", schedule=CronSchedule(expression="0 6 * * *"),
                    prompt="digest", execution=NewSession(), sinks=[NoopSinkSpec()])
    await _resolve_execution_session(SimpleNamespace(state=state), job)

    out = await _talk_from(sess, earlier.id).execute("c1", {"to": "parent", "content": "one more finding"})

    assert sess.inbox.unread == [] and ran == [] and _inbox_file(FRESH) == []
    assert "not delivered" in out.content and "starts a new session" in out.content


async def test_a_sub_agent_under_a_copy_of_a_fresh_session_jobs_session_cannot_talk_into_its_runs(
    app_bridge, tmp_path
):
    # A run now in the app left the sub-agent, and the run ended. Its talk
    # went to the session's inbox file, which the tick owner reads.
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    sess = _copy(tmp_path, owns_tick=False, sid=FRESH)
    sess.state.sessions[FRESH] = sess
    sess.subagent_registry = SubAgentRegistry()
    child = sess.subagent_registry.register("sub_19f0fresh_1", "worker", "t", "general")

    out = await _talk_from(sess, child.id).execute("c1", {"to": "parent", "content": "one more finding"})

    assert _inbox_file(FRESH) == [] and sess.inbox.unread == []
    assert "not delivered" in out.content


async def _take_the_tick(service, monkeypatch) -> None:
    """``service`` starts while a peer owns the tick, and takes it when the
    peer exits (the scheduler daemon restarts, and the gateway takes over)."""
    import asyncio

    import bridge.scheduler.service as service_mod
    from bridge.scheduler.persistence import FileLock, owner_lock_path

    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.02)
    peer = FileLock(owner_lock_path())
    assert peer.acquire()
    await service.start()
    assert not service._is_owner
    peer.release()
    for _ in range(200):
        if service._is_owner:
            return
        await asyncio.sleep(0.01)


async def test_a_process_that_takes_the_tick_drops_the_copies_of_job_sessions_it_had_loaded(
    app_bridge, monkeypatch, tmp_path
):
    # The copy here missed the runs the old owner made since. Kept as the
    # new owner's, the job's next run went on it, and so did a talk the
    # inbox watch took in, and each saved it over those runs.
    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    state = sess.state
    state.sessions[SCHED] = sess

    await _take_the_tick(state.scheduler, monkeypatch)

    assert state.scheduler._is_owner
    assert SCHED not in state.sessions
    await state.scheduler.stop()


async def test_a_process_that_takes_the_tick_stops_the_sub_agents_under_a_copy_it_drops(
    app_bridge, monkeypatch, tmp_path
):
    # A run here past its wait on background work left one running. It
    # went on under the dropped copy, and its memo saved the copy's stale
    # inbox over the new owner's file.
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    _owner_saved()
    tp.save_inbox_state(SCHED, {"session_id": SCHED, "unread": [], "delivered": []})
    sess = _copy(tmp_path, owns_tick=False)
    sess.subagent_registry = SubAgentRegistry()
    child = sess.subagent_registry.register(SCHED_SUB, "worker", "t", "general")
    child.notify_parent = True
    state = sess.state
    state.sessions[SCHED] = sess
    before = tp._inbox_path(SCHED).read_bytes()

    await _take_the_tick(state.scheduler, monkeypatch)
    child.state = SubAgentState.CANCELLED  # it saw its cancel event
    await sess._on_child_terminal(child)

    assert SCHED not in state.sessions
    assert child.cancel_event.is_set()
    assert tp._inbox_path(SCHED).read_bytes() == before
    await state.scheduler.stop()


async def test_a_process_that_takes_the_tick_stops_the_shell_commands_and_monitors_under_a_copy_it_drops(
    app_bridge, monkeypatch, tmp_path
):
    # Left running under the dropped copy, which nothing here reaches any
    # more, a command ran on with no way to stop it from the app, and a
    # monitor watched for up to half an hour with each event dropped. Each
    # exit was silent: the copy is closed.
    import time

    from bridge.tools import background_shell
    from bridge.tools.background_shell import BackgroundCommand

    _owner_saved()
    sess = _copy(tmp_path, owns_tick=False)
    work = [
        BackgroundCommand(
            id=f"bg_{kind}", command="make", summary=kind, pid=1, output_path=str(tmp_path / "bg.log"),
            started_at=time.time(), backgrounded_at=time.time(), reason="cut_in", kind=kind,
            _process=SimpleNamespace(proc=SimpleNamespace(pid=1)),
        )
        for kind in ("bash", "monitor")
    ]
    sess.background_commands.update({bg.id: bg for bg in work})
    stopped: list = []
    monkeypatch.setattr(background_shell, "stop_process_group", stopped.append)
    state = sess.state
    state.sessions[SCHED] = sess

    await _take_the_tick(state.scheduler, monkeypatch)

    assert SCHED not in state.sessions
    assert [bg.state for bg in work] == ["stopped", "stopped"]
    assert stopped == [bg._process.proc for bg in work]
    await state.scheduler.stop()


async def test_a_process_that_takes_the_tick_keeps_a_job_session_its_own_run_holds(
    app_bridge, monkeypatch, tmp_path
):
    sess = _copy(tmp_path, owns_tick=False)
    state = sess.state
    state.sessions[SCHED] = sess
    state.scheduler._sessions_in_runs[SCHED] += 1  # a run now here goes on

    await _take_the_tick(state.scheduler, monkeypatch)

    assert state.scheduler._is_owner
    assert state.sessions[SCHED] is sess
    await state.scheduler.stop()


# ── talks and sub-agents ─────────────────────────────────────────────


async def test_a_talk_to_a_scheduled_jobs_session_waits_in_its_inbox_file(app_bridge):
    # It counted as a closed desktop session: the app loaded it, pushed
    # the message, and woke it to run a turn on the copy.
    state, _events, built = app_bridge
    _owner_saved()

    status = await fb.deliver_talk_message_locally(state, SCHED, _talk("status?"))

    assert built == [] and _inbox_file() == ["status?"]
    assert status.startswith("queued to inbox sidecar")


async def test_an_agents_talk_to_a_scheduled_jobs_session_loaded_in_the_app_waits_in_its_inbox_file(
    app_bridge, tmp_path
):
    # A copy loaded here took the message into its inbox, which it never
    # saves, and woke to run a turn on it.
    from bridge.tools.talk_tool import TalkRouter, TalkRouterContext, TalkTool

    state, _events, built = app_bridge
    _owner_saved()
    sess, _turns = _stale_copy(state, tmp_path)
    router = TalkRouter(
        bridge_state=state, get_running_sessions=lambda: state.sessions,
        resolve_archived_sub=fb._resolve_archived_subagent,
        wake_archived_sub=lambda sid, _msg: built.append(sid),
    )
    ctx = TalkRouterContext(
        caller_session_id="session-1", caller_label="lead", caller_role="agent", parent_session_id=None,
    )

    await TalkTool(router, ctx).execute("c1", {"to": SCHED, "content": "status?"})

    assert built == [] and _inbox_file() == ["status?"]
    assert sess.inbox.unread == []


async def test_operator_talk_to_a_scheduled_jobs_session_waits_in_its_inbox_file(app_bridge, tmp_path):
    state, _events, built = app_bridge
    _owner_saved()
    sess, _turns = _stale_copy(state, tmp_path)

    await fb._handle_command(state, {"type": "operator_talk", "sessionId": SCHED, "content": "status?"})

    assert built == [] and _inbox_file() == ["status?"]
    assert sess.inbox.unread == []


async def test_an_artifact_note_on_a_scheduled_jobs_session_waits_in_its_inbox_file(app_bridge):
    state, _events, built = app_bridge
    _owner_saved()

    await fb.deliver_artifact_note(state, SCHED, _talk("fix the chart"))

    assert built == [] and _inbox_file() == ["fix the chart"]


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


async def test_a_talk_to_a_sub_agent_under_a_scheduled_jobs_session_loads_no_copy_of_that_session(app_bridge):
    # It was re-woken under its parent, which the app loaded as the host
    # (_host_for_subagent_wake): a copy of the job's session that ran the
    # sub-agent's memo as a turn and saved over the owner's.
    state, _events, built = app_bridge
    _owner_saved()
    _owner_saved(SCHED_SUB, parent=SCHED)
    resumed = _app_root_that_could_host(state)

    await fb.deliver_talk_message_locally(state, SCHED_SUB, _talk("status?"))

    assert built == [] and resumed == []
    assert _inbox_file(SCHED_SUB) == ["status?"]


async def test_a_message_typed_for_a_sub_agent_under_a_scheduled_jobs_session_is_refused(app_bridge):
    state, events, built = app_bridge
    _owner_saved()
    _owner_saved(SCHED_SUB, parent=SCHED)
    resumed = _app_root_that_could_host(state)

    await fb._handle_command(
        state, {"type": "send_message", "sessionId": SCHED_SUB, "content": "also check X", "clientId": "m3"},
    )

    assert built == [] and resumed == []
    (refused,) = [e for e in events if e["type"] == "send_refused"]
    assert (refused["sessionId"], refused["clientId"]) == (SCHED_SUB, "m3")
    (err,) = [e for e in events if e["type"] == "error"]
    assert "scheduled job" in err["message"]


async def test_a_sub_agent_running_in_the_app_under_a_job_session_still_gets_its_talks(app_bridge):
    # A run that went on here after the tick went elsewhere spawned it.
    from bridge.inbox import SessionInbox
    from bridge.tools.sub_agent_registry import SubAgentState

    state, _events, _built = app_bridge
    rec = SimpleNamespace(id=SCHED_SUB, state=SubAgentState.RUNNING, inbox=SessionInbox(session_id=SCHED_SUB))
    registry = SimpleNamespace(get=lambda sid: rec if sid == SCHED_SUB else None)
    state.sessions["session-1"] = SimpleNamespace(subagent_registry=registry)
    _owner_saved(SCHED_SUB, parent=SCHED)

    assert not fb._scheduler_runs_elsewhere(state, SCHED_SUB)
    status = await fb.deliver_talk_message_locally(state, SCHED_SUB, _talk("status?"))
    assert status == "delivered to sub-agent"



# ── delete ───────────────────────────────────────────────────────────


async def test_deleting_a_scheduled_jobs_session_in_the_app_keeps_the_owners_files(app_bridge, tmp_path):
    # The tick owner has the session loaded, and may be running the job on
    # it. The app only stops showing it.
    state, events, built = app_bridge
    _owner_saved()
    _owner_saved(SCHED_SUB, parent=SCHED)
    tp.save_inbox_state(SCHED, {"sessionId": SCHED, "unread": [], "delivered": []})
    _stale_copy(state, tmp_path)

    await fb._handle_command(state, {"type": "delete_session", "sessionId": SCHED, "cascadeSessionIds": [SCHED_SUB]})

    assert built == [] and SCHED not in state.sessions
    assert tp.load_transcript(SCHED) is not None and tp.load_transcript(SCHED_SUB) is not None
    assert _saved_entries() == ["o1"] and _saved_entries(SCHED_SUB) == ["o1"]
    assert tp.load_inbox_state(SCHED) is not None and tp.load_subagent_state(SCHED_SUB) is not None
    # The app's toast said "Session deleted".
    (answer,) = [e for e in events if e["type"] == "session_deleted"]
    assert answer == {"type": "session_deleted", "deletedSessionId": SCHED, "filesKept": True}


async def test_the_tick_owner_still_deletes_its_job_sessions(app_bridge):
    state, events, _built = app_bridge
    state.scheduler._is_owner = True
    _owner_saved()

    await fb._handle_command(state, {"type": "delete_session", "sessionId": SCHED})

    assert tp.load_transcript(SCHED) is None
    (answer,) = [e for e in events if e["type"] == "session_deleted"]
    assert answer["filesKept"] is False


# ── run now ──────────────────────────────────────────────────────────


def _job(job_id: str, execution):
    from bridge.scheduler.models import IntervalSchedule, JobRecord, NoopSinkSpec
    from bridge.scheduler.persistence import save_job

    job = JobRecord(
        id=job_id, name="digest", prompt="write the digest",
        schedule=IntervalSchedule(seconds=3600), execution=execution, sinks=[NoopSinkSpec()],
    )
    save_job(job)
    return job


async def _run_now(state, events, job_id: str) -> dict:
    import asyncio

    await fb._handle_command(state, {"type": "scheduler.run_job_now", "id": "q1", "jobId": job_id})
    await asyncio.gather(*state.scheduler._in_flight_runs.values())
    (resp,) = [e for e in events if e.get("type") == "scheduler_response"]
    return resp


async def test_run_now_in_the_app_of_a_job_that_keeps_its_session_is_refused(app_bridge):
    # The tick owner has the job's session loaded from its earlier runs. A
    # run here loaded it again, ran, and saved; the owner's next save of
    # its own copy dropped that run.
    from bridge.scheduler.models import PersistentJobSession
    from bridge.scheduler.persistence import load_runs_for_job

    state, events, built = app_bridge
    job = _job("sched_19f0", PersistentJobSession())

    resp = await _run_now(state, events, job.id)

    assert built == [] and load_runs_for_job(job.id) == []
    assert "run" not in resp and "background scheduler" in resp["error"]


async def test_run_now_in_the_app_of_a_job_that_starts_a_new_session_each_run_runs_there(app_bridge, monkeypatch):
    # The Morning room's rebrief: each run starts from an empty session,
    # so a run here writes nothing the owner reads back, and its session
    # is the app's while it goes on.
    import time

    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.models import NewSession, RunRecord

    state, events, _built = app_bridge
    held: list[str | None] = []

    async def fire(_service, job, *, run_id=None, **_kw):
        held.append(fb._held_elsewhere(state, "scheduler:sched_19f1.ephemeral"))
        return RunRecord(run_id=run_id, job_id=job.id, job_name=job.name, started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fire)
    job = _job("sched_19f1", NewSession())

    resp = await _run_now(state, events, job.id)

    assert "error" not in resp and held == [None]
    assert fb._held_elsewhere(state, "scheduler:sched_19f1.ephemeral")


# ── the events file ──────────────────────────────────────────────────


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    """The app's bridge as app_bridge, emitting for real: to stdout and to
    each session's events file, which the owner also writes."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "_desktop_lock", object())
    monkeypatch.setattr(fb, "_DEBUG_LOG_ENABLED", False)
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_SESSION_EVENT_FILES", fb._OrderedDict())
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    fb._set_event_log_state(state)  # as _main does
    yield state
    for sid in list(fb._SESSION_EVENT_FILES):
        fb._close_session_event_file(sid)


def _events_in_file(sid: str) -> list[str]:
    path = fb._session_event_path(sid)
    return [json.loads(line)["type"] for line in path.read_text().splitlines()] if path.exists() else []


async def test_the_app_writes_nothing_to_a_scheduled_jobs_sessions_events_file(events_file, capsys):
    state = events_file
    _owner_saved(SCHED_SUB, parent=SCHED)

    await fb._handle_command(state, {"type": "compact", "sessionId": SCHED})
    fb.emit({"type": "system_event", "sessionId": SCHED_SUB, "subtype": "x", "message": "m"})

    assert _events_in_file(SCHED) == [] and _events_in_file(SCHED_SUB) == []
    # The app's own view still gets them.
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["sessionId"] for e in out if e.get("type") == "system_event"] == [SCHED, SCHED_SUB]


async def test_the_tick_owner_writes_its_job_sessions_events_file(events_file):
    state = events_file
    state.scheduler._is_owner = True

    fb.emit({"type": "system_event", "sessionId": SCHED, "subtype": "x", "message": "m"})

    assert _events_in_file(SCHED) == ["system_event"]


async def test_the_app_stops_writing_a_job_sessions_events_file_once_it_hands_the_tick_over(
    events_file, monkeypatch
):
    # emit() took the handle it had open for ownership, so it went on
    # appending to the file the LaunchAgent that took the tick writes.
    import bridge.scheduler.service as service_mod
    from bridge.scheduler.persistence import FileLock, owner_lock_path

    service = events_file.scheduler
    service._owner_lock = FileLock(owner_lock_path())
    assert service._owner_lock.acquire()
    service._is_owner = True
    fb.emit({"type": "system_event", "sessionId": SCHED, "subtype": "x", "message": "m"})
    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(service_mod, "always_on_scheduler_installed", lambda: True)
    monkeypatch.setattr(service_mod, "always_on_scheduler_running", lambda: True)

    await service._hand_over_to_launch_agent()
    fb.emit({"type": "compaction_skipped", "sessionId": SCHED})

    assert not service._is_owner
    assert _events_in_file(SCHED) == ["system_event"]


async def test_a_run_that_outlives_the_tick_stops_writing_its_sessions_events_file_when_it_ends(
    events_file, monkeypatch
):
    from bridge.scheduler.models import JobRecord, PersistentJobSession
    from bridge.scheduler.persistence import FileLock, job_run_lock_path

    service = events_file.scheduler
    service._is_owner = True
    job = JobRecord(id="sched_19f0", name="digest", execution=PersistentJobSession(session_id=SCHED))

    async def _run(_job, _slot, _attempt=0):
        service._is_owner = False  # handed to the LaunchAgent
        fb.emit({"type": "turn_complete", "sessionId": SCHED})

    monkeypatch.setattr(service, "_fire_under_lock", _run)
    lock = FileLock(job_run_lock_path(job.id))
    assert lock.acquire()
    await service._fire_with_concurrency(job, lock, None)
    fb.emit({"type": "compaction_skipped", "sessionId": SCHED})

    assert _events_in_file(SCHED) == ["turn_complete"]


@pytest.mark.parametrize("start_kwargs", [{}, {"listen": False}], ids=["gateway", "scheduler daemon"])
async def test_the_gateway_and_the_scheduler_daemon_tell_emit_their_state(monkeypatch, start_kwargs):
    from tests.test_scheduler_slack_sink import _start_gateway

    daemon = await _start_gateway(monkeypatch, **start_kwargs)

    assert fb._EVENT_LOG_STATE is daemon.state
