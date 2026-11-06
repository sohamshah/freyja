"""A slot that is due again because its fire did not run in the gateway or
the app is not missed: an owner restart leaves it due, whatever the job's
misfire policy.

A fire the gateway or the app refused, or that never reached it, makes the
job due at its slot again. So does a job whose session runs in the gateway
or the app while that is not running: it waits, due. A new owner's boot
before the next claim took such a slot for one missed while no scheduler
ran, and under misfire "skip" moved past it: the run was dropped.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
import bridge.transcript_persistence as tp
from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec, RunRecord
from bridge.scheduler.persistence import load_job, save_job
from bridge.scheduler.service import SchedulerService

SLACK = "freyja:slack:T1:channel:C1:1.0"
DESKTOP = "session-19f0desk"
DUE = float(int(time.time()) - 60)


@pytest.fixture
def fired(monkeypatch) -> list[float | None]:
    out: list[float | None] = []

    async def fake_fire(_service, job, *, scheduled_for=None, **_kw):
        out.append(scheduled_for)
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


@pytest.fixture
def gp(monkeypatch, tmp_path):
    """The scheduler daemon's view: the gateway and the app run in other
    processes."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(gp, "_desktop_lock", None)
    monkeypatch.setattr(gp, "desktop_bridge_runs", lambda: True)
    return gp


def _service(monkeypatch) -> SchedulerService:
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _hourly(session_id: str = SLACK) -> JobRecord:
    if session_id == DESKTOP:
        tp.save_transcript(DESKTOP, {
            "version": 1, "session_id": DESKTOP, "metadata": {},
            "transcript": {"entries": [{"id": "d1"}], "head_id": "d1"},
        })
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest", schedule=IntervalSchedule(seconds=3600),
        execution=ExistingSession(session_id=session_id), sinks=[NoopSinkSpec()],
        misfire_policy="skip",
    )
    job.next_fire_at = DUE
    save_job(job)
    return job


async def _owner_restarts(monkeypatch) -> None:
    owner = _service(monkeypatch)
    await owner.start()
    assert owner._is_owner
    await owner.stop()


async def test_a_fire_the_gateway_refused_keeps_its_slot_through_an_owner_restart(gp, fired, monkeypatch):
    job = _hourly()
    await _service(monkeypatch)._try_claim_and_fire(job.id)
    # The gateway does not run the thread's session yet: it refuses.
    assert not await _service(monkeypatch).fire_handed_over(job.id, DUE, 0)
    assert load_job(job.id).next_fire_at == DUE

    await _owner_restarts(monkeypatch)

    assert load_job(job.id).next_fire_at == DUE


async def test_a_fire_that_did_not_reach_the_gateway_keeps_its_slot_through_an_owner_restart(
    gp, fired, monkeypatch,
):
    import bridge.gateway.control_channel as cc

    def unwritable(*_a, **_kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(cc, "append_command", unwritable)
    job = _hourly()
    await _service(monkeypatch)._try_claim_and_fire(job.id)
    assert load_job(job.id).next_fire_at == DUE

    await _owner_restarts(monkeypatch)

    assert load_job(job.id).next_fire_at == DUE


@pytest.mark.parametrize("session_id", [SLACK, DESKTOP])
async def test_a_job_waiting_for_the_gateway_or_the_app_keeps_its_slot_through_an_owner_restart(
    gp, fired, monkeypatch, session_id,
):
    monkeypatch.setattr(gp, "get_running_pid", lambda: None)
    monkeypatch.setattr(gp, "desktop_bridge_runs", lambda: False)
    job = _hourly(session_id)
    await _service(monkeypatch)._try_claim_and_fire(job.id)
    assert load_job(job.id).next_fire_at == DUE

    await _owner_restarts(monkeypatch)

    assert load_job(job.id).next_fire_at == DUE
    # Once it runs, the fire is handed for that slot.
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(gp, "desktop_bridge_runs", lambda: True)
    await _service(monkeypatch)._try_claim_and_fire(job.id)
    import bridge.gateway.control_channel as cc

    path = cc.desktop_commands_path() if session_id == DESKTOP else cc.commands_path()
    assert f'"scheduled_for": {DUE}' in path.read_text()
