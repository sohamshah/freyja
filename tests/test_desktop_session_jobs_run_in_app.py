"""A scheduled job that fires into a desktop session runs in the app.

The app's bridge loads, runs and saves desktop sessions. The scheduler
daemon often owns the scheduler's tick, and it ran a job aimed at an
existing desktop session (execution "here", from the app) on its own copy
of that session, loaded with ensure_session. While the app ran, the two
copies saved the same transcript over each other.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
import bridge.transcript_persistence as tp
from bridge.scheduler.models import (
    ExistingSession,
    IntervalSchedule,
    JobRecord,
    NoopSinkSpec,
    RunRecord,
)
from bridge.scheduler.persistence import FileLock, job_run_lock_path, load_job, save_job
from bridge.scheduler.service import SchedulerService

DESKTOP = "session-19f0desk"


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    """The scheduler daemon: neither the gateway nor the app's bridge.
    Records every session it would build."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(gp, "_desktop_lock", None)
    built: list[str] = []

    async def ensure_session(sid, **_kw):
        built.append(sid)
        raise AssertionError(f"the scheduler daemon built a copy of {sid}")

    state = SimpleNamespace(
        sessions={}, ensure_session=ensure_session,
        gateway_runner=SimpleNamespace(adapters=[]),
    )
    service = SchedulerService(state)
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service, built


@contextmanager
def _app_running():
    """The app's bridge, in its own process, holding its lock."""
    import bridge.gateway.pid as gp

    with gp.desktop_lock_path().open("a") as app:
        fcntl.flock(app.fileno(), fcntl.LOCK_EX)
        yield


