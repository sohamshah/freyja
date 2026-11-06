"""HTTP transport + v2 lifecycle tests against tests/fixtures/mcp_http_fixture_server.py.

Fixture servers run as subprocesses on free loopback ports (offline,
tmp dirs for ready files, no real ~/.freyja paths). Modes with no
mutable server state (normal, stateless, auth, sse) are started once per
module and shared; tests that kill the server or mutate its tool list
(flaky, listchanged, restart) spawn their own. Every test drives the
server through McpManager with sub-second timers.

Covers (card_017): streamable HTTP connect + calls; stateless (no
Mcp-Session-Id) sessions; ${VAR} header expansion; HTTP 401 -> needs-auth
(no retry ladder) with the ``/mcp login`` reason, then a static bearer
header or an injected auth_factory authenticating; explicit SSE transport
and http -> SSE auto-fallback remembered across reconnects; flaky
transport -> rapid-drop budget -> parked; in-flight fast-fail when the
server dies mid-call; sampling decline; tools/list_changed diff refresh
(add / update in place / remove / poisoned re-description quarantine);
result cap; restart on the same port; clean stop.

``http_server``, ``_wait_until`` and ``_call`` are imported by the
commands_v2, oauth_e2e, bridge_wiring and gateway_mcp_v2 tests.
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

import pytest

from bridge.mcp.config import McpCatalog, McpServerSpec
from bridge.mcp.connection import State, sdk_httpx
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

    def _launch(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable, str(FIXTURE), "--port", str(self.port),
                "--ready-file", str(self.ready_file), *self.args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _ready(self) -> bool:
        if self.ready_file.exists():
            return True
        assert self.proc is not None
        if self.proc.poll() is not None:
            raise RuntimeError(f"fixture server exited early: {self.proc.returncode}")
        return False

    async def start(self) -> "HttpFixture":
        self._launch()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._ready():
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("fixture server did not become ready")

    def start_blocking(self) -> "HttpFixture":
        """Synchronous start for module-scoped fixtures."""
        self._launch()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._ready():
                return self
            time.sleep(0.02)
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


def _shared_server(tmp_path_factory, *args: str):
    fixture = HttpFixture(tmp_path_factory.mktemp("http-fixture"), *args).start_blocking()
    yield fixture
    fixture.kill()  # nothing needs a graceful exit; a SIGTERM wait costs ~0.2 s each


@pytest.fixture(scope="module")
def normal_server(tmp_path_factory):
    yield from _shared_server(tmp_path_factory)


@pytest.fixture(scope="module")
def stateless_server(tmp_path_factory):
    yield from _shared_server(tmp_path_factory, "--mode", "stateless")


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory):
    yield from _shared_server(tmp_path_factory, "--mode", "auth", "--token", "sekrit")


@pytest.fixture(scope="module")
def sse_server(tmp_path_factory):
    yield from _shared_server(tmp_path_factory, "--transport", "sse")


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

async def test_streamable_http_connects_and_calls(normal_server):
    async with running(_spec(normal_server.url)) as (manager, registry):
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


async def test_stateless_server_without_session_id_is_valid(stateless_server):
    async with running(_spec(stateless_server.url)) as (manager, registry):
        conn = manager.connection("hsrv")
        assert conn.state is State.ACTIVE, conn.reason
        assert conn.session_id is None
        result = await _call(registry, "mcp__hsrv__session_info", {})
        assert json.loads(_text(result))["session_id"] is None
        # Keepalive works without a GET stream / session.
        assert await _wait_until(lambda: conn.last_latency_ms is not None, 5)
        result = await _call(registry, "mcp__hsrv__echo", {"text": "stateless ok"})
        assert "stateless ok" in _text(result)


async def test_headers_expand_env_refs_at_connect_time(normal_server, monkeypatch):
    monkeypatch.setenv("FIXTURE_HDR_A", "alpha-value")
    spec = _spec(
        normal_server.url,
        headers={"X-Fixture-A": "${FIXTURE_HDR_A}", "X-Fixture-Static": "static-value"},
    )
    async with running(spec) as (manager, registry):
        assert manager.connection("hsrv").state is State.ACTIVE
        result = await _call(registry, "mcp__hsrv__headers_echo", {})
        seen = json.loads(_text(result))
        assert seen == {"x-fixture-a": "alpha-value", "x-fixture-static": "static-value"}


# ---------------------------------------------------------------------------
# Auth: 401 -> needs-auth (permanent); static header; auth_factory injection
# ---------------------------------------------------------------------------

async def test_auth_server_401_needs_auth_then_header_or_auth_factory_authenticates(
    auth_server, monkeypatch,
):
    recorder: list[dict] = []
    async with running(
        _spec(auth_server.url), status_listener=recorder.append,
    ) as (manager, registry):
        conn = manager.connection("hsrv")
        assert conn.state is State.NEEDS_AUTH, conn.reason
        assert "run /mcp login hsrv" in conn.reason
        assert "401" in conn.reason
        states = [e["state"] for e in recorder]
        assert "backoff" not in states and "parked" not in states
        assert registry.get("mcp__hsrv__echo") is None
        assert manager.proxy_tools() == []

    monkeypatch.setenv("FIXTURE_API_TOKEN", "sekrit")
    spec = _spec(auth_server.url, headers={"Authorization": "Bearer ${FIXTURE_API_TOKEN}"})
    async with running(spec) as (manager, registry):
        assert manager.connection("hsrv").state is State.ACTIVE
        result = await _call(registry, "mcp__hsrv__echo", {"text": "authed"})
        assert "authed" in _text(result)

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

    async with running(_spec(auth_server.url), auth_factory=factory) as (manager, registry):
        conn = manager.connection("hsrv")
        assert conn.state is State.ACTIVE, conn.reason
        assert calls == [("hsrv", False)]  # background connects are never interactive
        result = await _call(registry, "mcp__hsrv__echo", {"text": "via-auth"})
        assert "via-auth" in _text(result)


# ---------------------------------------------------------------------------
# SSE: explicit + auto-fallback remembered across reconnects
# ---------------------------------------------------------------------------

async def test_sse_explicit_and_http_fallback_remembered_across_reconnect(sse_server):
    async with running(_spec(sse_server.url, transport="sse")) as (manager, registry):
        conn = manager.connection("hsrv")
        assert conn.state is State.ACTIVE, conn.reason
        assert conn.transport_in_use == "sse"
        result = await _call(registry, "mcp__hsrv__echo", {"text": "sse ok"})
        assert "sse ok" in _text(result)
        assert manager.status_snapshot()[0]["transport"] == "sse"

    async with running(_spec(sse_server.url, transport="http")) as (manager, registry):
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
            _spec(srv.url), status_listener=recorder.append, keepalive_interval_s=0.05,
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
            assert await _wait_until(lambda: len(conn._inflight) == 1, 5)  # noqa: SLF001
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
# Sampling (elicitation round trips live in test_mcp_bridge_wiring.py)
# ---------------------------------------------------------------------------

async def test_sampling_is_declined_with_clean_error(normal_server):
    async with running(_spec(normal_server.url)) as (manager, registry):
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


# ---------------------------------------------------------------------------
# Result cap
# ---------------------------------------------------------------------------

async def test_result_cap_truncates_with_notice(normal_server):
    spec = _spec(normal_server.url, max_result_chars=500)
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
        async with running(
            _spec(fixture.url), keepalive_interval_s=0.05, backoff_max_retries=30, backoff_cap_s=0.15,
        ) as (manager, registry):
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
        assert await _wait_until(lambda: len(conn._inflight) == 1, 5)  # noqa: SLF001
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
