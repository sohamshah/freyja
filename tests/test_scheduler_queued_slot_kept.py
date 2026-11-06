"""A slot queued behind a run of its job that still goes on is not missed:
an owner restart leaves it due, whatever the job's misfire policy.

Under overlap "queue" (and "cancel_running", until the run it asked to stop
ends) a slot that comes due while the job's last run goes on waits, due.
Nothing marked it as not missed (waiting_slot), so under misfire "skip" a new
owner's boot before that run ended took it for a slot missed while no
scheduler ran, moved past it, and the run was dropped.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import FileLock, job_run_lock_path, load_job, save_job
from bridge.scheduler.service import SchedulerService

DUE = float(int(time.time()) - 60)


def _service(monkeypatch) -> SchedulerService:
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


@pytest.mark.parametrize("overlap", ["queue", "cancel_running"])
async def test_a_slot_queued_behind_a_running_run_keeps_its_slot_through_an_owner_restart(
    monkeypatch, overlap,
):
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest", schedule=IntervalSchedule(seconds=3600),
        sinks=[NoopSinkSpec()], misfire_policy="skip", overlap_policy=overlap,
    )
    job.next_fire_at = DUE
    save_job(job)
    # The job's last run goes on, in another process.
    running = FileLock(job_run_lock_path(job.id))
    assert running.acquire()
    try:
        await _service(monkeypatch)._try_claim_and_fire(job.id)
        assert load_job(job.id).next_fire_at == DUE

        owner = _service(monkeypatch)
        await owner.start()
        assert owner._is_owner
        await owner.stop()

        assert load_job(job.id).next_fire_at == DUE
    finally:
        running.release()
