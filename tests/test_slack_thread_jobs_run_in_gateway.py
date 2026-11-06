"""A scheduled job that fires into a Slack thread runs in the gateway.

The gateway runs a Slack thread's session and saves its transcript, inbox
and goal files. The scheduler daemon also builds a GatewayDaemon, send-only,
to post scheduled output to Slack, and it often owns the scheduler's tick.
A job aimed at a Slack thread then ran on the daemon's own copy of that
session, and the copy's saves went over the gateway's transcript. The
daemon's gateway runner made the copy look like the gateway's own session,
so a6132a9's check for the app's copies passed it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
import bridge.transcript_persistence as tp
from bridge.scheduler.models import (
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
)
from bridge.scheduler.persistence import FileLock, job_run_lock_path, load_job, save_job
from bridge.scheduler.service import SchedulerService

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    """The scheduler daemon: its state has a send-only gateway runner, it
    does not hold the gateway lock, and a gateway runs in another process.
    Records every session it would build."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    built: list[str] = []

    async def ensure_session(sid, **_kw):
        built.append(sid)
        raise AssertionError(f"the scheduler daemon built a copy of {sid}")

    state = SimpleNamespace(
        sessions={}, ensure_session=ensure_session,
        gateway_runner=SimpleNamespace(adapters=[]),
    )
    service = SchedulerService(state)
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service, built


