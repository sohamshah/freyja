"""End-to-end OAuth lifecycle through the /mcp command set (card_019).

The HTTP fixture MCP server runs in ``auth`` mode with a fixed bearer
token and a ``WWW-Authenticate`` header pointing at the fake OAuth
authorization server's protected-resource metadata; the fake AS mints
exactly that bearer. Everything is loopback; roots are tmp_path.

    /mcp add <fixture url>   -> probe detects 401 + resource_metadata -> auth: oauth
    server enabled           -> needs-auth (non-interactive auth factory)
    /mcp login <server>      -> test flow visits the authorize URL -> tokens 0600
                             -> mcp_oauth_url / mcp_oauth_result / mcp_status emitted
                             -> server ACTIVE, proxy tools registered
    mcp__<server>__echo call -> real tool round-trip with the bearer
    /mcp logout <server>     -> stored state removed -> needs-auth
    /mcp reauth <server>     -> recovers to ACTIVE
Plus: the token-file watch reconnects a needs-auth server when another
process drops tokens.json, and one login per server at a time.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from fake_oauth_as import FakeAuthorizationServer, visit_authorization_url  # noqa: E402
from test_mcp_http_transport import _call, _wait_until, http_server  # noqa: E402

from bridge.mcp.commands import (  # noqa: E402
    handle_mcp_command,
    make_auth_factory,
    wait_background_tasks,
)
from bridge.mcp.connection import State  # noqa: E402
from bridge.mcp.manager import McpManager  # noqa: E402
from bridge.mcp.oauth import callback as cb  # noqa: E402
from bridge.mcp.oauth.flow import HandoffFlow  # noqa: E402
from bridge.tools.base import ToolRegistry  # noqa: E402

FIXED_TOKEN = "fixture-e2e-bearer-token-XYZ"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "home"))
    cb.reset_port_state_for_tests()
    yield
    cb.reset_port_state_for_tests()


class AutoBrowser:
    """HandoffFlow whose on_authorize_url acts like a browser: follows the
    authorize 302 to the loopback callback in a worker thread."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.flow = HandoffFlow(self._present)

    async def _present(self, url: str) -> None:
        self.urls.append(url)
        self.tasks.append(asyncio.create_task(asyncio.to_thread(visit_authorization_url, url)))

    async def drain(self) -> None:
        for task in self.tasks:
            try:
                await task
            except Exception:  # noqa: BLE001
                pass


def _manager(path: Path, tmp_path: Path, **overrides) -> McpManager:
    kwargs = dict(
        keepalive_interval_s=0.3,
        keepalive_timeout_s=2.0,
        backoff_base_s=0.05,
        backoff_cap_s=0.1,
        backoff_max_retries=1,
        park_probe_interval_s=0.3,
        watchdog=False,
        auth_factory=make_auth_factory(tmp_path / "tokens"),
        token_root=tmp_path / "tokens",
        token_watch_interval_s=0.2,
        run_dir=tmp_path / "mcp-run",
    )
    kwargs.update(overrides)
    return McpManager.load(path, **kwargs)


def _events_of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("type") == kind]


def _text(result) -> str:
    content = result.content
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


