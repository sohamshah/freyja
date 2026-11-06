"""A one-shot resumed while its fire is on its way to the gateway or the app
is refused, and runs once.

The claim uses a one-shot up and hands its fire to the process that runs
its session, which takes the job's run lock when it reads the fire: a
quarter second later, or when it is back from a restart. A resume in
between found no run lock held and no run since the one-shot's time, so
it made the one-shot due again, the owner claimed it again, and the
reminder ran twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
import bridge.transcript_persistence as tp
from bridge.scheduler.models import (
    ExistingSession,
    JobPatch,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

SLACK = "freyja:slack:T1:channel:C1:1.0"
DESKTOP = "session-19f0desk"
DUE = float(int(time.time()) - 60)


@pytest.fixture
def fired(monkeypatch) -> list[float | None]:
    out: list[float | None] = []

    async def fake_fire(_service, job, *, scheduled_for=None, **_kw):
        out.append(scheduled_for)
        run = RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                        started_at=time.time(), finished_at=time.time(), status="succeeded")
        save_run(run)
        return run

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


@pytest.fixture
def owner(monkeypatch, tmp_path):
    """The scheduler daemon, which owns the tick, while the gateway and the
    app run in other processes and read their control channels."""
    import bridge.gateway.control_channel as cc
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(gp, "_desktop_lock", None)
    monkeypatch.setattr(gp, "desktop_bridge_runs", lambda: True)
    cc.start_reading_at_end_if_new(cc.commands_path(), cc.commands_offset_path())
    cc.start_reading_at_end_if_new(cc.desktop_commands_path(), cc.desktop_commands_offset_path())
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _reminder(session_id: str) -> JobRecord:
    if session_id == DESKTOP:
        tp.save_transcript(DESKTOP, {
            "version": 1, "session_id": DESKTOP, "metadata": {},
            "transcript": {"entries": [{"id": "d1"}], "head_id": "d1"},
        })
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        execution=ExistingSession(session_id=session_id), sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = DUE
    save_job(job)
    return job


def _paths(session_id: str):
    import bridge.gateway.control_channel as cc

    if session_id == DESKTOP:
        return cc.desktop_commands_path(), cc.desktop_commands_offset_path()
    return cc.commands_path(), cc.commands_offset_path()


def _unread(session_id: str) -> list[dict]:
    path, offset = _paths(session_id)
    data = path.read_bytes()[int(offset.read_text() or 0):] if path.exists() else b""
    return [json.loads(line) for line in data.splitlines() if line.strip()]


async def _read_by_receiver(session_id: str, monkeypatch) -> None:
    """The gateway or the app reads its control channel, as its reader does:
    it fires each command, then saves where it got to."""
    path, offset = _paths(session_id)
    cmds = _unread(session_id)
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: session_id == SLACK)
    import bridge.gateway.pid as gp

    monkeypatch.setattr(gp, "_desktop_lock", object() if session_id == DESKTOP else None)
    receiver = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(receiver, "emit_event", lambda **_kw: None)
    for cmd in cmds:
        await receiver.fire_handed_over(cmd["job_id"], cmd["scheduled_for"], cmd["attempt"])
    offset.write_text(str(path.stat().st_size))
    await asyncio.gather(*receiver._in_flight_runs.values())
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "_desktop_lock", None)


@pytest.mark.parametrize("session_id", [SLACK, DESKTOP])
@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_one_shot_resumed_while_its_fire_is_handed_over_runs_once(
    owner, fired, monkeypatch, session_id, how,
):
    job = _reminder(session_id)
    await owner._try_claim_and_fire(job.id)
    assert len(_unread(session_id)) == 1
    assert load_job(job.id).status == "disabled_max_fires"

    with pytest.raises(ValueError, match="its run is starting"):
        if how == "resume_job":
            await owner.resume_job(job.id)
        else:
            await owner.update_job(job.id, JobPatch(enabled=True))
    assert load_job(job.id).status == "disabled_max_fires"
    await owner._try_claim_and_fire(job.id)
    assert len(_unread(session_id)) == 1

    await _read_by_receiver(session_id, monkeypatch)
    # And the ticks after.
    await owner._try_claim_and_fire(job.id)
    await _read_by_receiver(session_id, monkeypatch)

    assert fired == [DUE]


async def test_a_one_shot_whose_fire_was_read_resumes_as_before(owner, fired, monkeypatch):
    # Its run failed; the fire is no longer on its way.
    job = _reminder(SLACK)
    await owner._try_claim_and_fire(job.id)
    path, offset = _paths(SLACK)
    offset.write_text(str(path.stat().st_size))

    resumed = await owner.resume_job(job.id)

    assert (resumed.enabled, resumed.status, resumed.next_fire_at) == (True, "active", DUE)
