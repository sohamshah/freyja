"""v1 lifecycle tests: keepalive/degraded, backoff -> parked -> revive
with tool re-registration diff, and permanent-failure classification.

All timers are constructor-injected so the suite runs in seconds. All
catalogs/pid paths live in tmp_path; the real ~/.freyja is never touched.
The watchdog wrapper is disabled here (covered by test_mcp_watchdog.py)
so kill/SIGSTOP tests act on the SDK's direct child.
"""

from __future__ import annotations

import asyncio
import os
import random
import signal
import sys
import time
from pathlib import Path

from bridge.mcp.config import McpCatalog, McpServerSpec
from bridge.mcp.connection import (
    ACTIVE_LIKE,
    McpConnection,
    State,
    describe_error,
    looks_like_auth_error,
    looks_like_dead_stream,
    normalize_keepalive_interval,
)
from bridge.mcp.manager import McpManager
from bridge.tools.base import ToolRegistry
from engine.types import ToolCall

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"

# Wrapper that only starts the real server while the gate file exists;
# lets tests make reconnect attempts fail deterministically and then
# revive the server (optionally as a DIFFERENT server for diff tests).
GATE_RUNNER_SOURCE = """\
import os, sys
gate = sys.argv[1]
try:
    target = open(gate).read().strip()
except OSError:
    sys.stderr.write("gate closed\\n")
    sys.exit(1)
os.execv(sys.executable, [sys.executable, target])
"""

# Alternate fixture with a DIFFERENT tool set, for the revive-diff test.
ALT_SERVER_SOURCE = """\
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="altfix")


@server.tool()
def echo_two(text: str) -> str:
    \"\"\"Echo the text back from the replacement server.\"\"\"
    return "two:" + text


if __name__ == "__main__":
    server.run("stdio")
"""


def _spec(tmp_path: Path, *, gate: bool) -> tuple[McpServerSpec, Path]:
    """Spec for the fixture server, optionally behind the gate runner."""
    gate_file = tmp_path / "gate.txt"
    if gate:
        runner = tmp_path / "gate_runner.py"
        runner.write_text(GATE_RUNNER_SOURCE, encoding="utf-8")
        gate_file.write_text(str(FIXTURE_SERVER), encoding="utf-8")
        command, args = sys.executable, [str(runner), str(gate_file)]
    else:
        command, args = sys.executable, [str(FIXTURE_SERVER)]
    spec = McpServerSpec(
        name="flappy",
        transport="stdio",
        command=command,
        args=args,
        trust="trusted",
        connect_timeout_s=5.0,
        call_timeout_s=15.0,
    )
    return spec, gate_file


def _manager(spec: McpServerSpec, **overrides) -> McpManager:
    kwargs = dict(
        keepalive_interval_s=0.2,
        keepalive_timeout_s=0.5,
        backoff_base_s=0.05,
        backoff_cap_s=0.1,
        backoff_max_retries=2,
        park_probe_interval_s=0.25,
        watchdog=False,
    )
    kwargs.update(overrides)
    return McpManager(McpCatalog(specs={spec.name: spec}), **kwargs)


async def _call(registry: ToolRegistry, name: str, arguments: dict) -> object:
    return await registry.execute(ToolCall(id="t1", name=name, arguments=arguments))