async def test_oauth_lifecycle_add_login_call_logout_reauth(tmp_path):
    fake = FakeAuthorizationServer(fixed_access_token=FIXED_TOKEN)
    fake.start()
    events: list[dict] = []
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": {}}), encoding="utf-8")
    manager = _manager(path, tmp_path)
    registry = ToolRegistry()
    manager.register_into(registry)
    token_root = tmp_path / "tokens"
    try:
        async with http_server(
            tmp_path, "--mode", "auth", "--token", FIXED_TOKEN,
            "--resource-metadata", fake.prm_url,
        ) as fixture:
            fake.resource_override = fixture.url

            # -- add: discovery detects OAuth -----------------------------------
            result = await handle_mcp_command(
                manager,
                {"action": "add", "args": [fixture.url, "--name", "e2e", "--enable"]},
                emit=events.append, token_root=token_root,
            )
            assert result["ok"] is True, result["message"]
            spec = manager.specs["e2e"]
            assert spec.auth == "oauth"
            assert spec.transport == "http"
            assert spec.oauth == {"resource_metadata_url": fake.prm_url}
            probe = result["data"]["probe"]
            assert probe["status"] == 401 and probe["auth"] == "oauth"
            assert "/mcp login e2e" in result["message"]
            on_disk = json.loads(path.read_text(encoding="utf-8"))["servers"]["e2e"]
            assert on_disk["auth"] == "oauth" and on_disk["enabled"] is True
            # Enabled at add time -> connected non-interactively -> needs-auth.
            conn = manager.connection("e2e")
            assert conn is not None and conn.state is State.NEEDS_AUTH
            row = result["servers"][0]
            assert row["needs_auth"] is True
            assert row["login_hint"] == "/mcp login e2e"
            assert row["has_tokens"] is False
            assert registry.get("mcp__e2e__echo") is None
            assert _events_of(events, "mcp_status"), "add must emit mcp_status"

            # -- login -----------------------------------------------------------
            browser = AutoBrowser()
            events.clear()
            result = await asyncio.wait_for(
                handle_mcp_command(
                    manager, {"action": "login", "server": "e2e"},
                    surface="test", flow=browser.flow, emit=events.append,
                    token_root=token_root, login_timeout_s=30,
                ),
                timeout=60,
            )
            await browser.drain()
            assert result["ok"] is True, result["message"]
            assert len(browser.urls) == 1
            assert fake.hits["authorize"] >= 1 and fake.hits["token"] >= 1
            oauth_results = _events_of(events, "mcp_oauth_result")
            assert len(oauth_results) == 1
            assert oauth_results[0]["server"] == "e2e" and oauth_results[0]["ok"] is True
            assert oauth_results[0]["expiresAt"] is not None
            assert _events_of(events, "mcp_status"), "login must emit mcp_status"
            # Tokens persisted 0600 under the tmp root; the value never leaks.
            tokens_file = token_root / "e2e" / "tokens.json"
            assert tokens_file.is_file()
            assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
            assert json.loads(tokens_file.read_text())["access_token"] == FIXED_TOKEN
            assert FIXED_TOKEN not in result["message"]
            assert FIXED_TOKEN not in json.dumps(events)
            # ACTIVE + tools registered.
            conn = manager.connection("e2e")
            assert conn is not None and conn.state is State.ACTIVE, conn.reason
            assert result["servers"][0]["state"] == "active"
            assert result["servers"][0]["has_tokens"] is True
            assert result["servers"][0]["token_expires_at"] is not None
            assert manager.token_expires_at("e2e") is not None
            assert await _wait_until(lambda: registry.get("mcp__e2e__echo") is not None, 5)

            # -- real tool call with the bearer -----------------------------------
            call = await _call(registry, "mcp__e2e__echo", {"text": "oauth-ok"})
            assert not call.is_error
            assert "oauth-ok" in _text(call)
            call2 = await handle_mcp_command(
                manager, {"action": "call", "server": "e2e", "tool": "echo",
                          "args": ['{"text": "via-command"}']},
            )
            assert call2["ok"] is True and "via-command" in call2["content"]

            # -- status shows the oauth details -------------------------------------
            status = await handle_mcp_command(manager, {"action": "status"})
            row = status["servers"][0]
            assert row["auth"] == "oauth" and row["has_tokens"] is True
            assert row["needs_auth"] is False
            assert "auth=oauth" in status["message"]
            assert "token expires" in status["message"]

            # -- logout -------------------------------------------------------------------
            events.clear()
            result = await asyncio.wait_for(
                handle_mcp_command(
                    manager, {"action": "logout", "server": "e2e"},
                    emit=events.append, token_root=token_root,
                ),
                timeout=60,
            )
            assert result["ok"] is True, result["message"]
            assert not tokens_file.exists()
            conn = manager.connection("e2e")
            assert conn is not None and conn.state is State.NEEDS_AUTH
            assert result["servers"][0]["needs_auth"] is True
            assert "/mcp login e2e" in result["message"]
            assert await _wait_until(lambda: registry.get("mcp__e2e__echo") is None, 5)
            assert _events_of(events, "mcp_status")

            # -- reauth recovers ----------------------------------------------------------
            browser = AutoBrowser()
            events.clear()
            result = await asyncio.wait_for(
                handle_mcp_command(
                    manager, {"action": "reauth", "server": "e2e"},
                    flow=browser.flow, emit=events.append, token_root=token_root,
                    login_timeout_s=30,
                ),
                timeout=60,
            )
            await browser.drain()
            assert result["ok"] is True, result["message"]
            assert result["action"] == "reauth"
            assert tokens_file.is_file()
            conn = manager.connection("e2e")
            assert conn is not None and conn.state is State.ACTIVE, conn.reason
            assert await _wait_until(lambda: registry.get("mcp__e2e__echo") is not None, 5)
            call = await _call(registry, "mcp__e2e__echo", {"text": "after-reauth"})
            assert "after-reauth" in _text(call)

            # -- remove --purge cleans the token dir -------------------------------------
            result = await handle_mcp_command(
                manager, {"action": "remove", "server": "e2e", "args": ["--purge"]},
                token_root=token_root,
            )
            assert result["ok"] is True
            assert result["data"]["purged"] is True
            assert not (token_root / "e2e").exists()
            assert "e2e" not in manager.specs
            assert manager.connection("e2e") is None
            assert "e2e" not in json.loads(path.read_text())["servers"]
            assert await _wait_until(lambda: registry.get("mcp__e2e__echo") is None, 5)
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)
        await wait_background_tasks(timeout=5)
        fake.stop()


