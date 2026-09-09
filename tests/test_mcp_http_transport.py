"""HTTP transport + v2 lifecycle tests against tests/fixtures/mcp_http_fixture_server.py.

Every test spawns its own fixture server subprocess on a free loopback
port (offline, tmp_path for ready files, no real ~/.freyja paths) and
drives it through McpManager with sub-second timers.

Covers (card_017): streamable HTTP connect + calls; stateless (no
Mcp-Session-Id) sessions; ${VAR} header expansion; missing secret header
var -> needs-auth; HTTP 401 -> needs-auth (no retry ladder) with the
``/mcp login`` reason; injected auth_factory; explicit SSE transport;
http -> SSE auto-fallback remembered across reconnects; flaky transport
-> rapid-drop budget -> parked; in-flight fast-fail when the server dies
mid-call; elicitation routing (accept / decline / default decline);
sampling decline; tools/list_changed diff refresh (add / update in place
/ remove / poisoned re-description quarantine); result cap; status
snapshot v2 details.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from bridge.mcp.config import McpCatalog, McpServerSpec
from bridge.mcp.connection import ElicitRequest, State, sdk_httpx
from bridge.mcp.manager import McpManager
from bridge.tools.base import ToolCall, ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_http_fixture_server.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpFixture:
    """A running fixture server subprocess."""

    def __init__(self, tmp_path: Path, *args: str, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.ready_file = tmp_path / f"ready-{self.port}-{time.monotonic_ns()}"
        self.args = list(args)
        self.proc: subprocess.Popen[bytes] | None = None

    async def start(self) -> "HttpFixture":
        self.proc = subprocess.Popen(
            [
                sys.executable, str(FIXTURE), "--port", str(self.port),
                "--ready-file", str(self.ready_file), *self.args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.ready_file.exists():
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"fixture server exited early: {self.proc.returncode}")
            await asyncio.sleep(0.02)
        raise RuntimeError("fixture server did not become ready")

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@contextlib.asynccontextmanager
async def http_server(tmp_path: Path, *args: str, port: int | None = None):
    fixture = HttpFixture(tmp_path, *args, port=port)
    await fixture.start()
    try:
        yield fixture
    finally:
        fixture.stop()


def _spec(url: str, name: str = "hsrv", **overrides) -> McpServerSpec:
    kwargs = dict(
        name=name,
        transport="http",
        url=url,
        trust="trusted",
        connect_timeout_s=10.0,
        call_timeout_s=20.0,
    )
    kwargs.update(overrides)
    return McpServerSpec(**kwargs)


def _manager(spec: McpServerSpec, **overrides) -> McpManager:
    kwargs = dict(
        keepalive_interval_s=0.2,
        keepalive_timeout_s=1.0,
        backoff_base_s=0.05,
        backoff_cap_s=0.1,
        backoff_max_retries=2,
        park_probe_interval_s=0.3,
        watchdog=False,
    )
    kwargs.update(overrides)
    return McpManager(McpCatalog(specs={spec.name: spec}), **kwargs)


@contextlib.asynccontextmanager
async def running(spec: McpServerSpec, **overrides):
    manager = _manager(spec, **overrides)
    registry = ToolRegistry()
    try:
        await asyncio.wait_for(manager.start(), timeout=40)
        manager.register_into(registry)
        yield manager, registry
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def _call(registry: ToolRegistry, name: str, arguments: dict) -> object:
    return await registry.execute(ToolCall(id="t1", name=name, arguments=arguments))


async def _wait_until(predicate, timeout_s: float, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


def _text(result) -> str:
    return str(result.content)


# ---------------------------------------------------------------------------
# Streamable HTTP basics
# ---------------------------------------------------------------------------

async def test_streamable_http_connects_and_calls(tmp_path):
    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url)) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn is not None and conn.state is State.ACTIVE, conn.reason
            assert conn.transport_in_use == "http"
            assert conn.protocol_mode == "handshake"
            assert conn.session_id, "stateful server should issue Mcp-Session-Id"
            assert registry.get("mcp__hsrv__echo") is not None
            # The injected description must never register.
            assert registry.get("mcp__hsrv__shady") is None
            result = await _call(registry, "mcp__hsrv__echo", {"text": "over http"})
            assert result.is_error is False and "over http" in _text(result)
            # A successful call proves the session.
            assert conn.session_proven is True
            # Status snapshot carries the v2 details.
            row = manager.status_snapshot()[0]
            assert row["transport_in_use"] == "http"
            assert row["session_proven"] is True
            assert row["quarantined"] == 1  # shady
            quarantine = manager.pending_quarantine()
            assert [q["tool"] for q in quarantine] == ["shady"]
            assert quarantine[0]["server"] == "hsrv" and quarantine[0]["pattern"]


async def test_stateless_server_without_session_id_is_valid(tmp_path):
    async with http_server(tmp_path, "--mode", "stateless") as srv:
        async with running(_spec(srv.url)) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE, conn.reason
            assert conn.session_id is None
            result = await _call(registry, "mcp__hsrv__session_info", {})
            assert json.loads(_text(result))["session_id"] is None
            # Keepalive works without a GET stream / session.
            assert await _wait_until(lambda: conn.last_latency_ms is not None, 5)
            result = await _call(registry, "mcp__hsrv__echo", {"text": "stateless ok"})
            assert "stateless ok" in _text(result)


async def test_headers_expand_env_refs_at_connect_time(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_HDR_A", "alpha-value")
    async with http_server(tmp_path) as srv:
        spec = _spec(
            srv.url,
            headers={"X-Fixture-A": "${FIXTURE_HDR_A}", "X-Fixture-Static": "static-value"},
        )
        async with running(spec) as (manager, registry):
            assert manager.connection("hsrv").state is State.ACTIVE
            result = await _call(registry, "mcp__hsrv__headers_echo", {})
            seen = json.loads(_text(result))
            assert seen == {"x-fixture-a": "alpha-value", "x-fixture-static": "static-value"}


async def test_missing_secret_header_var_marks_needs_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("FIXTURE_API_TOKEN", raising=False)
    async with http_server(tmp_path) as srv:
        spec = _spec(srv.url, headers={"Authorization": "Bearer ${FIXTURE_API_TOKEN}"})
        async with running(spec) as (manager, _registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.NEEDS_AUTH
            assert "FIXTURE_API_TOKEN" in conn.reason
            assert conn.restart_count == 0


# ---------------------------------------------------------------------------
# Auth: 401 -> needs-auth (permanent), auth_factory injection
# ---------------------------------------------------------------------------

async def test_http_401_marks_needs_auth_without_retry_ladder(tmp_path):
    recorder: list[dict] = []
    async with http_server(tmp_path, "--mode", "auth", "--token", "sekrit") as srv:
        async with running(_spec(srv.url), status_listener=recorder.append) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.NEEDS_AUTH, conn.reason
            assert "run /mcp login hsrv" in conn.reason
            assert "401" in conn.reason
            states = [e["state"] for e in recorder]
            assert "backoff" not in states and "parked" not in states
            assert registry.get("mcp__hsrv__echo") is None
            assert manager.proxy_tools() == []


async def test_static_bearer_header_authenticates(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_API_TOKEN", "sekrit")
    async with http_server(tmp_path, "--mode", "auth", "--token", "sekrit") as srv:
        spec = _spec(srv.url, headers={"Authorization": "Bearer ${FIXTURE_API_TOKEN}"})
        async with running(spec) as (manager, registry):
            assert manager.connection("hsrv").state is State.ACTIVE
            result = await _call(registry, "mcp__hsrv__echo", {"text": "authed"})
            assert "authed" in _text(result)


async def test_auth_factory_supplies_httpx_auth(tmp_path):
    httpx_mod = sdk_httpx()
    calls: list[tuple[str, bool]] = []

    class BearerAuth(httpx_mod.Auth):
        def __init__(self, token: str) -> None:
            self.token = token

        def auth_flow(self, request):
            request.headers["Authorization"] = f"Bearer {self.token}"
            yield request

    def factory(spec: McpServerSpec, interactive: bool):
        calls.append((spec.name, interactive))
        return BearerAuth("sekrit")

    async with http_server(tmp_path, "--mode", "auth", "--token", "sekrit") as srv:
        async with running(_spec(srv.url), auth_factory=factory) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE, conn.reason
            assert calls == [("hsrv", False)]  # background connects are never interactive
            result = await _call(registry, "mcp__hsrv__echo", {"text": "via-auth"})
            assert "via-auth" in _text(result)


async def test_auth_factory_raising_needs_auth_shaped_error(tmp_path):
    class NeedsAuthError(Exception):
        pass

    def factory(spec: McpServerSpec, interactive: bool):
        raise NeedsAuthError("no cached tokens for hsrv")

    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url), auth_factory=factory) as (manager, _registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.NEEDS_AUTH
            assert "run /mcp login hsrv" in conn.reason
            assert "no cached tokens" in conn.reason


async def test_auth_factory_returning_none_means_unauthenticated(tmp_path):
    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url), auth_factory=lambda spec, interactive: None) as (manager, _r):
            assert manager.connection("hsrv").state is State.ACTIVE


# ---------------------------------------------------------------------------
# SSE: explicit + auto-fallback
# ---------------------------------------------------------------------------

async def test_explicit_sse_transport(tmp_path):
    async with http_server(tmp_path, "--transport", "sse") as srv:
        async with running(_spec(srv.url, transport="sse")) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE, conn.reason
            assert conn.transport_in_use == "sse"
            result = await _call(registry, "mcp__hsrv__echo", {"text": "sse ok"})
            assert "sse ok" in _text(result)
            assert manager.status_snapshot()[0]["transport"] == "sse"


async def test_http_falls_back_to_sse_and_remembers_it(tmp_path):
    async with http_server(tmp_path, "--transport", "sse") as srv:
        async with running(_spec(srv.url, transport="http")) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE, conn.reason
            assert conn.spec.transport == "http"
            assert conn.transport_in_use == "sse"
            result = await _call(registry, "mcp__hsrv__echo", {"text": "fell back"})
            assert "fell back" in _text(result)
            row = manager.status_snapshot()[0]
            assert row["transport"] == "http" and row["transport_in_use"] == "sse"

            # A reconnect must go straight to SSE (remembered), not re-probe.
            assert conn.sse_fallbacks == 1
            conn.request_reconnect("test-driven reconnect")
            assert await _wait_until(lambda: conn.restart_count == 1 and conn.state is State.ACTIVE, 15)
            assert conn.transport_in_use == "sse"
            assert conn.sse_fallbacks == 1, "reconnect must reuse the remembered transport"
            assert all(s not in (404, 405) for s in conn._http_recorder.statuses)  # noqa: SLF001
            result = await _call(registry, "mcp__hsrv__echo", {"text": "still sse"})
            assert "still sse" in _text(result)


# ---------------------------------------------------------------------------
# Rapid-drop budget / in-flight fast-fail
# ---------------------------------------------------------------------------

async def test_flaky_transport_hits_rapid_drop_budget_and_parks(tmp_path):
    # Every 4th POST drops: initialize, initialized, tools/list succeed and
    # the first keepalive ping dies -> ACTIVE but never proven, x3 -> parked
    # by the rapid-drop budget while the retry ladder still has room.
    recorder: list[dict] = []
    async with http_server(tmp_path, "--mode", "flaky", "--flaky-every", "4") as srv:
        async with running(
            _spec(srv.url), status_listener=recorder.append,
            backoff_max_retries=20, rapid_drop_budget=3, park_probe_interval_s=30.0,
        ) as (manager, registry):
            conn = manager.connection("hsrv")
            assert await _wait_until(lambda: conn.state is State.PARKED, 30), (conn.state, conn.reason)
            assert "3 rapid drops" in conn.reason
            assert conn.rapid_drops == 3
            states = [e["state"] for e in recorder]
            assert states.count("active") == 3
            assert states.count("backoff") == 2  # two drops walked the ladder, the third parked
            assert registry.get("mcp__hsrv__echo") is None


async def test_inflight_call_fails_fast_when_server_dies(tmp_path):
    async with http_server(tmp_path) as srv:
        spec = _spec(srv.url, call_timeout_s=60.0)
        async with running(spec, backoff_max_retries=1, park_probe_interval_s=60.0) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE
            started = time.monotonic()
            task = asyncio.ensure_future(_call(registry, "mcp__hsrv__slow", {"seconds": 30}))
            await asyncio.sleep(0.4)
            assert not task.done()
            srv.kill()
            result = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            assert result.is_error is True
            text = _text(result).lower()
            assert any(k in text for k in ("transport died", "connection closed", "stream ended")), text
            assert elapsed < 15, "in-flight call must not wait out its 60s timeout"
            assert await _wait_until(lambda: conn.state in (State.BACKOFF, State.PARKED), 10)
            assert registry.get("mcp__hsrv__echo") is None


# ---------------------------------------------------------------------------
# Elicitation + sampling
# ---------------------------------------------------------------------------

async def test_elicitation_routes_through_approval_handler(tmp_path):
    seen: list[ElicitRequest] = []

    async def approve(request: ElicitRequest):
        seen.append(request)
        return {"confirm": True}

    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url), approval_handler=approve) as (manager, registry):
            result = await _call(registry, "mcp__hsrv__elicit_me", {"msg": "Proceed with payment?"})
            payload = json.loads(_text(result))
            assert payload == {"action": "accept", "content": {"confirm": True}}
            assert len(seen) == 1
            req = seen[0]
            assert req.server == "hsrv" and req.message == "Proceed with payment?"
            assert req.mode == "form"
            assert req.requested_schema and "confirm" in req.requested_schema["properties"]
            conn = manager.connection("hsrv")
            assert conn.elicitations["requests"] == 1 and conn.elicitations["accepted"] == 1


async def test_elicitation_decline_and_default_handler(tmp_path):
    async def decline(request: ElicitRequest):
        return None

    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url), approval_handler=decline) as (manager, registry):
            result = await _call(registry, "mcp__hsrv__elicit_me", {"msg": "ok?"})
            assert json.loads(_text(result))["action"] == "decline"
            assert manager.connection("hsrv").elicitations["declined"] == 1
        # No handler wired -> declined (never blocks the bridge).
        async with running(_spec(srv.url)) as (manager, registry):
            result = await _call(registry, "mcp__hsrv__elicit_me", {"msg": "ok?"})
            assert json.loads(_text(result))["action"] == "decline"


async def test_sampling_is_declined_with_clean_error(tmp_path):
    async with http_server(tmp_path) as srv:
        async with running(_spec(srv.url)) as (manager, registry):
            result = await _call(registry, "mcp__hsrv__sample_me", {})
            payload = json.loads(_text(result))
            assert "error" in payload
            assert "not supported" in payload["error"].lower()
            conn = manager.connection("hsrv")
            assert conn.sampling_declined == 1
            assert conn.state is State.ACTIVE  # declining never destabilizes the session


# ---------------------------------------------------------------------------
# tools/list_changed diff refresh
# ---------------------------------------------------------------------------

async def test_list_changed_diff_refresh_add_update_remove_poison(tmp_path):
    async with http_server(tmp_path, "--mode", "listchanged") as srv:
        async with running(_spec(srv.url)) as (manager, registry):
            conn = manager.connection("hsrv")
            echo_before = registry.get("mcp__hsrv__echo")
            assert echo_before is not None
            assert registry.get("mcp__hsrv__dynamic_tool") is None

            # add -> registered live, existing proxies untouched (no repave)
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "add"})
            assert await _wait_until(lambda: registry.get("mcp__hsrv__dynamic_tool") is not None, 10)
            dyn = registry.get("mcp__hsrv__dynamic_tool")
            assert registry.get("mcp__hsrv__echo") is echo_before
            result = await _call(registry, "mcp__hsrv__dynamic_tool", {"text": "x"})
            assert "dynamic:x" in _text(result)
            assert "revised" not in dyn.definition.description

            # redescribe -> same proxy object, updated in place
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "redescribe"})
            assert await _wait_until(
                lambda: "revised" in registry.get("mcp__hsrv__dynamic_tool").definition.description, 10
            )
            assert registry.get("mcp__hsrv__dynamic_tool") is dyn

            # remove -> unregistered
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "remove"})
            assert await _wait_until(lambda: registry.get("mcp__hsrv__dynamic_tool") is None, 10)
            assert registry.get("mcp__hsrv__echo") is echo_before

            # poison -> re-added with an injected description: refused + quarantined
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "poison"})
            assert await _wait_until(
                lambda: any(q["tool"] == "dynamic_tool" for q in manager.pending_quarantine()), 10
            )
            await asyncio.sleep(0.1)
            assert registry.get("mcp__hsrv__dynamic_tool") is None
            assert {q["tool"] for q in manager.pending_quarantine()} == {"shady", "dynamic_tool"}
            assert conn.state is State.ACTIVE
            assert conn.tool_count == len(conn.tools)


async def test_poisoned_live_tool_is_unregistered_on_refresh(tmp_path):
    async with http_server(tmp_path, "--mode", "listchanged") as srv:
        async with running(_spec(srv.url)) as (manager, registry):
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "add"})
            assert await _wait_until(lambda: registry.get("mcp__hsrv__dynamic_tool") is not None, 10)
            # Rug pull: a LIVE tool's description changes to an injection.
            await _call(registry, "mcp__hsrv__mutate_tools", {"op": "poison"})
            assert await _wait_until(lambda: registry.get("mcp__hsrv__dynamic_tool") is None, 10)
            assert any(q["tool"] == "dynamic_tool" for q in manager.pending_quarantine())


# ---------------------------------------------------------------------------
# Result cap
# ---------------------------------------------------------------------------

async def test_result_cap_truncates_with_notice(tmp_path):
    async with http_server(tmp_path) as srv:
        spec = _spec(srv.url, max_result_chars=500)
        async with running(spec) as (manager, registry):
            result = await _call(registry, "mcp__hsrv__big", {"chars": 5000})
            text = _text(result)
            assert result.is_error is False
            assert text.startswith("0123456789" * 50)
            assert "TRUNCATED" in text and "4,500" in text and "limits.max_result_chars" in text
            assert len(text) < 800
            small = await _call(registry, "mcp__hsrv__big", {"chars": 400})
            assert len(_text(small)) == 400


# ---------------------------------------------------------------------------
# Reconnect after server restart on the same port (HTTP path)
# ---------------------------------------------------------------------------

async def test_http_server_restart_reconnects_and_reregisters(tmp_path):
    port = _free_port()
    fixture = HttpFixture(tmp_path, port=port)
    await fixture.start()
    try:
        async with running(_spec(fixture.url), backoff_max_retries=30, backoff_cap_s=0.3) as (manager, registry):
            conn = manager.connection("hsrv")
            assert conn.state is State.ACTIVE
            await _call(registry, "mcp__hsrv__echo", {"text": "before"})
            fixture.kill()
            assert await _wait_until(lambda: registry.get("mcp__hsrv__echo") is None, 15)
            fixture = HttpFixture(tmp_path, port=port)
            await fixture.start()
            assert await _wait_until(lambda: registry.get("mcp__hsrv__echo") is not None, 30)
            assert conn.restart_count >= 1
            result = await _call(registry, "mcp__hsrv__echo", {"text": "after"})
            assert "after" in _text(result)
    finally:
        fixture.stop()


# ---------------------------------------------------------------------------
# No orphans: stop() leaves no supervisor/refresh tasks or open HTTP clients
# ---------------------------------------------------------------------------

async def test_stop_leaves_no_orphan_tasks_or_clients(tmp_path):
    async with http_server(tmp_path, "--mode", "listchanged") as srv:
        manager = _manager(_spec(srv.url))
        registry = ToolRegistry()
        await asyncio.wait_for(manager.start(), timeout=40)
        manager.register_into(registry)
        conn = manager.connection("hsrv")
        assert conn.state is State.ACTIVE
        # Exercise a refresh task and an in-flight call, then stop underneath them.
        await _call(registry, "mcp__hsrv__mutate_tools", {"op": "add"})
        assert await _wait_until(lambda: registry.get("mcp__hsrv__dynamic_tool") is not None, 10)
        pending = asyncio.ensure_future(_call(registry, "mcp__hsrv__slow", {"seconds": 30}))
        await asyncio.sleep(0.3)
        await asyncio.wait_for(manager.stop(), timeout=30)
        result = await asyncio.wait_for(pending, timeout=5)
        assert result.is_error is True  # fast-failed, not waiting out 30s
        await asyncio.sleep(0.05)
        leftover = [
            t.get_name() for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and t.get_name().startswith("mcp-")
        ]
        assert leftover == [], leftover
        assert conn.state is State.DISABLED and conn._session is None  # noqa: SLF001
        assert registry.get("mcp__hsrv__echo") is None
        assert manager.proxy_tools() == []
        # The server we spawned is the only fixture process and still ours to stop.
        assert srv.proc is not None and srv.proc.poll() is None
