"""Integration tests: spawn tests/fixtures/mcp_fixture_server.py over
stdio through the full McpManager -> McpConnection -> McpProxyTool ->
ToolRegistry path.

Everything runs against tmp_path catalogs; the real ~/.freyja/mcp.json is
never read or written.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from bridge.mcp import McpManager
from bridge.mcp.connection import State
from bridge.tools.base import ToolRegistry
from engine.types import ToolCall

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"

# All fixture tools that must register (shady is injection-skipped).
EXPECTED_TOOLS = [
    "mcp__fixture__echo",
    "mcp__fixture__slow",
    "mcp__fixture__fail",
    "mcp__fixture__pid",
    "mcp__fixture__lore",
]


def _write_catalog(tmp_path: Path, *, call_s: float = 30.0, connect_s: float = 30.0) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "version": 1,
        "servers": {
            "fixture": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(FIXTURE_SERVER)],
                "trust": "trusted",
                "tier": "warm",
                "timeouts": {"connect_s": connect_s, "call_s": call_s},
            }
        },
    }), encoding="utf-8")
    return path


@contextlib.asynccontextmanager
async def running_manager(
    tmp_path: Path,
    *,
    call_s: float = 30.0,
    ping_interval_s: float = 0.25,
    max_reconnects: int = 1,
):
    catalog_path = _write_catalog(tmp_path, call_s=call_s)
    manager = McpManager.load(
        catalog_path,
        ping_interval_s=ping_interval_s,
        max_reconnects=max_reconnects,
    )
    try:
        await asyncio.wait_for(manager.start(), timeout=60)
        conn = manager.connection("fixture")
        assert conn is not None, "fixture connection missing"
        assert conn.state is State.ACTIVE, f"fixture not active: {conn.state} ({conn.reason})"
        yield manager
    finally:
        await manager.stop()


async def _call(registry: ToolRegistry, name: str, arguments: dict) -> object:
    return await registry.execute(ToolCall(id="t1", name=name, arguments=arguments))


async def _wait_until(predicate, timeout_s: float, interval_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------

async def test_discovery_registers_warm_tools(tmp_path):
    async with running_manager(tmp_path) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)

        for name in EXPECTED_TOOLS:
            entry = registry.get_catalog_entry(name)
            assert entry is not None, f"{name} not registered"
            # WARM: summary in the prompt, schema withheld until promoted.
            assert entry.summary_visible is True, name
            assert entry.schema_visible is False, name

        # Injection-scanned tool is skipped entirely.
        assert registry.get("mcp__fixture__shady") is None

        # Summary shape: "[server] " + first sentence, <= 140 chars.
        summaries = registry.list_summaries()
        lore_summary = summaries["mcp__fixture__lore"]
        assert lore_summary.startswith("[fixture] ")
        assert lore_summary.endswith("Retrieve fixture lore about a topic from the archive.")
        assert len(lore_summary) <= 140

        conn = manager.connection("fixture")
        assert conn.tool_count == 6  # remote count, pre-filter


async def test_tool_search_promotes_schema(tmp_path):
    from bridge.tools.tool_search_tool import ToolSearchTool

    async with running_manager(tmp_path) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        search = ToolSearchTool(registry)

        result = await search.execute("s1", {"tool_name": "mcp__fixture__echo"})
        assert not result.is_error

        entry = registry.get_catalog_entry("mcp__fixture__echo")
        assert entry.schema_visible is True
        definitions = {d.name for d in registry.list_definitions()}
        assert "mcp__fixture__echo" in definitions
        # Remote input schema passed through.
        schema = registry.get("mcp__fixture__echo").definition.parameters
        assert "text" in schema.get("properties", {})


async def test_build_desktop_registry_wiring(tmp_path):
    from bridge.tools.registry import build_desktop_registry

    async with running_manager(tmp_path) as manager:
        registry = build_desktop_registry(
            workspace=tmp_path,
            include_bash=False,
            include_web=False,
            include_subagents=False,
            include_computer=False,
            mcp_manager=manager,
        )
        for name in EXPECTED_TOOLS:
            assert registry.get(name) is not None, name
        # attach() happened: a later disconnect must mutate THIS registry
        # (covered end-to-end in the kill test; here we assert the weakref
        # took by checking the manager's live-registry view).
        assert any(r is registry for r in manager._live_registries())


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

async def test_echo_round_trip(tmp_path):
    async with running_manager(tmp_path) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        result = await _call(registry, "mcp__fixture__echo", {"text": "round trip!"})
        assert result.is_error is False
        assert "round trip!" in str(result.content)


async def test_fail_maps_to_error_result(tmp_path):
    async with running_manager(tmp_path) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        result = await _call(registry, "mcp__fixture__fail", {})
        assert result.is_error is True
        assert "fail" in str(result.content).lower() or "error" in str(result.content).lower()


async def test_call_timeout_honored(tmp_path):
    async with running_manager(tmp_path, call_s=1.0) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        started = time.monotonic()
        result = await _call(registry, "mcp__fixture__slow", {"seconds": 15})
        elapsed = time.monotonic() - started
        assert result.is_error is True
        assert "timed out" in str(result.content).lower()
        assert elapsed < 10, f"timeout not honored: took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _fixture_pid(registry: ToolRegistry) -> int:
    result = await _call(registry, "mcp__fixture__pid", {})
    assert result.is_error is False
    return int(str(result.content).strip())


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Reap in case it's our zombie child.
    with contextlib.suppress(ChildProcessError, OSError):
        done, _status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    return True


async def test_server_kill_unregisters_tools(tmp_path):
    async with running_manager(tmp_path, max_reconnects=0) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        pid = await _fixture_pid(registry)

        os.kill(pid, signal.SIGKILL)

        unregistered = await _wait_until(
            lambda: registry.get("mcp__fixture__echo") is None, timeout_s=15
        )
        assert unregistered, "tools not unregistered after server kill"
        for name in EXPECTED_TOOLS:
            assert registry.get(name) is None, name
            assert registry.get_catalog_entry(name) is None, name

        conn = manager.connection("fixture")
        assert conn.state is State.FAILED

        # Calls after failure produce a clean error, not a hang.
        proxies_left = manager.proxy_tools()
        assert proxies_left == []


async def test_manager_stop_leaves_no_child_processes(tmp_path):
    catalog_path = _write_catalog(tmp_path)
    manager = McpManager.load(catalog_path, ping_interval_s=0.25)
    await asyncio.wait_for(manager.start(), timeout=60)
    registry = ToolRegistry()
    manager.register_into(registry)
    pid = await _fixture_pid(registry)
    assert _process_alive(pid)

    await manager.stop()

    gone = await _wait_until(lambda: not _process_alive(pid), timeout_s=15)
    assert gone, f"fixture server pid {pid} still alive after manager.stop()"
    # Tools pulled from the registry on shutdown too.
    assert registry.get("mcp__fixture__echo") is None


async def test_reconnect_after_kill_reregisters(tmp_path):
    """v0 single-reconnect: a killed server comes back once and its tools
    reappear in attached registries."""
    async with running_manager(tmp_path, max_reconnects=1) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        pid = await _fixture_pid(registry)

        os.kill(pid, signal.SIGKILL)

        # Tools drop out...
        dropped = await _wait_until(
            lambda: registry.get("mcp__fixture__echo") is None, timeout_s=15
        )
        assert dropped
        # ...then the single reconnect brings them back.
        returned = await _wait_until(
            lambda: registry.get("mcp__fixture__echo") is not None, timeout_s=30
        )
        assert returned, "tools did not re-register after reconnect"
        result = await _call(registry, "mcp__fixture__echo", {"text": "back"})
        assert result.is_error is False
        assert "back" in str(result.content)