async def test_reauth_all_iterates_enabled_oauth_servers(tmp_path):
    fake = FakeAuthorizationServer(fixed_access_token=FIXED_TOKEN)
    fake.start()
    path = tmp_path / "mcp.json"
    token_root = tmp_path / "tokens"
    try:
        async with http_server(
            tmp_path, "--mode", "auth", "--token", FIXED_TOKEN,
            "--resource-metadata", fake.prm_url,
        ) as fixture:
            fake.resource_override = fixture.url
            path.write_text(json.dumps({"version": 1, "servers": {
                "one": {"transport": "http", "url": fixture.url, "auth": "oauth",
                        "enabled": True, "trust": "trusted"},
                "two": {"transport": "http", "url": fixture.url, "auth": "oauth",
                        "enabled": False, "trust": "trusted"},
                "plain": {"transport": "http", "url": fixture.url, "enabled": False},
            }}), encoding="utf-8")
            manager = _manager(path, tmp_path)
            try:
                await asyncio.wait_for(manager.start(), timeout=40)
                assert manager.connection("one").state is State.NEEDS_AUTH
                browser = AutoBrowser()
                result = await asyncio.wait_for(
                    handle_mcp_command(
                        manager, {"action": "reauth", "args": ["--all"]},
                        flow=browser.flow, token_root=token_root, login_timeout_s=30,
                    ),
                    timeout=90,
                )
                await browser.drain()
                assert result["ok"] is True, result["message"]
                # Only enabled OAuth servers are targeted: 'two' is disabled,
                # 'plain' is not oauth.
                assert [r["server"] for r in result["rows"]] == ["one"]
                assert result["rows"][0]["ok"] is True
                assert "re-authenticated 1/1" in result["message"]
                assert manager.connection("one").state is State.ACTIVE
                assert (token_root / "one" / "tokens.json").is_file()
                assert not (token_root / "two").exists()
            finally:
                await asyncio.wait_for(manager.stop(), timeout=30)
    finally:
        fake.stop()


async def test_login_concurrency_guard_one_per_server(tmp_path):
    fake = FakeAuthorizationServer(fixed_access_token=FIXED_TOKEN)
    fake.start()
    path = tmp_path / "mcp.json"
    token_root = tmp_path / "tokens"
    try:
        async with http_server(
            tmp_path, "--mode", "auth", "--token", FIXED_TOKEN,
            "--resource-metadata", fake.prm_url,
        ) as fixture:
            fake.resource_override = fixture.url
            path.write_text(json.dumps({"version": 1, "servers": {
                "guarded": {"transport": "http", "url": fixture.url, "auth": "oauth",
                            "enabled": False, "trust": "trusted"},
            }}), encoding="utf-8")
            manager = _manager(path, tmp_path)
            try:
                release = asyncio.Event()
                urls: list[str] = []

                async def _hold(url: str) -> None:
                    urls.append(url)
                    await release.wait()
                    await asyncio.to_thread(visit_authorization_url, url)

                first = asyncio.create_task(handle_mcp_command(
                    manager, {"action": "login", "server": "guarded"},
                    flow=HandoffFlow(_hold), token_root=token_root, login_timeout_s=30,
                ))
                assert await _wait_until(lambda: bool(urls), 10)
                assert "guarded" in manager.logins_in_progress
                second = await handle_mcp_command(
                    manager, {"action": "login", "server": "guarded"},
                    flow=HandoffFlow(_hold), token_root=token_root,
                )
                assert second["ok"] is False
                assert "already in progress" in second["message"]
                release.set()
                result = await asyncio.wait_for(first, timeout=60)
                assert result["ok"] is True, result["message"]
                assert "guarded" not in manager.logins_in_progress
                # login on a disabled server enables it (flips mcp.json).
                assert manager.specs["guarded"].enabled is True
                assert json.loads(path.read_text())["servers"]["guarded"]["enabled"] is True
                assert manager.connection("guarded").state is State.ACTIVE
            finally:
                await asyncio.wait_for(manager.stop(), timeout=30)
    finally:
        fake.stop()


