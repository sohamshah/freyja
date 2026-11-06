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
