"""The per-job flock makes a due job fire once across every scheduler process.

Three processes (desktop bridge, gateway, scheduler daemon) share the job
files on disk. A claim, a manual run and an update each take the job's
flock first, and a process that is firing keeps it for the whole run; a
peer that finds it held skips or fails fast instead of racing. A flock
belongs to an open file description, so two descriptors in one process
conflict exactly as two processes do, which is what these tests use.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import JobPatch, JobRecord, NoopSinkSpec, OnceSchedule, RunRecord
from bridge.scheduler.persistence import FileLock, job_lock_path, load_job, save_job
from bridge.scheduler.service import SchedulerService


@pytest.fixture
def fired(tmp_path, monkeypatch) -> list[str]:
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    out: list[str] = []

    async def fake_fire(_service, job):
        out.append(job.id)
        await asyncio.sleep(0)  # the run holds the lock across a loop turn
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


def _due_job(job_id: str) -> JobRecord:
    at = time.time() - 1
    job = JobRecord(
        id=job_id,
        name="digest",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(at, timezone.utc).isoformat()),
        prompt="digest",
        sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = at
    save_job(job)
    return job


async def test_two_schedulers_claiming_one_due_job_fire_it_once(fired):
    job = _due_job("sched_race")
    a = SchedulerService(SimpleNamespace())
    b = SchedulerService(SimpleNamespace())

    await asyncio.gather(a._try_claim_and_fire(job.id), b._try_claim_and_fire(job.id))
    await asyncio.gather(*a._in_flight_runs.values(), *b._in_flight_runs.values())
    # A late tick after the run finished finds the job already advanced.
    await b._try_claim_and_fire(job.id)

    assert fired == [job.id]
    assert not load_job(job.id).enabled  # a once job is spent


async def test_a_job_a_peer_is_writing_is_skipped_by_the_tick_and_its_edits_fail_fast(fired):
    """The record lock is held only while a job's record is read and written
    (a claim, an edit, a run's writes at its start and end). A tick that
    meets it held looks again next time, and an edit fails fast with
    "retry shortly" instead of clobbering the peer's write. A run holds the
    separate run lock, whose rules test_scheduler_edit_while_running covers."""
    job = _due_job("sched_busy")
    peer = FileLock(job_lock_path(job.id))
    assert peer.acquire()
    service = SchedulerService(SimpleNamespace())

    await service._try_claim_and_fire(job.id)
    assert fired == [] and not service._in_flight_runs
    assert job.id in service._waiting_job_ids
    with pytest.raises(RuntimeError, match="locked by a peer"):
        await service.update_job(job.id, JobPatch(prompt="changed"))
    assert load_job(job.id).prompt == "digest"

    peer.release()
    assert (await service.update_job(job.id, JobPatch(prompt="changed"))).prompt == "changed"
    assert fired == []
