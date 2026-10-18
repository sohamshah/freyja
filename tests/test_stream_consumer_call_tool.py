"""A ``call_tool`` card in Slack names the tool it runs.

``call_tool`` runs a deferred tool by name. The bridge emits ``tool_input_end``
twice for one call id: first with the wrapper's ``{tool_name, arguments}``,
then, as the inner tool starts, with the inner tool's name (``toolName``) and
its own args. The card used to read "call_tool" for every deferred tool.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.stream_consumer import SlackStreamConsumer, _unwrap_call_tool


class _CardAdapter:
    """Records the cards the consumer appends to the stream."""

    max_message_chars = 39_000

    def __init__(self) -> None:
        self.cards: list[tuple[str, str, str]] = []  # (id, title, status)
        self.plans: list[str] = []

    def format_content(self, content: str) -> str:
        return content

    async def send(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True, message_id="1.0")

    async def start_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True, message_id="stream-ts")

    async def append_stream(self, chat_id: str, ts: str, chunks: list[Any], **kw: Any) -> SendResult:
        for chunk in chunks:
            if hasattr(chunk, "status"):
                self.cards.append((chunk.id, chunk.title, chunk.status))
            elif hasattr(chunk, "title"):
                self.plans.append(chunk.title)
        return SendResult(ok=True)

    async def stop_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        return None

    async def stop_typing(self, *a: Any, **kw: Any) -> None:
        return None


def _consumer(adapter: _CardAdapter) -> SlackStreamConsumer:
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C123", chat_type="channel",
        user_id="U1", message_id="100.1", thread_id="100.1",
    )
    return SlackStreamConsumer(
        adapter,  # type: ignore[arg-type]
        source,
        session_key="freyja:slack:T1:channel:C123:100.1",
        verbosity="all",
    )


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


def _wrapped(call_id: str, tool: str, args: Any) -> list[dict[str, Any]]:
    """The events the bridge emits for one call_tool call."""
    inner = args if isinstance(args, dict) else {}
    return [
        {"type": "tool_use_start", "id": call_id, "name": "call_tool"},
        {"type": "tool_input_end", "id": call_id, "toolName": "call_tool",
         "arguments": {"tool_name": tool, "arguments": args}},
        {"type": "tool_input_end", "id": call_id, "toolName": tool, "arguments": inner},
        {"type": "tool_result", "id": call_id, "preview": "ok", "isError": False},
    ]


@pytest.mark.asyncio
async def test_a_call_tool_card_names_the_tool_it_runs_from_start_to_finish() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), _wrapped("c1", "bash", {"command": "ls -la"}))

    assert adapter.cards, "no card was emitted"
    assert all(card_id == "c1" for card_id, _, _ in adapter.cards)
    assert all(title.startswith("bash") for _, title, _ in adapter.cards), adapter.cards
    assert not any("call_tool" in title for _, title, _ in adapter.cards)
    assert adapter.cards[-1][2] == "complete"


@pytest.mark.asyncio
async def test_the_card_title_is_the_inner_tools_argument_preview() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), _wrapped("c1", "read_file", {"path": "/tmp/notes.md"}))
    assert any("/tmp/notes.md" in title for _, title, _ in adapter.cards), adapter.cards


@pytest.mark.asyncio
async def test_the_inner_tool_sets_the_phase_title() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), _wrapped("c1", "summarize_context", {"scope": "all"}))
    assert adapter.plans == ["Compacting context"], adapter.plans


@pytest.mark.asyncio
async def test_when_the_wrapper_event_is_missed_the_inner_events_name_wins() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), [
        {"type": "tool_use_start", "id": "c2", "name": "call_tool"},
        {"type": "tool_input_end", "id": "c2", "toolName": "bash", "arguments": {"command": "pwd"}},
        {"type": "tool_result", "id": "c2", "preview": "/tmp", "isError": False},
    ])
    assert all(title.startswith("bash") for _, title, _ in adapter.cards), adapter.cards


@pytest.mark.asyncio
async def test_a_heartbeat_behind_call_tool_is_still_quiet() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), _wrapped("c3", "tasks", {"action": "heartbeat"}))
    assert adapter.cards == []


@pytest.mark.asyncio
async def test_a_direct_call_is_unchanged() -> None:
    adapter = _CardAdapter()
    await _feed(_consumer(adapter), [
        {"type": "tool_use_start", "id": "d1", "name": "bash"},
        {"type": "tool_input_end", "id": "d1", "toolName": "bash", "arguments": {"command": "ls"}},
        {"type": "tool_result", "id": "d1", "preview": "x", "isError": False},
    ])
    assert [title for _, title, _ in adapter.cards] == ["bash: ls", "bash: ls"]


def test_unwrap_reads_the_name_and_arguments_whatever_their_form() -> None:
    assert _unwrap_call_tool("bash", {"command": "ls"}) == ("bash", {"command": "ls"})
    assert _unwrap_call_tool("call_tool", {"tool_name": " bash ", "arguments": {"a": 1}}) == ("bash", {"a": 1})
    # The model may send the arguments as a JSON string.
    assert _unwrap_call_tool("call_tool", {"tool_name": "bash", "arguments": '{"a": 1}'}) == ("bash", {"a": 1})
    assert _unwrap_call_tool("call_tool", {"tool_name": "bash", "arguments": '{"a'}) == ("bash", {})
    assert _unwrap_call_tool("call_tool", {"tool_name": "bash"}) == ("bash", {})
    # No wrapper args: the inner event's declared name is used.
    assert _unwrap_call_tool("call_tool", {"command": "ls"}, "bash") == ("bash", {"command": "ls"})
    # Nothing names the tool.
    assert _unwrap_call_tool("call_tool", {"command": "ls"}) == ("call_tool", {"command": "ls"})
    assert _unwrap_call_tool("call_tool", {"tool_name": "  "}) == ("call_tool", {"tool_name": "  "})
