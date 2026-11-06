"""The emergency stop stops the work of every Freyja process.

It reached only the app's bridge. The Slack gateway and the scheduler
daemon run turns too, and their turns, queues and sub-agents ran on. The
app's bridge now sends the stop to the gateway on the control channel, and
to the scheduler daemon, which reads no control channel, by a marker file
it looks at every second. Each stops everything it runs, once, and tells
the app's bridge, which reports which processes confirmed the stop. A stop
sent before a process started stops nothing there: what it was for went
with the old process, and it would stop work started since.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import bridge.freyja_bridge as fb
from bridge.gateway.control_channel import (
    ControlChannelReader,
    commands_path,
    desktop_commands_path,
    emergency_stop_marker_path,
)

SLACK = "freyja:slack:T1:channel:C1:1.0"


@pytest.fixture
def events(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(fb, "emit", seen.append)
    monkeypatch.setattr(ControlChannelReader, "POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(fb, "_EMERGENCY_STOP_WATCH_SECONDS", 0.01)
    return seen


def _gateway_running(monkeypatch, pid: int | None) -> None:
    import bridge.gateway.pid as gp

    monkeypatch.setattr(gp, "get_running_pid", lambda: pid)


def _the_gateway(monkeypatch, tmp_path):
    """The Slack gateway, run in this process: it holds the gateway lock,
    and the app's bridge sees it run with another pid."""
    from bridge.gateway.run import GatewayDaemon

    monkeypatch.setattr(fb, "_process_owns_gateway", lambda: True)
    _gateway_running(monkeypatch, os.getpid() + 1)
    gateway = GatewayDaemon()
    gateway.state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    return gateway


async def _busy_session(state, sid: str):
    """A session with a turn running, a turn queued behind it, and a Slack
    consumer streaming the running turn. Returns it, the turns that ran,
    and the consumer's aborts."""
    sess = await state.ensure_session(sid)
    ran: list[str] = []

    async def run_turn(content, _attachments=None, **_kw):
        ran.append(content)
        await asyncio.sleep(60)

    sess.run_turn = run_turn
    aborts: list[dict] = []
    sess._active_stream_consumer = SimpleNamespace(finalized=False, abort=lambda **kw: aborts.append(kw))
    fb._schedule_or_queue_turn(sess, "running")
    fb._schedule_or_queue_turn(sess, "queued")
    for _ in range(200):
        if ran:
            break
        await asyncio.sleep(0)
    return sess, ran, aborts


