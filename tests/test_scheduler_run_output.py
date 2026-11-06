"""A run's output is the reply of its own turns.

A fire into the operator's session waits until the session is quiet, so a
turn the operator queued behind the fire's ran in that wait, and the run
read the last reply in the transcript when it ended: the operator's turn's
reply was taken for the run's output and delivered to the job's sinks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

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
    """The operator's session, run by the real run_turn. Each turn replies
    what ``replies`` gives for its prompt; one whose prompt holds a key of
    ``holds`` waits on that event first. With ``slow_start`` set, the next
    turn's start waits on it (a session's MCP servers starting), and
    ``starting`` is set then."""
    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp
    from engine.session import Session

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession("s1", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                             reasoning_level="auto", coordination_strategy="bus", state=state)
    sess.session = Session.create(system_prompt="t")
    sess.replies = {}
    sess.holds = {}
    sess.slow_start = None
    sess.starting = asyncio.Event()

    async def _initialize():
        gate, sess.slow_start = sess.slow_start, None
        if gate is not None:
            sess.starting.set()
            await gate.wait()

    class _Runner:
        usage = SimpleNamespace(input=0, output=0, cache_read=0, cache_write=0,
                                effective_context_tokens=lambda: 0)

        async def run(self, session, message, stream=True):
            session.add_user_message(message)
            prompt = _text(message)
            for key, hold in sess.holds.items():
                if key in prompt:
                    await hold.wait()
            reply = next(r for key, r in sess.replies.items() if key in prompt)
            await asyncio.sleep(0.05)
            session.add_assistant_message(reply)
            return SimpleNamespace(success=True, response=reply, stop_reason="end_turn")

    sess.initialize = _initialize
    sess.runner = _Runner()
    return sess


def _job() -> JobRecord:
    job = JobRecord(id="sched_digest", name="digest", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the digest", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    return job


def _last_reply(sess) -> str:
    return _text(sess.session.transcript.entries[-1].message)


async def test_a_runs_output_is_its_own_reply_not_that_of_a_turn_queued_behind_it(operator_session):
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"write the digest": "Here is the digest", "how is the build": "The build is green"}
    sess.holds = {"write the digest": asyncio.Event()}
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    while sess.pending_task is None:
        await asyncio.sleep(0.01)
    fb._schedule_or_queue_turn(sess, "how is the build?")  # Sent while the fire's turn runs.
    sess.holds["write the digest"].set()

    run = await asyncio.wait_for(fire, 10)

    assert _last_reply(sess) == "The build is green"  # It ran in the run's wait.
    assert run.output_text == "Here is the digest"
