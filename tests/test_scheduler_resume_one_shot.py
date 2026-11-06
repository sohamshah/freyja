"""A one-shot resumed after its time passed runs once, late, unless it ran.

resume_job set the next fire from the schedule, which has no slot left
once a one-shot's time has passed: the job was marked done
(disabled_max_fires) without running, and the resume said it worked.
That hit a one-shot paused over its time, one stopped by abort_job on
its daily budget, and one stopped for failing. A one-shot that ran is
refused, and the refusal says when it ran.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bridge.scheduler.models import (
    BudgetSpec,
    DeliveryReport,
    ExistingSession,
    JobPatch,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
)
from bridge.scheduler.persistence import (
    FileLock,
    job_run_lock_path,
    load_job,
    save_job,
    save_run,
)
from bridge.scheduler.service import SchedulerService
from engine.session import TranscriptManager
from engine.tools import ToolRegistry
from engine.types import Message

HOUR = 3600


@pytest.fixture
def service(monkeypatch):
    """A scheduler whose jobs' turns reply, and that keeps their prompts."""
    import bridge.freyja_bridge as fb
    import bridge.scheduler.sinks as sinks

    sess = SimpleNamespace(
        tool_registry=ToolRegistry(),
        pending_task=None,
        session=SimpleNamespace(transcript=TranscriptManager()),
        _save_transcript=lambda: None,
    )
    prompts: list[str] = []

    async def turn(prompt: str) -> None:
        prompts.append(prompt)
        sess.session.transcript.append_message(Message(role="user", content=prompt))
        sess.session.transcript.append_message(Message(role="assistant", content="Done."))

    def schedule_turn(s, prompt, attachments=None, on_turn_start=None):
        s.pending_task = asyncio.ensure_future(turn(prompt))
        return True

    async def quiescent(s, **_kw):
        await s.pending_task

    async def deliver_all(job, run, output, ctx):
        return [DeliveryReport(sink_index=i, sink_kind=s.kind, success=True, delivered_at=time.time())
                for i, s in enumerate(job.sinks)]

    monkeypatch.setattr(fb, "_schedule_or_queue_turn", schedule_turn)
    monkeypatch.setattr(fb, "wait_until_quiescent", quiescent)
    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    svc = SchedulerService(SimpleNamespace(sessions={"s1": sess}, permission_tier="low"))
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    svc.prompts = prompts
    return svc


DUE = float(int(time.time()) - 2 * HOUR)  # whole seconds: its ISO time reads back the same


def _reminder(**fields) -> JobRecord:
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        execution=ExistingSession(session_id="s1"), sinks=[NoopSinkSpec()], **fields,
    )
    save_job(job)
    return job


async def _tick(service: SchedulerService, job_id: str) -> JobRecord:
    await service._try_claim_and_fire(job_id)
    await asyncio.gather(*service._in_flight_runs.values())
    return load_job(job_id)


async def _resume(service: SchedulerService, job_id: str, how: str) -> JobRecord:
    if how == "resume_job":
        return await service.resume_job(job_id)
    return await service.update_job(job_id, JobPatch(enabled=True))


def _ran(job: JobRecord, status: str, *, at: float, n: int = 1, cost: float = 0.0) -> None:
    save_run(RunRecord(run_id=f"run_{n}", job_id=job.id, job_name=job.name, started_at=at,
                       finished_at=at + 5, status=status, cost_usd=cost,
                       error="the sheet is gone" if status == "failed" else None))
    time.sleep(0.01)  # runs are ordered by file mtime


