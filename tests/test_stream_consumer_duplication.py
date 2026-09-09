"""Regression tests for the 2026-08-26 Slack rendering incident.

One @mention produced: (a) a run-on body where every interstitial
prose block was concatenated with no separator ("…checks.Now let me
see…"), and (b) the ENTIRE body posted twice. Root causes:

  · body_buffer appended text_delta blocks with no paragraph break
    between phases (tool calls / thinking between prose blocks);
  · a turn that died before turn_complete ("ZAI_API_KEY is not set")
    never finalized its SlackStreamConsumer, so the consumer stayed
    registered and the NEXT turn's events fanned out to two consumers
    — two thinking streams, two identical body postMessages.

These tests pin the paragraph-break insertion in the consumer and the
single-consumer-per-session takeover in the gateway hooks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.run import _attach_turn_consumer, _detach_turn_consumer
from bridge.gateway.stream_consumer import SlackStreamConsumer


class _FakeAdapter:
    """Minimal adapter double: records sends, no-ops the stream API."""

    max_message_chars = 39_000

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.streams_stopped: list[str] = []

    def format_content(self, content: str) -> str:
        return content

    async def send(self, chat_id: str, content: str, **kwargs: Any) -> SendResult:
        self.sent.append(content)
        return SendResult(ok=True, message_id="1.0")

    async def start_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True, message_id="stream-ts")

    async def append_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True)

    async def stop_stream(self, chat_id: str, ts: str, **kw: Any) -> SendResult:
        self.streams_stopped.append(ts)
        return SendResult(ok=True)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        return None

    async def stop_typing(self, *a: Any, **kw: Any) -> None:
        return None


def _source() -> MessageSource:
    return MessageSource(
        platform="slack",
        workspace_id="T1",
        chat_id="C123",
        chat_type="channel",
        user_id="U1",
        message_id="100.1",
        thread_id="100.1",
    )


def _consumer(adapter: _FakeAdapter) -> SlackStreamConsumer:
    return SlackStreamConsumer(
        adapter,  # type: ignore[arg-type]
        _source(),
        session_key="freyja:slack:T1:channel:C123:100.1",
        verbosity="off",  # skip card plumbing — body path under test
    )


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


@pytest.mark.asyncio
async def test_body_gets_paragraph_break_between_phases() -> None:
    adapter = _FakeAdapter()
    consumer = _consumer(adapter)
    await _feed(
        consumer,
        [
            {"type": "text_delta", "text": "Looking at the repo"},
            {"type": "text_delta", "text": " now."},
            {"type": "tool_use_start", "id": "t1", "name": "bash"},
            {"type": "tool_result", "id": "t1", "preview": "ok"},
            {"type": "text_delta", "text": "Found the bug."},
            {"type": "turn_complete"},
        ],
    )
    assert adapter.sent == ["Looking at the repo now.\n\nFound the bug."]


@pytest.mark.asyncio
async def test_contiguous_deltas_not_split() -> None:
    adapter = _FakeAdapter()
    consumer = _consumer(adapter)
    await _feed(
        consumer,
        [
            {"type": "text_delta", "text": "One block"},
            {"type": "text_delta", "text": ", still one block."},
            {"type": "turn_complete"},
        ],
    )
    assert adapter.sent == ["One block, still one block."]


@pytest.mark.asyncio
async def test_no_double_break_when_block_already_ends_with_newline() -> None:
    adapter = _FakeAdapter()
    consumer = _consumer(adapter)
    await _feed(
        consumer,
        [
            {"type": "text_delta", "text": "Para one.\n\n"},
            {"type": "tool_use_start", "id": "t1", "name": "bash"},
            {"type": "text_delta", "text": "Para two."},
            {"type": "turn_complete"},
        ],
    )
    assert adapter.sent == ["Para one.\n\nPara two."]


class _FakeSession:
    id = "freyja:slack:T1:channel:C123:100.1"


@pytest.mark.asyncio
async def test_stale_consumer_torn_down_on_next_registration(monkeypatch) -> None:
    """A consumer orphaned by a failed turn (no turn_complete) must be
    unregistered and neutralized when the next turn attaches its own."""
    registered: list[Any] = []

    def _register(key: str, cb: Any) -> None:
        registered.append(cb)

    def _unregister(key: str, cb: Any) -> None:
        if cb in registered:
            registered.remove(cb)

    import bridge.freyja_bridge as fb

    monkeypatch.setattr(fb, "register_session_listener", _register)
    monkeypatch.setattr(fb, "unregister_session_listener", _unregister)

    adapter = _FakeAdapter()
    session = _FakeSession()
    key = session.id

    orphan = _consumer(adapter)
    _attach_turn_consumer(session, key, orphan)
    assert registered == [orphan.on_event]
    assert not orphan.finalized

    # Turn dies without turn_complete; next inbound attaches a fresh
    # consumer. The orphan must be gone and dead.
    fresh = _consumer(adapter)
    _attach_turn_consumer(session, key, fresh)
    assert registered == [fresh.on_event]
    assert orphan.finalized

    # Orphan drops all further events — nothing is buffered or sent.
    orphan.on_event({"type": "text_delta", "text": "ghost"})
    await asyncio.sleep(0)
    assert orphan._state.body_buffer == ""

    # Normal completion of the fresh consumer clears the session slot.
    await fresh.finalize()
    _detach_turn_consumer(session, fresh.on_event)
    assert getattr(session, "_active_stream_consumer", None) is None


@pytest.mark.asyncio
async def test_abort_seals_open_stream() -> None:
    adapter = _FakeAdapter()
    consumer = SlackStreamConsumer(
        adapter,  # type: ignore[arg-type]
        _source(),
        session_key="k",
        verbosity="all",
    )
    await consumer._handle_event_async(
        {"type": "thinking_delta", "thinking": "hmm"}
    )
    assert consumer._state.stream_ts == "stream-ts"

    consumer.abort()
    # abort schedules the seal via run_coroutine_threadsafe; give the
    # loop a few cycles to run it.
    for _ in range(20):
        if adapter.streams_stopped:
            break
        await asyncio.sleep(0.01)
    assert consumer.finalized
    assert adapter.streams_stopped == ["stream-ts"]
    # Body from the dead turn is never delivered.
    assert adapter.sent == []
