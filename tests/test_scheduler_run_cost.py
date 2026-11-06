"""A run's cost is what its own model calls cost.

A run's cost was how much its session's total grew while it ran. A fire
into the operator's session waits until the session is quiet, so a
message the operator sent meanwhile ran in that wait, and its turn was
billed to the run, toward the run's budget and the job's daily one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bridge.inbox import KIND_MEMO, InboxMessage, new_message_id
from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
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
    """The operator's session. Each turn makes one model call, which costs
    what ``costs`` gives for its prompt; ``during_fire`` runs inside the
    fire's turn."""
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
            sess._on_llm_call({"provider": "anthropic", "model": "claude-sonnet-5-5",
                               "input_tokens": 1000, "output_tokens": cents})
            await asyncio.sleep(0)
            session.add_assistant_message("done")
            return SimpleNamespace(success=True, response="done", stop_reason="end_turn")

    sess.initialize = _initialize
    sess.runner = _Runner()
    return sess


def _job() -> JobRecord:
    job = JobRecord(id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the digest", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
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
