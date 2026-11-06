"""mcp_command handler tests that v2 does not own: enable/disable/call
mutating the catalog on disk + the live registry, reload diffing,
``missing_env_refs`` and the /mcp table helper. Status shape, action
guards, OAuth login/logout/reauth/add/remove/test/tools/catalog and the
bridge IPC branch live in tests/test_mcp_commands_v2.py and
tests/test_mcp_bridge_wiring.py.

The handler is called directly (as freyja_bridge._handle_command and the
gateway /mcp command do); catalogs live in tmp_path only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from bridge.mcp.commands import (
    format_mcp_table,
    handle_mcp_command,
    missing_env_refs,
)
from bridge.mcp.config import McpCatalog, McpServerSpec
from bridge.mcp.manager import McpManager
from bridge.tools.base import ToolRegistry

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"


def _write_catalog(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"version": 1, "servers": servers}), encoding="utf-8"
    )
    return path


def _two_server_catalog(tmp_path: Path) -> Path:
    return _write_catalog(tmp_path, {
        "fixture": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "enabled": False,
            "trust": "trusted",
            "timeouts": {"connect_s": 15.0, "call_s": 15.0},
        },
        "hosted": {
            "transport": "http",
            "url": "https://example.com/mcp",
            "enabled": False,
            "oauth": {"client_id": "abc", "callback_port": 3118},
        },
    })


def _manager(path: Path, tmp_path: Path) -> McpManager:
    return McpManager.load(
        path,
        ping_interval_s=0.5,
        watchdog=False,
        run_dir=tmp_path / "mcp-run",
        backoff_base_s=0.05,
        backoff_max_retries=1,
        park_probe_interval_s=0.2,
    )


# ---------------------------------------------------------------------------
# enable / disable / call: catalog on disk + live registry, one server spawn
# ---------------------------------------------------------------------------

async def test_enable_call_disable_mutate_catalog_and_registry(tmp_path):
    path = _two_server_catalog(tmp_path)
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    events = []
    manager.status_listener = events.append
    try:
        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "enable", "server": "fixture"}),
            timeout=60,
        )
        assert result["ok"] is True, result
        assert result["servers"][0]["state"] == "active"
        assert result["servers"][0]["tool_count"] == 6  # remote count
        assert result["servers"][0]["schema_chars"] > 0

        # mcp.json flipped on disk (save_catalog path); other entries preserved.
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["servers"]["fixture"]["enabled"] is True
        assert doc["servers"]["hosted"]["oauth"]["client_id"] == "abc"

        # Live registry mutated without a restart; transitions surfaced.
        assert registry.get("mcp__fixture__echo") is not None
        assert any(e["state"] == "active" for e in events)

        # call: by remote name, by full proxy name, remote failure -> is_error.
        result = await asyncio.wait_for(
            handle_mcp_command(manager, {
                "action": "call", "server": "fixture", "tool": "echo", "args": {"text": "ping-42"},
            }),
            timeout=60,
        )
        assert result["ok"] is True, result
        assert result["tool_name"] == "mcp__fixture__echo"
        assert result["is_error"] is False and "ping-42" in result["content"]
        result = await asyncio.wait_for(
            handle_mcp_command(manager, {
                "action": "call", "server": "fixture", "tool": "mcp__fixture__echo",
                "args": {"text": "by-proxy-name"},
            }),
            timeout=60,
        )
        assert result["ok"] is True and "by-proxy-name" in result["content"]
        result = await asyncio.wait_for(
            handle_mcp_command(
                manager, {"action": "call", "server": "fixture", "tool": "fail", "args": {}},
            ),
            timeout=60,
        )
        assert result["ok"] is False and result["is_error"] is True
        # Validation: missing tool arg, unknown tool, non-dict args.
        result = await handle_mcp_command(manager, {"action": "call", "server": "fixture"})
        assert result["ok"] is False and "usage" in result["message"]
        result = await handle_mcp_command(
            manager, {"action": "call", "server": "fixture", "tool": "nope"}
        )
        assert result["ok"] is False and "available" in result["message"]
        result = await handle_mcp_command(manager, {
            "action": "call", "server": "fixture", "tool": "echo", "args": "not-a-dict",
        })
        assert result["ok"] is False and "JSON object" in result["message"]

        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "disable", "server": "fixture"}),
            timeout=60,
        )
        assert result["ok"] is True
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["servers"]["fixture"]["enabled"] is False
        assert registry.get("mcp__fixture__echo") is None
        assert result["servers"][0]["state"] == "disabled"
        assert manager.connection("fixture") is None
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

async def test_reload_applies_catalog_diff(tmp_path):
    path = _two_server_catalog(tmp_path)
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    try:
        # Edit the file out-of-band: enable fixture, add a server.
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["servers"]["fixture"]["enabled"] = True
        doc["servers"]["extra"] = {
            "transport": "stdio", "command": sys.executable,
            "args": [str(FIXTURE_SERVER)], "enabled": False,
        }
        path.write_text(json.dumps(doc), encoding="utf-8")

        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "reload"}), timeout=60
        )
        assert result["ok"] is True
        assert "extra" in result["added"]
        assert "fixture" in result["changed"]
        assert registry.get("mcp__fixture__echo") is not None
        states = {row["server"]: row["state"] for row in result["servers"]}
        assert states["fixture"] == "active"
        assert states["extra"] == "disabled"

        # Remove fixture entirely -> connection stopped, tools pulled.
        del doc["servers"]["fixture"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "reload"}), timeout=60
        )
        assert "fixture" in result["removed"]
        assert registry.get("mcp__fixture__echo") is None
        assert manager.connection("fixture") is None
    finally:
        await manager.stop()

    # A manager built without a catalog path reports reload as unavailable.
    pathless = McpManager(McpCatalog(), watchdog=False)
    result = await handle_mcp_command(pathless, {"action": "reload"})
    assert result["ok"] is True
    assert "reload unavailable" in result["message"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_missing_env_refs_scans_all_fields():
    spec = McpServerSpec(
        name="s",
        transport="stdio",
        command="${MISSING_BIN_TOKEN}",
        args=["--token", "${ARG_KEY}"],
        env={"A": "${ENV_TOKEN_ONE:-fallback}", "B": "${ENV_TOKEN_TWO}"},
        headers={"Authorization": "Bearer ${HDR_TOKEN}"},
    )
    missing = missing_env_refs(spec, environ={"ARG_KEY": "set"})
    # Defaulted refs are not missing; set refs are not missing.
    assert missing == ["ENV_TOKEN_TWO", "HDR_TOKEN", "MISSING_BIN_TOKEN"]


def test_format_mcp_table():
    rows = [
        {"server": "slack", "transport": "stdio", "state": "active",
         "tool_count": 12, "reason": ""},
        {"server": "hosted", "transport": "http", "state": "needs-auth",
         "tool_count": 0, "reason": "401 on initialize " + "x" * 60},
    ]
    table = format_mcp_table(rows)
    lines = table.splitlines()
    assert lines[0].split() == ["name", "transport", "state", "tools", "reason"]
    assert lines[1].startswith("-")
    assert "slack" in lines[2] and "active" in lines[2] and "12" in lines[2]
    assert "needs-auth" in lines[3]
    assert "…" in lines[3]  # long reason truncated
    # Empty catalog message.
    assert format_mcp_table([]) == "no MCP servers configured"
