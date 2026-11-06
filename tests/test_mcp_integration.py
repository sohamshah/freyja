"""Integration tests: spawn tests/fixtures/mcp_fixture_server.py over
stdio through the full McpManager -> McpConnection -> McpProxyTool ->
ToolRegistry path.

Everything runs against tmp_path catalogs; the real ~/.freyja/mcp.json is
never read or written. Kill/reconnect lifecycles live in
tests/test_mcp_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

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
async def running_manager(tmp_path: Path, *, call_s: float = 30.0):
    catalog_path = _write_catalog(tmp_path, call_s=call_s)
    manager = McpManager.load(catalog_path, ping_interval_s=0.25, watchdog=False)
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


async def _wait_until(predicate, timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


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


# ---------------------------------------------------------------------------
# Discovery + registration + calls + shutdown, on one spawned server
# ---------------------------------------------------------------------------

async def test_discovery_registers_warm_tools_serves_calls_and_stop_reaps(tmp_path):
    from bridge.tools.registry import build_desktop_registry

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

        # Calls: success round trip and a remote failure mapped to is_error.
        result = await _call(registry, "mcp__fixture__echo", {"text": "round trip!"})
        assert result.is_error is False
        assert "round trip!" in str(result.content)
        result = await _call(registry, "mcp__fixture__fail", {})
        assert result.is_error is True
        assert "fail" in str(result.content).lower() or "error" in str(result.content).lower()

        # The desktop registry builder registers the proxies and attaches the
        # registry so later disconnects mutate it.
        desktop = build_desktop_registry(
            workspace=tmp_path,
            include_bash=False,
            include_web=False,
            include_subagents=False,
            include_computer=False,
            mcp_manager=manager,
        )
        for name in EXPECTED_TOOLS:
            assert desktop.get(name) is not None, name
        assert any(r is desktop for r in manager._live_registries())  # noqa: SLF001

        result = await _call(registry, "mcp__fixture__pid", {})
        pid = int(str(result.content).strip())
        assert _process_alive(pid)

    # manager.stop() ran: the child is reaped and tools are pulled.
    gone = await _wait_until(lambda: not _process_alive(pid), timeout_s=15)
    assert gone, f"fixture server pid {pid} still alive after manager.stop()"
    assert registry.get("mcp__fixture__echo") is None
    assert desktop.get("mcp__fixture__echo") is None


async def test_tool_search_returns_the_schema_and_call_tool_runs_it(tmp_path):
    # The tool list sits at the front of the cached prompt, so loading a
    # deferred tool must not change it: tool_search hands the schema back as a
    # result, and call_tool runs the tool by name.
    from bridge.tools.tool_search_tool import CallToolTool, ToolSearchTool

    async with running_manager(tmp_path) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        search = ToolSearchTool(registry)
        before = [d.name for d in registry.list_definitions()]

        result = await search.execute("s1", {"tool_name": "mcp__fixture__echo"})
        assert not result.is_error
        # Remote input schema passed through, in the result.
        assert '"text"' in str(result.content)
        assert "call_tool" in str(result.content)
        assert [d.name for d in registry.list_definitions()] == before
        assert registry.get_catalog_entry("mcp__fixture__echo").schema_visible is False

        ran = await CallToolTool(registry).execute(
            "c1", {"tool_name": "mcp__fixture__echo", "arguments": {"text": "round trip!"}}
        )
        assert ran.is_error is False
        assert ran.call_id == "c1"
        assert "round trip!" in str(ran.content)


async def test_call_timeout_honored(tmp_path):
    async with running_manager(tmp_path, call_s=0.3) as manager:
        registry = ToolRegistry()
        manager.register_into(registry)
        started = time.monotonic()
        result = await _call(registry, "mcp__fixture__slow", {"seconds": 15})
        elapsed = time.monotonic() - started
        assert result.is_error is True
        assert "timed out" in str(result.content).lower()
        assert elapsed < 10, f"timeout not honored: took {elapsed:.1f}s"
