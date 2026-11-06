"""A run's cost is what its own model calls cost, and a run stops at the
call that takes it over its budget.

A run's cost was how much its session's total grew while it ran. A fire
into the operator's session waits until the session is quiet, so a
message the operator sent meanwhile ran in that wait, and its turn was
billed to the run, toward the run's budget and the job's daily one.

The per-run cost cap was checked only when the run ended, so a run over
it had already spent the money.

A run's sub-agents report their calls under their own session ids, so
neither the run's budget nor its job's daily one counted them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bridge.inbox import KIND_MEMO, InboxMessage, new_message_id
from bridge.scheduler.models import (
    BudgetSpec,
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
)
from bridge.scheduler.persistence import save_job


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = SimpleNamespace(permission_tier="low", sessions=sessions)

    def emit_event(self, **_kw) -> None:
        pass


def _text(message) -> str:
    if isinstance(message, str):
        return message
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return "".join(getattr(b, "text", "") or "" for b in content or [])


@pytest.fixture
def operator_session(monkeypatch, tmp_path):
    """The operator's session. Each turn makes ``calls`` model calls, each
    costing what ``costs`` gives for its prompt, and ``made`` counts them;
    ``during_fire`` runs inside the fire's turn."""
    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp
    import engine.providers as providers
    from engine.session import Session

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    # A call's cost in cents is its output tokens.
    monkeypatch.setattr(providers, "compute_cost", lambda _m, **kw: kw["output_tokens"] / 100)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession("s1", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                             reasoning_level="auto", coordination_strategy="bus", state=state)
    sess.session = Session.create(system_prompt="t")
    sess.costs = {}
    sess.calls = 1
    sess.made = 0
    sess.during_fire = None

    async def _initialize():
        return None

    class _Runner:
        usage = SimpleNamespace(input=0, output=0, cache_read=0, cache_write=0,
                                effective_context_tokens=lambda: 0)

        async def run(self, session, message, stream=True):
            sess.inbox.drain_injectable()
            session.add_user_message(message)
            prompt = _text(message)
            if "[Scheduled run" in prompt and sess.during_fire is not None:
                sess.during_fire()
            cents = next(c for key, c in sess.costs.items() if key in prompt)
            for _ in range(sess.calls):
                sess._on_llm_call({"provider": "anthropic", "model": "claude-sonnet-5-5",
                                   "input_tokens": 1000, "output_tokens": cents})
                sess.made += 1
                await asyncio.sleep(0.05)
            session.add_assistant_message("done")
            return SimpleNamespace(success=True, response="done", stop_reason="end_turn")

    sess.initialize = _initialize
    sess.runner = _Runner()
    return sess


async def _sub_agent_of(parent_id: str, tmp_path, monkeypatch):
    """A sub-agent of ``parent_id`` and the hook its runner reports each
    model call to."""
    import bridge.tools.sub_agent_tool as sat
    import engine.runner as runner_module
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    hooks: list = []

    class _ChildRunner(runner_module.AsyncAgentRunner):
        def __init__(self, *a, **kw):
            hooks.append(kw["on_llm_call"])
            super().__init__(*a, **kw)

        async def run(self, *_a, **_k):
            raise RuntimeError("stop here")

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

    async def _emit(_event):
        return None

    registry = SubAgentRegistry()
    spec = sat.SubAgentSpec(
        parent_workspace=str(tmp_path), parent_model="claude-sonnet-5-5",
        build_provider=lambda *a, **k: _Provider(), parent_registry=ToolRegistry(),
        registry=registry, emit_event=_emit, parent_session_id=parent_id,
    )
    record = registry.register(id="sub_r_1", label="researcher", task="find sources",
                               mode="background")
    with monkeypatch.context() as m:
        m.setattr(runner_module, "AsyncAgentRunner", _ChildRunner)
        with pytest.raises(RuntimeError, match="stop here"):
            await sat.SubAgentTool(spec)._run_child(record)
    return record, hooks[0]


