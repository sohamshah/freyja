"""A run whose session cannot be set up is written to events.jsonl as failed,
like every other run that ends.

fire_job ended such a run as failed and sent scheduler_run_failed to the
app, but wrote no line to the scheduler's events.jsonl: the audit log had
its claim and nothing after, as if it still ran.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    IntervalSchedule,
    JobRecord,
    NewSession,
    NoopSinkSpec,
    SelfPacedSchedule,
)
from bridge.scheduler.persistence import load_job, load_run, read_recent_events, save_job
from bridge.scheduler.runtime import fire_job


async def test_a_run_whose_session_cannot_be_set_up_writes_its_failure_to_the_events_log():
    async def ensure_session(_session_id, **_kw):
        raise OSError("No space left on device")

    told: list[dict] = []
    service = SimpleNamespace(
        state=SimpleNamespace(ensure_session=ensure_session),
        emit_event=lambda **kw: told.append(kw),
    )
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), execution=NewSession(),
                    sinks=[NoopSinkSpec()])
    save_job(job)

    run = await fire_job(service, job)

    assert load_run(job.id, run.run_id).status == "failed"
    assert [e["subtype"] for e in told] == ["scheduler_run_claimed", "scheduler_run_failed"]
    ended = [e for e in read_recent_events() if e.get("run_id") == run.run_id]
    assert [e["type"] for e in ended] == ["scheduler_run_claimed", "scheduler_run_failed"]
    assert ended[-1]["job_id"] == job.id


async def test_a_run_whose_session_cannot_be_set_up_counts_as_a_fire():
    # Every other run that ends counts, a crashed one too: this one did not,
    # so a job with max_fires fired past it and its last fire stayed.
    async def ensure_session(_session_id, **_kw):
        raise OSError("No space left on device")

    service = SimpleNamespace(
        state=SimpleNamespace(ensure_session=ensure_session),
        emit_event=lambda **_kw: None,
    )
    job = JobRecord(id="sched_digest", name="digest", prompt="digest",
                    schedule=IntervalSchedule(seconds=3600), execution=NewSession(),
                    sinks=[NoopSinkSpec()], max_fires=1)
    save_job(job)

    run = await fire_job(service, job)

    saved = load_job(job.id)
    assert saved.fire_count == 1
    assert saved.last_fire_at == run.started_at
    assert (saved.enabled, saved.status) == (False, "disabled_max_fires")


async def test_a_loop_whose_session_cannot_be_set_up_waits_its_maximum_delay():
    # Its next fire after the claim is its minimum delay, a placeholder its
    # run replaces; this run replaced nothing, so the loop ran again at once.
    async def ensure_session(_session_id, **_kw):
        raise OSError("No space left on device")

    service = SimpleNamespace(
        state=SimpleNamespace(ensure_session=ensure_session),
        emit_event=lambda **_kw: None,
    )
    job = JobRecord(id="sched_loop", name="watch the deploy", prompt="watch",
                    schedule=SelfPacedSchedule(min_delay_seconds=60, max_delay_seconds=1800),
                    execution=NewSession(), sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() + 60
    save_job(job)

    await fire_job(service, job)

    assert load_job(job.id).next_fire_at == pytest.approx(time.time() + 1800, abs=5)
