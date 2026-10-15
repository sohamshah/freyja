from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bridge.tools import background_shell as bs
from bridge.tools import monitor_tool as mt
from bridge.tools.base import ToolTier
from bridge.tools.bash_tool import classify_command_risk


def _python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


@dataclass
class Session:
    ctx: bs.ToolContext
    events: list[tuple[bs.BackgroundCommand, str, bool]] = field(default_factory=list)
    exits: list[bs.BackgroundCommand] = field(default_factory=list)
    stopping: bool = False

    @property
    def records(self):
        return self.ctx.background_commands

    async def call(self, tool, **arguments):
        token = bs.CURRENT_TOOL_CONTEXT.set(self.ctx)
        try:
            return await tool.execute("call", arguments)
        finally:
            bs.CURRENT_TOOL_CONTEXT.reset(token)

    async def start(self, tool, command="sleep 30", **arguments):
        result = await self.call(tool, command=command, description="test monitor", **arguments)
        assert not result.is_error, result.content
        return list(self.records.values())[-1]


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "TERM_GRACE_S", 0.04)
    created = []

    def make():
        registry = {}
        ctx = bs.ToolContext(
            session_id=f"s{len(created)}",
            tool_yield=bs.ToolYield(),
            output_dir=tmp_path / f"s{len(created)}",
            on_background_start=lambda bg: registry.__setitem__(bg.id, bg),
            on_background_exit=lambda bg: session.exits.append(bg),
            background_commands=registry,
            on_monitor_event=lambda bg, text: session.events.append((bg, text, bg.is_running)),
            is_stopping=lambda: session.stopping,
        )
        session = Session(ctx)
        created.append(session)
        return session

    yield make
    for session in created:
        for record in session.records.values():
            if record.kind == "monitor" and record._process is not None:
                task = record._watch_task
                if task is None or not task.done():
                    if record.is_running:
                        record.state = "stopped"
                    await record._process.stop()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)


async def _finished(record):
    await asyncio.wait_for(asyncio.shield(record._watch_task), timeout=5)


async def _until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        await asyncio.sleep(0.01)