async def _until(done, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError("never happened")
        await asyncio.sleep(0.01)


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _app_stops(tmp_path, reason: str = "user"):
    """The app's bridge, with nothing of its own running, fires the
    emergency stop. Returns its state."""
    app = fb._BridgeState(workspace=str(tmp_path / "app"), default_model="kimi-k3-fast")
    await fb._handle_command(app, {"type": "computer.emergency_stop", "reason": reason})
    return app


async def test_the_apps_emergency_stop_goes_to_the_gateway_and_the_scheduler_daemon(events, monkeypatch, tmp_path):
    _gateway_running(monkeypatch, os.getpid() + 1)
    await _app_stops(tmp_path)
    (cmd,) = _lines(commands_path())
    assert cmd["type"] == "emergency_stop" and cmd["stopId"] and cmd["at"] <= time.time()
    marker = json.loads(emergency_stop_marker_path().read_text())
    assert (marker["stopId"], marker["at"]) == (cmd["stopId"], cmd["at"])


async def test_no_command_waits_for_a_gateway_that_does_not_run(events, monkeypatch, tmp_path):
    _gateway_running(monkeypatch, None)
    await _app_stops(tmp_path)
    assert _lines(commands_path()) == []
    assert emergency_stop_marker_path().exists()


async def test_the_gateway_stops_everything_it_runs(events, monkeypatch, tmp_path):
    gateway = _the_gateway(monkeypatch, tmp_path)
    sess, ran, aborts = await _busy_session(gateway.state, SLACK)
    await gateway._start_control_channel()
    try:
        await _app_stops(tmp_path)
        await _until(lambda: sess.pending_task.done())
    finally:
        await gateway.control_channel.stop()
    assert ran == ["running"] and sess.queued_messages == []
    # As /stop does: Slack has not sealed the thread's message.
    assert aborts == [{"stopped_by_user": False}]
    (done,) = _lines(desktop_commands_path())
    assert (done["type"], done["process"]) == ("emergency_stop_done", "Slack gateway")
    assert done["stopId"] == _lines(commands_path())[0]["stopId"]


async def test_the_scheduler_daemon_stops_everything_it_runs(events, monkeypatch, tmp_path):
    from bridge.gateway.run import GatewayDaemon

    _gateway_running(monkeypatch, None)  # the daemon never holds the gateway lock
    daemon = GatewayDaemon()
    daemon.state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess, ran, _aborts = await _busy_session(daemon.state, "sched-job-session")
    await daemon._start_control_channel()
    assert daemon.control_channel is None
    try:
        await _app_stops(tmp_path)
        await _until(lambda: sess.pending_task.done())
    finally:
        await daemon.shutdown()
    assert ran == ["running"] and sess.queued_messages == []
    (done,) = _lines(desktop_commands_path())
    assert (done["type"], done["process"]) == ("emergency_stop_done", "scheduler daemon")


async def test_a_stop_sent_before_a_process_started_stops_nothing_there(events, monkeypatch, tmp_path):
    """A marker left from before a restart, or a command that waited in the
    control file while no gateway ran."""
    from bridge.gateway.run import GatewayDaemon

    _gateway_running(monkeypatch, None)
    await _app_stops(tmp_path)  # then the daemon (re)starts
    monkeypatch.setattr(fb, "_PROCESS_STARTED_AT", time.time())
    daemon = GatewayDaemon()
    daemon.state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess, ran, _aborts = await _busy_session(daemon.state, "sched-job-session")
    await daemon._start_control_channel()
    try:
        await asyncio.sleep(0.2)
        assert not sess.pending_task.done()
        stale = json.loads(emergency_stop_marker_path().read_text())
        await daemon._on_emergency_stop(stale)  # as the gateway gets it
        assert not sess.pending_task.done()
    finally:
        await daemon.shutdown()
        sess.pending_task.cancel()
    assert _lines(desktop_commands_path()) == []


async def test_a_process_takes_each_stop_once(events, monkeypatch, tmp_path):
    from bridge.gateway.run import GatewayDaemon

    _gateway_running(monkeypatch, None)
    daemon = GatewayDaemon()
    daemon.state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    await daemon._start_control_channel()
    try:
        await _app_stops(tmp_path)
        await _until(lambda: len(_lines(desktop_commands_path())) == 1)
        sess, _ran, _aborts = await _busy_session(daemon.state, "sched-job-session")
        marker = emergency_stop_marker_path()
        marker.write_text(marker.read_text())  # seen again
        await asyncio.sleep(0.2)
        assert not sess.pending_task.done()
    finally:
        await daemon.shutdown()
        sess.pending_task.cancel()


async def test_the_app_reports_which_processes_confirmed_the_stop(events, monkeypatch, tmp_path):
    monkeypatch.setattr(fb, "_EMERGENCY_STOP_CONFIRM_S", 0.5)
    monkeypatch.setattr(fb, "_scheduler_daemon_running", lambda: True)
    gateway = _the_gateway(monkeypatch, tmp_path)
    await _busy_session(gateway.state, SLACK)
    await gateway._start_control_channel()
    app = fb._BridgeState(workspace=str(tmp_path / "app"), default_model="kimi-k3-fast")
    reader = await fb._start_desktop_control_reader(app)
    try:
        await fb._handle_command(app, {"type": "computer.emergency_stop", "reason": "user"})
        await _until(lambda: any(e["type"] == "emergency_stop_report" for e in events))
    finally:
        await gateway.control_channel.stop()
        await reader.stop()
        gateway.state.sessions[SLACK].pending_task.cancel()
    (report,) = [e for e in events if e["type"] == "emergency_stop_report"]
    assert [(p["process"], p["confirmed"]) for p in report["processes"]] == [
        ("app", True), ("Slack gateway", True), ("scheduler daemon", False),
    ]
    assert report["message"] == (
        "Emergency stop confirmed by the app (0 tasks) and the Slack gateway (2 tasks). "
        "The scheduler daemon did not confirm it."
    )


async def test_a_scheduled_run_the_emergency_stop_stops_is_cancelled_and_not_delivered(events, monkeypatch, tmp_path):
    """Stopped, its turn ended, and the run went on as a finished one: its
    partial answer went to the job's sinks (in the daemon, to Slack)."""
    import bridge.gateway.pid as gp
    import bridge.scheduler.runtime as runtime
    from bridge.scheduler.models import ExistingSession, IntervalSchedule, JobRecord, NoopSinkSpec
    from bridge.scheduler.persistence import save_job
    from bridge.scheduler.runtime import fire_job
    from engine.types import Message

    monkeypatch.setattr(fb, "_SESSION_EVENT_DIR", tmp_path / "events")
    monkeypatch.setattr(gp, "holds_desktop_lock", lambda: True)
    _gateway_running(monkeypatch, None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="kimi-k3-fast")
    sess = await state.ensure_session("s1")
    await sess.initialize()

    async def run_turn(content, _attachments=None, **_kw):
        transcript = sess.session.transcript
        transcript.append_message(Message(role="user", content=content))
        transcript.append_message(Message(role="assistant", content="Half of the report"))
        await asyncio.sleep(60)

    sess.run_turn = run_turn
    delivered: list[str] = []

    async def deliver_all(job, run, output, ctx):
        delivered.append(output.text)
        return []

    monkeypatch.setattr(runtime, "deliver_all", deliver_all)
    job = JobRecord(id="sched_report", name="report", schedule=IntervalSchedule(seconds=3600),
                    prompt="write the report", execution=ExistingSession(session_id="s1"),
                    sinks=[NoopSinkSpec()])
    save_job(job)
    service = SimpleNamespace(state=SimpleNamespace(permission_tier="low", sessions={"s1": sess}),
                              emit_event=lambda **_kw: None)
    fire = asyncio.create_task(fire_job(service, job))
    await _until(lambda: sess.pending_task is not None and len(sess.session.transcript.entries) >= 2)
    await fb._handle_command(state, {"type": "computer.emergency_stop", "reason": "user"})
    run = await asyncio.wait_for(fire, 10)
    assert run.status == "cancelled"
    assert delivered == []
