"""A fire the gateway or the app refuses stays due, for every job.

The scheduler that owns the tick claims a job whose session runs in the
gateway (a Slack thread) or in the app (a desktop session), moves its
next fire past the slot, and hands the fire over. The process handed it
refused it when a run of the job held its run lock, or when it did not
run the job's session (an app restarting, before its old bridge let go
of the lock): the slot was dropped with a log line, and the run was
missed without a trace in the job's history.
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
DESKTOP = "session-19f0desk"


@pytest.fixture
def fired(monkeypatch) -> list[tuple[str, float | None, int]]:
    out: list[tuple[str, float | None, int]] = []

    async def fake_fire(_service, job, *, scheduled_for=None, attempt=0, **_kw):
        out.append((job.id, scheduled_for, attempt))
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


@pytest.fixture
def owner(monkeypatch, tmp_path):
    """The scheduler daemon, which owns the tick, while the gateway and the
    app run in other processes."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(gp, "_desktop_lock", None)
    monkeypatch.setattr(gp, "desktop_bridge_runs", lambda: True)
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _hourly(session_id: str, **fields) -> JobRecord:
    if session_id == DESKTOP:
        tp.save_transcript(DESKTOP, {
            "version": 1, "session_id": DESKTOP, "metadata": {},
            "transcript": {"entries": [{"id": "d1"}], "head_id": "d1"},
        })
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest", schedule=IntervalSchedule(seconds=3600),
        execution=ExistingSession(session_id=session_id), sinks=[NoopSinkSpec()], **fields,
    )
    job.next_fire_at = time.time() - 60
    save_job(job)
    return job


def _handed(path_name: str) -> list[dict]:
    import bridge.gateway.control_channel as cc

    path = getattr(cc, path_name)()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _receiver(monkeypatch) -> SchedulerService:
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _due_again(job: JobRecord, attempt: int = 0) -> None:
    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (True, "active")
    assert (saved.next_fire_at, saved.retry_attempt) == (job.next_fire_at, attempt)


async def test_a_fire_the_gateway_refuses_while_a_run_goes_on_stays_due(owner, fired, monkeypatch):
    job = _hourly(SLACK, retry_attempt=2)
    await owner._try_claim_and_fire(job.id)
    (cmd,) = _handed("commands_path")
    assert load_job(job.id).next_fire_at > time.time()

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    gateway = _receiver(monkeypatch)
    with FileLock(job_run_lock_path(job.id)) as got:
        assert got  # a run now of the job, in the gateway
        assert not await gateway.fire_handed_over(job.id, cmd["scheduled_for"], cmd["attempt"])
        _due_again(job, attempt=2)

        # Its overlap policy (queue) holds it while that run goes on.
        monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
        await owner._try_claim_and_fire(job.id)
        assert len(_handed("commands_path")) == 1

    await owner._try_claim_and_fire(job.id)
    (_, again) = _handed("commands_path")
    assert (again["scheduled_for"], again["attempt"]) == (job.next_fire_at, 2)


async def test_a_fire_the_app_refuses_before_it_holds_its_lock_stays_due(owner, fired, monkeypatch):
    # A restarted app's bridge reads its control channel while the old one
    # still holds the lock: it does not run desktop sessions yet.
    job = _hourly(DESKTOP)
    await owner._try_claim_and_fire(job.id)
    (cmd,) = _handed("desktop_commands_path")

    app = _receiver(monkeypatch)
    await fb._handle_scheduler_fire_command(SimpleNamespace(scheduler=app), cmd)

    assert fired == []
    _due_again(job)


async def test_a_fire_that_did_not_reach_the_gateway_stays_due(owner, monkeypatch):
    import bridge.gateway.control_channel as cc

    def append_command(_cmd, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cc, "append_command", append_command)
    job = _hourly(SLACK)

    await owner._try_claim_and_fire(job.id)

    _due_again(job)
    assert job.id in owner._waiting_job_ids


async def test_a_refused_fire_of_a_job_paused_meanwhile_stays_paused(owner, monkeypatch):
    job = _hourly(SLACK)
    await owner._try_claim_and_fire(job.id)
    (cmd,) = _handed("commands_path")
    await owner.pause_job(job.id)

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    with FileLock(job_run_lock_path(job.id)) as got:
        assert got
        await _receiver(monkeypatch).fire_handed_over(job.id, cmd["scheduled_for"], cmd["attempt"])

    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "paused", None)
