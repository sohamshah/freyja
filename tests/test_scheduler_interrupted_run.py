"""A run whose process exited before it ended is marked interrupted, and a
one-shot it used up is due again.

The claim moves a job past its slot, and a one-shot past its only one
(disabled_max_fires), before the run. A crash, a quit app or a gateway
restart during the run left its record "running" for good, and the
one-shot used up: the reminder never ran, and its history said it still
ran. A run in fact still going in another live process holds the job's
run lock, and is left alone.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
)
from bridge.scheduler.persistence import (
    FileLock,
    job_run_lock_path,
    load_job,
    load_run,
    owner_lock_path,
    save_job,
    save_run,
)
from bridge.scheduler.service import SchedulerService

DUE = float(int(time.time()) - 2 * 3600)  # whole seconds: its ISO time reads back the same


@pytest.fixture
def fired(monkeypatch) -> list[tuple[str, float | None, int]]:
    out: list[tuple[str, float | None, int]] = []

    async def fake_fire(_service, job, *, scheduled_for=None, attempt=0, **_kw):
        out.append((job.id, scheduled_for, attempt))
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


def _service(monkeypatch) -> SchedulerService:
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


def _used_up_reminder(**fields) -> JobRecord:
    """A one-shot the claim used up, whose run started and never ended."""
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        sinks=[NoopSinkSpec()], enabled=False, status="disabled_max_fires", **fields,
    )
    save_job(job)
    _left_running(job, attempt=0)
    return job


def _left_running(job: JobRecord, *, attempt: int = 0, status: str = "running") -> RunRecord:
    run = RunRecord(run_id="run_cut", job_id=job.id, job_name=job.name,
                    started_at=DUE + 1, status=status, attempt=attempt)
    save_run(run)
    return run


async def _start_and_stop(service: SchedulerService) -> None:
    await service.start()
    await service.stop()


@pytest.mark.parametrize("policy", [None, "skip"])
async def test_a_one_shot_cut_off_mid_run_is_due_again_on_the_next_boot(fired, monkeypatch, policy):
    job = _used_up_reminder(misfire_policy=policy)

    await _start_and_stop(_service(monkeypatch))

    run = load_run(job.id, "run_cut")
    assert run.status == "interrupted"
    assert run.finished_at is not None and run.error
    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (True, "active", DUE)

    owner = _service(monkeypatch)
    await owner._try_claim_and_fire(job.id)
    await asyncio.gather(*owner._in_flight_runs.values())
    # Its own slot, so the run says how late it is.
    assert fired == [(job.id, DUE, 0)]


@pytest.mark.parametrize("status", ["claimed", "delivering"])
async def test_a_run_cut_off_before_or_after_its_turn_is_interrupted_too(fired, monkeypatch, status):
    job = _used_up_reminder()
    _left_running(job, status=status)

    await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "interrupted"
    assert load_job(job.id).next_fire_at == DUE


async def test_a_run_still_going_in_another_process_is_left_alone(fired, monkeypatch):
    job = _used_up_reminder()

    with FileLock(job_run_lock_path(job.id)) as got:
        assert got  # the live process that runs it
        await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "running"
    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (False, "disabled_max_fires")


async def test_a_one_shot_that_ran_since_is_not_run_again(fired, monkeypatch):
    # A manual run cut off after the reminder had run.
    job = _used_up_reminder(fire_count=1)
    save_run(RunRecord(run_id="run_done", job_id=job.id, job_name=job.name,
                       started_at=DUE, finished_at=DUE + 5, status="succeeded"))

    await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "interrupted"
    assert load_job(job.id).status == "disabled_max_fires"


async def test_a_recurring_job_keeps_its_next_slot(fired, monkeypatch):
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() + 1800
    save_job(job)
    _left_running(job)

    await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "interrupted"
    assert load_job(job.id).next_fire_at == job.next_fire_at
    assert fired == []


async def test_a_process_that_does_not_own_the_tick_settles_them_too(fired, monkeypatch):
    # The gateway restarted after a crash in a Slack thread's run, while the
    # scheduler daemon kept the tick.
    job = _used_up_reminder()

    with FileLock(owner_lock_path()) as got:
        assert got  # the scheduler daemon
        gateway = _service(monkeypatch)
        await gateway.start()
        assert not gateway._is_owner
        await gateway.stop()

    assert load_run(job.id, "run_cut").status == "interrupted"
    assert load_job(job.id).status == "active"


@pytest.mark.parametrize("name", ["digest", "digest " * 1000])
async def test_a_run_cut_off_before_many_newer_runs_is_interrupted_too(fired, monkeypatch, name):
    # Cut off while a run of the job held its run lock at every start since,
    # or before this sweep existed; more than ten runs came after it.
    job = JobRecord(id="sched_digest", name=name, prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() + 1800
    save_job(job)
    _left_running(job)
    for n in range(12):
        time.sleep(0.002)  # runs are ordered by file mtime
        save_run(RunRecord(run_id=f"run_{n}", job_id=job.id, job_name=name, started_at=DUE + 10 + n,
                           finished_at=DUE + 15 + n, status="succeeded"))

    await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "interrupted"
    assert {load_run(job.id, f"run_{n}").status for n in range(12)} == {"succeeded"}


# A removed job's runs stay in its history. The sweep went over the jobs that
# exist, so a run cut off before or after its job was removed said "running"
# for good. One still going in another process holds the run lock it took
# before the job was removed, and is left alone.


def _removed_job_with_a_run_cut_off(service: SchedulerService) -> JobRecord:
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), sinks=[NoopSinkSpec()])
    save_job(job)
    _left_running(job)
    return job


async def test_a_run_of_a_removed_job_cut_off_is_interrupted(fired, monkeypatch):
    service = _service(monkeypatch)
    job = _removed_job_with_a_run_cut_off(service)
    assert await service.remove_job(job.id)

    await _start_and_stop(_service(monkeypatch))

    run = load_run(job.id, "run_cut")
    assert run.status == "interrupted"
    assert run.finished_at is not None and run.error
    assert load_job(job.id) is None


async def test_a_run_of_a_removed_job_still_going_in_another_process_is_left_alone(fired, monkeypatch):
    service = _service(monkeypatch)
    job = _removed_job_with_a_run_cut_off(service)

    with FileLock(job_run_lock_path(job.id)) as got:
        assert got  # the live process that runs it, since before the removal
        assert await service.remove_job(job.id)
        await _start_and_stop(_service(monkeypatch))

    assert load_run(job.id, "run_cut").status == "running"


async def test_the_runs_of_the_last_day_that_failed_count_an_interrupted_one():
    # The dashboard counts a run cut off by a crash or a restart among the
    # runs that did not complete (FAILED_RUN_STATUSES); failed_24h, which
    # the Slack /freyja metrics and the schedule tool show, left it out.
    now = time.time()
    for n, status in enumerate(["succeeded", "failed", "interrupted", "cancelled"]):
        save_run(RunRecord(run_id=f"run_{n}", job_id="sched_digest", job_name="digest",
                           started_at=now - 60 * (n + 1), status=status))

    metrics = await SchedulerService(SimpleNamespace()).metrics()

    assert (metrics.runs_24h, metrics.succeeded_24h, metrics.failed_24h) == (4, 1, 2)
