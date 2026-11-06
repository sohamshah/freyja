"""A one-shot job missed while no scheduler ran still runs, and says it is late.

Every job defaulted to misfire_policy "skip". When a one-shot's time
passed while no process ticked (a restart, the app closed, the
LaunchAgents down), the next owner's boot found no future fire and
disabled it: the reminder never ran and never said so. A one-shot whose
policy was never set now defaults to fire_once, and a run that starts
well after its slot tells the agent how late it is.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import JobRecord, NoopSinkSpec, OnceSchedule, RunRecord
from bridge.scheduler.persistence import load_job, save_job
from bridge.scheduler.service import SchedulerService

HOUR = 3600


@pytest.fixture
def fires(tmp_path, monkeypatch) -> list[tuple[str, float | None]]:
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    out: list[tuple[str, float | None]] = []

    async def fake_fire(_service, job, *, scheduled_for=None, **_kw):
        out.append((job.id, scheduled_for))
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


def _reminder(due: float, **fields) -> JobRecord:
    job = JobRecord(
        id="sched_reminder",
        name="call the dentist",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(due, timezone.utc).isoformat()),
        prompt="Remind me to call the dentist.",
        sinks=[NoopSinkSpec()],
        **fields,
    )
    job.next_fire_at = due
    save_job(job)
    return job


async def _run_owner(seconds: float) -> None:
    service = SchedulerService(SimpleNamespace())
    await service.start()
    assert service._is_owner
    await asyncio.sleep(seconds)
    await service.stop()


async def test_a_one_shot_missed_while_no_scheduler_ran_still_runs(fires):
    job = _reminder(time.time() - 3 * HOUR)

    await _run_owner(0.5)

    assert fires == [(job.id, job.next_fire_at)]
    assert load_job(job.id).status == "disabled_max_fires"


async def test_an_explicit_skip_still_drops_a_missed_one_shot(fires):
    job = _reminder(time.time() - 3 * HOUR, misfire_policy="skip")

    await _run_owner(0.5)

    assert fires == []
    assert load_job(job.id).status == "disabled_max_fires"


def test_a_policy_that_was_never_set_stays_unset_on_disk(tmp_path, monkeypatch):
    # Resolved and saved at creation, a one-shot later moved to a daily
    # schedule would keep fire_once instead of the daily default.
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    job = _reminder(time.time() + HOUR)

    assert load_job(job.id).misfire_policy is None


def _prompt(due: float, started_at: float) -> str:
    job = JobRecord(id="sched_reminder", name="call the dentist",
                    prompt="Remind me to call the dentist.")
    run = RunRecord(run_id="run_1", job_id=job.id, job_name=job.name,
                    started_at=started_at, fire_number=1)
    return runtime._compose_fire_prompt(job, run, SimpleNamespace(), scheduled_for=due)


def test_a_late_run_tells_the_agent_how_late_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    due = time.time() - 4 * HOUR

    prompt = _prompt(due, started_at=due + 3 * HOUR + 12 * 60)

    assert "This run is late" in prompt
    assert datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M") in prompt
    assert "3 h 12 min" in prompt
    assert prompt.index("This run is late") < prompt.index("# Your task")


def test_a_run_on_time_says_nothing_about_being_late(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    due = time.time()

    assert "This run is late" not in _prompt(due, started_at=due + 2)
