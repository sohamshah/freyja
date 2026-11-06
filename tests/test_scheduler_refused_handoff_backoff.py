"""A fire the gateway or the app keeps refusing is handed again less and
less often, not every second.

A refused fire is due again (713b404), and the scheduler that owns the
tick claimed it and handed it again on its next tick, once a second. While
the receiving process kept refusing (an app's bridge waiting on its old
one's lock, a session each side thinks the other runs), each attempt wrote
a control-channel line and a log line on both sides, for as long as it
lasted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
import bridge.scheduler.runtime as runtime
import bridge.scheduler.service as service_mod
from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec, RunRecord
from bridge.scheduler.persistence import load_job, save_job
from bridge.scheduler.service import SchedulerService

SLACK = "freyja:slack:T1:channel:C1:1.0"
DUE = float(int(time.time()) - 60)


@pytest.fixture
def fired(monkeypatch) -> list[float | None]:
    out: list[float | None] = []

    async def fake_fire(_service, job, *, scheduled_for=None, **_kw):
        out.append(scheduled_for)
        return RunRecord(run_id=f"run_{len(out)}", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fake_fire)
    return out


@pytest.fixture
def owner(monkeypatch):
    """The scheduler daemon, which owns the tick, while the gateway runs in
    another process. Waits of 50 ms up to 200 ms stand for 1 s up to 60 s."""
    import bridge.gateway.pid as gp

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
    monkeypatch.setattr(gp, "get_running_pid", lambda: os.getpid() + 1)
    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_MAX_SECONDS", 0.2)
    service = SchedulerService(SimpleNamespace(sessions={}))
    monkeypatch.setattr(service, "emit_event", lambda **_kw: None)
    return service


class _Gateway:
    """Reads its control channel, as its reader does, and runs or refuses
    each fire."""

    def __init__(self, monkeypatch) -> None:
        self.service = SchedulerService(SimpleNamespace(sessions={}))
        monkeypatch.setattr(self.service, "emit_event", lambda **_kw: None)
        self.read = 0
        self.handed_at: list[float] = []

    async def read_commands(self) -> None:
        import json

        from bridge.gateway.control_channel import commands_path

        path = commands_path()
        lines = path.read_text().splitlines() if path.exists() else []
        for line in lines[self.read:]:
            cmd = json.loads(line)
            self.handed_at.append(time.monotonic())
            await self.service.fire_handed_over(cmd["job_id"], cmd["scheduled_for"], cmd["attempt"])
        self.read = len(lines)
        await asyncio.gather(*self.service._in_flight_runs.values())


def _hourly() -> JobRecord:
    job = JobRecord(
        id="sched_digest", name="digest", prompt="digest", schedule=IntervalSchedule(seconds=3600),
        execution=ExistingSession(session_id=SLACK), sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = DUE
    save_job(job)
    return job


async def _ticks(owner: SchedulerService, gateway: _Gateway, job: JobRecord, seconds: float) -> None:
    """The owner's ticks, each claiming the due job, for ``seconds``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await owner._try_claim_and_fire(job.id)
        await gateway.read_commands()
        await asyncio.sleep(0.01)


async def test_a_fire_the_gateway_keeps_refusing_is_handed_again_less_often(
    owner, fired, monkeypatch, caplog,
):
    # The gateway does not run the thread's session: it refuses every fire.
    gateway = _Gateway(monkeypatch)
    job = _hourly()

    with caplog.at_level(logging.INFO, logger="freyja.scheduler.service"):
        await _ticks(owner, gateway, job, 1.0)

    # At the next tick, then 0.05, 0.1 and 0.2 s on, and every 0.2 s after.
    handed = gateway.handed_at
    assert 4 <= len(handed) <= 10, len(handed)
    gaps = [b - a for a, b in zip(handed, handed[1:])]
    assert gaps[1] >= 0.05 and gaps[2] >= 0.1 and all(g >= 0.2 for g in gaps[3:]), gaps
    assert fired == []
    # One log line per step, not per tick.
    again = [r for r in caplog.records if "came back" in r.getMessage()]
    assert len(again) == len(handed) - 1
    saved = load_job(job.id)
    assert (saved.next_fire_at, saved.enabled) == (DUE, True)


async def test_once_the_gateway_takes_it_the_fire_runs_at_its_slot(owner, fired, monkeypatch):
    gateway = _Gateway(monkeypatch)
    job = _hourly()
    await _ticks(owner, gateway, job, 0.5)
    assert fired == []

    # The gateway runs the thread's session now, and takes the next fire.
    async def accept() -> None:
        monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
        await gateway.read_commands()
        monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)

    end = time.monotonic() + 0.5
    while not fired and time.monotonic() < end:
        await owner._try_claim_and_fire(job.id)
        await accept()
        await asyncio.sleep(0.01)

    assert fired == [DUE]
    assert load_job(job.id).next_fire_at > time.time()


async def test_the_next_slot_is_handed_at_once(owner, fired, monkeypatch):
    # The backoff is for one slot that keeps coming back, not for the job.
    gateway = _Gateway(monkeypatch)
    job = _hourly()
    await _ticks(owner, gateway, job, 0.4)
    count = len(gateway.handed_at)

    later = load_job(job.id)
    later.next_fire_at = DUE + 30
    save_job(later)
    await owner._try_claim_and_fire(job.id)
    await gateway.read_commands()

    assert len(gateway.handed_at) == count + 1
