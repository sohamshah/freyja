"""With `slack.enable_tool_filter` on, nothing reaches a tool outside
`SLACK_TOOL_ALLOWLIST`: not the tool list, not `tool_search`, not
`call_tool`.

The filter popped each other tool from the registry's tool table but left
its catalog entry, and the catalog is what the request's tool list,
`tool_search` and a read-only fork are built from. So the model was still
offered `bash` and the browser tools, `tool_search` handed out the schema
of a stripped tool and said to run it with `call_tool`, and a read-only
fork of the session ran `recall` and `view_image` for real. The registry
also stayed attached to the MCP manager, so a server that connected or
reconnected after the session started registered its tools into it again,
where `call_tool` ran them.
"""

from __future__ import annotations

import os
from pathlib import Path

from bridge.gateway.capabilities import tools_allowed_for_gateway
from bridge.gateway.platforms.base import MessageSource, Platform
from engine.tools import SimpleTool


async def _filtered_session(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    from bridge.freyja_bridge import _BridgeSession, _BridgeState
    from bridge.mcp.manager import McpManager

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    (Path(os.environ["FREYJA_HOME"]) / "gateway.yaml").write_text("slack:\n  enable_tool_filter: true\n")
    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.mcp_manager = McpManager.load(tmp_path / "mcp.json")
    sess = _BridgeSession(
        "freyja:slack:T1:channel:C1:5.5", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.gateway_source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
    )
    await sess.initialize()
    return sess, state.mcp_manager


async def _call(registry, name: str, arguments: dict):
    return await registry.get(name).execute("c1", arguments)


async def test_the_model_is_offered_only_allowed_tools(monkeypatch, tmp_path):
    sess, _mcp = await _filtered_session(monkeypatch, tmp_path)
    reg = sess.tool_registry
    allowed = tools_allowed_for_gateway(Platform.SLACK)
    assert {d.name for d in reg.list_definitions()} <= allowed
    assert set(reg.list_summaries()) <= allowed


async def test_tool_search_finds_no_stripped_tool(monkeypatch, tmp_path):
    sess, _mcp = await _filtered_session(monkeypatch, tmp_path)
    for name in ("bash", "recall", "generate_image", "session_memory"):
        result = await _call(sess.tool_registry, "tool_search", {"tool_name": name})
        assert result.is_error, (name, result.content)


async def test_a_read_only_fork_holds_no_stripped_tool(monkeypatch, tmp_path):
    from bridge.tools.fork_registry import build_read_only_fork_registry

    sess, _mcp = await _filtered_session(monkeypatch, tmp_path)
    fork = build_read_only_fork_registry(sess.tool_registry)
    assert set(fork.list_names()) <= tools_allowed_for_gateway(Platform.SLACK)
    result = await _call(fork, "call_tool", {"tool_name": "recall", "arguments": {"query": "x"}})
    assert result.is_error and "no tool named" in result.content


async def test_call_tool_cannot_run_an_mcp_tool_that_arrives_later(monkeypatch, tmp_path):
    sess, mcp = await _filtered_session(monkeypatch, tmp_path)
    ran: list[dict] = []

    def _handler(**kwargs):
        ran.append(kwargs)
        return "deleted"

    late = SimpleTool("mcp__drive__delete_file", "Delete a file", {"type": "object", "properties": {}}, _handler)
    # What the manager does for every attached registry when a server
    # (re)connects after the session was built.
    for registry in mcp._live_registries():
        mcp._register_proxy(registry, late)
    result = await _call(
        sess.tool_registry, "call_tool", {"tool_name": "mcp__drive__delete_file", "arguments": {}},
    )
    assert ran == []
    assert result.is_error
