"""Desktop bridge wiring for MCP v2 (card_019): the ElicitationBridge
round-trip, the manager hooks (auth_factory + approval_handler), the
mcp_command background dispatch and the event emission points
(mcp_oauth_url / mcp_oauth_result / mcp_status / mcp_elicitation).

Offline; fixture servers + tmp roots only. The wire contract asserted
here is FIXED (the desktop renderer builds against it).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from test_mcp_http_transport import _wait_until, http_server  # noqa: E402

import bridge.freyja_bridge as fb  # noqa: E402
from bridge.mcp.commands import (  # noqa: E402
    BACKGROUND_ACTIONS,
    SurfaceLoginFlow,
    elicitation_bridge_of,
    emit_status,
    make_auth_factory,
    make_login_flow,
    manager_hooks,
    non_interactive_auth_factory,
    resolve_elicitation_response,
    wait_background_tasks,
)
from bridge.mcp.config import McpServerSpec  # noqa: E402
from bridge.mcp.connection import ElicitRequest  # noqa: E402
from bridge.mcp.elicitation import (  # noqa: E402
    ElicitationBridge,
    coerce_answer,
    describe_schema_fields,
    parse_answer_tokens,
)
from bridge.mcp.manager import McpManager  # noqa: E402
from bridge.mcp.oauth.gates import OAuthNonInteractiveError  # noqa: E402
from bridge.tools.base import ToolRegistry  # noqa: E402

STDIO_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"

FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "confirm": {"type": "boolean", "description": "Go ahead?"},
        "count": {"type": "integer"},
        "mode": {"type": "string", "enum": ["fast", "safe"]},
        "note": {"type": "string"},
    },
    "required": ["confirm"],
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "home"))


def _req(**over) -> ElicitRequest:
    base = dict(server="srv", message="Proceed?", mode="form", requested_schema=FORM_SCHEMA)
    base.update(over)
    return ElicitRequest(**base)


# ---------------------------------------------------------------------------
# ElicitationBridge
# ---------------------------------------------------------------------------

async def test_elicitation_bridge_emit_and_accept_round_trip():
    events: list[dict] = []
    bridge = ElicitationBridge(events.append, timeout_s=5, id_factory=lambda: "elicit-1")
    task = asyncio.create_task(bridge(_req()))
    assert await _wait_until(lambda: bool(events), 2)
    ev = events[0]
    assert ev == {
        "type": "mcp_elicitation",
        "requestId": "elicit-1",
        "server": "srv",
        "message": "Proceed?",
        "mode": "form",
        "requestedSchema": FORM_SCHEMA,
        "timeoutS": 5.0,
    }
    assert [p["requestId"] for p in bridge.pending()] == ["elicit-1"]
    assert bridge.get("elicit-1")["server"] == "srv"
    # Wrong id / bad action are ignored; the right one settles the future.
    assert bridge.resolve("nope", "accept", {}) is False
    assert bridge.resolve("elicit-1", "explode", {}) is False
    assert bridge.resolve("elicit-1", "accept", {"confirm": True}) is True
    assert await asyncio.wait_for(task, 2) == {"action": "accept", "content": {"confirm": True}}
    assert bridge.pending() == [] and bridge.get("elicit-1") is None
    assert bridge.resolve("elicit-1", "accept", {}) is False  # stale
    assert bridge.stats["emitted"] == 1 and bridge.stats["accepted"] == 1


async def test_elicitation_bridge_decline_cancel_timeout_and_url_mode():
    events: list[dict] = []
    ids = iter(["a", "b", "c", "d"])
    bridge = ElicitationBridge(events.append, timeout_s=0.2, id_factory=lambda: next(ids))

    t = asyncio.create_task(bridge(_req()))
    assert await _wait_until(lambda: len(events) == 1, 2)
    assert bridge.resolve("a", "decline") is True
    assert await t == {"action": "decline"}

    t = asyncio.create_task(bridge(_req()))
    assert await _wait_until(lambda: len(events) == 2, 2)
    # content is ignored for non-accept actions
    assert bridge.resolve("b", "cancel", {"confirm": True}) is True
    assert await t == {"action": "cancel"}

    # No answer -> cancel (fail closed) and the pending entry is cleared.
    assert await asyncio.wait_for(bridge(_req()), 2) == {"action": "cancel"}
    assert bridge.pending() == []
    assert bridge.stats["timed_out"] == 1

    # URL mode carries url, no schema.
    t = asyncio.create_task(
        bridge(_req(mode="url", url="https://x/consent", requested_schema=None))
    )
    assert await _wait_until(lambda: len(events) == 4, 2)
    assert events[3]["mode"] == "url" and events[3]["url"] == "https://x/consent"
    assert "requestedSchema" not in events[3]
    assert bridge.resolve("d", "accept") is True
    assert await t == {"action": "accept", "content": {}}


async def test_elicitation_bridge_async_sink_and_sink_failure_declines():
    posted: list[dict] = []

    async def sink(ev):
        await asyncio.sleep(0)
        posted.append(ev)

    bridge = ElicitationBridge(sink, timeout_s=2, id_factory=lambda: "x")
    t = asyncio.create_task(bridge(_req()))
    assert await _wait_until(lambda: bool(posted), 2)
    bridge.resolve("x", "accept", {"confirm": False})
    assert await t == {"action": "accept", "content": {"confirm": False}}

    def boom(ev):
        raise RuntimeError("no channel")

    bridge = ElicitationBridge(boom, timeout_s=2)
    assert await bridge(_req()) == {"action": "decline"}
    assert bridge.pending() == []

    # Cancellation from the connection's own timeout guard cleans up too.
    bridge = ElicitationBridge(lambda ev: None, timeout_s=30, id_factory=lambda: "y")
    t = asyncio.create_task(bridge(_req()))
    assert await _wait_until(lambda: bridge.pending(), 2)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert bridge.pending() == []


def test_answer_parsing_and_schema_coercion():
    assert parse_answer_tokens([]) == ("accept", {})
    assert parse_answer_tokens(["decline"]) == ("decline", {})
    assert parse_answer_tokens(["CANCEL"]) == ("cancel", {})
    assert parse_answer_tokens(["confirm=yes", "count=3", 'note="hi there"']) == (
        "accept", {"confirm": "yes", "count": "3", "note": "hi there"},
    )
    with pytest.raises(ValueError):
        parse_answer_tokens(["whatever"])
    with pytest.raises(ValueError):
        parse_answer_tokens(["=x"])

    content, problems = coerce_answer(
        FORM_SCHEMA, {"confirm": "yes", "count": "3", "mode": "FAST", "note": "ok"},
    )
    assert problems == []
    assert content == {"confirm": True, "count": 3, "mode": "fast", "note": "ok"}
    content, problems = coerce_answer(
        FORM_SCHEMA, {"confirm": "maybe", "count": "x", "mode": "slow"},
    )
    assert "field 'confirm': expected yes/no" in problems
    assert any(p.startswith("field 'count'") for p in problems)
    assert any("one of fast, safe" in p for p in problems)
    _, problems = coerce_answer(FORM_SCHEMA, {"bogus": "1"})
    assert any("unknown field 'bogus'" in p for p in problems)
    assert "missing required field 'confirm'" in problems
    # No schema -> pass-through strings; typed values are kept as-is.
    assert coerce_answer(None, {"a": "1"}) == ({"a": "1"}, [])
    assert coerce_answer(FORM_SCHEMA, {"confirm": True}) == ({"confirm": True}, [])
    lines = describe_schema_fields(FORM_SCHEMA)
    assert lines[0] == "confirm: boolean (required) — Go ahead?"
    assert "mode: string one of [fast, safe]" in lines[2]
    assert describe_schema_fields(None) == []


# ---------------------------------------------------------------------------
# manager_hooks + auth factory
# ---------------------------------------------------------------------------

def test_manager_hooks_shape_and_auth_factory_semantics(tmp_path):
    events: list[dict] = []
    hooks = manager_hooks(events.append, elicitation_timeout_s=7, token_root=tmp_path / "t")
    assert set(hooks) == {"auth_factory", "approval_handler", "elicitation_timeout_s", "token_root"}
    assert isinstance(hooks["approval_handler"], ElicitationBridge)
    assert hooks["approval_handler"].timeout_s == 7
    assert hooks["auth_factory"].token_root == tmp_path / "t"
    assert "token_root" not in manager_hooks(events.append)
    assert non_interactive_auth_factory.token_root is None

    factory = make_auth_factory(tmp_path / "t")
    plain = McpServerSpec(name="p", transport="http", url="https://x/mcp")
    assert factory(plain, True) is None  # non-oauth -> no auth object
    stdio_oauth = McpServerSpec(name="s", transport="stdio", command="x", auth="oauth")
    assert factory(stdio_oauth, True) is None  # no url -> nothing to authorize
    oauth = McpServerSpec(name="o", transport="http", url="https://x/mcp", auth="oauth")
    # interactive=True is IGNORED: with no cached tokens the factory raises the
    # needs-auth error (the connection maps it to NEEDS_AUTH) — never a browser.
    with pytest.raises(OAuthNonInteractiveError) as excinfo:
        factory(oauth, True)
    assert "/mcp login o" in str(excinfo.value)
    # With tokens under the requested root the provider is built.
    (tmp_path / "t" / "o").mkdir(parents=True)
    (tmp_path / "t" / "o" / "tokens.json").write_text(
        json.dumps({"access_token": "tok", "token_type": "Bearer", "expires_at": 4102444800}),
    )
    assert factory(oauth, False) is not None
    assert factory(oauth, True) is not None

    # McpManager accepts the hooks verbatim (this is how freyja_bridge loads it).
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": {}}))
    manager = McpManager.load(path, **hooks)
    assert manager.auth_factory is hooks["auth_factory"]
    assert manager.approval_handler is hooks["approval_handler"]
    assert manager.token_root == tmp_path / "t"
    assert elicitation_bridge_of(manager) is hooks["approval_handler"]
    assert elicitation_bridge_of(None) is None
    assert resolve_elicitation_response(manager, {"requestId": "zz", "action": "accept"}) is False
    assert resolve_elicitation_response(None, {"requestId": "zz", "action": "accept"}) is False


def test_freyja_bridge_boot_wiring_uses_manager_hooks():
    """The bridge's boot path passes manager_hooks(emit) into McpManager.load
    and its IPC branch handles mcp_elicitation_response (static check of
    the wiring; the file is 11k lines so we assert on the source)."""
    src = Path(fb.__file__).read_text(encoding="utf-8")
    boot = src[src.index("# External MCP servers"):src.index("# External MCP servers") + 2500]
    assert "manager_hooks" in boot and "**_mcp_manager_hooks(emit)" in boot
    assert 'status_listener=_emit_mcp_status' in boot
    ipc = src[src.index('if ctype == "mcp_command":'):]
    ipc = ipc[: ipc.index('if ctype == "diagnose":')]
    assert 'surface="desktop"' in ipc and "emit=emit" in ipc
    assert "BACKGROUND_ACTIONS" in ipc and "spawn_background" in ipc
    assert 'if ctype == "mcp_elicitation_response":' in ipc
    assert "resolve_elicitation_response" in ipc


# ---------------------------------------------------------------------------
# Login flows: event emission points
# ---------------------------------------------------------------------------

async def test_surface_login_flow_emits_mcp_oauth_url_per_surface():
    events: list[dict] = []
    opened: list[str] = []

    def _opener(url: str) -> bool:
        opened.append(url)
        return True

    desktop = make_login_flow("desktop", emit=events.append, timeout_s=42, opener=_opener)
    assert isinstance(desktop, SurfaceLoginFlow)
    await desktop.on_authorize_url(
        "https://as/authorize?x=1", server_name="srv", redirect_uri="http://127.0.0.1:5/cb",
    )
    assert opened == ["https://as/authorize?x=1"]  # the bridge opened the browser
    assert events == [{
        "type": "mcp_oauth_url", "server": "srv", "url": "https://as/authorize?x=1",
        "redirectUri": "http://127.0.0.1:5/cb", "expiresInS": 42, "opened": True,
    }]
    # Opener failure -> opened=false, URL still emitted for the fallback link.
    events.clear()
    failing = make_login_flow(
        "desktop", emit=events.append, timeout_s=1, opener=lambda url: False,
    )
    await failing.on_authorize_url("https://as/a", server_name="s", redirect_uri="r")
    assert events[0]["opened"] is False
    # Gateway/test surfaces never open anything.
    events.clear()
    gateway = make_login_flow("gateway", emit=events.append, timeout_s=300, opener=_opener)
    await gateway.on_authorize_url("https://as/b", server_name="s", redirect_uri="r")
    assert events[0]["opened"] is False and len(opened) == 1
    assert gateway.last_url == "https://as/b" and gateway.urls == ["https://as/b"]
    assert gateway.paste_source is None and gateway.redirect_uri is None


async def test_emit_status_shape(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": {
        "s": {"transport": "stdio", "command": "x", "enabled": False},
    }}))
    manager = McpManager.load(path, watchdog=False)
    events: list[dict] = []
    await emit_status(events.append, manager)
    assert events[0]["type"] == "mcp_status"
    assert [r["server"] for r in events[0]["servers"]] == ["s"]
    assert events[0]["servers"][0]["needs_auth"] is False
    await emit_status(None, manager)  # no sink -> no-op


# ---------------------------------------------------------------------------
# _handle_command: background dispatch + elicitation response IPC
# ---------------------------------------------------------------------------

def _stdio_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": {
        "fixture": {
            "transport": "stdio", "command": sys.executable, "args": [str(STDIO_FIXTURE)],
            "enabled": False, "trust": "trusted", "timeouts": {"connect_s": 15, "call_s": 15},
        },
    }}))
    return path


async def test_bridge_ipc_background_actions_reply_asynchronously(tmp_path, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda e: events.append(e))
    manager = McpManager.load(
        _stdio_catalog(tmp_path), watchdog=False, run_dir=tmp_path / "run",
        **manager_hooks(lambda e: events.append(e)),
    )
    state = types.SimpleNamespace(mcp_manager=manager, active_session_id=None)
    try:
        # Inline action: the result is there when _handle_command returns.
        await fb._handle_command(
            state, {"type": "mcp_command", "action": "status", "requestId": "r1"},
        )
        assert [e["type"] for e in events] == ["mcp_command_result"]
        assert events[0]["requestId"] == "r1" and events[0]["servers"][0]["state"] == "disabled"
        events.clear()
        # Background action: _handle_command returns first, the result lands later.
        assert "enable" in BACKGROUND_ACTIONS
        await fb._handle_command(state, {"type": "mcp_command", "action": "enable",
                                         "server": "fixture", "requestId": "r2"})
        assert not [e for e in events if e.get("type") == "mcp_command_result"]
        await asyncio.wait_for(wait_background_tasks(), timeout=60)
        results = [e for e in events if e.get("type") == "mcp_command_result"]
        assert len(results) == 1 and results[0]["requestId"] == "r2" and results[0]["ok"] is True
        assert results[0]["servers"][0]["state"] == "active"
        # The surface emitted a full-snapshot mcp_status (servers list).
        statuses = [e for e in events if e.get("type") == "mcp_status" and "servers" in e]
        assert statuses and statuses[-1]["servers"][0]["state"] == "active"
        # Unknown elicitation response ids are ignored without raising.
        await fb._handle_command(state, {"type": "mcp_elicitation_response",
                                         "requestId": "ghost", "action": "accept"})
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def test_bridge_ipc_elicitation_round_trip_through_real_tool(tmp_path, monkeypatch):
    """HTTP fixture's elicit_me tool -> ElicitationBridge -> mcp_elicitation
    event -> mcp_elicitation_response IPC -> the tool sees the answer."""
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", lambda e: events.append(e))
    async with http_server(tmp_path) as fx:
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"version": 1, "servers": {
            "hsrv": {"transport": "http", "url": fx.url, "enabled": True, "trust": "trusted",
                     "timeouts": {"connect_s": 15, "call_s": 20}},
        }}))
        manager = McpManager.load(
            path, watchdog=False, keepalive_interval_s=5,
            **manager_hooks(lambda e: events.append(e), elicitation_timeout_s=10),
        )
        registry = ToolRegistry()
        manager.register_into(registry)
        state = types.SimpleNamespace(mcp_manager=manager, active_session_id=None)
        try:
            await asyncio.wait_for(manager.start(), timeout=40)
            tool = registry.get("mcp__hsrv__elicit_me")
            assert tool is not None
            call = asyncio.create_task(tool.execute("c1", {"msg": "Deploy?"}))
            assert await _wait_until(
                lambda: any(e.get("type") == "mcp_elicitation" for e in events), 10,
            )
            ev = next(e for e in events if e["type"] == "mcp_elicitation")
            assert ev["server"] == "hsrv" and ev["message"] == "Deploy?" and ev["mode"] == "form"
            assert ev["requestedSchema"]["required"] == ["confirm"]
            assert ev["timeoutS"] == 10.0
            await fb._handle_command(state, {
                "type": "mcp_elicitation_response", "requestId": ev["requestId"],
                "action": "accept", "content": {"confirm": True},
            })
            result = await asyncio.wait_for(call, 20)
            raw = result.content if isinstance(result.content, str) else json.dumps(result.content)
            body = json.loads(raw)
            if isinstance(body, dict) and "text" in body:
                body = json.loads(body["text"])
            assert body["action"] == "accept" and body["content"] == {"confirm": True}

            # Decline path via the /mcp answer command surface.
            from bridge.mcp.commands import handle_mcp_command

            events.clear()
            call = asyncio.create_task(tool.execute("c2", {"msg": "Again?"}))
            assert await _wait_until(
                lambda: any(e.get("type") == "mcp_elicitation" for e in events), 10,
            )
            ev = next(e for e in events if e["type"] == "mcp_elicitation")
            listing = await handle_mcp_command(manager, {"action": "answer"})
            assert listing["ok"] and listing["rows"][0]["requestId"] == ev["requestId"]
            bad = await handle_mcp_command(
                manager, {"action": "answer", "args": [ev["requestId"], "confirm=maybe"]},
            )
            assert bad["ok"] is False and "cannot accept" in bad["message"]
            ans = await handle_mcp_command(
                manager, {"action": "answer", "args": [ev["requestId"], "decline"]},
            )
            assert ans["ok"] is True and "declined" in ans["message"]
            result = await asyncio.wait_for(call, 20)
            text = result.content if isinstance(result.content, str) else json.dumps(result.content)
            assert '"decline"' in text
            assert manager.connection("hsrv").elicitations["declined"] == 1
        finally:
            await asyncio.wait_for(manager.stop(), timeout=30)
