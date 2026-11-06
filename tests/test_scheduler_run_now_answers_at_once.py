"""Run now in the app answers at once, and the app's bridge takes its next
command while the run goes on.

The bridge's command loop handles one command at a time, and its
``scheduler.run_job_now`` waited for the whole run before it answered. No
other command was handled until the run ended, and the renderer gave up
after 30 s ("Briefer failed to start: scheduler request timed out") while
the run went on.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
from bridge.scheduler.models import IntervalSchedule, JobRecord, NoopSinkSpec, RunRecord
from bridge.scheduler.persistence import FileLock, job_run_lock_path, save_job
from bridge.scheduler.service import SchedulerService


@pytest.fixture
def slow_run(monkeypatch):
    """A run that goes on until ``finish`` is set."""
    started = asyncio.Event()
    finish = asyncio.Event()
    runs: list[str | None] = []

    async def fire(_service, job, *, run_id=None, **_kw):
        runs.append(run_id)
        started.set()
        await finish.wait()
        return RunRecord(run_id=run_id or "run_x", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fire)
    return SimpleNamespace(started=started, finish=finish, runs=runs)


@pytest.fixture
def bridge(monkeypatch):
    """The app's bridge, reading its commands from a pipe as from the
    renderer. Returns a function that sends a command and the events
    emitted so far."""
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    read_end, write_end = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end, "rb", buffering=0))

    def send(cmd: dict) -> None:
        os.write(write_end, json.dumps(cmd).encode() + b"\n")

    return SimpleNamespace(service=service, events=events, send=send, close=lambda: os.close(write_end))


def _job() -> JobRecord:
    job = JobRecord(id="sched_briefer", name="Morning briefing", prompt="brief me",
                    schedule=IntervalSchedule(seconds=86400), sinks=[NoopSinkSpec()])
    job.next_fire_at = time.time() + 3600
    save_job(job)
    return job


async def _answer(events: list[dict], request_id: str, within: float = 2.0) -> dict | None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        for e in events:
            if e.get("type") == "scheduler_response" and e.get("requestId") == request_id:
                return e
        await asyncio.sleep(0.02)
    return None


async def test_run_now_answers_at_once_and_the_next_command_is_answered(bridge, slow_run):
    job = _job()
    loop = asyncio.create_task(fb._command_loop(SimpleNamespace(
        scheduler=bridge.service, active_session_id=None, sessions={},
    )))
    try:
        bridge.send({"type": "scheduler.run_job_now", "id": "req_run", "jobId": job.id})
        bridge.send({"type": "scheduler.list_jobs", "id": "req_list"})

        ran = await _answer(bridge.events, "req_run")
        listed = await _answer(bridge.events, "req_list")

        assert ran is not None, "run now was not answered while its run went on"
        assert listed is not None, "the command after run now waited for the run"
        assert "error" not in ran, ran
        assert slow_run.started.is_set()
        # The answer names the run that goes on, for the run events to follow.
        assert ran["run"]["run_id"] == slow_run.runs[0]
        assert ran["run"]["job_id"] == job.id
        # The run holds the job's run lock until it ends.
        with FileLock(job_run_lock_path(job.id)) as free:
            assert not free

        slow_run.finish.set()
        await asyncio.gather(*bridge.service._in_flight_runs.values())
        with FileLock(job_run_lock_path(job.id)) as free:
            assert free
    finally:
        slow_run.finish.set()
        bridge.close()
        await asyncio.wait_for(loop, 2)


async def test_run_now_of_a_job_that_runs_is_refused_at_once(bridge, slow_run):
    job = _job()
    state = SimpleNamespace(scheduler=bridge.service, active_session_id=None, sessions={})
    await asyncio.wait_for(fb._handle_command(
        state, {"type": "scheduler.run_job_now", "id": "req_1", "jobId": job.id},
    ), 2)
    await asyncio.wait_for(slow_run.started.wait(), 2)

    await asyncio.wait_for(fb._handle_command(
        state, {"type": "scheduler.run_job_now", "id": "req_2", "jobId": job.id},
    ), 2)

    refused = await _answer(bridge.events, "req_2")
    assert refused is not None and "is running" in refused["error"]
    assert len(slow_run.runs) == 1
    slow_run.finish.set()
    await asyncio.gather(*bridge.service._in_flight_runs.values())
    bridge.close()


async def test_the_schedule_tool_still_waits_for_the_run(slow_run, monkeypatch):
    # An agent that runs a job now reads its output in the same call.
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    job = _job()

    waiting = asyncio.create_task(service.run_job_now(job.id))
    await asyncio.wait_for(slow_run.started.wait(), 2)
    assert not waiting.done()
    slow_run.finish.set()

    run = await asyncio.wait_for(waiting, 2)
    assert run.status == "succeeded"
