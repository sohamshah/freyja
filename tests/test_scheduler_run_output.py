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
    what ``replies`` gives for its prompt, in one model call that costs what
    ``costs`` gives in cents (none by default); one whose prompt holds a key
    of ``holds`` waits on that event first, and one whose prompt holds a key
    of ``tool_calls`` makes that call. With ``slow_start`` set, the next
    turn's start waits on it (a session's MCP servers starting), and
    ``starting`` is set then."""
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
    sess.replies = {}
    sess.costs = {}
    sess.holds = {}
    sess.tool_calls = {}
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
            for key, call in sess.tool_calls.items():
                if key in prompt:
                    await call()
            reply = next(r for key, r in sess.replies.items() if key in prompt)
            cents = next((c for key, c in sess.costs.items() if key in prompt), 0)
            sess._on_llm_call({"provider": "anthropic", "model": "claude-sonnet-5-5",
                               "input_tokens": 1000, "output_tokens": cents})
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


# ─── a stop that lands before the run's turn has started ──────────────


async def test_a_run_stopped_as_its_turn_starts_is_cancelled_with_no_output(operator_session):
    # Stopped before it said turn_start, the turn said nothing at all: the
    # run took the reply before its turn for its output and delivered it.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.session.add_user_message("how is the build?")
    sess.session.add_assistant_message("The build is green")
    sess.replies = {"write the digest": "Here is the digest"}
    sess.slow_start = asyncio.Event()
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    await asyncio.wait_for(sess.starting.wait(), 5)

    fb._force_cancel_session(sess, scope="turn")
    run = await asyncio.wait_for(fire, 10)

    assert (run.status, run.output_text, run.delivery_reports) == ("cancelled", "", [])


async def test_a_turn_queued_behind_a_run_stopped_as_its_turn_starts_is_not_the_runs(operator_session):
    # The run took the next turn that started for its own: the operator's
    # turn queued behind it, whose reply it delivered.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"how is the build": "The build is green", "write the digest": "Here is the digest",
                    "and the tests": "The tests pass"}
    sess.holds = {"how is the build": asyncio.Event()}
    fb._schedule_or_queue_turn(sess, "how is the build?")
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    while not sess.queued_messages:
        await asyncio.sleep(0.01)
    fb._schedule_or_queue_turn(sess, "and the tests?")  # Queued behind the run's prompt.
    sess.slow_start = asyncio.Event()  # The run's turn starts slowly.
    sess.holds["how is the build"].set()
    await asyncio.wait_for(sess.starting.wait(), 5)

    fb._force_cancel_session(sess, scope="turn")
    run = await asyncio.wait_for(fire, 10)
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), 5)

    assert _last_reply(sess) == "The tests pass"  # The stop was for the run's turn only.
    assert (run.status, run.output_text, run.delivery_reports) == ("cancelled", "", [])


# ─── a run whose turn fails before it starts ──────────────────────────


def _fails_to_start_once(sess, error: Exception) -> None:
    """The session's next turn fails as it starts (initialize raises: its
    model's provider cannot be built); the ones after it start."""
    initialize = sess.initialize

    async def fail():
        sess.initialize = initialize
        raise error

    sess.initialize = fail


async def test_a_run_whose_turn_fails_before_it_starts_does_not_take_the_turn_queued_behind_it(operator_session):
    # The run took the next turn that started for its own: the operator's
    # turn queued behind its prompt, whose reply it delivered as its output.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"how is the build": "The build is green", "write the digest": "Here is the digest",
                    "and the tests": "The tests pass"}
    sess.costs = {"and the tests": 30}
    sess.holds = {"how is the build": asyncio.Event()}
    fb._schedule_or_queue_turn(sess, "how is the build?")
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    while not sess.queued_messages:
        await asyncio.sleep(0.01)
    fb._schedule_or_queue_turn(sess, "and the tests?")  # Queued behind the run's prompt.
    _fails_to_start_once(sess, ValueError("GEMINI_API_KEY is not set"))
    sess.holds["how is the build"].set()

    run = await asyncio.wait_for(fire, 10)

    assert _last_reply(sess) == "The tests pass"
    assert (run.status, run.output_text, run.delivery_reports) == ("failed", "", [])
    assert "GEMINI_API_KEY is not set" in run.error
    assert run.cost_usd == 0


async def test_a_run_whose_turn_fails_before_it_starts_fails_with_the_turns_error(operator_session):
    # With no turn after it, the run said it was most likely asleep through
    # its time, and not why its turn failed.
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"write the digest": "Here is the digest"}
    _fails_to_start_once(sess, ValueError("GEMINI_API_KEY is not set"))

    run = await asyncio.wait_for(fire_job(_Service({"s1": sess}), _job()), 10)

    assert (run.status, run.output_text) == ("failed", "")
    assert "GEMINI_API_KEY is not set" in run.error


