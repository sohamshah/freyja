"""mcp_command handler tests: status snapshot shape, enable/disable
mutating the catalog on disk + the live registry, reload diffing, the
login on non-OAuth servers, and the /mcp table/arg helpers.
(v2 OAuth login/logout/reauth/add/remove/test/tools/catalog live in
tests/test_mcp_commands_v2.py.)

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
    parse_mcp_args,
)
from bridge.mcp.manager import McpManager
from bridge.tools.base import ToolRegistry

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"

SNAPSHOT_KEYS = {
    "server", "transport", "state", "reason", "since", "tool_count",
    "restart_count", "last_latency_ms", "enabled", "schema_chars",
}


def _write_catalog(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"version": 1, "servers": servers}), encoding="utf-8"
    )
    return path


def _three_server_catalog(tmp_path: Path) -> Path:
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
        "tokeny": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "enabled": False,
            "env": {"FAKE_TOKEN": "${FREYJA_TEST_FAKE_TOKEN_XYZ}"},
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
# status
# ---------------------------------------------------------------------------

async def test_status_snapshot_shape(tmp_path):
    manager = _manager(_three_server_catalog(tmp_path), tmp_path)
    result = await handle_mcp_command(manager, {"action": "status"})
    assert result["ok"] is True
    assert result["action"] == "status"
    servers = {row["server"]: row for row in result["servers"]}
    assert set(servers) == {"fixture", "hosted", "tokeny"}
    for row in servers.values():
        assert SNAPSHOT_KEYS <= set(row), row
    assert servers["fixture"]["state"] == "disabled"
    assert servers["fixture"]["enabled"] is False
    assert servers["hosted"]["transport"] == "http"


async def test_default_and_unknown_actions(tmp_path):
    manager = _manager(_three_server_catalog(tmp_path), tmp_path)
    # Missing action defaults to status.
    result = await handle_mcp_command(manager, {})
    assert result["ok"] is True and result["action"] == "status"
    # Unknown action is a clean error, not an exception.
    result = await handle_mcp_command(manager, {"action": "explode"})
    assert result["ok"] is False
    assert "unknown mcp_command action" in result["message"]
    # enable/disable/login without a server -> usage error.
    result = await handle_mcp_command(manager, {"action": "enable"})
    assert result["ok"] is False and "usage" in result["message"]
    # Unknown server name.
    result = await handle_mcp_command(manager, {"action": "enable", "server": "nope"})
    assert result["ok"] is False and "unknown MCP server" in result["message"]


# ---------------------------------------------------------------------------
# enable / disable: catalog on disk + live registry
# ---------------------------------------------------------------------------

async def test_enable_disable_mutate_catalog_and_registry(tmp_path):
    path = _three_server_catalog(tmp_path)
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

        # mcp.json flipped on disk (save_catalog path).
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["servers"]["fixture"]["enabled"] is True
        # Other entries preserved.
        assert doc["servers"]["hosted"]["oauth"]["client_id"] == "abc"

        # Live registry mutated without a restart.
        assert registry.get("mcp__fixture__echo") is not None
        # Status transitions surfaced through the listener.
        assert any(e["state"] == "active" for e in events)

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
# call: direct proxy-tool invocation (debug/E2E surface)
# ---------------------------------------------------------------------------

async def test_call_invokes_registered_proxy_tool(tmp_path):
    path = _three_server_catalog(tmp_path)
    manager = _manager(path, tmp_path)
    try:
        await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "enable", "server": "fixture"}),
            timeout=60,
        )
        result = await asyncio.wait_for(
            handle_mcp_command(
                manager,
                {
                    "action": "call",
                    "server": "fixture",
                    "tool": "echo",
                    "args": {"text": "ping-42"},
                },
            ),
            timeout=60,
        )
        assert result["ok"] is True, result
        assert result["tool_name"] == "mcp__fixture__echo"
        assert result["is_error"] is False
        assert "ping-42" in result["content"]

        # Matching by the full proxy name works too.
        result = await asyncio.wait_for(
            handle_mcp_command(
                manager,
                {
                    "action": "call",
                    "server": "fixture",
                    "tool": "mcp__fixture__echo",
                    "args": {"text": "by-proxy-name"},
                },
            ),
            timeout=60,
        )
        assert result["ok"] is True and "by-proxy-name" in result["content"]

        # A failing remote tool surfaces is_error without raising.
        result = await asyncio.wait_for(
            handle_mcp_command(
                manager,
                {"action": "call", "server": "fixture", "tool": "fail", "args": {}},
            ),
            timeout=60,
        )
        assert result["ok"] is False and result["is_error"] is True

        # Validation: missing tool arg, unknown tool, non-dict args.
        result = await handle_mcp_command(
            manager, {"action": "call", "server": "fixture"}
        )
        assert result["ok"] is False and "usage" in result["message"]
        result = await handle_mcp_command(
            manager, {"action": "call", "server": "fixture", "tool": "nope"}
        )
        assert result["ok"] is False and "available" in result["message"]
        result = await handle_mcp_command(
            manager,
            {
                "action": "call",
                "server": "fixture",
                "tool": "echo",
                "args": "not-a-dict",
            },
        )
        assert result["ok"] is False and "JSON object" in result["message"]
    finally:
        await manager.stop()



# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

async def test_reload_applies_catalog_diff(tmp_path):
    path = _three_server_catalog(tmp_path)
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


async def test_reload_without_catalog_path(tmp_path):
    from bridge.mcp.config import McpCatalog

    manager = McpManager(McpCatalog(), watchdog=False)
    result = await handle_mcp_command(manager, {"action": "reload"})
    assert result["ok"] is True
    assert "reload unavailable" in result["message"]


# ---------------------------------------------------------------------------
# login (v1 stub)
# ---------------------------------------------------------------------------

async def test_login_env_token_server_reports_missing_keys(tmp_path):
    manager = _manager(_three_server_catalog(tmp_path), tmp_path)
    result = await handle_mcp_command(
        manager, {"action": "login", "server": "tokeny"}, environ={}
    )
    assert result["ok"] is False
    assert result["missing_env"] == ["FREYJA_TEST_FAKE_TOKEN_XYZ"]
    assert "FREYJA_TEST_FAKE_TOKEN_XYZ" in result["message"]
    assert ".env" in result["message"]

    # With the var present the stub reports nothing missing.
    result = await handle_mcp_command(
        manager,
        {"action": "login", "server": "tokeny"},
        environ={"FREYJA_TEST_FAKE_TOKEN_XYZ": "xoxb-123"},
    )
    assert result["ok"] is True
    assert result["missing_env"] == []


async def test_login_explicit_non_oauth_auth_with_oauth_block_gets_clear_message(tmp_path):
    """An ``oauth`` block alone implies OAuth (Claude Code plugin shape); only an
    explicit non-oauth ``auth`` makes login a misconfiguration the handler
    explains instead of touching the network."""
    import json

    path = _three_server_catalog(tmp_path)
    doc = json.loads(path.read_text())
    servers = doc.get("servers") or doc.get("mcpServers")
    servers["hosted"]["auth"] = "none"
    path.write_text(json.dumps(doc))
    manager = _manager(path, tmp_path)
    result = await handle_mcp_command(manager, {"action": "login", "server": "hosted"})
    assert result["ok"] is False
    assert "not configured for OAuth" in result["message"]
    assert '"auth": "oauth"' in result["message"]
    assert "/mcp login hosted" in result["message"]


def test_missing_env_refs_scans_all_fields(tmp_path):
    from bridge.mcp.config import McpServerSpec

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


# ---------------------------------------------------------------------------
# Bridge stdin-IPC branch (freyja_bridge._handle_command)
# ---------------------------------------------------------------------------

async def test_bridge_mcp_command_ipc_branch(tmp_path, monkeypatch):
    """The mcp_command branch in _handle_command replies with a single
    mcp_command_result event (requestId passthrough) via emit()."""
    import types

    import bridge.freyja_bridge as fb

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda e: events.append(e))
    manager = _manager(_three_server_catalog(tmp_path), tmp_path)
    state = types.SimpleNamespace(mcp_manager=manager, active_session_id=None)

    await fb._handle_command(
        state, {"type": "mcp_command", "action": "status", "requestId": "r1"}
    )
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "mcp_command_result"
    assert event["ok"] is True
    assert event["requestId"] == "r1"
    assert {row["server"] for row in event["servers"]} == {
        "fixture", "hosted", "tokeny",
    }

    # Unknown action comes back as a clean ok=False result, not a crash.
    events.clear()
    await fb._handle_command(
        state, {"type": "mcp_command", "action": "detonate"}
    )
    assert events[0]["ok"] is False
    assert "unknown mcp_command action" in events[0]["message"]


# ---------------------------------------------------------------------------
# /mcp helpers
# ---------------------------------------------------------------------------

def test_parse_mcp_args():
    assert parse_mcp_args("") == ("status", "")
    assert parse_mcp_args("  ") == ("status", "")
    assert parse_mcp_args("status") == ("status", "")
    assert parse_mcp_args("Enable slack") == ("enable", "slack")
    assert parse_mcp_args("login slack extra-noise") == ("login", "slack")


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