def _job(**fields) -> JobRecord:
    job = JobRecord(id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the digest", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()], **fields)
    save_job(job)
    return job


async def test_a_turn_the_operator_starts_while_the_run_waits_is_not_its_cost(operator_session):
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 20, "how is the build": 100}
    sess.during_fire = lambda: fb._schedule_or_queue_turn(sess, "how is the build?")

    run = await fire_job(_Service({"s1": sess}), _job())

    # Both turns ran in the run's wait; only the fire's is the run's.
    assert sess.cumulative_cost == pytest.approx(1.20)
    assert run.cost_usd == pytest.approx(0.20)


async def test_the_turn_its_sub_agents_wake_is_its_cost(operator_session):
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 20, fb.MEMO_WAKE_PROMPT: 30}
    sess.during_fire = lambda: sess.inbox.push(InboxMessage(
        id=new_message_id(), from_session="sub-1", from_label="researcher",
        from_role="agent", content="found three sources", kind=KIND_MEMO,
    ))

    run = await fire_job(_Service({"s1": sess}), _job())

    assert run.cost_usd == pytest.approx(0.50)


async def test_a_turn_start_says_whether_a_message_started_it(operator_session, monkeypatch):
    import bridge.freyja_bridge as fb

    sess = operator_session
    sess.costs = {"hello": 1, fb.MEMO_WAKE_PROMPT: 1, "keep going": 1}
    starts: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda ev: ev["type"] == "turn_start" and starts.append(ev))

    await sess.run_turn("hello")
    await sess.run_turn(fb.MEMO_WAKE_PROMPT)
    await sess.run_turn("keep going", is_goal_continuation=True)

    assert [ev.get("automatic") for ev in starts] == [False, True, True]


async def test_a_run_stops_at_the_call_that_takes_it_over_its_budget(operator_session):
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 10}
    sess.calls = 5

    run = await fire_job(_Service({"s1": sess}), _job(budget=BudgetSpec(max_cost_usd_per_run=0.25)))

    # $0.10 a call: the third takes it over $0.25, and the turn stops there.
    assert sess.made == 3
    assert run.status == "budget_exceeded"
    assert run.cost_usd == pytest.approx(0.30)
    assert "budget exceeded" in run.error


async def test_the_calls_of_a_sub_agent_its_turn_spawns_are_its_cost(
    operator_session, tmp_path, monkeypatch,
):
    import time

    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 20}
    record, on_llm_call = await _sub_agent_of("s1", tmp_path, monkeypatch)

    def _spawn_and_work():
        record.start_time = time.time()
        for _ in range(2):
            on_llm_call({"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 30})

    sess.during_fire = _spawn_and_work

    run = await fire_job(_Service({"s1": sess}), _job())

    # Sub-agents report their calls under their own session ids.
    assert run.cost_usd == pytest.approx(0.80)


async def test_a_sub_agent_started_before_the_run_is_not_its_cost(
    operator_session, tmp_path, monkeypatch,
):
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 20}
    record, on_llm_call = await _sub_agent_of("s1", tmp_path, monkeypatch)
    record.start_time -= 60
    sess.during_fire = lambda: on_llm_call(
        {"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 30},
    )

    run = await fire_job(_Service({"s1": sess}), _job())

    assert run.cost_usd == pytest.approx(0.20)


async def test_a_sub_agents_call_that_takes_the_run_over_its_budget_stops_it(
    operator_session, tmp_path, monkeypatch,
):
    import time

    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.costs = {"write the digest": 10}
    sess.calls = 5
    record, on_llm_call = await _sub_agent_of("s1", tmp_path, monkeypatch)

    def _spawn_and_work():
        record.start_time = time.time()
        on_llm_call({"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 30})

    sess.during_fire = _spawn_and_work

    run = await fire_job(_Service({"s1": sess}), _job(budget=BudgetSpec(max_cost_usd_per_run=0.25)))

    # $0.30 from the sub-agent, then $0.10 from the turn's first call.
    assert sess.made == 1
    assert run.status == "budget_exceeded"
    assert run.cost_usd == pytest.approx(0.40)