# ─── a stop before the run's turn task has taken its first step ───────


async def test_a_run_stopped_as_its_turn_is_dispatched_is_cancelled(operator_session):
    # Stopped before its first step, the turn's task ran nothing and said
    # nothing, so the run ended failed: no output, no turn.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"write the digest": "Here is the digest"}
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    while sess.pending_task is None:
        await asyncio.sleep(0)
    fb._force_cancel_session(sess, scope="turn")  # The app's Stop, at once.

    run = await asyncio.wait_for(fire, 10)

    assert (run.status, run.output_text, run.delivery_reports) == ("cancelled", "", [])


# ─── a turn that fails before its prompt reaches the transcript ───────


async def test_a_run_whose_turn_fails_before_its_prompt_is_added_has_no_earlier_reply(operator_session):
    # The run read the last reply in the transcript as its turn ended: the
    # turn before its own, which it delivered as its output.
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.session.add_user_message("how is the build?")
    sess.session.add_assistant_message("The build is green")

    class _Broken:
        usage = sess.runner.usage

        async def run(self, session, message, stream=True):
            raise RuntimeError("the request could not be built")

    sess.runner = _Broken()

    run = await asyncio.wait_for(fire_job(_Service({"s1": sess}), _job()), 10)

    assert (run.status, run.output_text, run.delivery_reports) == ("failed", "", [])


# ─── a run whose runner raises ────────────────────────────────────────


class _Raises:
    """A runner that raises the error ``fails`` gives for a prompt holding
    its key, once that prompt's hold (if any) is set, as it builds the
    request; other prompts run as the session's runner runs them."""

    def __init__(self, sess, fails: dict) -> None:
        self.usage = sess.runner.usage
        self._sess = sess
        self._runner = sess.runner
        self._fails = fails

    async def run(self, session, message, stream=True):
        prompt = _text(message)
        for key, error in self._fails.items():
            if key in prompt:
                if key in self._sess.holds:
                    await self._sess.holds[key].wait()
                raise error
        return await self._runner.run(session, message, stream=stream)


async def test_a_run_whose_runner_raises_fails_with_the_runners_error(operator_session):
    # run_turn caught the error and set no last_turn_error, so the run said
    # it was most likely asleep through its time.
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.runner = _Raises(sess, {"write the digest": RuntimeError("the request could not be built")})

    run = await asyncio.wait_for(fire_job(_Service({"s1": sess}), _job()), 10)

    assert (run.status, run.output_text) == ("failed", "")
    assert "the request could not be built" in run.error


async def test_a_turn_queued_behind_a_run_whose_runner_raises_does_not_clear_its_error(operator_session):
    # The session's last error is its last turn's: the operator's turn that
    # ran in the run's wait had none.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"how is the build": "The build is green"}
    sess.holds = {"write the digest": asyncio.Event()}
    sess.runner = _Raises(sess, {"write the digest": RuntimeError("the request could not be built")})
    fire = asyncio.create_task(fire_job(_Service({"s1": sess}), _job()))
    while sess.pending_task is None:
        await asyncio.sleep(0.01)
    fb._schedule_or_queue_turn(sess, "how is the build?")  # Sent while the run's turn runs.
    sess.holds["write the digest"].set()

    run = await asyncio.wait_for(fire, 10)

    assert _last_reply(sess) == "The build is green"
    assert (run.status, run.output_text) == ("failed", "")
    assert "the request could not be built" in run.error


# ─── a run whose turn cannot run at all ───────────────────────────────


@pytest.mark.parametrize("missing", ["session", "runner"])
async def test_a_run_whose_session_is_not_set_up_fails_with_why(operator_session, missing):
    # run_turn returned with an error that names no run, so the run said it
    # was most likely asleep through its time.
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    setattr(sess, missing, None)  # initialize left it unset.

    run = await asyncio.wait_for(fire_job(_Service({"s1": sess}), _job()), 10)

    assert (run.status, run.output_text) == ("failed", "")
    assert f"{missing} not initialized" in run.error


async def test_a_run_into_a_session_another_process_runs_fails_with_why(operator_session, monkeypatch):
    # The refusal was a notice for the app alone, so the run said it was
    # most likely asleep through its time.
    import bridge.freyja_bridge as fb
    from bridge.scheduler.runtime import fire_job

    sess = operator_session
    sess.replies = {"write the digest": "Here is the digest"}
    monkeypatch.setattr(fb, "_held_elsewhere", lambda _state, _sid: fb._GATEWAY_RUNS_IT)

    run = await asyncio.wait_for(fire_job(_Service({"s1": sess}), _job()), 10)

    assert (run.status, run.output_text) == ("failed", "")
    assert "runs in the Slack gateway" in run.error