async def _runs_late_once(service: SchedulerService, job_id: str) -> None:
    resumed = load_job(job_id)
    assert (resumed.enabled, resumed.status, resumed.next_fire_at) == (True, "active", DUE)

    ran = await _tick(service, job_id)

    [prompt] = service.prompts
    assert "This run is late" in prompt
    assert (ran.enabled, ran.status) == (False, "disabled_max_fires")
    again = await _tick(service, job_id)
    assert len(service.prompts) == 1 and again.status == "disabled_max_fires"


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_one_shot_paused_over_its_time_runs_late_when_resumed(service, how):
    job = _reminder(enabled=False, status="paused")

    await _resume(service, job.id, how)

    await _runs_late_once(service, job.id)


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_one_shot_stopped_on_its_daily_budget_runs_late_when_resumed(service, how):
    # Its first run failed after spending the day's budget; abort_job
    # stopped its retry before it started.
    job = _reminder(budget=BudgetSpec(max_cost_usd_per_day=0.60, on_exceeded="abort_job"))
    _ran(job, "failed", at=DUE + 60, cost=0.70)
    job = load_job(job.id)
    job.next_fire_at = DUE
    save_job(job)
    stopped = await _tick(service, job.id)
    assert (stopped.enabled, stopped.status) == (False, "disabled_budget_exhausted")
    await service.update_job(job.id, JobPatch(clear_budget=True))

    await _resume(service, job.id, how)

    await _runs_late_once(service, job.id)


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_one_shot_stopped_for_failing_runs_late_when_resumed(service, how):
    job = _reminder(enabled=False, status="disabled_error", fire_count=3)
    for n in range(1, 4):
        _ran(job, "failed", at=DUE + n * 60, n=n)

    await _resume(service, job.id, how)

    await _runs_late_once(service, job.id)


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_resumed_one_shot_under_skip_runs_after_an_owner_restart(service, monkeypatch, how):
    # Under skip, the boot of a new owner moved past a due slot it took for
    # one missed while no scheduler ran: the resumed one-shot was used up.
    job = _reminder(enabled=False, status="paused", misfire_policy="skip")
    await _resume(service, job.id, how)

    restarted = SchedulerService(service.state)
    monkeypatch.setattr(restarted, "emit_event", lambda **_kw: None)
    await restarted.start()
    assert restarted._is_owner
    await restarted.stop()

    await _runs_late_once(service, job.id)


@pytest.mark.parametrize("how", ["resume_job", "enabled=True"])
async def test_a_one_shot_that_ran_is_not_resumed_and_says_when_it_ran(service, how):
    job = _reminder()
    job.next_fire_at = DUE
    save_job(job)
    done = await _tick(service, job.id)
    assert (done.status, done.fire_count) == ("disabled_max_fires", 1)

    with pytest.raises(ValueError, match="already ran at .*give it a new time first"):
        await _resume(service, job.id, how)

    saved = load_job(job.id)
    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "disabled_max_fires", None)
    assert len(service.prompts) == 1


async def test_a_one_shot_is_not_resumed_while_its_run_goes_on(service):
    # Resumed during the run of its slot, it ran a second time after.
    job = _reminder(enabled=False, status="disabled_max_fires")

    with FileLock(job_run_lock_path(job.id)) as got:
        assert got
        with pytest.raises(ValueError, match="a run of it is going on"):
            await service.resume_job(job.id)

    assert load_job(job.id).status == "disabled_max_fires"


async def test_a_one_shot_given_a_new_time_resumes_to_it(service):
    job = _reminder(enabled=False, status="disabled_max_fires", fire_count=1)
    _ran(job, "succeeded", at=DUE + 1)
    later = time.time() + HOUR

    updated = await service.update_job(job.id, JobPatch(
        enabled=True,
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(later, timezone.utc).isoformat()),
    ))

    assert (updated.enabled, updated.status) == (True, "active")
    assert updated.next_fire_at == pytest.approx(later, abs=1)


async def test_the_schedule_tool_and_slack_say_a_one_shot_already_ran(service):
    from bridge.gateway.run import GatewayDaemon
    from bridge.tools.schedule_tool import ScheduleTool

    job = _reminder(enabled=False, status="disabled_max_fires", fire_count=1)
    _ran(job, "succeeded", at=DUE + 1)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "resume", "job_id": job.id})
    reply = await GatewayDaemon._scheduler_schedule_subcommand(
        None, f"resume {job.id}", service=service, creator=None,
    )

    assert result.is_error
    when = datetime.fromtimestamp(DUE + 1).strftime("%Y-%m-%d %H:%M")
    for said in (result.content, reply):
        assert f"already ran at {when}" in said
