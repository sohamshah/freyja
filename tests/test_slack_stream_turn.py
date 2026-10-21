"""One Slack message per turn: live prose, widgets, seal, status.

Pins the shape probed live on 2026-10-20: markdown, task and blocks
chunks mix in one stream, and stopStream carries the tail, the
feedback block, the session metadata and the session status.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource
from bridge.gateway.stream_consumer import _RETRY_NOTE, SlackStreamConsumer
from tests.slack_fakes import StreamingFakeAdapter


def _source(chat_type: str = "dm") -> MessageSource:
    return MessageSource(
        platform="slack", workspace_id="T1", chat_id="D1", chat_type=chat_type,
        user_id="U1", message_id="10.1", thread_id=None if chat_type == "dm" else "10.1",
    )


def _consumer(adapter: Any, *, verbosity: str = "all", chat_type: str = "dm") -> SlackStreamConsumer:
    return SlackStreamConsumer(
        adapter, _source(chat_type), session_key="freyja:slack:T1:dm:D1", verbosity=verbosity,
    )


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


@pytest.mark.asyncio
async def test_prose_cards_and_seal_land_in_one_stream():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    await _feed(consumer, [
        {"type": "text_delta", "text": "Looking."},
        {"type": "tool_use_start", "id": "t1", "name": "bash"},
        {"type": "tool_input_end", "id": "t1", "arguments": {"command": "ls"}},
        {"type": "tool_result", "id": "t1", "preview": "3 files"},
        {"type": "text_delta", "text": "Done."},
        {"type": "turn_complete"},
    ])
    assert adapter.streams_started == 1
    assert adapter.body == "Looking.\n\nDone."
    assert [c[2] for c in adapter.cards] == ["in_progress", "complete"]
    assert adapter.sent == []  # no separate body post
    stop = adapter.stop_calls[-1]
    assert stop["session_status"] == "active"
    assert stop["metadata"]["event_type"] == "freyja_session"
    assert stop["metadata"]["event_payload"]["session_key"] == "freyja:slack:T1:dm:D1"
    assert stop["blocks"][-1]["type"] == "context_actions"
    assert adapter.statuses[0] == "processing"


@pytest.mark.asyncio
async def test_first_delta_streams_at_once_and_later_ones_wait():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    consumer.edit_interval_ms = 50
    await _feed(consumer, [{"type": "text_delta", "text": "Hello "}])
    await asyncio.sleep(0.01)
    assert adapter.streamed == ["Hello "]
    await _feed(consumer, [{"type": "text_delta", "text": "again"}])
    await asyncio.sleep(0.01)
    assert adapter.streamed == ["Hello "]  # throttled
    await asyncio.sleep(0.08)
    assert adapter.streamed == ["Hello ", "again"]


@pytest.mark.asyncio
async def test_widget_blocks_go_into_the_stream_after_pending_prose():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    consumer.edit_interval_ms = 10_000
    await _feed(consumer, [{"type": "text_delta", "text": "Here is the table:"}])
    await asyncio.sleep(0.01)
    ok = await consumer.append_blocks([{"type": "data_table", "rows": []}])
    assert ok is True
    assert adapter.streamed == ["Here is the table:"]
    assert adapter.block_chunks == [[{"type": "data_table", "rows": []}]]
    await _feed(consumer, [{"type": "text_delta", "text": "Notes."}, {"type": "turn_complete"}])
    assert adapter.body == "Here is the table:\n\nNotes."


@pytest.mark.asyncio
async def test_retry_after_streamed_text_keeps_it_and_marks_the_retry():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    await _feed(consumer, [{"type": "stream_start"}, {"type": "text_delta", "text": "Partial answer"}])
    await asyncio.sleep(0.01)  # first flush fires at once
    assert adapter.streamed == ["Partial answer"]
    await _feed(consumer, [
        {"type": "stream_reset"},
        {"type": "stream_start"},
        {"type": "text_delta", "text": "Full answer."},
        {"type": "turn_complete"},
    ])
    assert adapter.body == f"Partial answer\n\n{_RETRY_NOTE}\n\nFull answer."


@pytest.mark.asyncio
async def test_stream_failure_falls_back_to_one_post():
    adapter = StreamingFakeAdapter(start_ok=False)
    consumer = _consumer(adapter)
    await _feed(consumer, [
        {"type": "text_delta", "text": "Plain "},
        {"type": "text_delta", "text": "reply."},
        {"type": "turn_complete"},
    ])
    assert adapter.streamed == []
    assert adapter.sent == ["Plain reply."]
    assert adapter.statuses[-1] == "active"


@pytest.mark.asyncio
async def test_long_prose_rolls_over_into_a_continuation_stream(monkeypatch):
    from bridge.gateway import stream_consumer as sc

    monkeypatch.setattr(sc, "_STREAM_BUDGET", 20)
    monkeypatch.setattr(sc, "_CHUNK_OVERHEAD", 0)
    monkeypatch.setattr(sc, "_TEXT_PIECE_CAP", 10)
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    consumer.edit_interval_ms = 0
    await _feed(consumer, [{"type": "text_delta", "text": "a" * 55}])
    await asyncio.sleep(0.02)
    await _feed(consumer, [{"type": "turn_complete"}])
    assert adapter.body == "a" * 55
    assert adapter.streams_started >= 2
    assert adapter.streams_stopped[-1] == f"stream-ts-{adapter.streams_started}"


@pytest.mark.asyncio
async def test_stop_button_abort_makes_no_further_calls():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    await _feed(consumer, [{"type": "text_delta", "text": "working"}])
    before = (len(adapter.stop_calls), len(adapter.statuses))
    consumer.abort(stopped_by_user=True)
    await asyncio.sleep(0.01)
    assert consumer.finalized
    assert (len(adapter.stop_calls), len(adapter.statuses)) == before
    await _feed(consumer, [{"type": "text_delta", "text": "late"}, {"type": "turn_complete"}])
    assert "late" not in adapter.body


@pytest.mark.asyncio
async def test_plain_abort_seals_the_stream_and_sets_active():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off")
    await _feed(consumer, [{"type": "text_delta", "text": "working"}])
    consumer.abort()
    await asyncio.sleep(0.01)
    assert adapter.stop_calls[-1]["session_status"] == "active"


@pytest.mark.asyncio
async def test_code_channel_turn_streams_without_a_thread():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter, verbosity="off", chat_type="code")
    consumer._reply_thread_id = ""
    await _feed(consumer, [{"type": "text_delta", "text": "top level"}, {"type": "turn_complete"}])
    assert adapter.streams_started == 1 and adapter.body == "top level"
