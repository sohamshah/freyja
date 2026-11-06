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
            # On the owner's clock, which a test may move itself.
            self.handed_at.append(service_mod.time.monotonic())
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
    # The owner's real waits, on a clock the test moves a second each tick:
    # timed by the wall clock, a slow tick on a busy machine shortened the
    # gap seen to the next one, and the test failed now and then.
    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_FIRST_SECONDS", 1.0)
    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_MAX_SECONDS", 60.0)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(service_mod, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock.now))
    gateway = _Gateway(monkeypatch)
    job = _hourly()

    with caplog.at_level(logging.INFO, logger="freyja.scheduler.service"):
        for _ in range(200):
            await owner._try_claim_and_fire(job.id)
            await gateway.read_commands()
            clock.now += 1

    # At the next tick, then 1, 2, 4 ... 32 s on, and every 60 s after.
    handed = gateway.handed_at
    gaps = [b - a for a, b in zip(handed, handed[1:])]
    assert gaps == [1, 1, 2, 4, 8, 16, 32, 60, 60], gaps
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


async def test_a_one_shot_that_ran_after_many_refusals_and_is_resumed_is_handed_at_once(
    owner, monkeypatch,
):
    # The backoff is for one fire that keeps coming back. A one-shot keeps
    # its only slot: refused many times, then run and failed, then resumed,
    # it waited out the old backoff, and each refusal after that a minute.
    from datetime import datetime, timezone

    from bridge.scheduler.models import OnceSchedule
    from bridge.scheduler.persistence import save_run

    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_FIRST_SECONDS", 0.02)
    monkeypatch.setattr(service_mod, "_REFUSED_RETRY_MAX_SECONDS", 60.0)

    async def failing_fire(_service, job, **_kw):
        run = RunRecord(run_id="run_1", job_id=job.id, job_name=job.name, started_at=time.time(),
                        finished_at=time.time(), status="failed", error="the sheet is gone")
        save_run(run)
        return run

    monkeypatch.setattr(runtime, "fire_job", failing_fire)
    job = JobRecord(
        id="sched_reminder", name="call the dentist", prompt="Remind me to call the dentist.",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(DUE, timezone.utc).isoformat()),
        execution=ExistingSession(session_id=SLACK), sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = DUE
    save_job(job)
    gateway = _Gateway(monkeypatch)

    async def ticks(handed: int, *, taken: bool = False, seconds: float = 5.0) -> None:
        """The owner's ticks until the gateway was handed ``handed`` fires;
        it runs them when ``taken``, and refuses them otherwise."""
        end = time.monotonic() + seconds
        while len(gateway.handed_at) < handed and time.monotonic() < end:
            await owner._try_claim_and_fire(job.id)
            monkeypatch.setattr(fb, "_process_owns_gateway", lambda: taken)
            await gateway.read_commands()
            monkeypatch.setattr(fb, "_process_owns_gateway", lambda: False)
            await asyncio.sleep(0.01)

    # Refused seven times, then run: it would be handed again 1.28 s later.
    await ticks(7)
    await ticks(8, taken=True)
    assert len(gateway.handed_at) == 8
    assert load_job(job.id).status == "disabled_max_fires"

    await owner.resume_job(job.id)
    await ticks(10, seconds=0.5)

    # Handed at once, and again at the next look after a refusal.
    assert len(gateway.handed_at) == 10