async def test_token_file_watch_reconnects_needs_auth_server(tmp_path):
    """Another process (the desktop bridge) logs in and writes tokens.json;
    this process's needs-auth server notices on the next watch tick."""
    fake = FakeAuthorizationServer(fixed_access_token=FIXED_TOKEN)
    fake.start()
    path = tmp_path / "mcp.json"
    token_root = tmp_path / "tokens"
    try:
        async with http_server(
            tmp_path, "--mode", "auth", "--token", FIXED_TOKEN,
            "--resource-metadata", fake.prm_url,
        ) as fixture:
            fake.resource_override = fixture.url
            path.write_text(json.dumps({"version": 1, "servers": {
                "watched": {"transport": "http", "url": fixture.url, "auth": "oauth",
                            "enabled": True, "trust": "trusted"},
            }}), encoding="utf-8")
            manager = _manager(path, tmp_path)
            registry = ToolRegistry()
            manager.register_into(registry)
            try:
                await asyncio.wait_for(manager.start(), timeout=40)
                conn = manager.connection("watched")
                assert conn.state is State.NEEDS_AUTH
                assert manager.token_file_for(conn.spec) == token_root / "watched" / "tokens.json"
                # Let the watch record its baseline (no file yet).
                await asyncio.sleep(0.5)
                assert manager.token_reconnects == 0

                # "Other process" completes a login: run_login writes the
                # token files through the same storage layout.
                from bridge.mcp.oauth.flow import run_login

                browser = AutoBrowser()
                login = await asyncio.wait_for(
                    run_login(conn.spec, flow=browser.flow, storage_root=token_root, timeout=30),
                    timeout=60,
                )
                await browser.drain()
                assert login.ok, login.error

                assert await _wait_until(lambda: manager.token_reconnects >= 1, 10)
                assert await _wait_until(
                    lambda: (manager.connection("watched") or conn).state is State.ACTIVE, 20,
                )
                assert await _wait_until(lambda: registry.get("mcp__watched__echo") is not None, 5)
                call = await _call(registry, "mcp__watched__echo", {"text": "watched"})
                assert "watched" in _text(call)
                # Steady state: no further reconnects while active.
                before = manager.token_reconnects
                await asyncio.sleep(0.6)
                assert manager.token_reconnects == before
            finally:
                await asyncio.wait_for(manager.stop(), timeout=30)
    finally:
        fake.stop()


async def test_token_watch_tick_ignores_deleted_and_unchanged_files(tmp_path):
    """Direct tick semantics without a network: baseline first, unchanged
    mtime -> nothing, file appearing -> reconnect (via a stub), deletion ->
    nothing."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": {
        "s": {"transport": "http", "url": "http://127.0.0.1:9/mcp", "auth": "oauth",
              "enabled": True},
    }}), encoding="utf-8")
    manager = _manager(path, tmp_path, token_watch_interval_s=0)
    spec = manager.specs["s"]

    class _FakeConn:
        def __init__(self) -> None:
            self.spec = spec
            self.state = State.NEEDS_AUTH

    manager._connections["s"] = _FakeConn()  # noqa: SLF001
    calls: list[str] = []

    async def _fake_reconnect(name: str, reason: str = "") -> dict:
        calls.append(name)
        return {}

    manager.reconnect = _fake_reconnect  # type: ignore[method-assign]
    token_file = manager.token_file_for(spec)
    assert token_file == tmp_path / "tokens" / "s" / "tokens.json"

    assert await manager.token_watch_tick() == []  # baseline (no file)
    assert await manager.token_watch_tick() == []  # unchanged
    token_file.parent.mkdir(parents=True)
    token_file.write_text(json.dumps({"access_token": "x", "expires_at": 4102444800}))
    assert await manager.token_watch_tick() == ["s"]
    assert calls == ["s"]
    assert manager.token_expires_at("s") == 4102444800
    assert manager.has_tokens("s") is True
    # Same mtime again -> nothing; deletion -> nothing (needs-auth is right).
    assert await manager.token_watch_tick() == []
    token_file.unlink()
    assert await manager.token_watch_tick() == []
    assert calls == ["s"]
    # A login in progress in this process suppresses the watch.
    token_file.write_text(json.dumps({"access_token": "y"}))
    manager.logins_in_progress.add("s")
    assert await manager.token_watch_tick() == []
    manager.logins_in_progress.discard("s")
    os.utime(token_file, (1, 1))
    assert await manager.token_watch_tick() == ["s"]
