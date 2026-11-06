"""Which process owns the scheduler can be traced from the logs.

Three processes race for the owner lock (desktop bridge, gateway,
scheduler daemon), and the event log said only "owner" or "crud_only"
with no pid. The scheduler daemon never configured Python logging, so
its info lines (the scheduler's own "booted as owner") were dropped, and
warnings reached daemon.log bare, with no time, level or logger.
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

from bridge.scheduler.persistence import events_path
from bridge.scheduler.service import SchedulerService


def _ownership_events() -> list[dict]:
    rows = [json.loads(line) for line in events_path().read_text().splitlines()]
    return [r for r in rows if r["type"] in ("scheduler_started", "scheduler_stopped")]


async def test_ownership_events_and_log_lines_carry_the_pid(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    caplog.set_level(logging.INFO, logger="freyja.scheduler.service")
    owner = SchedulerService(SimpleNamespace())
    await owner.start()
    peer = SchedulerService(SimpleNamespace())
    await peer.start()
    await peer.stop()
    await owner.stop()

    events = _ownership_events()

    assert [e.get("mode", e["type"]) for e in events] == [
        "owner", "crud_only", "scheduler_stopped", "scheduler_stopped",
    ]
    assert all(e.get("pid") == os.getpid() for e in events)
    lines = [r.getMessage() for r in caplog.records if r.name == "freyja.scheduler.service"]
    assert len(lines) == 4
    assert all(f"pid {os.getpid()}" in line for line in lines)


async def test_the_headless_daemon_sends_python_logging_to_its_launchd_log(monkeypatch, capfd):
    import bridge.freyja_bridge as fb
    import bridge.gateway.run as gateway_run

    class _Gateway:
        state = None

        async def start(self, *, listen=None):
            logging.getLogger("freyja.scheduler.service").info(
                "scheduler booted as owner (owner, pid %d): %d jobs", os.getpid(), 3,
            )

    monkeypatch.setattr(fb, "emit", lambda _event: None)
    monkeypatch.setattr(gateway_run, "GatewayDaemon", _Gateway)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        await fb._main_headless()
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
        root.setLevel(level)

    err = capfd.readouterr().err
    assert "[INFO] freyja.scheduler.service: scheduler booted as owner" in err
