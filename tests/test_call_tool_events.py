"""The UI event stream names the tool that `call_tool` runs.

`call_tool` dispatches through `registry.execute`, and the bridge wraps that
method, so one call emits `tool_input_end` twice under one id: first for the
wrapper, then for the inner tool, whose args replace the wrapper's in the UI.
The inner event must carry the inner tool's name, or the UI can only say
"call_tool".
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge import freyja_bridge
from bridge.tools.base import ToolCall, ToolDefinition, ToolRegistry, ToolResult, ToolTier
from bridge.tools.tool_search_tool import CALL_TOOL_NAME, CallToolTool


class _EchoTool:
    """A deferred tool: out of the tool list, so only `call_tool` reaches it."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo_tool",
            summary="Echo the reason",
            tier=ToolTier.COLD,
            description="Echo.",
            parameters={"type": "object", "properties": {"reason": {"type": "string"}}},
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(call_id=call_id, content=f"echo: {arguments.get('reason')}")


@pytest.fixture
def events(monkeypatch):
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    return sent


def _registry():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register(CallToolTool(registry))
    return freyja_bridge._new_tracing_registry(registry, "s1")


def _input_ends(events, call_id):
    return [e for e in events if e["type"] == "tool_input_end" and e["id"] == call_id]


@pytest.mark.asyncio
async def test_call_tool_names_the_inner_tool_on_the_inner_event(events):
    registry = _registry()
    inner = {"reason": "context is full"}
    result = await registry.execute(
        ToolCall(id="c1", name=CALL_TOOL_NAME, arguments={"tool_name": "echo_tool", "arguments": inner})
    )
    assert result.content == "echo: context is full"

    wrapper_event, inner_event = _input_ends(events, "c1")
    assert wrapper_event["toolName"] == CALL_TOOL_NAME
    assert wrapper_event["arguments"] == {"tool_name": "echo_tool", "arguments": inner}
    assert inner_event["toolName"] == "echo_tool"
    assert inner_event["arguments"] == inner


@pytest.mark.asyncio
async def test_a_direct_call_names_its_own_tool(events):
    registry = _registry()
    await registry.execute(ToolCall(id="c2", name="echo_tool", arguments={"reason": "x"}))

    (event,) = _input_ends(events, "c2")
    assert event["toolName"] == "echo_tool"
    assert event["arguments"] == {"reason": "x"}
