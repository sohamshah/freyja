"""The end-of-turn self-check is visible, and the reply after it is a new paragraph.

When the model tries to end its turn after at least 10 steps, the last 6+
of which called the same tools, the runner adds a hidden steering message
asking it to check its work. It now also sends a ``verification_nudge``
system event: the desktop shows a chip, and every listener starts the
reply that follows on a new paragraph instead of running it on from the
one the model meant to end with ("Done.I verified...").
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.stream_consumer import SlackStreamConsumer
from engine.providers import APIUsage, ProviderResponse, ToolCallResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig


class _ScriptedProvider:
    name = "fake"
    model_id = "fake-model"
    context_window = 1_000_000

    def __init__(self, steps):
        self.steps = list(steps)

    async def stream_to_response(self, *, messages, on_event=None, **_kw):
        return self.steps.pop(0)

    async def complete_async(self, *, messages, **_kw):
        return await self.stream_to_response(messages=messages)


class _Bash:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="bash", description="bash", summary="bash")

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(call_id=call_id, content="ok", is_error=False)


def _bash(i: int) -> ProviderResponse:
    return ProviderResponse(
        content="",
        stop_reason="tool_use",
        usage=APIUsage(),
        # Different args each step, so the identical-call loop breaker stays quiet.
        tool_calls=[ToolCallResponse(id=f"c{i}", name="bash", arguments={"command": f"echo {i}"})],
    )


def _end(text: str) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage())


async def _run(steps):
    events: list[Any] = []

    async def on_system_event(ev):
        events.append(ev)

    registry = ToolRegistry()
    registry.register(_Bash())
    runner = AsyncAgentRunner(
        _ScriptedProvider(steps),
        config=AgentConfig(),
        tool_registry=registry,
        on_system_event=on_system_event,
    )
    result = await runner.run(Session.create(system_prompt="t"), "fix it")
    return result, [e for e in events if e.type == "verification_nudge"]


async def test_a_long_run_of_one_tool_gets_a_visible_self_check():
    result, nudges = await _run([*(_bash(i) for i in range(10)), _end("Done."), _end("Verified: all fixed.")])
    assert result.success
    assert len(nudges) == 1
    nudge = nudges[0]
    assert nudge.details["consecutive"] == 10
    assert nudge.details["tools"] == ["bash"]
    assert "verify all items" in nudge.details["prompt"]
    assert "10 bash calls in a row" in nudge.message
    assert result.response == "Done.\n\nVerified: all fixed."


async def test_a_short_run_ends_without_one():
    result, nudges = await _run([*(_bash(i) for i in range(3)), _end("Done.")])
    assert result.success
    assert nudges == []


# ── Slack consumer ────────────────────────────────────────────────────


from tests.slack_fakes import StreamingFakeAdapter as _FakeAdapter


@pytest.mark.asyncio
async def test_slack_reply_after_the_self_check_is_a_new_paragraph():
    adapter = _FakeAdapter()
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id="U1", message_id="1.1", thread_id="1.1",
    )
    consumer = SlackStreamConsumer(
        adapter,  # type: ignore[arg-type]
        source,
        session_key="freyja:slack:T1:channel:C1:1.1",
        verbosity="off",
    )
    for ev in [
        {"type": "text_delta", "text": "Done."},
        {"type": "system_event", "subtype": "context_pruning", "message": "x"},
        {"type": "text_delta", "text": " Still the same paragraph."},
        {"type": "system_event", "subtype": "verification_nudge", "message": "Asked to check"},
        {"type": "text_delta", "text": "Verified."},
        {"type": "turn_complete"},
    ]:
        if ev["type"] == "system_event" and ev["subtype"] != "verification_nudge":
            consumer.on_event(ev)  # filtered out before it reaches the handler
            continue
        await consumer._handle_event_async(ev)
    assert adapter.body == "Done. Still the same paragraph.\n\nVerified."
