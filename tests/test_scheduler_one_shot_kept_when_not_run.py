"""A one-shot whose claimed run did not start is due again, not used up.

The claim marks a one-shot done (disabled_max_fires) before its run, as
it moves the job past its only slot. A fire handed to the gateway that
the gateway dropped, because a run of the job held its run lock, left it
so: the reminder never ran and was gone. So did a fire that never
reached the gateway's control channel, and a slot skipped under
skip_if_running while a run of the job went on.
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
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
)
from bridge.scheduler.persistence import FileLock, job_run_lock_path, load_job, save_job
from bridge.scheduler.service import SchedulerService

SLACK = "freyja:slack:T1:channel:C1:1.0"
DUE = time.time() - 60


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
def daemon(monkeypatch, tmp_path):
    """The scheduler daemon, which owns the tick, while a gateway runs in
    another process."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _reminder(session_id: str | None = SLACK, **fields) -> JobRecord:
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        execution=ExistingSession(session_id=session_id), sinks=[NoopSinkSpec()], **fields,
    )
    job.next_fire_at = DUE
    save_job(job)
    return job


def _commands() -> list[dict]:
    from bridge.gateway.control_channel import commands_path

    path = commands_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _due_again(job_id: str, slot: float, attempt: int = 0) -> None:
    saved = load_job(job_id)
    assert (saved.enabled, saved.status) == (True, "active")
    assert (saved.next_fire_at, saved.retry_attempt) == (slot, attempt)


async def test_a_one_shot_whose_handed_fire_was_dropped_is_due_again(daemon, fired, monkeypatch):
    job = _reminder(retry_attempt=1)
    await daemon._try_claim_and_fire(job.id)
    (cmd,) = _commands()
    assert load_job(job.id).status == "disabled_max_fires"

    # The gateway's end, while a run of the job (a run now) holds its run lock.
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    gateway = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(gateway, "emit_event", lambda **_kw: None)
    with FileLock(job_run_lock_path(job.id)) as got:
        assert got
        assert not await gateway.fire_handed_over(job.id, cmd["scheduled_for"], cmd["attempt"])

    assert fired == []
    _due_again(job.id, DUE, attempt=1)

    # Once that run ends it is claimed and handed again, and runs.
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    await daemon._try_claim_and_fire(job.id)
    (_, again) = _commands()
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    assert await gateway.fire_handed_over(job.id, again["scheduled_for"], again["attempt"])
    await asyncio.gather(*gateway._in_flight_runs.values())
    assert fired == [(job.id, DUE, 1)]


async def test_a_one_shot_whose_fire_did_not_reach_the_gateway_is_due_again(daemon, monkeypatch):
    import bridge.gateway.control_channel as cc

    def append_command(_cmd, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cc, "append_command", append_command)
    job = _reminder()

    await daemon._try_claim_and_fire(job.id)

    _due_again(job.id, DUE)
    # Looked at again on the loop's usual once a second, not every 0.05 s.
    assert job.id in daemon._waiting_job_ids


async def test_a_one_shot_due_while_a_run_of_it_goes_on_is_not_skipped(fired):
    service = SchedulerService(SimpleNamespace())
    job = _reminder(session_id=None, overlap_policy="skip_if_running")

    with FileLock(job_run_lock_path(job.id)) as got:
        assert got
        await service._try_claim_and_fire(job.id)
        _due_again(job.id, DUE)

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())
    assert fired == [(job.id, DUE, 0)]
    assert load_job(job.id).status == "disabled_max_fires"
