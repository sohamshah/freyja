"""Setting enabled on a job pauses or resumes it.

update_job set the enabled flag and nothing else. A paused job set
enabled=True kept status "paused", which the tick skips, so it never ran
again; a job made disabled set enabled=True kept no next fire. The
schedule tool's update action and the dashboard's edit call reach jobs
through update_job.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import IntervalSchedule, JobPatch, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_job
from bridge.scheduler.service import SchedulerService
from bridge.tools.schedule_tool import ScheduleTool


@pytest.fixture
def service(monkeypatch):
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    return svc


async def _hourly(service, **fields) -> JobRecord:
    return await service.create_job(JobRecord(
        id="sched_hourly", name="hourly", schedule=IntervalSchedule(seconds=3600),
        prompt="check", sinks=[NoopSinkSpec()], **fields,
    ))


def _runs_from_its_schedule(job: JobRecord) -> bool:
    return (
        job.enabled
        and job.status == "active"
        and job.next_fire_at is not None
        and time.time() < job.next_fire_at <= time.time() + 3600
    )


async def test_enabling_a_paused_job_resumes_it(service):
    job = await _hourly(service)
    await service.pause_job(job.id)

    updated = await service.update_job(job.id, JobPatch(enabled=True))

    assert _runs_from_its_schedule(updated)
    assert _runs_from_its_schedule(load_job(job.id))


async def test_enabling_a_job_made_disabled_gives_it_a_next_fire(service):
    job = await _hourly(service, enabled=False)
    assert job.next_fire_at is None

    assert _runs_from_its_schedule(await service.update_job(job.id, JobPatch(enabled=True)))


async def test_disabling_a_job_pauses_it(service):
    job = await _hourly(service)

    updated = await service.update_job(job.id, JobPatch(enabled=False))

    assert (updated.enabled, updated.status) == (False, "paused")
    assert (await service.metrics()).paused_jobs == 1


async def test_enabling_a_running_job_leaves_its_next_fire(service):
    job = await _hourly(service)

    updated = await service.update_job(job.id, JobPatch(enabled=True))

    assert updated.next_fire_at == job.next_fire_at


async def test_a_new_schedule_given_with_enabled_applies(service):
    job = await _hourly(service)
    await service.pause_job(job.id)

    updated = await service.update_job(job.id, JobPatch(
        enabled=True, schedule=IntervalSchedule(seconds=600),
    ))

    assert updated.status == "active"
    assert time.time() < updated.next_fire_at <= time.time() + 600


@pytest.mark.parametrize("action", [
    {"action": "update", "enabled": True},
    {"action": "resume"},
])
async def test_the_schedule_tool_resumes_a_paused_job(service, action):
    job = await _hourly(service)
    await service.pause_job(job.id)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {**action, "job_id": job.id})

    assert not result.is_error, result.content
    assert _runs_from_its_schedule(load_job(job.id))


async def test_the_slack_resume_command_says_when_the_job_runs_next(service):
    from bridge.gateway.run import GatewayDaemon, _format_ts
    from bridge.scheduler.persistence import save_job

    job = await _hourly(service)
    await service.pause_job(job.id)
    # Paused a day ago: the next fire it kept has passed.
    paused = load_job(job.id)
    paused.next_fire_at = time.time() - 24 * 3600
    save_job(paused)

    reply = await GatewayDaemon._scheduler_schedule_subcommand(
        None, f"resume {job.id}", service=service, creator=None,
    )

    assert f"Next fire: {_format_ts(load_job(job.id).next_fire_at)}" in reply
