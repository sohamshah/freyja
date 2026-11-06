"""The always-on processes own the scheduler, not the desktop bridge.

The desktop bridge lives only while the app is open, and a scheduled run
it owns dies when the app quits. Since the takeover retry, every process
without the owner lock tried it again every 15 s, the desktop bridge
too: when the gateway restarted while the app was open (rebuild.sh
re-registers the LaunchAgents), the desktop bridge took the lock and the
gateway came back without it. At boot the desktop bridge also took the
lock whenever it happened to be free.

An owner that shut down kept the lock until its in-flight runs ended,
and the gateway never let go of it before its process exited, so a peer
could not take over until then.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bridge.scheduler.daemon as daemon_mod
import bridge.scheduler.runtime as runtime
import bridge.scheduler.service as service_mod
from bridge.scheduler.models import JobRecord, NoopSinkSpec, OnceSchedule, RunRecord
from bridge.scheduler.persistence import FileLock, owner_lock_path, save_job
from bridge.scheduler.service import SchedulerService


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / ".freyja"))
    monkeypatch.setattr(daemon_mod, "is_supported_platform", lambda: True)
    monkeypatch.setattr(service_mod, "_OWNER_RETRY_SECONDS", 0.05)
    return tmp_path


def _install_launch_agent(home, label: str) -> None:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{label}.plist").write_text("<plist/>")


def _lock_is_free() -> bool:
    peer = FileLock(owner_lock_path())
    if not peer.acquire():
        return False
    peer.release()
    return True


@pytest.mark.parametrize("label", ["co.freyja.gateway", "com.freyja.scheduler"])
async def test_the_desktop_bridge_does_not_take_over_while_the_gateway_restarts(home, label):
    _install_launch_agent(home, label)
    gateway = FileLock(owner_lock_path())
    assert gateway.acquire()
    desktop = SchedulerService(SimpleNamespace())
    await desktop.start(always_on=False)

    gateway.release()
    await asyncio.sleep(0.3)

    assert not desktop._is_owner
    assert _lock_is_free()
    await desktop.stop()


async def test_the_desktop_bridge_leaves_a_free_lock_to_the_launch_agents(home):
    _install_launch_agent(home, "co.freyja.gateway")
    desktop = SchedulerService(SimpleNamespace())

    await desktop.start(always_on=False)

    assert not desktop._is_owner
    assert _lock_is_free()
    await desktop.stop()


async def test_the_desktop_bridge_owns_the_scheduler_when_no_launch_agent_is_installed(home):
    desktop = SchedulerService(SimpleNamespace())

    await desktop.start(always_on=False)

    assert desktop._is_owner
    await desktop.stop()


def _due_job() -> None:
    due = time.time() + 0.2
    job = JobRecord(
        id="sched_due",
        name="reminder",
        schedule=OnceSchedule(at_iso=datetime.fromtimestamp(due, timezone.utc).isoformat()),
        prompt="ping",
        sinks=[NoopSinkSpec()],
    )
    job.next_fire_at = due
    save_job(job)


@pytest.fixture
def slow_fire(monkeypatch):
    """A fire that runs until the test ends it."""
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fire(_service, job, **_kw):
        started.set()
        await finish.wait()
        return RunRecord(run_id="run_1", job_id=job.id, job_name=job.name,
                         started_at=time.time(), status="succeeded")

    monkeypatch.setattr(runtime, "fire_job", fire)
    return SimpleNamespace(started=started, finish=finish)


async def test_an_owner_that_stops_frees_the_lock_while_its_runs_finish(home, slow_fire):
    _due_job()
    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    await asyncio.wait_for(slow_fire.started.wait(), 5)

    stopping = asyncio.create_task(owner.stop())
    for _ in range(40):
        if _lock_is_free():
            break
        await asyncio.sleep(0.05)

    assert _lock_is_free()
    assert not stopping.done()
    slow_fire.finish.set()
    await stopping


async def test_stop_waits_only_briefly_for_a_run_that_does_not_end(home, slow_fire, monkeypatch):
    monkeypatch.setattr(service_mod, "_STOP_RUN_GRACE_SECONDS", 0.2, raising=False)
    _due_job()
    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    await asyncio.wait_for(slow_fire.started.wait(), 5)

    try:
        await asyncio.wait_for(owner.stop(), 3)
    finally:
        slow_fire.finish.set()

    assert _lock_is_free()


async def test_the_gateway_frees_the_owner_lock_when_it_shuts_down(home):
    from bridge.gateway.run import GatewayDaemon

    gateway = GatewayDaemon.__new__(GatewayDaemon)
    gateway.state = SimpleNamespace(sessions={}, scheduler=SchedulerService(SimpleNamespace()))
    gateway.control_channel = None
    gateway.adapters = []
    await gateway.state.scheduler.start()
    assert gateway.state.scheduler._is_owner

    await gateway.shutdown()

    assert _lock_is_free()
