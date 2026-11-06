"""A job's overlap policy decides what happens when it comes due while its
last run still goes on.

``overlap_policy`` was stored but never read: every job waited for its
last run and then fired (queue), whatever it said. The dashboard's menu
offered "skip", which is not a policy, and did not offer
skip_if_running; create and update took any value.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import (
    IntervalSchedule,
    JobPatch,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import (
    load_job,
    read_run_cancel_requested,
    save_job,
    save_run,
)
from bridge.scheduler.service import SchedulerService


@pytest.fixture
def runs(monkeypatch):
    """Fires that go on until ``finish`` is set, or until their run is
    asked to stop."""
    state = SimpleNamespace(started=[], finish=asyncio.Event(), cancelled=[])

    async def fire(_service, job, **_kw):
        run = RunRecord(run_id=f"run_{len(state.started) + 1}", job_id=job.id,
                        job_name=job.name, started_at=time.time(), status="running")
        save_run(run)
        state.started.append(run.run_id)
        while not state.finish.is_set():
            if read_run_cancel_requested(job.id, run.run_id):
                state.cancelled.append(run.run_id)
                run.status = "cancelled"
                break
            await asyncio.sleep(0.05)
        else:
            run.status = "succeeded"
        run.finished_at = time.time()
        save_run(run)
        return run

    monkeypatch.setattr(runtime, "fire_job", fire)
    return state


async def _start(policy: str | None, schedule=None) -> tuple[SchedulerService, JobRecord]:
    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    job = JobRecord(id="sched_digest", name="digest",
                    schedule=schedule or IntervalSchedule(seconds=3600),
                    prompt="digest", sinks=[NoopSinkSpec()], overlap_policy=policy)
    job.next_fire_at = time.time() - 1
    save_job(job)
    return owner, job


def _due_again(job: JobRecord) -> None:
    """The next slot passes while the first run goes on."""
    fresh = load_job(job.id)
    fresh.next_fire_at = time.time() - 1
    save_job(fresh)


async def _until(check, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("policy", ["queue", "", None])
async def test_queue_fires_the_slot_once_the_last_run_ends(runs, policy):
    owner, job = await _start(policy)
    await _until(lambda: runs.started)
    _due_again(job)

    await asyncio.sleep(1.2)
    assert runs.started == ["run_1"]

    runs.finish.set()
    await _until(lambda: len(runs.started) == 2)
    await owner.stop()
    assert runs.started == ["run_1", "run_2"]


# "skip" is what the dashboard saved for skip_if_running.
@pytest.mark.parametrize("policy", ["skip_if_running", "skip"])
async def test_skip_if_running_drops_the_slot(runs, policy):
    owner, job = await _start(policy)
    await _until(lambda: runs.started)
    _due_again(job)

    await asyncio.sleep(1.2)
    assert load_job(job.id).next_fire_at > time.time() + 3000
    runs.finish.set()
    await asyncio.sleep(1.2)
    await owner.stop()

    assert runs.started == ["run_1"]


async def test_cancel_running_stops_the_last_run_and_fires(runs):
    owner, job = await _start("cancel_running")
    await _until(lambda: runs.started)
    _due_again(job)

    await _until(lambda: len(runs.started) == 2)
    runs.finish.set()
    await owner.stop()

    assert runs.cancelled == ["run_1"]
    assert runs.started == ["run_1", "run_2"]


async def test_a_self_paced_loop_is_not_cancelled_when_its_placeholder_time_passes(runs):
    # The claim sets a loop's next fire to its minimum delay until the run
    # decides; a run longer than that is not "overlapping".
    owner, job = await _start("cancel_running", SelfPacedSchedule(min_delay_seconds=60))
    await _until(lambda: runs.started)
    _due_again(job)

    await asyncio.sleep(1.2)
    runs.finish.set()
    await asyncio.sleep(0.2)
    await owner.stop()

    assert runs.cancelled == []


async def test_an_unknown_overlap_policy_is_refused():
    service = SchedulerService(SimpleNamespace())
    job = await service.create_job(JobRecord(id="", name="digest", schedule=IntervalSchedule(seconds=60),
                                             prompt="digest", sinks=[NoopSinkSpec()]))

    with pytest.raises(ValueError, match="skip"):
        await service.update_job(job.id, JobPatch(overlap_policy="skip"))
    with pytest.raises(ValueError, match="skip"):
        await service.create_job(JobRecord(id="", name="digest", schedule=IntervalSchedule(seconds=60),
                                           prompt="digest", overlap_policy="skip"))

    await service.update_job(job.id, JobPatch(overlap_policy="cancel_running"))
    assert load_job(job.id).overlap_policy == "cancel_running"