def _slack_thread_job(job_id: str = "sched_thread") -> JobRecord:
    job = JobRecord(
        id=job_id,
        name="check the deploys",
        schedule=IntervalSchedule(seconds=3600),
        prompt="check the deploys",
        execution=ExistingSession(session_id=SLACK),
        sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = time.time() - 1
    save_job(job)
    return job


def _commands() -> list[dict]:
    from bridge.gateway.control_channel import commands_path

    path = commands_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_a_slack_thread_job_due_in_the_scheduler_daemon_goes_to_the_gateway(daemon):
    service, built = daemon
    job = _slack_thread_job()
    slot = job.next_fire_at

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    assert built == []
    (cmd,) = _commands()
    assert (cmd["type"], cmd["job_id"], cmd["scheduled_for"], cmd["attempt"]) == (
        "scheduler_fire", job.id, slot, 0,
    )
    # Claimed here, once: the slot is spent, and the run lock is free for
    # the gateway to take.
    assert load_job(job.id).next_fire_at > time.time()
    run_lock = FileLock(job_run_lock_path(job.id))
    assert run_lock.acquire()
    run_lock.release()


async def test_a_slack_thread_job_waits_while_no_gateway_runs(daemon, monkeypatch):
    import bridge.gateway.pid as gp

    service, built = daemon
    monkeypatch.setattr(gp, "get_running_pid", lambda: None)
    job = _slack_thread_job()

    await service._try_claim_and_fire(job.id)

    assert built == [] and _commands() == []
    assert not service._in_flight_runs
    # Still due: it fires once a gateway runs.
    assert load_job(job.id).next_fire_at == job.next_fire_at
    assert job.id in service._waiting_job_ids


async def test_the_gateway_runs_the_fire_it_was_handed(daemon, monkeypatch):
    # The gateway's end: the run, its record, sinks and retries are the
    # ones the owner would have made, under the job's run lock.
    from bridge.gateway.run import GatewayDaemon

    service, _built = daemon
    job = _slack_thread_job()
    await service._try_claim_and_fire(job.id)
    (cmd,) = _commands()

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    fired: list[tuple] = []

    async def fake_fire(_service, fresh, **kw):
        assert not FileLock(job_run_lock_path(fresh.id)).acquire()
        fired.append((fresh.id, kw.get("scheduled_for"), kw.get("attempt")))
        return RunRecord(run_id="run_1", job_id=fresh.id, job_name=fresh.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    gateway_service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(gateway_service, "emit_event", lambda **_kw: None)
    gateway = GatewayDaemon()
    gateway.state = SimpleNamespace(scheduler=gateway_service)

    await gateway._on_scheduler_fire(cmd)
    await asyncio.gather(*gateway_service._in_flight_runs.values())

    assert fired == [(job.id, cmd["scheduled_for"], 0)]


async def test_a_job_into_the_gateways_own_thread_fires_in_the_gateway(daemon, monkeypatch):
    service, _built = daemon
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    fired: list[str] = []

    async def fake_fire(_service, job, **_kw):
        fired.append(job.id)
        return RunRecord(run_id="run_1", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    job = _slack_thread_job()

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    assert fired == [job.id] and _commands() == []


async def test_a_manual_run_outside_the_gateway_builds_no_copy(daemon):
    # Run now from the app or the schedule tool fires in its own process.
    service, built = daemon
    job = _slack_thread_job()

    run = await service.run_job_now(job.id)

    assert built == []
    assert run.status == "failed" and "Slack gateway" in run.error


async def test_a_copy_in_the_scheduler_daemon_saves_nothing_over_the_gateways_files(daemon, tmp_path):
    from engine.session import Session

    tp.save_transcript(SLACK, {
        "version": 1, "session_id": SLACK, "metadata": {},
        "transcript": {"entries": [{"id": "g1"}], "head_id": "g1"},
    })
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.gateway_runner = SimpleNamespace(adapters=[])  # send-only
    sess = fb._BridgeSession(
        SLACK, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.session = Session.create(system_prompt="t")
    sess.session.add_user_message("from the scheduler daemon")

    sess._save_transcript()

    assert [e["id"] for e in tp.load_transcript(SLACK)["transcript"]["entries"]] == ["g1"]


def _thread_saved(strategy: str) -> None:
    """The gateway's saved transcript of the thread, in ``strategy``."""
    from engine.session import Session

    session = Session.create(system_prompt="t")
    session.add_user_message("plan the release")
    session.add_assistant_message("on it")
    data = session.serialize_transcript()
    data.setdefault("metadata", {}).update(
        model_id="claude-sonnet-5-5", reasoning_level="auto", coordination_strategy=strategy,
    )
    tp.save_transcript(SLACK, data)


@pytest.mark.parametrize(("job_strategy", "loaded_as"), [(None, "kanban"), ("goal", "goal")])
async def test_a_fire_loads_a_thread_in_its_own_mode_unless_the_job_sets_one(
    monkeypatch, tmp_path, job_strategy, loaded_as,
):
    # The job passed its own mode, bus when none was set, so a kanban
    # thread the gateway had not loaded came back as bus, without its board.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    monkeypatch.setattr(fb, "emit", lambda _event: None)
    _thread_saved("kanban")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    job = _slack_thread_job()
    if job_strategy is not None:
        job.coordination_strategy = job_strategy

    sess, _sid, _owns = await runtime._resolve_execution_session(SchedulerService(state), job)
    try:
        assert sess.coordination_strategy == loaded_as
        assert (sess.kanban_board is not None) == (loaded_as == "kanban")
    finally:
        sess._stop_kanban_dispatcher()


async def test_a_job_made_without_a_mode_has_none(monkeypatch):
    from bridge.tools.schedule_tool import ScheduleTool

    service = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    tool = ScheduleTool(service=service, current_session_id=SLACK)

    result = await tool.execute(
        "call_1", {"action": "create", "name": "deploys", "when": "every 2h", "prompt": "check",
                   "execution": "here", "sinks": ["noop"]},
    )

    assert not result.is_error, result.content
    (job,) = await service.list_jobs()
    assert job.coordination_strategy is None
    # A job saved before mode was optional reads back as saved.
    assert JobRecord.from_dict({**job.to_dict(), "coordination_strategy": "bus"}).coordination_strategy == "bus"
    assert JobRecord.from_dict({k: v for k, v in job.to_dict().items() if k != "coordination_strategy"}).coordination_strategy is None


# ── run now from the app ─────────────────────────────────────────────


async def test_run_now_in_the_app_of_a_slack_thread_job_goes_to_the_gateway(daemon):
    # It failed with "its Slack thread runs in the Slack gateway".
    service, built = daemon
    job = _slack_thread_job()

    run = await service.start_job_now(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    assert built == [] and not service._in_flight_runs
    (cmd,) = _commands()
    assert (cmd["type"], cmd["job_id"], cmd["run_id"], cmd["scheduled_for"]) == (
        "scheduler_fire", job.id, run.run_id, None,
    )
    # A manual run leaves the job's cadence alone.
    assert load_job(job.id).next_fire_at == job.next_fire_at


async def test_run_now_in_the_app_of_a_slack_thread_job_while_no_gateway_runs_says_so(daemon, monkeypatch):
    import bridge.gateway.pid as gp

    service, built = daemon
    monkeypatch.setattr(gp, "get_running_pid", lambda: None)
    job = _slack_thread_job()

    with pytest.raises(ValueError, match="Slack gateway is not running"):
        await service.start_job_now(job.id)

    assert built == [] and _commands() == [] and not service._in_flight_runs


def _gateway_service(monkeypatch):
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    gateway = GatewayDaemon()
    gateway.state = SimpleNamespace(scheduler=service)
    return gateway, service


async def test_the_gateway_runs_a_run_now_it_was_handed_under_the_apps_run_id(daemon, monkeypatch):
    service, _built = daemon
    job = _slack_thread_job()
    run = await service.start_job_now(job.id)
    (cmd,) = _commands()
    gateway, gateway_service = _gateway_service(monkeypatch)
    fired: list[tuple] = []

    async def fake_fire(_service, fresh, **kw):
        assert not FileLock(job_run_lock_path(fresh.id)).acquire()
        fired.append((fresh.id, kw.get("run_id"), kw.get("scheduled_for")))
        return RunRecord(run_id=kw["run_id"], job_id=fresh.id, job_name=fresh.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)

    await gateway._on_scheduler_fire(cmd)
    await asyncio.gather(*gateway_service._in_flight_runs.values())

    assert fired == [(job.id, run.run_id, None)]
    assert load_job(job.id).next_fire_at == job.next_fire_at
    run_lock = FileLock(job_run_lock_path(job.id))
    assert run_lock.acquire()
    run_lock.release()


async def test_a_run_now_the_gateway_cannot_start_is_recorded_as_failed(daemon, monkeypatch):
    from bridge.scheduler.persistence import load_run

    service, _built = daemon
    job = _slack_thread_job()
    run = await service.start_job_now(job.id)
    (cmd,) = _commands()
    gateway, gateway_service = _gateway_service(monkeypatch)
    busy = FileLock(job_run_lock_path(job.id))
    assert busy.acquire()  # a scheduled run of it started first
    try:
        await gateway._on_scheduler_fire(cmd)
    finally:
        busy.release()

    assert not gateway_service._in_flight_runs
    recorded = load_run(job.id, run.run_id)
    assert recorded.status == "failed" and "a run of it goes on" in recorded.error
    assert recorded.finished_at is not None
    assert load_job(job.id).next_fire_at == job.next_fire_at