async def _wait_until(predicate, timeout_s: float, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


class _Recorder:
    """Collects mcp_status events + connection transitions."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)

    def states(self) -> list[str]:
        return [e["state"] for e in self.events]


# ---------------------------------------------------------------------------
# Kill -> backoff -> parked -> revive (with tool-set diff)
# ---------------------------------------------------------------------------

async def test_kill_backoff_park_revive_rewires_tools(tmp_path):
    spec, gate_file = _spec(tmp_path, gate=True)
    manager = _manager(spec)
    recorder = _Recorder()
    manager.status_listener = recorder
    registry = ToolRegistry()
    try:
        await asyncio.wait_for(manager.start(), timeout=30)
        manager.register_into(registry)
        conn = manager.connection("flappy")
        assert conn is not None and conn.state is State.ACTIVE, conn.reason
        assert registry.get("mcp__flappy__echo") is not None

        result = await _call(registry, "mcp__flappy__pid", {})
        pid = int(str(result.content).strip())

        # Close the gate so every reconnect attempt fails, then kill.
        gate_file.unlink()
        os.kill(pid, signal.SIGKILL)

        parked = await _wait_until(
            lambda: conn.state is State.PARKED, timeout_s=25
        )
        assert parked, f"never parked: {conn.state} ({conn.reason})"
        states = recorder.states()
        assert "backoff" in states, states
        assert "parked" in states, states
        # Retry budget respected: 2 backoff transitions before parking.
        assert states.count("backoff") == 2, states
        # Tools pulled while dead.
        assert registry.get("mcp__flappy__echo") is None
        assert manager.proxy_tools() == []

        # Revive as a DIFFERENT server: the parked probe must reconnect
        # and re-register with a diff (stale names out, new names in).
        alt = tmp_path / "alt_server.py"
        alt.write_text(ALT_SERVER_SOURCE, encoding="utf-8")
        gate_file.write_text(str(alt), encoding="utf-8")

        revived = await _wait_until(
            lambda: conn.state is State.ACTIVE
            and registry.get("mcp__flappy__echo_two") is not None,
            timeout_s=25,
        )
        assert revived, f"never revived: {conn.state} ({conn.reason})"
        # Stale tool names from the previous activation are NOT re-registered.
        assert registry.get("mcp__flappy__echo") is None
        assert registry.get_catalog_entry("mcp__flappy__pid") is None
        assert conn.restart_count >= 1
        result = await _call(registry, "mcp__flappy__echo_two", {"text": "hi"})
        assert result.is_error is False
        assert "two:hi" in str(result.content)

        # mcp_status event shape (design 4.4): every transition carries
        # the full status-model fields.
        for event in recorder.events:
            for key in (
                "server", "state", "reason", "since", "tool_count",
                "schema_chars", "restart_count", "last_latency_ms", "enabled",
            ):
                assert key in event, (key, event)
            assert event["server"] == "flappy"
    finally:
        await manager.stop()


async def test_parked_probe_keeps_parking_while_server_down(tmp_path):
    spec, gate_file = _spec(tmp_path, gate=True)
    gate_file.unlink()  # never comes up
    manager = _manager(spec, backoff_max_retries=1, park_probe_interval_s=0.1)
    recorder = _Recorder()
    manager.status_listener = recorder
    try:
        await asyncio.wait_for(manager.start(), timeout=30)
        conn = manager.connection("flappy")
        assert conn is not None
        await _wait_until(lambda: recorder.states().count("parked") >= 1, timeout_s=20)
        # At least one full probe cycle after parking: connecting again,
        # failing, staying parked.
        cycled = await _wait_until(
            lambda: recorder.states().count("connecting") >= 3, timeout_s=20
        )
        assert cycled, recorder.states()
        assert conn.state in (State.PARKED, State.CONNECTING, State.BACKOFF)
        assert "active" not in recorder.states()
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# Keepalive: hung server -> degraded (tools stay), recovery -> active
# ---------------------------------------------------------------------------

async def test_sigstop_hang_degrades_then_recovers(tmp_path):
    spec, _gate = _spec(tmp_path, gate=False)
    manager = _manager(
        spec, keepalive_interval_s=0.3, keepalive_timeout_s=0.3,
        backoff_base_s=0.05, backoff_max_retries=5,
    )
    registry = ToolRegistry()
    degraded_kept_tools: list[bool] = []

    def _listener(event: dict) -> None:
        if event["state"] == "degraded":
            # Synchronous inside the transition: degraded must NOT
            # unregister the server's tools (design 4.1).
            degraded_kept_tools.append(registry.get("mcp__flappy__echo") is not None)

    manager.status_listener = _listener
    try:
        await asyncio.wait_for(manager.start(), timeout=30)
        manager.register_into(registry)
        conn = manager.connection("flappy")
        assert conn is not None and conn.state is State.ACTIVE

        result = await _call(registry, "mcp__flappy__pid", {})
        pid = int(str(result.content).strip())
        os.kill(pid, signal.SIGSTOP)

        hit_degraded = await _wait_until(
            lambda: len(degraded_kept_tools) > 0, timeout_s=15
        )
        assert hit_degraded, f"never degraded: {conn.state} ({conn.reason})"
        assert degraded_kept_tools[0] is True, "degraded unregistered tools"

        os.kill(pid, signal.SIGCONT)
        # Recovers to ACTIVE — either directly (keepalive recovered) or,
        # if a second probe already confirmed death, via the backoff
        # ladder reconnect. Both paths must land with usable tools.
        recovered = await _wait_until(
            lambda: conn.state is State.ACTIVE
            and registry.get("mcp__flappy__echo") is not None,
            timeout_s=20,
        )
        assert recovered, f"never recovered: {conn.state} ({conn.reason})"
        result = await _call(registry, "mcp__flappy__echo", {"text": "still here"})
        assert result.is_error is False
        assert "still here" in str(result.content)
    finally:
        await manager.stop()


async def test_degraded_connection_still_serves_calls(tmp_path):
    """A degraded server (one missed ping) keeps serving tool calls."""
    spec, _gate = _spec(tmp_path, gate=False)
    manager = _manager(spec, keepalive_interval_s=0.3, keepalive_timeout_s=0.3)
    registry = ToolRegistry()
    try:
        await asyncio.wait_for(manager.start(), timeout=30)
        manager.register_into(registry)
        conn = manager.connection("flappy")
        result = await _call(registry, "mcp__flappy__pid", {})
        pid = int(str(result.content).strip())
        os.kill(pid, signal.SIGSTOP)
        await _wait_until(lambda: conn.state is State.DEGRADED, timeout_s=15)
        try:
            assert conn.state in ACTIVE_LIKE
            # proxy_tools still includes the degraded server's tools.
            assert any(
                p.definition.name == "mcp__flappy__echo" for p in manager.proxy_tools()
            )
        finally:
            os.kill(pid, signal.SIGCONT)
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# Permanent-failure classification (design 3.3/4.2): no retry ladder
# ---------------------------------------------------------------------------

async def test_nonexistent_command_fails_without_retry_ladder():
    spec = McpServerSpec(
        name="ghost",
        transport="stdio",
        command="/nonexistent/freyja-test-binary-xyz",
        connect_timeout_s=5.0,
    )
    transitions: list[State] = []
    conn = McpConnection(
        spec,
        on_state_change=lambda _c, _old, new: transitions.append(new),
        backoff_base_s=0.01,
        backoff_max_retries=5,
        park_probe_interval_s=0.05,
        watchdog=False,
    )
    started = time.monotonic()
    await conn.start()
    assert conn.state is State.FAILED
    assert "command not found" in conn.reason
    # Ladder skipped entirely: no backoff/parked states, immediate exit.
    assert State.BACKOFF not in transitions
    assert State.PARKED not in transitions
    assert time.monotonic() - started < 2.0
    await asyncio.sleep(0.05)
    assert conn._task is None or conn._task.done()
    await conn.stop()
    assert conn.state is State.FAILED  # permanent states survive stop()


async def test_missing_secret_env_needs_auth_without_retry_ladder(monkeypatch):
    monkeypatch.delenv("FREYJA_TEST_MISSING_TOKEN", raising=False)
    spec = McpServerSpec(
        name="authy",
        transport="stdio",
        command=sys.executable,
        env={"FREYJA_TEST_MISSING_TOKEN": "${FREYJA_TEST_MISSING_TOKEN}"},
    )
    transitions: list[State] = []
    conn = McpConnection(
        spec,
        on_state_change=lambda _c, _old, new: transitions.append(new),
        backoff_base_s=0.01,
        watchdog=False,
    )
    await conn.start()
    assert conn.state is State.NEEDS_AUTH
    assert "FREYJA_TEST_MISSING_TOKEN" in conn.reason
    assert State.BACKOFF not in transitions
    assert State.PARKED not in transitions
    await conn.stop()
    assert conn.state is State.NEEDS_AUTH


async def test_auth_shaped_error_maps_to_needs_auth(monkeypatch):
    spec = McpServerSpec(name="httpish", transport="stdio", command=sys.executable)
    transitions: list[State] = []
    conn = McpConnection(
        spec,
        on_state_change=lambda _c, _old, new: transitions.append(new),
        backoff_base_s=0.01,
        watchdog=False,
    )

    async def _boom() -> None:
        raise RuntimeError("server rejected initialize: HTTP 401 Unauthorized")

    monkeypatch.setattr(conn, "_run_once", _boom)
    await conn.start()
    assert conn.state is State.NEEDS_AUTH
    assert "401" in conn.reason
    assert State.BACKOFF not in transitions
    await conn.stop()


def test_looks_like_auth_error_classification():
    positives = [
        "HTTP 401 Unauthorized",
        "403 Forbidden: insufficient_scope",
        "invalid_grant: refresh token revoked",
        "authentication failed for user",
        "Invalid API key provided",
        "token expired, please reauthenticate",
    ]
    negatives = [
        "connection closed",
        "read timeout after 30s",
        "command not found: 'foo'",
        "server exited with code 1",
    ]
    for text in positives:
        assert looks_like_auth_error(text), text
    for text in negatives:
        assert not looks_like_auth_error(text), text


def test_dead_stream_and_error_description_helpers():
    group = ExceptionGroup("boom", [BrokenPipeError("pipe gone")])
    assert looks_like_dead_stream(group)
    assert looks_like_dead_stream(RuntimeError("Connection closed by peer"))
    assert not looks_like_dead_stream(RuntimeError("request timed out"))
    assert "pipe gone" in describe_error(group)
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("v1")])])
    assert describe_error(nested) == "v1"


# ---------------------------------------------------------------------------
# Timer knobs
# ---------------------------------------------------------------------------

def test_keepalive_floor_applies_to_config_values():
    assert normalize_keepalive_interval(1) == 5.0
    assert normalize_keepalive_interval(300) == 300.0
    assert normalize_keepalive_interval("garbage") == 180.0
    assert normalize_keepalive_interval(None) == 180.0


def test_manager_load_clamps_config_keepalive(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        '{"version": 1, "settings": {"keepalive_interval_s": 0.5}, "servers": {}}',
        encoding="utf-8",
    )
    manager = McpManager.load(path)
    assert manager._keepalive_interval_s == 5.0  # noqa: SLF001
    # Explicit programmatic values (test seam) bypass the floor.
    fast = McpManager.load(path, ping_interval_s=0.1)
    assert fast._keepalive_interval_s == 0.1  # noqa: SLF001


def test_backoff_delay_exponential_with_jitter_and_cap():
    spec = McpServerSpec(name="b", transport="stdio", command=sys.executable)
    conn = McpConnection(
        spec, backoff_base_s=1.0, backoff_cap_s=60.0, rng=random.Random(42),
        watchdog=False,
    )
    for attempt, expected in ((1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)):
        delay = conn._backoff_delay(attempt)  # noqa: SLF001
        # v2 (card_017): FULL jitter, U(0, d) with a tiny floor — the old
        # equal-jitter lower bound (d/2) no longer applies by design.
        assert 0.0 < delay <= expected, (attempt, delay)
    # Cap: attempt 30 would be 2^29 without the 60s ceiling.
    assert conn._backoff_delay(30) <= 60.0  # noqa: SLF001


def test_connection_snapshot_shape():
    spec = McpServerSpec(name="snap", transport="stdio", command=sys.executable)
    conn = McpConnection(spec, watchdog=False)
    snap = conn.snapshot()
    assert snap == {
        "server": "snap",
        "transport": "stdio",
        "state": "disabled",
        "reason": "",
        "since": snap["since"],
        "tool_count": 0,
        "restart_count": 0,
        "last_latency_ms": None,
    }
    assert isinstance(snap["since"], float)