def _desktop_session_job(job_id: str = "sched_desk") -> JobRecord:
    tp.save_transcript(DESKTOP, {
        "version": 1, "session_id": DESKTOP, "metadata": {},
        "transcript": {"entries": [{"id": "d1"}], "head_id": "d1"},
    })
    job = JobRecord(
        id=job_id,
        name="summarize the inbox",
        schedule=IntervalSchedule(seconds=3600),
        prompt="summarize the inbox",
        execution=ExistingSession(session_id=DESKTOP),
        sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = time.time() - 1
    save_job(job)
    return job


def _app_commands() -> list[dict]:
    from bridge.gateway.control_channel import desktop_commands_path

    path = desktop_commands_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_a_desktop_session_job_due_in_the_scheduler_daemon_goes_to_the_app_while_it_runs(daemon):
    service, built = daemon
    job = _desktop_session_job()
    slot = job.next_fire_at

    with _app_running():
        await service._try_claim_and_fire(job.id)
        await asyncio.gather(*service._in_flight_runs.values())

    assert built == []
    (cmd,) = _app_commands()
    assert (cmd["type"], cmd["job_id"], cmd["scheduled_for"], cmd["attempt"]) == (
        "scheduler_fire", job.id, slot, 0,
    )
    # Claimed here, once: the slot is spent, and the run lock is free for
    # the app to take.
    assert load_job(job.id).next_fire_at > time.time()
    run_lock = FileLock(job_run_lock_path(job.id))
    assert run_lock.acquire()
    run_lock.release()


async def test_a_desktop_session_job_waits_while_the_app_is_closed(daemon):
    service, built = daemon
    job = _desktop_session_job()

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    assert built == [] and _app_commands() == []
    assert not service._in_flight_runs
    # Still due: it fires once the app runs.
    assert load_job(job.id).next_fire_at == job.next_fire_at
    assert job.id in service._waiting_job_ids


async def test_the_app_runs_the_fire_it_was_handed(daemon, monkeypatch):
    # The app's end: the run, its record, sinks and retries are the ones
    # the owner would have made, under the job's run lock.
    import bridge.gateway.pid as gp

    service, _built = daemon
    job = _desktop_session_job()
    with _app_running():
        await service._try_claim_and_fire(job.id)
    (cmd,) = _app_commands()

    monkeypatch.setattr(gp, "_desktop_lock", object())  # this process is now the app's bridge
    fired: list[tuple] = []

    async def fake_fire(_service, fresh, **kw):
        assert not FileLock(job_run_lock_path(fresh.id)).acquire()
        fired.append((fresh.id, kw.get("scheduled_for"), kw.get("attempt")))
        return RunRecord(run_id="run_1", job_id=fresh.id, job_name=fresh.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    app_service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(app_service, "emit_event", lambda **_kw: None)

    await fb._handle_scheduler_fire_command(SimpleNamespace(scheduler=app_service), cmd)
    await asyncio.gather(*app_service._in_flight_runs.values())

    assert fired == [(job.id, cmd["scheduled_for"], 0)]


async def test_a_desktop_session_job_fires_in_the_apps_own_tick(daemon, monkeypatch):
    import bridge.gateway.pid as gp

    service, _built = daemon
    monkeypatch.setattr(gp, "_desktop_lock", object())
    fired: list[str] = []

    async def fake_fire(_service, job, **_kw):
        fired.append(job.id)
        return RunRecord(run_id="run_1", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    job = _desktop_session_job()

    await service._try_claim_and_fire(job.id)
    await asyncio.gather(*service._in_flight_runs.values())

    assert fired == [job.id] and _app_commands() == []


async def test_a_run_now_outside_the_app_goes_to_the_app(daemon):
    # The schedule tool in the scheduler daemon or the gateway, or the
    # gateway's /freyja schedule run: it failed with "its session runs in
    # the app".
    service, built = daemon
    job = _desktop_session_job()

    with _app_running():
        run = await service.run_job_now(job.id)

    assert built == [] and not service._in_flight_runs
    (cmd,) = _app_commands()
    assert (cmd["type"], cmd["job_id"], cmd["run_id"], cmd["scheduled_for"]) == (
        "scheduler_fire", job.id, run.run_id, None,
    )
    assert run.status == "claimed"
    # A manual run leaves the job's cadence alone.
    assert load_job(job.id).next_fire_at == job.next_fire_at


async def test_a_run_now_outside_the_app_while_the_app_is_closed_says_so(daemon):
    service, built = daemon
    job = _desktop_session_job()

    with pytest.raises(ValueError, match="the app is not open"):
        await service.run_job_now(job.id)

    assert built == [] and _app_commands() == [] and not service._in_flight_runs


async def test_the_app_runs_a_run_now_it_was_handed_under_its_run_id(daemon, monkeypatch):
    import bridge.gateway.pid as gp

    service, _built = daemon
    job = _desktop_session_job()
    with _app_running():
        run = await service.run_job_now(job.id)
    (cmd,) = _app_commands()

    monkeypatch.setattr(gp, "_desktop_lock", object())  # this process is now the app's bridge
    fired: list[tuple] = []

    async def fake_fire(_service, fresh, **kw):
        assert not FileLock(job_run_lock_path(fresh.id)).acquire()
        fired.append((fresh.id, kw.get("run_id"), kw.get("scheduled_for")))
        return RunRecord(run_id=kw["run_id"], job_id=fresh.id, job_name=fresh.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    app_service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(app_service, "emit_event", lambda **_kw: None)

    await fb._handle_scheduler_fire_command(SimpleNamespace(scheduler=app_service), cmd)
    await asyncio.gather(*app_service._in_flight_runs.values())

    assert fired == [(job.id, run.run_id, None)]
    assert load_job(job.id).next_fire_at == job.next_fire_at


# ── A job aimed at a sub-agent ───────────────────────────────────────

SUB = "sub_19f0desk_4"


def _sub_agent_job(job_id: str = "sched_sub") -> JobRecord:
    tp.save_transcript(SUB, {
        "version": 1, "session_id": SUB, "metadata": {"parent_session_id": DESKTOP},
        "transcript": {"entries": [{"id": "s1"}], "head_id": "s1"},
    })
    return JobRecord(
        id=job_id,
        name="check on the sub-agent",
        schedule=IntervalSchedule(seconds=3600),
        prompt="how far along are you?",
        execution=ExistingSession(session_id=SUB),
        sinks=[NoopSinkSpec()],
    )


async def test_a_job_aimed_at_a_sub_agent_is_refused_when_it_is_made(daemon):
    from bridge.scheduler.models import JobPatch

    service, _built = daemon
    with pytest.raises(ValueError, match="sub-agent"):
        await service.create_job(_sub_agent_job())
    assert load_job("sched_sub") is None

    job = await service.create_job(_desktop_session_job("sched_desk2"))
    with pytest.raises(ValueError, match="sub-agent"):
        await service.update_job(job.id, JobPatch(execution=ExistingSession(session_id=SUB)))
    assert load_job(job.id).execution.session_id == DESKTOP


async def test_a_saved_job_aimed_at_a_sub_agent_builds_no_root_copy_of_it_in_the_app(daemon, monkeypatch):
    # The app runs it, as the sub-agent's root is a desktop session. A
    # root session built on the sub-agent's id ran beside the sub-agent,
    # outside its parent, and saved over its transcript.
    import bridge.gateway.pid as gp

    service, built = daemon
    monkeypatch.setattr(gp, "_desktop_lock", object())  # this process is the app's bridge
    job = _sub_agent_job()
    save_job(job)  # made before jobs like it were refused

    run = await service.run_job_now(job.id)

    assert built == []
    assert run.status == "failed" and "sub-agent" in run.error


async def test_a_saved_job_aimed_at_a_sub_agent_is_stopped_once_when_its_owner_boots_and_says_how_to_fix_it(
    daemon, monkeypatch
):
    # Handed to the app on every fire, it failed each time until three
    # failures stopped it. Stopped at the owner's boot instead, with its
    # reason as its last run's error and a notice that names the fix.
    from bridge.scheduler.persistence import load_runs_for_job

    service, built = daemon
    job = _sub_agent_job()
    job.next_fire_at = time.time() - 1
    save_job(job)  # made before jobs like it were refused
    told: list[str] = []

    async def tell(_job, _run, notice):
        told.append(notice)

    monkeypatch.setattr(service, "_tell_stopped", tell)
    for _boot in range(2):
        await service.start()
        await asyncio.sleep(0.05)
        await service.stop()

    stopped = load_job(job.id)
    assert (stopped.enabled, stopped.status, stopped.next_fire_at) == (False, "disabled_error", None)
    (notice,) = told
    assert SUB in notice and DESKTOP in notice and "resume" in notice
    (run,) = load_runs_for_job(job.id)
    assert run.status == "failed" and "sub-agent" in run.error
    assert built == [] and _app_commands() == []
    # Resumed as it is, it would fail again: resume says why.
    with pytest.raises(ValueError, match="sub-agent"):
        await service.resume_job(job.id)
