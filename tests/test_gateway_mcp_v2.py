"""Slack gateway /mcp router parity (card_019): bridge/gateway/mcp_slack.py.

Drives ``GatewayDaemon._handle_mcp_command`` with a fake adapter and a
tmp McpManager: every subcommand replies through the shared handler,
login posts the authorize URL (HandoffFlow, ephemeral), elicitations are
posted into the running session's thread and answered with
``/mcp answer`` (slash command or plain threaded reply). Offline.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from fake_oauth_as import FakeAuthorizationServer, visit_authorization_url  # noqa: E402
from test_mcp_http_transport import _wait_until, http_server  # noqa: E402

from bridge.gateway import mcp_slack  # noqa: E402
from bridge.gateway.mcp_slack import (  # noqa: E402
    MCP_HELP,
    SlackElicitationBridge,
    format_elicitation_for_slack,
    format_oauth_result_for_slack,
    format_oauth_url_for_slack,
    format_result_for_slack,
    promote_inline_mcp_answer,
)
from bridge.gateway.platforms.base import IncomingMessage, MessageSource, Platform  # noqa: E402
from bridge.gateway.run import GatewayDaemon  # noqa: E402
from bridge.mcp.commands import make_auth_factory, wait_background_tasks  # noqa: E402
from bridge.mcp.connection import State  # noqa: E402
from bridge.mcp.manager import McpManager  # noqa: E402
from bridge.mcp.oauth import callback as cb  # noqa: E402
from bridge.tools.base import ToolRegistry  # noqa: E402

STDIO_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"
FIXED_TOKEN = "gateway-e2e-bearer"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "home"))
    cb.reset_port_state_for_tests()
    yield
    cb.reset_port_state_for_tests()


class FakeAdapter:
    name = "slack"  # GatewayDaemon._adapter_for_platform matches on .name

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, chat_id, text, *, thread_id=None, ephemeral_user_id=None,
                   raw_hint=None, **_):
        self.sent.append({
            "chat_id": chat_id, "text": text, "thread_id": thread_id,
            "ephemeral": ephemeral_user_id,
        })
        return types.SimpleNamespace(ok=True, message_id=str(len(self.sent)))

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


def _source(thread: str | None = None) -> MessageSource:
    return MessageSource(
        platform=Platform.SLACK, workspace_id="W1", chat_type="channel", chat_id="C1",
        user_id="U1", thread_id=thread,
    )


def _slash(args: str, thread: str | None = None) -> IncomingMessage:
    return IncomingMessage(
        source=_source(thread), text=f"/mcp {args}", is_slash_command=True,
        slash_command_name="mcp", slash_command_args=args, raw={},
    )


def _daemon(manager: McpManager, adapter: FakeAdapter) -> GatewayDaemon:
    daemon = GatewayDaemon()
    daemon.state = types.SimpleNamespace(sessions={}, mcp_manager=manager)
    daemon.adapters = [adapter]
    return daemon


def _manager(tmp_path: Path, servers: dict, **over) -> McpManager:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"version": 1, "servers": servers}))
    kwargs = dict(
        watchdog=False, keepalive_interval_s=0.3, keepalive_timeout_s=2.0,
        backoff_base_s=0.05, backoff_cap_s=0.1, backoff_max_retries=1,
        run_dir=tmp_path / "run", token_root=tmp_path / "tokens",
        auth_factory=make_auth_factory(tmp_path / "tokens"), token_watch_interval_s=0,
    )
    kwargs.update(over)
    return McpManager.load(path, **kwargs)


def _stdio(**over) -> dict:
    d = {"transport": "stdio", "command": sys.executable, "args": [str(STDIO_FIXTURE)],
         "enabled": False, "trust": "trusted", "timeouts": {"connect_s": 15, "call_s": 15}}
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_result_for_slack_per_action():
    status = {
        "ok": True, "action": "status", "servers": [{"server": "a"}],
        "message": "name  state\n----  -----\na     active\na: auth=oauth",
        "data": {"table": "name  state\n----  -----\na     active", "details": "a: auth=oauth"},
    }
    out = format_result_for_slack(status)
    assert out.startswith("```\nname  state") and out.endswith("```\na: auth=oauth")
    assert "No MCP servers configured" in format_result_for_slack(
        {"ok": True, "action": "status", "servers": [], "message": "no MCP servers configured"},
    )
    tools = {"ok": True, "action": "tools", "message": "2 tools\ntool  tier\n----  ----\nx  warm"}
    assert format_result_for_slack(tools) == "2 tools\n```\ntool  tier\n----  ----\nx  warm\n```"
    cat = {"ok": True, "action": "catalog", "message": "1 catalog entry\nname  transport\nx  http"}
    assert format_result_for_slack(cat).startswith("1 catalog entry\n```\n")
    test = {"ok": False, "action": "test",
            "message": "test 'x': FAILED (5ms total)\n  connect FAIL"}
    assert format_result_for_slack(test) == "```\n" + test["message"] + "\n```"
    reauth = {"ok": True, "action": "reauth", "message": "ok   a: fine\nre-authenticated 1/1"}
    assert format_result_for_slack(reauth).startswith("```\nok   a")
    assert format_result_for_slack({"ok": True, "action": "enable", "message": "'a' enabled"}) == (
        "'a' enabled"
    )
    assert format_result_for_slack({"ok": False, "action": "remove", "message": "unknown"}) == (
        ":warning: unknown"
    )
    assert format_result_for_slack({"ok": True, "action": "x", "message": ""}) == "Done."
    assert format_result_for_slack({"ok": False, "action": "x"}) == "Failed."
    login = {"ok": True, "action": "login", "message": "'a' authenticated — state: active"}
    assert format_result_for_slack(login) == "'a' authenticated — state: active"
    add = {"ok": True, "action": "add", "message": "added 'a'\nnext: x"}
    assert format_result_for_slack(add) == "added 'a'\nnext: x"
    assert format_result_for_slack({"ok": True, "action": "call", "message": "hi"}) == "hi"
    answer = {"ok": True, "action": "answer", "message": "declined"}
    assert format_result_for_slack(answer) == "declined"


def test_format_oauth_and_elicitation_posts():
    url = format_oauth_url_for_slack({
        "type": "mcp_oauth_url", "server": "acme", "url": "https://as/a?x=1",
        "redirectUri": "http://127.0.0.1:5/cb", "expiresInS": 300, "opened": False,
    })
    assert "Authorize *acme*" in url and "<https://as/a?x=1|" in url and "~5 min" in url
    assert "http://127.0.0.1:5/cb" in url
    assert format_oauth_result_for_slack(
        {"server": "acme", "ok": True, "scopes": ["read", "write"]}
    ) == ":white_check_mark: *acme* authenticated (scopes: read write)"
    assert format_oauth_result_for_slack({"server": "acme", "ok": False, "error": "denied"}) == (
        ":x: *acme* login failed: denied"
    )
    form = format_elicitation_for_slack({
        "requestId": "e1", "server": "acme", "message": "Deploy?", "mode": "form",
        "requestedSchema": {"type": "object", "properties": {
            "confirm": {"type": "boolean", "description": "Really?"},
            "env": {"type": "string", "enum": ["prod", "dev"]},
        }, "required": ["confirm"]},
        "timeoutS": 120,
    })
    assert "*acme* is asking" in form and "> Deploy?" in form
    assert "`confirm: boolean (required) — Really?`" in form
    assert "`env: string one of [prod, dev]`" in form
    assert "/mcp answer e1 confirm=... env=..." in form
    assert "/mcp answer e1 decline" in form and "120s" in form
    url_mode = format_elicitation_for_slack({
        "requestId": "e2", "server": "acme", "message": "Consent", "mode": "url",
        "url": "https://x/consent", "timeoutS": 0,
    })
    assert "<https://x/consent>" in url_mode and "/mcp answer e2 accept" in url_mode
    assert "cancels" not in url_mode


def test_promote_inline_mcp_answer_only_for_answers():
    msg = IncomingMessage(source=_source("T1"), text="/mcp answer e1 confirm=yes", raw={})
    assert promote_inline_mcp_answer(msg) is True
    assert msg.is_slash_command and msg.slash_command_name == "mcp"
    assert msg.slash_command_args == "answer e1 confirm=yes"
    msg = IncomingMessage(source=_source("T1"), text="  mcp answer e1 decline", raw={})
    assert promote_inline_mcp_answer(msg) is True and msg.slash_command_args == "answer e1 decline"
    for text in ("/mcp status", "answer e1", "please mcp answer", "/mcp answerx"):
        m = IncomingMessage(source=_source("T1"), text=text, raw={})
        assert promote_inline_mcp_answer(m) is False and m.is_slash_command is False
    already = _slash("answer e1 decline")
    assert promote_inline_mcp_answer(already) is False


# ---------------------------------------------------------------------------
# Router parity
# ---------------------------------------------------------------------------

async def test_router_covers_every_subcommand(tmp_path):
    manager = _manager(tmp_path, {"fixture": _stdio(), "hosted": {
        "transport": "http", "url": "http://127.0.0.1:9/mcp", "auth": "oauth", "enabled": False,
    }})
    registry = ToolRegistry()
    manager.register_into(registry)
    adapter = FakeAdapter()
    daemon = _daemon(manager, adapter)
    try:
        async def run(args: str) -> str:
            adapter.sent.clear()
            assert await daemon._handle_mcp_command(_slash(args), adapter) is True
            assert adapter.sent, f"no reply for /mcp {args}"
            assert adapter.sent[-1]["ephemeral"] == "U1"  # replies are ephemeral to the caller
            return adapter.sent[-1]["text"]

        assert (await run("help")) == MCP_HELP
        text = await run("")
        assert text.startswith("```\nname") and "fixture" in text and "hosted" in text
        assert "hosted: auth=oauth (no stored token)" in text
        text = await run("status hosted")
        assert "hosted" in text and "fixture" not in text
        text = await run("enable fixture")
        assert "'fixture' enabled — state: active" in text
        assert registry.get("mcp__fixture__echo") is not None
        text = await run("tools fixture")
        assert "mcp__fixture__echo" in text and "pending quarantine" in text
        text = await run('call fixture echo {"text": "from slack"}')
        assert "from slack" in text
        text = await run("call fixture echo text=kv")
        assert "kv" in text
        text = await run("test fixture")
        assert text.startswith("```\ntest 'fixture': OK")
        text = await run("disable fixture")
        assert "disabled" in text and registry.get("mcp__fixture__echo") is None
        text = await run("reload")
        assert "no catalog changes" in text
        text = await run("catalog list")
        assert "catalog entr" in text
        text = await run("catalog search zzz-no-such-entry")
        assert "0 match(es)" in text
        text = await run("catalog info zzz-no-such-entry")
        assert text.startswith(":warning:") and "unknown catalog entry" in text
        text = await run("login fixture")  # background action -> ack first
        assert "Starting `/mcp login fixture`" in text
        await asyncio.wait_for(wait_background_tasks(), 30)
        assert any("not an OAuth server" in t for t in adapter.texts())
        text = await run("logout hosted")
        assert "logged out" in text
        text = await run("reauth --all")
        assert "Starting `/mcp reauth all OAuth servers`" in text
        await asyncio.wait_for(wait_background_tasks(), 30)
        assert any("no enabled OAuth servers" in t for t in adapter.texts())
        text = await run("add")
        assert text.startswith(":warning: usage: add")
        text = await run(f"add {sys.executable} --name viaslack -- {STDIO_FIXTURE}")
        assert text.startswith("added 'viaslack' (stdio, disabled)")
        assert "viaslack" in manager.specs
        text = await run("remove viaslack")
        assert text.startswith("removed 'viaslack'")
        text = await run("answer")
        assert "no pending elicitations" in text
        text = await run("answer nope decline")
        assert "no pending elicitation with id 'nope'" in text
        text = await run("detonate")
        assert "unknown mcp_command action" in text
    finally:
        await asyncio.wait_for(manager.stop(), timeout=30)


async def test_router_lazily_builds_manager_with_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "homedir"))
    (tmp_path / "homedir" / ".freyja").mkdir(parents=True)
    (tmp_path / "homedir" / ".freyja" / "mcp.json").write_text(
        json.dumps({"version": 1, "servers": {"fixture": _stdio(enabled=True)}}),
    )
    adapter = FakeAdapter()
    daemon = GatewayDaemon()
    daemon.state = types.SimpleNamespace(sessions={})
    daemon.adapters = [adapter]
    try:
        assert await daemon._handle_mcp_command(_slash("status"), adapter) is True
        manager = daemon.state.mcp_manager
        assert isinstance(manager, McpManager)
        assert isinstance(manager.approval_handler, SlackElicitationBridge)
        assert manager.auth_factory.token_root is None  # default ~/.freyja/mcp-tokens
        assert manager.approval_handler.last_source is not None
        # Servers configured -> connections started in the background.
        await asyncio.wait_for(wait_background_tasks(), 40)
        assert manager.connection("fixture").state is State.ACTIVE
    finally:
        manager = getattr(daemon.state, "mcp_manager", None)
        if manager is not None:
            await asyncio.wait_for(manager.stop(), timeout=30)

    # No state yet -> friendly message, no crash.
    daemon = GatewayDaemon()
    daemon.adapters = [adapter]
    adapter.sent.clear()
    assert await daemon._handle_mcp_command(_slash("status"), adapter) is True
    assert "not ready" in adapter.sent[-1]["text"]


# ---------------------------------------------------------------------------
# Login posts the URL + elicitation via thread
# ---------------------------------------------------------------------------

async def test_login_posts_authorize_url_then_result(tmp_path):
    fake = FakeAuthorizationServer(fixed_access_token=FIXED_TOKEN)
    fake.start()
    adapter = FakeAdapter()
    try:
        async with http_server(
            tmp_path, "--mode", "auth", "--token", FIXED_TOKEN,
            "--resource-metadata", fake.prm_url,
        ) as fx:
            fake.resource_override = fx.url
            manager = _manager(tmp_path, {"acme": {
                "transport": "http", "url": fx.url, "auth": "oauth", "enabled": True,
                "trust": "trusted",
            }})
            daemon = _daemon(manager, adapter)
            try:
                await asyncio.wait_for(manager.start(), 40)
                assert manager.connection("acme").state is State.NEEDS_AUTH
                assert await daemon._handle_mcp_command(_slash("login acme"), adapter) is True
                assert "Starting `/mcp login acme`" in adapter.sent[-1]["text"]
                # The HandoffFlow posts the authorize URL (ephemeral, same chat).
                assert await _wait_until(
                    lambda: any("Authorize *acme*" in t for t in adapter.texts()), 20,
                )
                post = next(m for m in adapter.sent if "Authorize *acme*" in m["text"])
                assert post["ephemeral"] == "U1" and post["chat_id"] == "C1"
                url = post["text"].split("<", 1)[1].split("|", 1)[0]
                assert url.startswith(fake.authorization_endpoint)
                # "User clicks the link".
                await asyncio.to_thread(visit_authorization_url, url)
                await asyncio.wait_for(wait_background_tasks(), 60)
                texts = adapter.texts()
                assert any(t.startswith(":white_check_mark: *acme* authenticated") for t in texts)
                assert any("'acme' authenticated" in t and "state: active" in t for t in texts)
                assert manager.connection("acme").state is State.ACTIVE
                assert (tmp_path / "tokens" / "acme" / "tokens.json").is_file()
                assert FIXED_TOKEN not in json.dumps(texts)
            finally:
                await asyncio.wait_for(manager.stop(), timeout=30)
    finally:
        fake.stop()


async def test_elicitation_posted_to_running_session_thread_and_answered(tmp_path):
    async with http_server(tmp_path) as fx:
        manager = _manager(tmp_path, {"hsrv": {
            "transport": "http", "url": fx.url, "enabled": True, "trust": "trusted",
            "timeouts": {"connect_s": 15, "call_s": 20},
        }}, elicitation_timeout_s=10)
        adapter = FakeAdapter()
        daemon = _daemon(manager, adapter)
        bridge = SlackElicitationBridge(daemon, timeout_s=10)
        manager.approval_handler = bridge
        registry = ToolRegistry()
        manager.register_into(registry)
        try:
            await asyncio.wait_for(manager.start(), 40)
            tool = registry.get("mcp__hsrv__elicit_me")
            assert tool is not None

            # A gateway session with an in-flight turn in thread T9.
            gate = asyncio.Event()
            session = types.SimpleNamespace(
                gateway_source=_source("T9"), pending_task=asyncio.create_task(gate.wait()),
            )
            daemon.state.sessions["sess"] = session
            call = asyncio.create_task(tool.execute("c1", {"msg": "Ship it?"}))
            assert await _wait_until(lambda: bool(adapter.sent), 10)
            post = adapter.sent[-1]
            assert post["thread_id"] == "T9" and post["chat_id"] == "C1"
            assert post["ephemeral"] is None  # visible to the thread
            assert "*hsrv* is asking" in post["text"] and "> Ship it?" in post["text"]
            request_id = bridge.pending()[0]["requestId"]
            assert f"/mcp answer {request_id} confirm=..." in post["text"]

            # Plain threaded reply (no slash) is promoted and routed.
            reply = IncomingMessage(
                source=_source("T9"), text=f"/mcp answer {request_id} confirm=yes", raw={},
            )
            assert promote_inline_mcp_answer(reply) is True
            adapter.sent.clear()
            assert await daemon._handle_mcp_command(reply, adapter) is True
            assert "accepted elicitation" in adapter.sent[-1]["text"]
            result = await asyncio.wait_for(call, 20)
            text = result.content if isinstance(result.content, str) else json.dumps(result.content)
            assert '"accept"' in text and '"confirm": true' in text

            # Second request: bad value is rejected with guidance, then declined.
            adapter.sent.clear()
            call = asyncio.create_task(tool.execute("c2", {"msg": "Again?"}))
            assert await _wait_until(lambda: bool(bridge.pending()), 10)
            request_id = bridge.pending()[0]["requestId"]
            assert await daemon._handle_mcp_command(
                _slash(f"answer {request_id} confirm=perhaps"), adapter,
            )
            assert "cannot accept" in adapter.sent[-1]["text"]
            assert await daemon._handle_mcp_command(_slash(f"answer {request_id} decline"), adapter)
            result = await asyncio.wait_for(call, 20)
            text = result.content if isinstance(result.content, str) else json.dumps(result.content)
            assert '"decline"' in text
            gate.set()
            await session.pending_task

            # No running session -> falls back to the last /mcp conversation.
            daemon.state.sessions.clear()
            bridge.note_source(_source("T-last"))
            adapter.sent.clear()
            call = asyncio.create_task(tool.execute("c3", {"msg": "Fallback?"}))
            assert await _wait_until(lambda: bool(adapter.sent), 10)
            assert adapter.sent[-1]["thread_id"] == "T-last"
            bridge.resolve(bridge.pending()[0]["requestId"], "cancel")
            result = await asyncio.wait_for(call, 20)

            # Nowhere to post -> declined (fail closed), tool gets the answer.
            bridge.last_source = None
            call = asyncio.create_task(tool.execute("c4", {"msg": "Nowhere?"}))
            result = await asyncio.wait_for(call, 20)
            text = result.content if isinstance(result.content, str) else json.dumps(result.content)
            assert '"decline"' in text
            assert bridge.stats["declined"] >= 2
        finally:
            await asyncio.wait_for(manager.stop(), timeout=30)


def test_module_exports():
    assert set(mcp_slack.__all__) >= {
        "handle_mcp_slash", "SlackElicitationBridge", "promote_inline_mcp_answer", "MCP_HELP",
    }