def _pid_alive(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


async def test_definition_and_risk_permissions():
    tool = mt.MonitorTool()
    assert tool.requires_permission
    assert tool.definition.name == "monitor"
    assert tool.definition.tier == ToolTier.WARM
    assert "WebSocket/WS" in tool.definition.description
    for command in ["cat file", "python app.py", "pip install foo", "rm -rf target"]:
        prompt = await tool.permission_prompt({"command": command, "description": "Watch a job"})
        assert prompt.level == classify_command_risk(command)
        assert "Watch a job" in prompt.prompt
    assert await tool.permission_prompt({"action": "list"}) is None
    assert await tool.permission_prompt({"action": "stop", "id": "monitor_owned"}) is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": "echo ok"},
        {"command": " ", "description": "test"},
        {"command": 123, "description": "test"},
        {"command": "echo ok", "description": []},
        {"action": "bad"},
        {"action": "stop"},
        {"action": "stop", "id": 1},
        {"command": "echo ok", "description": "test", "url": "ws://localhost"},
        {"command": "echo\x00hidden", "description": "test"},
        {"command": "echo ok", "description": "\x1b[2Jhidden"},
        {"command": "echo ok", "description": "hidden\u202e"},
        *[
            {"command": "echo ok", "description": "test", "timeout_ms": timeout}
            for timeout in [
                0,
                -1,
                float("nan"),
                float("inf"),
                -float("inf"),
                True,
                "1",
                None,
                mt.MAX_TIMEOUT_MS + 1,
                10**1000,
            ]
        ],
        {"command": "echo ok", "description": "test", "working_dir": ""},
    ],
)
async def test_invalid_arguments_never_spawn_or_prompt(arguments, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid input spawned a process")

    monkeypatch.setattr(mt.asyncio, "create_subprocess_shell", forbidden)
    tool = mt.MonitorTool()
    result = await tool.execute("invalid", arguments)
    assert result.is_error
    assert await tool.permission_prompt(arguments) is None


async def test_context_required_and_stopping_refused(sessions):
    tool = mt.MonitorTool()
    result = await tool.execute("no-context", {"command": "echo x", "description": "x"})
    assert result.is_error and "session" in result.content
    session = sessions()
    session.stopping = True
    result = await session.call(tool, command="echo x", description="x")
    assert result.is_error and "stopping" in result.content
    assert not session.records
    session.stopping = False
    session.ctx.on_monitor_event = None
    result = await session.call(tool, action="list")
    assert result.is_error


async def test_start_returns_and_delivers_before_exit(sessions, tmp_path):
    tool = mt.MonitorTool(working_dir=str(tmp_path))
    session = sessions()
    started = time.monotonic()
    record = await session.start(tool, "printf 'ready\\n'; sleep 1; printf 'done\\n'")
    assert time.monotonic() - started < 0.5
    assert record.kind == record.reason == "monitor"
    assert record.id.startswith("monitor_")
    assert record.timeout_s == 300
    assert record._process.proc.pid == record.pid
    await _until(lambda: session.events)
    assert session.events[0][1] == "ready"
    assert session.events[0][2]
    assert record._process.proc.returncode is None
    await _finished(record)
    assert record.state == "exited" and record.exit_code == 0
    assert record.ended_at is not None
    assert record.event_count == 2
    assert session.exits == [record]
    assert Path(record.output_path).read_text() == "ready\ndone\n"


async def test_many_lines_are_one_notification_and_one_token(sessions):
    session = sessions()
    record = await session.start(
        mt.MonitorTool(), _python("print('\\n'.join(map(str, range(30))))")
    )
    await _finished(record)
    assert len(session.events) == record.event_count == 1
    assert session.events[0][1].splitlines() == list(map(str, range(30)))
    assert record.suppressed_count == 0
    assert record._process._tokens == pytest.approx(mt.TOKEN_CAPACITY - 1, abs=0.05)


async def test_stderr_is_drained_logged_and_never_delivered(sessions, monkeypatch):
    monkeypatch.setattr(mt, "MAX_LOG_BYTES", 2048)
    session = sessions()
    record = await session.start(mt.MonitorTool(), _python("import os; os.write(2, b'e' * 200000)"))
    await _finished(record)
    assert not session.events and record.event_count == 0
    assert Path(record.stderr_path).stat().st_size == 2048
    assert Path(record.output_path).read_bytes() == b""
    assert session.exits == [record]


async def test_partial_utf8_and_final_unterminated_line(sessions, monkeypatch):
    monkeypatch.setattr(mt, "READ_CHUNK_BYTES", 1)
    session = sessions()
    source = "import os,time; os.write(1,b'\\xe2'); time.sleep(.02); os.write(1,b'\\x82\\xac tail')"
    record = await session.start(mt.MonitorTool(), _python(source))
    await _finished(record)
    assert [event[1] for event in session.events] == ["€ tail"]
    assert Path(record.output_path).read_text() == "€ tail"


@pytest.mark.parametrize("output", [False, True])
async def test_timeout_for_silent_or_active_source(sessions, output):
    session = sessions()
    command = "printf 'before timeout\\n'; sleep 30" if output else "sleep 30"
    record = await session.start(mt.MonitorTool(), command, timeout_ms=120)
    await _finished(record)
    assert record.state == "timed_out"
    assert record.exit_code is not None and record.ended_at is not None
    assert session.exits == [record]
    assert bool(session.events) == output
    assert all(log.sink.closed for log in record._process._logs)


async def test_nonzero_exit_keeps_stderr_for_terminal_memo(sessions):
    session = sessions()
    record = await session.start(mt.MonitorTool(), "printf 'failed detail' >&2; exit 7")
    await _finished(record)
    assert record.exit_code == 7 and record.state == "exited"
    assert Path(record.stderr_path).read_text() == "failed detail"
    assert session.exits == [record]


async def test_list_stop_and_session_ownership(sessions):
    one, two = sessions(), sessions()
    tool = mt.MonitorTool()
    record = await one.start(tool)
    unrelated = bs.BackgroundCommand(
        id="bg_unrelated",
        command="sleep 1",
        summary="bash",
        pid=0,
        output_path="",
        started_at=0,
        backgrounded_at=0,
        reason="followup",
    )
    one.records[unrelated.id] = unrelated
    for owner, monitor_id in [(two, record.id), (one, unrelated.id)]:
        result = await owner.call(tool, action="stop", id=monitor_id)
        assert result.is_error
        assert record.is_running
    listed = await one.call(tool, action="list")
    assert record.id in listed.content and unrelated.id not in listed.content
    assert record.id not in (await two.call(tool, action="list")).content
    stopped = await one.call(tool, action="stop", id=record.id)
    assert not stopped.is_error
    assert record.state == "stopped" and record.exit_code is not None
    assert record.ended_at is not None and session_count(one.exits, record) == 1
    assert not (await one.call(tool, action="stop", id=record.id)).is_error
    assert one.exits == [record]


def session_count(records, record):
    return sum(value is record for value in records)


async def test_list_bounds_recent_records(sessions, monkeypatch):
    monkeypatch.setattr(mt, "MAX_RECENT_MONITORS", 3)
    session = sessions()
    for index in range(10):
        session.records[str(index)] = bs.BackgroundCommand(
            id=str(index),
            command="x",
            summary="x",
            pid=0,
            output_path="",
            started_at=index,
            backgrounded_at=index,
            reason="monitor",
            kind="monitor",
            state="exited",
        )
    result = await session.call(mt.MonitorTool(), action="list")
    assert [line.split(" | ")[0] for line in result.content.splitlines()] == ["9", "8", "7"]


async def test_concurrent_monitor_limit_includes_startup_reservations(sessions, monkeypatch):
    monkeypatch.setattr(mt, "MAX_CONCURRENT_MONITORS", 2)
    original = mt.asyncio.create_subprocess_shell

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await original(*args, **kwargs)

    monkeypatch.setattr(mt.asyncio, "create_subprocess_shell", delayed)
    session = sessions()
    tool = mt.MonitorTool()
    results = await asyncio.gather(
        *[session.call(tool, command="sleep 30", description="watch") for _ in range(4)]
    )
    assert sum(not result.is_error for result in results) == 2
    assert len(session.records) == 2


async def test_spawn_uses_clean_environment_and_own_group(sessions, monkeypatch, tmp_path):
    original = mt.asyncio.create_subprocess_shell
    calls = []
    monkeypatch.setattr(mt, "child_env", lambda: {"MONITOR_CLEAN_ENV": "yes"})

    async def inspect_spawn(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(mt.asyncio, "create_subprocess_shell", inspect_spawn)
    session = sessions()
    record = await session.start(
        mt.MonitorTool(), "printf '%s' \"$MONITOR_CLEAN_ENV\"", working_dir=str(tmp_path)
    )
    await _finished(record)
    assert calls[0]["start_new_session"] is True
    assert calls[0]["env"] == {"MONITOR_CLEAN_ENV": "yes"}
    assert calls[0]["cwd"] == str(tmp_path)
    assert session.events[0][1] == "yes"


async def test_startup_cancellation_reaps_created_process_group(sessions, monkeypatch, tmp_path):
    original = mt.asyncio.create_subprocess_shell
    spawned = asyncio.Event()
    processes = []
    pid_path = tmp_path / "startup-child.pid"
    command = f"(trap '' TERM; exec sleep 30) & echo $! > {shlex.quote(str(pid_path))}; wait"

    async def delayed(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await asyncio.sleep(0.15)
        return proc

    monkeypatch.setattr(mt.asyncio, "create_subprocess_shell", delayed)
    session = sessions()
    task = asyncio.create_task(
        session.call(mt.MonitorTool(), command=command, description="cancel")
    )
    await spawned.wait()
    await _until(pid_path.exists)
    child_pid = int(pid_path.read_text())
    assert _pid_alive(child_pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: not _pid_alive(child_pid))
    assert processes[0].returncode is not None
    assert not session.records and not session.exits


@pytest.mark.parametrize("immediate", [False, True])
async def test_watch_cancellation_stops_whole_process_group(sessions, tmp_path, immediate):
    session = sessions()
    pid_path = tmp_path / "watch-child.pid"
    command = f"(trap '' TERM; exec sleep 30) & echo $! > {shlex.quote(str(pid_path))}; wait"
    record = await session.start(mt.MonitorTool(), command)
    if not immediate:
        await _until(pid_path.exists)
    record._watch_task.cancel()
    await _until(lambda: bool(session.exits))
    assert session.exits == [record]
    assert record.state == "stopped" and record.exit_code is not None
    if pid_path.exists():
        await _until(lambda: not _pid_alive(int(pid_path.read_text())))
    assert all(log.sink.closed for log in record._process._logs)


async def test_suppressed_lines_reported_on_next_delivery(sessions, monkeypatch):
    monkeypatch.setattr(mt, "TOKEN_CAPACITY", 1)
    monkeypatch.setattr(mt, "TOKEN_REFILL_S", 0.12)
    monkeypatch.setattr(mt, "BATCH_INTERVAL_S", 0.015)
    session = sessions()
    command = _python(
        "import time; print('first'); time.sleep(.05); print('drop'); "
        "time.sleep(.15); print('last')"
    )
    record = await session.start(mt.MonitorTool(), command)
    await _finished(record)
    assert record.event_count == 2 and record.suppressed_count == 1
    assert session.events[0][1] == "first"
    assert session.events[1][1] == "[1 lines suppressed]\nlast"


async def test_huge_unterminated_source_is_bounded_and_stopped(sessions, monkeypatch):
    monkeypatch.setattr(mt, "MAX_LOG_BYTES", 8192)
    monkeypatch.setattr(mt, "NOISE_LIMIT_S", 0.12)
    monkeypatch.setattr(mt, "BATCH_INTERVAL_S", 0.01)
    monkeypatch.setattr(mt, "TOKEN_CAPACITY", 1)
    monkeypatch.setattr(mt, "TOKEN_REFILL_S", 0.05)
    session = sessions()
    source = (
        "import os,time\nwhile True:\n"
        " os.write(1,b'x'*8000); os.write(2,b'e'*8000); time.sleep(.001)"
    )
    record = await session.start(mt.MonitorTool(), _python(source))
    await _finished(record)
    assert record.state == "rate_limited"
    assert record.suppressed_count > 0
    assert Path(record.output_path).stat().st_size <= 8192
    assert Path(record.stderr_path).stat().st_size <= 8192
    assert record._process._pending_chars <= mt.MAX_BATCH_CHARS
    assert session.events
    for _, text, _ in session.events:
        assert len(text) <= mt.MAX_BATCH_CHARS
        assert all(len(line) <= mt.MAX_LINE_CHARS for line in text.splitlines())


async def test_allowed_refill_does_not_reset_sustained_noise_timer(sessions, monkeypatch):
    monkeypatch.setattr(mt, "TOKEN_CAPACITY", 1)
    monkeypatch.setattr(mt, "TOKEN_REFILL_S", 0.04)
    monkeypatch.setattr(mt, "BATCH_INTERVAL_S", 0.008)
    monkeypatch.setattr(mt, "NOISE_LIMIT_S", 0.18)
    monkeypatch.setattr(mt, "NOISE_QUIET_S", 0.08)
    session = sessions()
    source = "import time\nwhile True:\n print('steady',flush=True); time.sleep(.01)"
    record = await session.start(mt.MonitorTool(), _python(source))
    await _finished(record)
    assert record.state == "rate_limited"
    assert record.event_count >= 3
    assert record.suppressed_count > 3
    assert record.elapsed < 1


async def test_quiet_interval_resets_suppression_window(sessions, monkeypatch):
    monkeypatch.setattr(mt, "TOKEN_CAPACITY", 1)
    monkeypatch.setattr(mt, "TOKEN_REFILL_S", 0.04)
    monkeypatch.setattr(mt, "BATCH_INTERVAL_S", 0.008)
    monkeypatch.setattr(mt, "NOISE_LIMIT_S", 0.2)
    monkeypatch.setattr(mt, "NOISE_QUIET_S", 0.06)
    session = sessions()
    source = (
        "import time\nfor burst in range(3):\n"
        " for i in range(7):\n  print('burst',flush=True); time.sleep(.01)\n"
        " time.sleep(.09)"
    )
    record = await session.start(mt.MonitorTool(), _python(source))
    await _finished(record)
    assert record.state == "exited" and record.exit_code == 0
    assert record.suppressed_count > 0 and record.event_count >= 3


async def test_delivery_failure_stops_source_and_finalizes_once(sessions):
    session = sessions()

    async def fail_event(bg, text):
        raise RuntimeError("delivery failed")

    session.ctx.on_monitor_event = fail_event
    record = await session.start(mt.MonitorTool(), "printf 'event\\n'; sleep 30")
    await _finished(record)
    assert record.state == "failed"
    assert record.exit_code is not None and session.exits == [record]
    assert all(log.sink.closed for log in record._process._logs)


async def test_normal_exit_cleans_children_with_redirected_streams(sessions):
    session = sessions()
    record = await session.start(
        mt.MonitorTool(), "(trap '' TERM; exec sleep 30) >/dev/null 2>&1 & echo $!"
    )
    await _finished(record)
    child_pid = int(Path(record.output_path).read_text().strip())
    assert not _pid_alive(child_pid)
    assert record.state == "exited" and record.exit_code == 0
    assert session.exits == [record]


async def test_working_directory_failure_closes_startup_resources(sessions, tmp_path):
    session = sessions()
    result = await session.call(
        mt.MonitorTool(),
        command="echo test",
        description="test",
        working_dir=str(tmp_path / "missing"),
    )
    assert result.is_error
    assert not session.records and not session.events and not session.exits
    for path in session.ctx.output_dir.iterdir():
        with path.open("ab") as log:
            assert log.tell() == 0


async def test_shell_newline_and_tab_are_valid_and_async_callbacks_work(sessions):
    session = sessions()

    async def event(bg, text):
        session.events.append((bg, text, bg.is_running))

    async def exited(bg):
        session.exits.append(bg)

    session.ctx.on_monitor_event = event
    session.ctx.on_background_exit = exited
    record = await session.start(mt.MonitorTool(), "printf '\\tfirst\\n'\nprintf final")
    await _finished(record)
    assert session.events[0][1] == "\tfirst\nfinal"
    assert session.exits == [record]


async def test_stop_waits_for_term_ignoring_grandchild(sessions, tmp_path):
    session = sessions()
    pid_path = tmp_path / "stop-child.pid"
    command = f"(trap '' TERM; exec sleep 30) & echo $! > {shlex.quote(str(pid_path))}; wait"
    tool = mt.MonitorTool()
    record = await session.start(tool, command)
    await _until(pid_path.exists)
    child_pid = int(pid_path.read_text())
    assert _pid_alive(child_pid)
    result = await session.call(tool, action="stop", id=record.id)
    assert not result.is_error
    assert not _pid_alive(child_pid)
    assert record.state == "stopped" and session.exits == [record]


async def test_shutdown_racing_startup_never_publishes_unregistered_events(sessions, monkeypatch):
    session = sessions()
    original = mt.asyncio.create_subprocess_shell
    processes = []

    async def stop_while_spawning(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        session.stopping = True
        return proc

    monkeypatch.setattr(mt.asyncio, "create_subprocess_shell", stop_while_spawning)
    result = await session.call(
        mt.MonitorTool(), command="printf 'early event\\n'; sleep 30", description="race"
    )
    assert result.is_error and "stopped" in result.content
    assert processes[0].returncode is not None
    assert not session.records and not session.events and not session.exits


async def test_registration_failure_still_finalizes_registered_record(sessions):
    session = sessions()
    register = session.ctx.on_background_start

    def fail_after_register(bg):
        register(bg)
        raise RuntimeError("registration hook failed")

    session.ctx.on_background_start = fail_after_register
    result = await session.call(mt.MonitorTool(), command="sleep 30", description="failure")
    assert result.is_error
    (record,) = session.records.values()
    assert record.state == "failed" and record.exit_code is not None
    assert session.exits == [record]
    assert all(log.sink.closed for log in record._process._logs)
