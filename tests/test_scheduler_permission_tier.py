"""A scheduled run's permission tier stays on the run's own session.

fire_job set the job's tier on the process-wide ``permission_tier`` for
the whole run, which can last many minutes, and put the old value back at
the end. Every session created in that process meanwhile (a new desktop
chat, a session restored from disk) inherited the job's tier, often
yolo. And a tier the operator chose in Settings during the run was
overwritten when the run ended.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import JobRecord, NewSession, NoopSinkSpec
from bridge.scheduler.persistence import save_job
from engine.session import TranscriptManager
from engine.types import Message


class _State:
    """Stands in for _BridgeState: a new session takes the process tier."""

    def __init__(self) -> None:
        self.permission_tier = "low"
        self.sessions: dict = {}

    async def ensure_session(self, session_id: str, **_kw):
        sess = self.sessions.get(session_id)
        if sess is None:
            sess = SimpleNamespace(
                permission_tier=self.permission_tier,
                permission_handler=None,
                pending_task=None,
                session=SimpleNamespace(transcript=TranscriptManager()),
            )
            self.sessions[session_id] = sess
        return sess


class _Service:
    def __init__(self) -> None:
        self.state = _State()

    def emit_event(self, **_kw) -> None:
        pass


async def test_a_run_s_tier_reaches_its_session_and_no_other(monkeypatch) -> None:
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    service = _Service()
    state = service.state
    seen: dict[str, str] = {}

    async def turn(sess, prompt: str) -> None:
        seen["run session"] = sess.permission_tier
        # Meanwhile the operator opens a new chat, then raises the tier
        # in Settings.
        other = await state.ensure_session("desktop-new")
        seen["new chat"] = other.permission_tier
        state.permission_tier = "medium"
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=prompt))
        transcript.append_message(Message(role="assistant", content="Done."))

    def schedule_turn(sess, prompt, attachments=None, on_turn_start=None):
        sess.pending_task = asyncio.ensure_future(turn(sess, prompt))
        return True

    async def quiescent(sess, **_kw):
        await sess.pending_task

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    job = JobRecord(
        id="sched_tier",
        name="nightly cleanup",
        prompt="clean up",
        execution=NewSession(),
        permission_snapshot="yolo",
        sinks=[NoopSinkSpec()],
    )
    save_job(job)

    run = await fire_job(service, job)

    assert run.status == "succeeded"
    assert seen == {"run session": "yolo", "new chat": "low"}
    assert state.permission_tier == "medium"


@pytest.mark.parametrize("built", [False, True], ids=["handler not built", "handler built"])
async def test_a_job_session_runs_at_the_job_s_tier_each_fire(built) -> None:
    from bridge.scheduler.runtime import _resolve_execution_session

    service = _Service()
    job = JobRecord(id="sched_tier", name="n", execution=NewSession(), permission_snapshot="high")
    sess = await service.state.ensure_session("scheduler:sched_tier.ephemeral")
    policies: list[str] = []
    if built:
        sess.permission_handler = SimpleNamespace(set_policy=policies.append)

    got, _sid, _owns = await _resolve_execution_session(service, job)

    assert got is sess
    assert sess.permission_tier == "high"
    assert policies == (["high"] if built else [])
    assert service.state.permission_tier == "low"
