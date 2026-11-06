"""A run that fails for a passing reason does not stop its job.

The Morning briefing (cron 06:00) and the Routing radar (cron 05:00) each
fired three times while the Mac was in DarkWake: the network was down and
every run failed with "Connection error" in seconds. After three failed
runs in a row the scheduler stopped each job for good (disabled_error),
nothing ever enabled them again, and the briefing's notice went to its
noop sink, so no briefing ran from Nov 2 on and nobody was told.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bridge.scheduler.runtime as runtime
import bridge.scheduler.service as service_mod
from bridge.scheduler.models import (
    CreatorRef,
    CronSchedule,
    DeliveryReport,
    DesktopSinkSpec,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    OnceSchedule,
    RunRecord,
)
from bridge.scheduler.persistence import load_job, save_job, save_run
from bridge.scheduler.service import SchedulerService

# The error the three runs of each job recorded.
CONNECTION_ERROR = "the turn failed before the model replied — unknown: Connection error."
REAL_ERROR = "the turn failed before the model replied — format: Error code: 400 - tools.27.custom.name: String should match pattern"


def _briefer(**fields) -> JobRecord:
    job = JobRecord(
        id="sched_briefer",
        name="Morning briefing",
        creator=CreatorRef(surface="api", session_id="briefing:boot"),
        schedule=CronSchedule(expression="0 6 * * *", timezone="UTC"),
        prompt="brief me",
        sinks=[NoopSinkSpec()],
        tags=["briefer", "system"],
        **fields,
    )
    job.next_fire_at = time.time() + 6 * 3600
    save_job(job)
    return job


def _run(job: JobRecord, n: int, *, status: str = "failed", error: str | None = CONNECTION_ERROR,
         reports: list[DeliveryReport] | None = None) -> RunRecord:
    run = RunRecord(run_id=f"run_{n}", job_id=job.id, job_name=job.name,
                    started_at=time.time(), status=status, error=error,
                    delivery_reports=reports or [])
    save_run(run)
    time.sleep(0.01)  # runs are ordered by file mtime
    return run


@pytest.fixture
def service(monkeypatch):
    posted: list[tuple[list, str]] = []

    async def deliver_all(job, run, output, ctx):
        posted.append((list(job.sinks), output.text))
        return []

    import bridge.scheduler.sinks as sinks

    monkeypatch.setattr(sinks, "deliver_all", deliver_all)
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    svc.posted = posted
    return svc


@pytest.mark.parametrize("error", [
    CONNECTION_ERROR,
    "the turn failed before the model replied — timeout: Request timed out.",
    "the turn failed before the model replied — rate_limit: Error code: 529 - "
    "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}",
    "the turn failed before the model replied — unknown: Error code: 502 - Bad Gateway",
    "agent turn failed: peer closed connection without sending complete message body",
])
async def test_runs_that_failed_for_a_passing_reason_do_not_stop_the_job(service, error):
    job = _briefer()
    for n in (1, 2, 3, 4):
        assert not await service._stop_if_failing(job, _run(job, n, error=error))

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (True, "active")
    assert service.posted == []


async def test_a_sink_that_could_not_connect_is_a_passing_failure(service):
    job = _briefer()
    down = DeliveryReport(sink_index=0, sink_kind="slack", success=False, delivered_at=time.time(),
                          error="Cannot connect to host slack.com:443 ssl:default "
                                "[nodename nor servname provided, or not known]")
    for n in (1, 2, 3):
        assert not await service._stop_if_failing(job, _run(job, n, error=None, reports=[down]))


async def test_real_errors_between_passing_failures_still_stop_the_job(service):
    job = _briefer()
    for n, error in enumerate([REAL_ERROR, CONNECTION_ERROR, REAL_ERROR, CONNECTION_ERROR]):
        assert not await service._stop_if_failing(job, _run(job, n, error=error))

    assert await service._stop_if_failing(job, _run(job, 9, error=REAL_ERROR))
    assert load_job(job.id).status == "disabled_error"


async def test_a_job_that_fails_for_a_passing_reason_for_a_day_is_stopped(service):
    job = _briefer()
    limit = service_mod.MAX_PASSING_FAILURES_IN_A_ROW
    for n in range(limit - 1):
        assert not await service._stop_if_failing(job, _run(job, n))

    assert await service._stop_if_failing(job, _run(job, limit))
    assert "Connection error" in service.posted[0][1]


# ─── a passing failure is retried soon ────────────────────────────────


async def _fire_failing(service, job, monkeypatch, error=CONNECTION_ERROR):
    async def fire(_service, fired_job, **_kw):
        return _run(fired_job, int(time.time() * 1000), error=error)

    monkeypatch.setattr(runtime, "fire_job", fire)
    await service._fire_under_lock(job, None)
    return load_job(job.id)


async def test_a_passing_failure_runs_the_job_again_two_minutes_later(service, monkeypatch):
    job = _briefer()

    saved = await _fire_failing(service, job, monkeypatch)

    assert saved.next_fire_at == pytest.approx(time.time() + 120, abs=5)


async def test_the_retry_waits_twice_as_long_after_each_passing_failure(service, monkeypatch):
    job = _briefer()
    _run(job, 1)
    _run(job, 2)

    saved = await _fire_failing(service, job, monkeypatch)

    assert saved.next_fire_at == pytest.approx(time.time() + 480, abs=5)


async def test_the_retry_never_comes_after_the_next_slot(service, monkeypatch):
    job = JobRecord(id="sched_often", name="uptime", schedule=IntervalSchedule(seconds=60),
                    prompt="check", sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() + 60
    save_job(job)

    saved = await _fire_failing(service, job, monkeypatch)

    assert saved.next_fire_at == pytest.approx(time.time() + 60, abs=5)


async def test_a_one_shot_reminder_that_failed_for_a_passing_reason_runs_again(service, monkeypatch):
    job = JobRecord(id="sched_once", name="reminder", schedule=OnceSchedule(at_iso="2026-01-01T09:00:00+00:00"),
                    prompt="ping", sinks=[NoopSinkSpec()], fire_count=1,
                    enabled=False, status="disabled_max_fires")
    save_job(job)

    saved = await _fire_failing(service, job, monkeypatch)

    assert (saved.enabled, saved.status) == (True, "active")
    assert saved.next_fire_at == pytest.approx(time.time() + 120, abs=5)


async def test_a_real_error_is_not_retried_by_this_rule(service, monkeypatch):
    job = _briefer()
    slot = job.next_fire_at

    saved = await _fire_failing(service, job, monkeypatch, error=REAL_ERROR)

    assert saved.next_fire_at == pytest.approx(slot, abs=1)


async def test_a_job_paused_during_its_run_is_not_retried(service, monkeypatch):
    job = _briefer()
    paused = load_job(job.id)
    paused.enabled, paused.status, paused.next_fire_at = False, "paused", None
    save_job(paused)

    saved = await _fire_failing(service, job, monkeypatch)

    assert (saved.enabled, saved.status, saved.next_fire_at) == (False, "paused", None)


# ─── jobs stopped by the old rule run again ───────────────────────────


def _stopped(job: JobRecord) -> None:
    job.enabled, job.status, job.next_fire_at = False, "disabled_error", None
    save_job(job)


async def test_a_job_stopped_only_by_passing_failures_runs_again_after_a_boot():
    job = _briefer()
    _run(job, 0, status="succeeded", error=None)
    for n in (1, 2, 3):
        _run(job, n)
    _stopped(job)

    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    await owner.stop()

    saved = load_job(job.id)
    assert (saved.enabled, saved.status) == (True, "active")
    assert time.time() < saved.next_fire_at <= time.time() + 24 * 3600


@pytest.mark.parametrize("hand_off", ["paused", "real_errors", "no_runs"])
async def test_other_stopped_jobs_stay_stopped_after_a_boot(hand_off):
    job = _briefer()
    if hand_off == "paused":
        for n in (1, 2, 3):
            _run(job, n)
        job.enabled, job.status, job.next_fire_at = False, "paused", None
        save_job(job)
    else:
        if hand_off == "real_errors":
            for n in (1, 2, 3):
                _run(job, n, error=REAL_ERROR)
        _stopped(job)

    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    await owner.stop()

    saved = load_job(job.id)
    assert not saved.enabled
    assert saved.status == ("paused" if hand_off == "paused" else "disabled_error")


# ─── runs wait while the Mac is in DarkWake or offline ────────────────


async def test_a_due_job_waits_while_the_mac_cannot_run_it(monkeypatch):
    fired: list[str] = []

    async def fire(_service, job, **_kw):
        fired.append(job.id)
        return RunRecord(run_id="run_x", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fire)
    reason = ["the Mac is in DarkWake"]
    monkeypatch.setattr(service_mod, "why_runs_wait", lambda: reason[0])
    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    # The 06:00 slot comes while the owner runs, the Mac in DarkWake.
    job = _briefer()
    job.next_fire_at = time.time() - 1
    save_job(job)
    try:
        await asyncio.sleep(1.2)
        assert fired == []
        assert load_job(job.id).next_fire_at < time.time()

        reason[0] = None
        await asyncio.sleep(1.5)
    finally:
        await owner.stop()

    assert fired == [job.id]


def test_dark_wake_is_a_wake_without_graphics():
    from bridge.scheduler import machine

    full_wake = 0x1F  # CPU, video, audio, network, disk: pmset's "CDNVA"
    dark_wake = 0x39  # CPU, disk, network, push service: pmset's "CDNP"
    assert not machine.dark_wake_from(full_wake)
    assert machine.dark_wake_from(dark_wake)
    assert not machine.dark_wake_from(0)  # unknown


def test_reachability_flags():
    from bridge.scheduler import machine

    assert machine.online_from(0x2)          # reachable
    assert machine.online_from(0x3)          # reachable, over a VPN
    assert not machine.online_from(0x0)      # no route
    assert not machine.online_from(0x2 | 0x4)  # reachable once a connection is made
    assert machine.online_from(0x2 | 0x4 | 0x8)  # ... which traffic makes by itself


def test_the_checks_run_on_this_machine():
    from bridge.scheduler import machine

    assert machine.why_runs_wait() in (None, "the Mac is in DarkWake", "the network is unreachable")


# ─── a job stopped for real errors tells someone ──────────────────────


async def test_a_stopped_job_whose_sinks_reach_nobody_tells_the_conversation_it_came_from(service):
    job = JobRecord(id="sched_file", name="nightly notes", schedule=CronSchedule(expression="0 1 * * *"),
                    creator=CreatorRef(surface="desktop", session_id="session-abc"),
                    prompt="write notes", sinks=[NoopSinkSpec()])
    save_job(job)
    for n in (1, 2):
        await service._stop_if_failing(job, _run(job, n, error=REAL_ERROR))

    assert await service._stop_if_failing(job, _run(job, 3, error=REAL_ERROR))

    sinks, text = service.posted[0]
    assert DesktopSinkSpec(session_id="session-abc") in sinks
    assert "nightly notes" in text
