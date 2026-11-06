"""A stopped turn of a harness session (Claude Code, Codex) ends as a native
turn does: with a turn_complete that says it was stopped.

_run_harness_turn said turn_complete when its turn ended or failed, but a
stop went past its handlers and it said nothing: the app could show the
turn as still running, and a scheduled run whose turn it was never heard
of the stop (2b373c0), so it ran on as a finished run.
"""

from __future__ import annotations

import asyncio

from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_runs_for_job, save_job


class _Service:
    def __init__(self, sessions: dict) -> None:
        self.state = type("State", (), {"permission_tier": "low", "sessions": sessions})()

    def emit_event(self, **_kw) -> None:
        pass


class _Adapter:
    """A harness whose turn works on until it is stopped."""

    label = "Claude Code"
    harness_session_id = "harness-1"

    def __init__(self) -> None:
        self.working = asyncio.Event()

    async def run_turn(self, text, *, mcp_servers=None, turn_id=""):
        self.working.set()
        await asyncio.sleep(60)

    async def cancel(self) -> None:
        pass


async def _harness_session(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    import bridge.gateway.pid as gp

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(fb, "_SESSION_EVENT_LISTENERS", {})
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)  # this is the app's bridge
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("h1", runtime="claude_code")
    await sess.initialize()
    sess.harness_adapter = _Adapter()
    return state, sess


async def test_a_stopped_harness_turn_says_it_was_stopped(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb

    state, sess = await _harness_session(monkeypatch, tmp_path)
    events: list[dict] = []
    fb.register_session_listener("h1", events.append)
    fb._schedule_or_queue_turn(sess, "refactor the parser")
    await asyncio.wait_for(sess.harness_adapter.working.wait(), 5)

    await fb._handle_command(state, {"type": "cancel", "sessionId": "h1", "scope": "turn"})
    await asyncio.wait_for(asyncio.gather(sess.pending_task, return_exceptions=True), 5)

    [started] = [e for e in events if e["type"] == "turn_start"]
    assert [
        (e["turnId"], e["success"], e.get("stopped")) for e in events if e["type"] == "turn_complete"
    ] == [(started["turnId"], False, True)]


async def test_a_run_whose_harness_turn_the_operator_stops_is_cancelled_and_not_delivered(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.runtime import fire_job

    state, sess = await _harness_session(monkeypatch, tmp_path)
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="h1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    fire = asyncio.create_task(fire_job(_Service({"h1": sess}), job))
    await asyncio.wait_for(sess.harness_adapter.working.wait(), 5)

    await fb._handle_command(state, {"type": "cancel", "sessionId": "h1", "scope": "turn"})
    run = await asyncio.wait_for(fire, 10)

    assert delivered == []
    assert run.status == "cancelled"
    assert load_runs_for_job(job.id, limit=1)[0].status == "cancelled"


# ─── a run whose harness turn fails ───────────────────────────────────


class _Raises(_Adapter):
    async def run_turn(self, text, *, mcp_servers=None, turn_id=""):
        raise RuntimeError("the codex app-server exited")


def _report_job() -> JobRecord:
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="h1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    return job


async def test_a_run_whose_harness_turn_raises_fails_with_the_harness_error(monkeypatch, tmp_path):
    # _run_harness_turn noted no error, so the run said it was most likely
    # asleep through its time.
    from bridge.scheduler.runtime import fire_job

    _state, sess = await _harness_session(monkeypatch, tmp_path)
    sess.harness_adapter = _Raises()

    run = await asyncio.wait_for(fire_job(_Service({"h1": sess}), _report_job()), 10)

    assert run.status == "failed"
    assert "the codex app-server exited" in run.error


async def test_a_run_whose_harness_cannot_be_built_fails_with_why(monkeypatch, tmp_path):
    from bridge.scheduler.runtime import fire_job

    _state, sess = await _harness_session(monkeypatch, tmp_path)
    sess.harness_adapter = None
    sess.runtime = "aider"  # A runtime this build does not know.

    run = await asyncio.wait_for(fire_job(_Service({"h1": sess}), _report_job()), 10)

    assert run.status == "failed"
    assert "unknown harness runtime: 'aider'" in run.error
