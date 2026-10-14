"""Self-paced scheduled loops, failing jobs, and sessions reloaded from disk.

A self-paced loop set up from a Slack thread never ran a single turn:

* its loop tools were named ``scheduler.continue_loop``; every provider
  rejects a dot in a tool name, so each fire failed before the model ran;
* the tools took the job lock that the fire already holds, so they could
  never have worked, and the fire's save at the end would have undone them;
* removing the tools after a fire left them in the registry's catalog, so
  the operator's own later messages carried the bad name and failed too;
* each run was billed the session's lifetime spend, so a fire into a session
  that had spent more than the per-run cap was "over budget" at once;
* the job fired 180 times in six hours and nobody was told;
* after a gateway restart the thread came back on the process default model
  (Opus) instead of the GPT model it was started on.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import ExistingSession, JobRecord, RunRecord, SelfPacedSchedule
from bridge.scheduler.persistence import load_job, save_job, save_run
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message

# The strictest rule across the providers Freyja sends tools to.
_PROVIDER_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class _Service:
    def __init__(self, sessions: dict | None = None) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions or {})
        self.events: list[str] = []

    def emit_event(self, *, subtype: str, message: str, details=None, session_id=None) -> None:
        self.events.append(subtype)


def _job(job_id: str = "sched_loop") -> JobRecord:
    job = JobRecord(
        id=job_id,
        name="babysit the merge queue",
        prompt="check the PRs",
        schedule=SelfPacedSchedule(min_delay_seconds=120, max_delay_seconds=600),
        execution=ExistingSession(session_id="s1"),
        sinks=[],
    )
    save_job(job)
    return job


def _session(cost_so_far: float) -> SimpleNamespace:
    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        cumulative_cost=cost_so_far,
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        saves=0,
    )
    sess._save_transcript = lambda: setattr(sess, "saves", sess.saves + 1)
    return sess


def _drive_fire(
    monkeypatch,
    sess,
    *,
    loop_call: tuple[str, dict] | None,
    cost: float,
    replies: bool = True,
    operator_says: str | None = None,
):
    """Stand in for the agent turn: it sees the offered tools, makes the
    given loop-tool call, spends ``cost`` and replies. With ``replies``
    off the provider rejects the request, so nothing answers the prompt;
    ``operator_says`` lands in the session while the fire runs."""
    import bridge.freyja_bridge as fb

    offered: list[str] = []

    async def turn(prompt: str) -> None:
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=prompt))
        if operator_says is not None:
            transcript.append_message(Message(role="user", content=operator_says))
        if not replies:
            sess.last_turn_error = "auth: Error code: 401 - Incorrect API key provided"
            return
        offered.extend(d.name for d in sess.tool_registry.list_definitions())
        if loop_call is not None:
            name, args = loop_call
            await sess.tool_registry.get(name).execute("call-1", args)
        sess.cumulative_cost += cost
        transcript.append_message(Message(role="assistant", content="Checked the queue."))

    def schedule_turn(s, prompt, attachments=None):
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    return offered


# ─── the loop tools ───────────────────────────────────────────────────


def test_loop_tool_names_are_valid_on_every_provider() -> None:
    from bridge.tools.schedule_tool import (
        LoopDecision,
        build_complete_loop_tool,
        build_continue_loop_tool,
    )

    schedule = SelfPacedSchedule(min_delay_seconds=120, max_delay_seconds=600)
    for build in (build_continue_loop_tool, build_complete_loop_tool):
        name = build(schedule=schedule, decision=LoopDecision()).definition.name
        assert _PROVIDER_TOOL_NAME.match(name), name


async def test_continue_is_applied_and_the_tools_leave_with_the_fire(monkeypatch) -> None:
    from bridge.scheduler.runtime import fire_job
    from bridge.tools.schedule_tool import COMPLETE_LOOP_TOOL, CONTINUE_LOOP_TOOL

    sess = _session(cost_so_far=6.45)
    service = _Service({"s1": sess})
    job = _job()
    offered = _drive_fire(
        monkeypatch, sess, loop_call=(CONTINUE_LOOP_TOOL, {"delay_seconds": 300}), cost=0.25
    )

    before = time.time()
    run = await fire_job(service, job)

    assert {CONTINUE_LOOP_TOOL, COMPLETE_LOOP_TOOL} <= set(offered)
    # Gone from the tool table AND from the list each request sends.
    assert CONTINUE_LOOP_TOOL not in sess.tool_registry
    assert not any(
        d.name in (CONTINUE_LOOP_TOOL, COMPLETE_LOOP_TOOL)
        for d in sess.tool_registry.list_definitions()
    )
    saved = load_job(job.id)
    assert saved.enabled and before + 300 <= saved.next_fire_at <= time.time() + 300
    # The run costs what it added, not the session's $6.45 so far.
    assert run.cost_usd == pytest.approx(0.25)
    assert run.status == "succeeded"


async def test_a_session_that_never_ran_a_turn_still_gets_the_loop_tools(monkeypatch) -> None:
    # A new job session builds its tool registry on its first turn, after
    # the fire registers the loop tools; they used to be left out silently.
    from bridge.scheduler.runtime import fire_job
    from bridge.tools.schedule_tool import COMPLETE_LOOP_TOOL, CONTINUE_LOOP_TOOL

    sess = _session(cost_so_far=0.0)
    sess.tool_registry = None

    async def initialize() -> None:
        if sess.tool_registry is None:
            sess.tool_registry = ToolRegistry()

    sess.initialize = initialize
    job = _job()
    offered = _drive_fire(
        monkeypatch, sess, loop_call=(COMPLETE_LOOP_TOOL, {"reason": "done"}), cost=0.1
    )

    await fire_job(_Service({"s1": sess}), job)

    assert {CONTINUE_LOOP_TOOL, COMPLETE_LOOP_TOOL} <= set(offered)
    assert not load_job(job.id).enabled


async def test_complete_disables_the_job_for_good(monkeypatch) -> None:
    from bridge.scheduler.runtime import fire_job
    from bridge.tools.schedule_tool import COMPLETE_LOOP_TOOL

    sess = _session(cost_so_far=0.0)
    job = _job()
    _drive_fire(
        monkeypatch, sess, loop_call=(COMPLETE_LOOP_TOOL, {"reason": "all merged"}), cost=0.1
    )

    await fire_job(_Service({"s1": sess}), job)

    saved = load_job(job.id)
    assert not saved.enabled and saved.next_fire_at is None


async def test_no_decision_waits_the_maximum_delay(monkeypatch) -> None:
    from bridge.scheduler.runtime import fire_job

    sess = _session(cost_so_far=0.0)
    job = _job()
    # The pre-advance before a fire sets the minimum delay.
    job.next_fire_at = time.time() + 120
    _drive_fire(monkeypatch, sess, loop_call=None, cost=0.1)

    await fire_job(_Service({"s1": sess}), job)

    assert load_job(job.id).next_fire_at >= time.time() + 590


async def test_a_fire_nothing_answered_leaves_no_prompt_behind(monkeypatch) -> None:
    from bridge.scheduler.runtime import fire_job

    sess = _session(cost_so_far=0.0)
    transcript = sess.session.transcript
    transcript.append_message(Message(role="user", content="merge the stack"))
    transcript.append_message(Message(role="assistant", content="On it."))
    job = _job()
    _drive_fire(monkeypatch, sess, loop_call=None, cost=0.0, replies=False,
                operator_says="any news?")

    run = await fire_job(_Service({"s1": sess}), job)

    texts = [e.message.content for e in transcript.entries]
    assert texts == ["merge the stack", "On it.", "any news?"]
    assert [e.parent_id for e in transcript.entries[1:]] == [e.id for e in transcript.entries[:-1]]
    assert transcript.head_id == transcript.entries[-1].id
    assert sess.saves == 1
    # The run record keeps the prompt, and the turn's real error.
    assert run.status == "failed" and run.prompt
    assert "Incorrect API key" in run.error


async def test_an_answered_fire_keeps_its_prompt(monkeypatch) -> None:
    from bridge.scheduler.runtime import fire_job

    sess = _session(cost_so_far=0.0)
    _drive_fire(monkeypatch, sess, loop_call=None, cost=0.1)

    await fire_job(_Service({"s1": sess}), _job())

    roles = [e.message.role for e in sess.session.transcript.entries]
    assert roles == ["user", "assistant"] and sess.saves == 0


# ─── a bad tool name can't reach a request ────────────────────────────


class _NamedTool:
    requires_permission = False

    def __init__(self, name: str) -> None:
        from engine.tools import ToolDefinition

        self.definition = ToolDefinition(
            name=name, summary="s", description="d", parameters={"type": "object"}
        )

    async def execute(self, call_id, arguments):  # pragma: no cover - never run
        raise AssertionError


@pytest.mark.parametrize("name", ["scheduler.continue_loop", "has space", "x" * 65, ""])
def test_the_registry_refuses_a_name_providers_reject(name) -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="must match"):
        registry.register(_NamedTool(name))
    assert registry.list_definitions() == []


def test_the_registry_takes_valid_names() -> None:
    registry = ToolRegistry()
    registry.register(_NamedTool("scheduler_continue_loop"))
    registry.register(_NamedTool("mcp__slack__read-thread"))
    assert len(registry.list_definitions()) == 2


def test_removing_entries_relinks_the_history() -> None:
    transcript = TranscriptManager()
    ids = [transcript.append_message(Message(role="user", content=str(i))) for i in range(4)]

    assert transcript.remove_entries({ids[1], ids[2]}) == 2
    assert [e.id for e in transcript.entries] == [ids[0], ids[3]]
    assert transcript.entries[1].parent_id == ids[0]

    transcript.remove_entries({ids[3]})
    assert transcript.head_id == ids[0]
    transcript.append_message(Message(role="user", content="next"))
    assert transcript.entries[-1].parent_id == ids[0]


# ─── a failing job stops and says so ──────────────────────────────────


def _failed_run(job: JobRecord, n: int) -> RunRecord:
    run = RunRecord(
        run_id=f"run_{n}",
        job_id=job.id,
        job_name=job.name,
        started_at=time.time(),
        status="failed",
        error="Error code: 400 - tools.27.custom.name: String should match pattern",
    )
    save_run(run)
    time.sleep(0.01)  # runs are ordered by file mtime
    return run


async def test_a_job_stops_after_three_failed_runs_and_posts_why(monkeypatch) -> None:
    import bridge.scheduler.sinks as sinks
    from bridge.scheduler.service import SchedulerService

    posted: list[str] = []

    async def deliver_all(job, run, output, ctx):
        posted.append(output.text)
        return []

    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    service = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    job = _job()

    for n in (1, 2):
        assert not await service._stop_if_failing(job, _failed_run(job, n))
    assert load_job(job.id).enabled

    assert await service._stop_if_failing(job, _failed_run(job, 3))
    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_error", None)
    assert len(posted) == 1 and "tools.27.custom.name" in posted[0]


async def test_a_success_between_failures_keeps_the_job(monkeypatch) -> None:
    from bridge.scheduler.service import SchedulerService

    service = SchedulerService(SimpleNamespace())
    job = _job()
    _failed_run(job, 1)
    ok = RunRecord(run_id="run_2", job_id=job.id, job_name=job.name, started_at=time.time(),
                   status="succeeded")
    save_run(ok)
    time.sleep(0.01)
    _failed_run(job, 3)

    assert not await service._stop_if_failing(job, _failed_run(job, 4))
    assert load_job(job.id).enabled


# ─── a session reloaded from disk keeps its model ─────────────────────


class _ColdSession:
    def __init__(self, session_id, *, model_id, reasoning_level, **_kw) -> None:
        self.id = session_id
        self.model_id = model_id
        self.reasoning_level = reasoning_level
        self.gateway_source = None
        self.restore_notice = None

    async def try_restore_transcript(self) -> bool:
        return True


def _persist(tmp_path, monkeypatch, sid: str, metadata: dict) -> None:
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path)
    tp._transcript_path(sid).write_text(  # noqa: SLF001
        json.dumps({"version": 1, "metadata": metadata,
                    "transcript": {"entries": [], "head_id": None}})
    )


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({"model_id": "gpt-6.1-sol-ultrafast", "reasoning_level": "xhigh"},
         ("gpt-6.1-sol-ultrafast", "xhigh")),
        # A model that is no longer offered falls back to the default.
        ({"model_id": "gpt-retired", "reasoning_level": "high"}, ("claude-opus-5-5", None)),
    ],
)
async def test_a_reloaded_session_keeps_the_model_it_was_saved_with(
    tmp_path, monkeypatch, saved, expected
) -> None:
    import bridge.freyja_bridge as fb

    sid = "freyja:slack:T1:channel:C1:1.2"
    _persist(tmp_path, monkeypatch, sid, saved)
    monkeypatch.setattr(fb, "_BridgeSession", _ColdSession)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-opus-5-5")

    sess = await state.ensure_session(sid)

    assert sess.model_id == expected[0]
    if expected[1] is not None:
        assert sess.reasoning_level == expected[1]


async def test_an_explicit_model_still_wins(tmp_path, monkeypatch) -> None:
    import bridge.freyja_bridge as fb

    sid = "freyja:slack:T1:channel:C1:1.3"
    _persist(tmp_path, monkeypatch, sid, {"model_id": "gpt-6.1-sol-ultrafast"})
    monkeypatch.setattr(fb, "_BridgeSession", _ColdSession)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-opus-5-5")

    sess = await state.ensure_session(sid, model_id="claude-sonnet-5-5")

    assert sess.model_id == "claude-sonnet-5-5"


# ─── the failure message ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': \"tools.27.custom.name: String should match pattern "
        "'^[a-zA-Z0-9_-]{1,128}$'\"}}",
        "Invalid tool name 'scheduler.continue_loop': must match ^[a-zA-Z0-9_-]+$",
    ],
)
def test_a_rejected_tool_does_not_send_the_operator_to_repair(message) -> None:
    from bridge.freyja_bridge import _format_user_facing_runner_failure

    text = _format_user_facing_runner_failure(
        reason="format", message=message, already_streamed=False
    )

    assert "rejected one of the tools" in text
    assert "will not help" in text
