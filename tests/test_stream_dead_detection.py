"""Both of Slack's "that stream is gone" errors must disable card emission.

Slack rejects an append to a finished stream two different ways: ``message_not_in_streaming_state`` (the message exists, but stopStream already sealed it) and ``message_not_found`` (the message is gone entirely). Only the first was ever matched. A stream that died the second way left ``cards_disabled`` False, so every subsequent event retried the append and logged a full traceback. A Slack turn on 2026-09-09 produced 44 identical failures this way while the progress card sat frozen on "Searching".
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.stream_consumer import SlackStreamConsumer, _stream_is_dead
from tests.slack_fakes import StreamingFakeAdapter

_DEAD = [
    "message_not_in_streaming_state",
    "message_not_found",
    "The request to the Slack API failed. {'ok': False, 'error': 'message_not_found'}",
    "{'ok': False, 'error': 'message_not_in_streaming_state'}",
    "MESSAGE_NOT_FOUND",
]
# Disabling cards on a transient error would silently drop progress output for the rest of the turn.
_LIVE = [None, "", "ratelimited", "channel_not_found", "invalid_auth", "not connected"]


def test_dead_stream_errors_are_recognized_and_transient_ones_are_not():
    assert [_stream_is_dead(e) for e in _DEAD] == [True] * len(_DEAD)
    assert [_stream_is_dead(e) for e in _LIVE] == [False] * len(_LIVE)


class _FailingAdapter(StreamingFakeAdapter):
    """Every append fails the same way; counts the attempts."""

    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error
        self.appends = 0

    async def append_stream(
        self, chat_id: str, ts: str, *, chunks: list[Any] | None = None, **kw: Any,
    ) -> SendResult:
        self.appends += 1
        return SendResult(ok=False, error=self.error)


def _consumer(adapter: Any) -> SlackStreamConsumer:
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id="U1", message_id="1.1", thread_id="1.1",
    )
    c = SlackStreamConsumer(
        adapter, source, session_key="freyja:slack:T1:channel:C1:1.1", verbosity="all",
    )
    c.edit_interval_ms = 0
    return c


_TURN = [
    {"type": "text_delta", "text": "Searching."},
    {"type": "tool_use_start", "id": "t1", "name": "bash"},
    {"type": "tool_input_end", "id": "t1", "arguments": {"command": "rg needle"}},
    {"type": "tool_result", "id": "t1", "preview": "ok"},
    {"type": "text_delta", "text": "Found it."},
    {"type": "turn_complete"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["message_not_found", "message_not_in_streaming_state"])
async def test_a_dead_stream_is_not_retried_and_the_prose_falls_back_to_one_post(error):
    adapter = _FailingAdapter(error)
    consumer = _consumer(adapter)
    for ev in _TURN:
        await consumer._handle_event_async(ev)
    assert consumer.finalized
    assert adapter.appends == 1, "a dead stream must not be appended to again"
    assert adapter.streams_stopped == []
    assert adapter.sent == ["Searching.\n\nFound it."]


@pytest.mark.asyncio
async def test_a_transient_error_keeps_the_stream_alive():
    adapter = _FailingAdapter("ratelimited")
    consumer = _consumer(adapter)
    for ev in _TURN:
        await consumer._handle_event_async(ev)
    assert consumer.finalized
    assert adapter.appends > 1
    # The seal still carries the prose Slack never took.
    assert adapter.streams_stopped == ["stream-ts"]
    assert adapter.body == "Searching.\n\nFound it."
