"""A scheduled job's session in the app never writes the tick owner's files.

The process that owns the scheduler's tick (the scheduler daemon, or the
gateway) loads, runs and saves a job's ``scheduler:`` session when the job
fires. Since d5b0fa1 the app only views one when it opens it. Its other
commands (compact, repair, a model or mode change, a goal, an edit) and a
talk, or a message to a sub-agent under the job's session, still built a
copy in the app, and the copy ran turns and saved over the owner's files.
"""

from __future__ import annotations

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


def _copy(tmp_path, *, owns_tick: bool):
    """A bridge session for the job's session over a real engine session,
    in a process whose scheduler owns (or does not own) the tick."""
    from engine.session import Session

    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.scheduler._is_owner = owns_tick
    sess = fb._BridgeSession(
        SCHED, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
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
