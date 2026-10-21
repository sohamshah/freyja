"""Event order and Slack's 12k cap in the stream consumer.

Pins what the 2026-10-20 production thread showed. Deltas handled out
of order put a paragraph break inside a word ("C" / "I on #1371") and
sealed the message before the last deltas arrived, so the tail went
out as a second message under the feedback buttons. A long turn hit
``msg_too_long`` at Slack's 12,000-character cap, after which every
append was lost and the prose came out as one flat post at the end.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from bridge.gateway import stream_consumer as sc
from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.stream_consumer import SlackStreamConsumer
from tests.slack_fakes import StreamingFakeAdapter


def _source() -> MessageSource:
    return MessageSource(
        platform="slack", workspace_id="T1", chat_id="D1", chat_type="dm",
        user_id="U1", message_id="10.1", thread_id=None,
    )


def _consumer(adapter: Any, *, verbosity: str = "all") -> SlackStreamConsumer:
    c = SlackStreamConsumer(
        adapter, _source(), session_key="freyja:slack:T1:dm:D1", verbosity=verbosity,
    )
    c.edit_interval_ms = 0
    return c


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


class SlowAdapter(StreamingFakeAdapter):
    """Every append takes a moment, like a real round trip to Slack."""

    async def append_stream(
        self, chat_id: str, ts: str, *, chunks: list[Any] | None = None, **kw: Any,
    ) -> SendResult:
        await asyncio.sleep(0.005)
        return await super().append_stream(chat_id, ts, chunks=chunks, **kw)


class CappedAdapter(StreamingFakeAdapter):
    """Counts prose and card content per stream and answers
    ``msg_too_long`` past the cap, the way Slack does."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap
        self.chars: dict[str, int] = {}
        self.by_stream: dict[str, list[Any]] = {}

    @staticmethod
    def _count(chunks: list[Any] | None) -> int:
        n = 0
        for c in chunks or []:
            if hasattr(c, "status"):
                n += len(c.title or "") + len(c.output or "")
            elif hasattr(c, "text"):
                n += len(c.text)
        return n

    async def start_stream(self, *a: Any, **kw: Any) -> SendResult:
        res = await super().start_stream(*a, **kw)
        if res.ok:
            self.chars[res.message_id] = 0
        return res

    async def append_stream(
        self, chat_id: str, ts: str, *, chunks: list[Any] | None = None, **kw: Any,
    ) -> SendResult:
        n = self._count(chunks)
        if self.chars.get(ts, 0) + n > self.cap:
            return SendResult(ok=False, error="msg_too_long")
        self.chars[ts] = self.chars.get(ts, 0) + n
        self.by_stream.setdefault(ts, []).extend(chunks or [])
        return await super().append_stream(chat_id, ts, chunks=chunks, **kw)

    async def stop_stream(self, chat_id: str, ts: str, **kw: Any) -> SendResult:
        n = self._count(kw.get("chunks"))
        if self.chars.get(ts, 0) + n > self.cap:
            return SendResult(ok=False, error="msg_too_long")
        self.chars[ts] = self.chars.get(ts, 0) + n
        self.by_stream.setdefault(ts, []).extend(kw.get("chunks") or [])
        return await super().stop_stream(chat_id, ts, **kw)


async def _wait_sealed(adapter: StreamingFakeAdapter, consumer: SlackStreamConsumer) -> None:
    for _ in range(300):
        await asyncio.sleep(0.01)
        last = adapter.stop_calls[-1] if adapter.stop_calls else {}
        if consumer.finalized and last.get("session_status") == "active":
            return
    raise AssertionError("the turn never sealed")


@pytest.mark.asyncio
async def test_events_keep_their_order_around_slow_appends():
    """The engine thread fires on_event faster than Slack answers. The
    prose must still come out in order, the paragraph break goes before
    the new sentence and not inside it, and the tail rides on the seal."""
    adapter = SlowAdapter()
    consumer = _consumer(adapter)
    events = [
        {"type": "stream_start"},
        {"type": "text_delta", "text": "Checking."},
        {"type": "tool_use_start", "id": "t1", "name": "bash"},
        {"type": "tool_input_end", "id": "t1", "arguments": {"command": "gh pr checks 1371"}},
        {"type": "tool_result", "id": "t1", "preview": "pass"},
        {"type": "stream_start"},
        {"type": "text_delta", "text": "C"},
        {"type": "text_delta", "text": "I on #1371 is green. "},
        {"type": "text_delta", "text": "Posting the comment on #1"},
        {"type": "text_delta", "text": "371 now."},
        {"type": "turn_complete"},
    ]

    def _emit() -> None:
        for ev in events:
            consumer.on_event(ev)

    threading.Thread(target=_emit).start()
    await _wait_sealed(adapter, consumer)
    assert adapter.body == "Checking.\n\nCI on #1371 is green. Posting the comment on #1371 now."
    assert adapter.sent == []
    assert adapter.stop_calls[-1]["chunks"], "the tail rode on the seal"
    assert adapter.stop_calls[-1]["blocks"][-1]["type"] == "context_actions"


@pytest.mark.asyncio
async def test_card_content_counts_toward_the_cap_and_the_turn_rolls_over(monkeypatch):
    monkeypatch.setattr(sc, "_STREAM_BUDGET", 700)
    monkeypatch.setattr(sc, "_STREAM_CHAR_CAP", 900)
    adapter = CappedAdapter(cap=900)
    consumer = _consumer(adapter)
    events: list[dict[str, Any]] = [{"type": "text_delta", "text": "Start."}]
    for i in range(5):
        events += [
            {"type": "tool_use_start", "id": f"t{i}", "name": "bash"},
            {"type": "tool_input_end", "id": f"t{i}", "arguments": {"command": f"step {i}"}},
            {"type": "tool_result", "id": f"t{i}", "preview": "o" * 200},
        ]
    events += [{"type": "text_delta", "text": "Done."}, {"type": "turn_complete"}]
    await _feed(consumer, events)
    assert adapter.body == "Start.\n\nDone."
    assert adapter.streams_started >= 2
    # Every tool resolved somewhere, nothing was dropped.
    done = {c[0] for c in adapter.cards if c[2] == "complete"}
    assert {f"t{i}" for i in range(5)} <= done
    # The earlier seals keep the session working; only the last one is
    # idle and carries the feedback buttons.
    earlier = adapter.stop_calls[:-1]
    assert [s["session_status"] for s in earlier] == ["processing"] * len(earlier)
    assert adapter.stop_calls[-1]["session_status"] == "active"
    assert all("blocks" not in s for s in adapter.stop_calls[:-1])
    assert adapter.stop_calls[-1]["blocks"][-1]["type"] == "context_actions"


@pytest.mark.asyncio
async def test_a_short_estimate_still_recovers_from_msg_too_long(monkeypatch):
    """The consumer believes there is room; Slack disagrees. The piece
    lands in a continuation stream instead of being lost."""
    monkeypatch.setattr(sc, "_CHUNK_OVERHEAD", 0)
    adapter = CappedAdapter(cap=50)
    consumer = _consumer(adapter, verbosity="off")
    await _feed(consumer, [{"type": "text_delta", "text": "a" * 30}])
    await asyncio.sleep(0.01)
    await _feed(consumer, [{"type": "text_delta", "text": "b" * 30}])
    await asyncio.sleep(0.01)
    await _feed(consumer, [{"type": "turn_complete"}])
    assert adapter.body == "a" * 30 + "b" * 30
    assert adapter.streams_started == 2
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_the_seal_tail_moves_to_a_new_message_when_it_does_not_fit(monkeypatch):
    monkeypatch.setattr(sc, "_STREAM_BUDGET", 40)
    monkeypatch.setattr(sc, "_STREAM_CHAR_CAP", 50)
    monkeypatch.setattr(sc, "_CHUNK_OVERHEAD", 0)
    adapter = CappedAdapter(cap=50)
    consumer = _consumer(adapter, verbosity="off")
    consumer.edit_interval_ms = 10_000
    await _feed(consumer, [{"type": "text_delta", "text": "x" * 30}])
    await asyncio.sleep(0.01)  # first flush goes out at once
    await _feed(consumer, [{"type": "text_delta", "text": "y" * 25}, {"type": "turn_complete"}])
    assert adapter.body == "x" * 30 + "y" * 25
    assert adapter.streams_started == 2
    assert adapter.sent == []
    assert adapter.stop_calls[-1]["session_status"] == "active"


@pytest.mark.asyncio
async def test_thinking_card_is_capped_with_a_marker(monkeypatch):
    monkeypatch.setattr(sc, "_THINKING_CARD_CAP", 50)
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    await _feed(consumer, [
        {"type": "thinking_delta", "thinking": "x" * 40},
        {"type": "thinking_delta", "thinking": "y" * 40},
        {"type": "thinking_delta", "thinking": "z" * 40},
        {"type": "text_delta", "text": "Reply."},
        {"type": "turn_complete"},
    ])
    out = "".join(o for _, o in adapter.card_outputs)
    assert out.startswith("x" * 40) and out.endswith(" …")
    assert len(out) <= 50 + 2
    assert "z" not in out


@pytest.mark.asyncio
async def test_a_running_card_is_closed_and_reopened_across_a_rollover(monkeypatch):
    monkeypatch.setattr(sc, "_STREAM_BUDGET", 120)
    monkeypatch.setattr(sc, "_STREAM_CHAR_CAP", 150)
    monkeypatch.setattr(sc, "_CHUNK_OVERHEAD", 0)
    adapter = CappedAdapter(cap=150)
    consumer = _consumer(adapter)
    await _feed(consumer, [
        {"type": "text_delta", "text": "Running the suite."},
        {"type": "tool_use_start", "id": "t1", "name": "bash"},
        {"type": "tool_input_end", "id": "t1", "arguments": {"command": "pytest"}},
        # Prose while the tool runs fills the message.
        {"type": "text_delta", "text": "p" * 110},
    ])
    await asyncio.sleep(0.02)  # the flush runs, and with it the rollover
    await _feed(consumer, [
        {"type": "tool_result", "id": "t1", "preview": "545 passed"},
        {"type": "text_delta", "text": "Green."},
        {"type": "turn_complete"},
    ])
    first, second = adapter.streams_stopped[0], adapter.streams_stopped[-1]
    assert first != second
    first_cards = [
        (c.id, c.status, c.output) for c in adapter.by_stream[first] if hasattr(c, "status")
    ]
    assert ("t1", "in_progress", None) in first_cards
    assert ("t1", "complete", sc._CONTINUED_NOTE) in first_cards
    second_cards = [(c.id, c.status) for c in adapter.by_stream[second] if hasattr(c, "status")]
    assert second_cards[:2] == [("t1", "in_progress"), ("t1", "complete")]
    assert adapter.body == "Running the suite.\n\n" + "p" * 110 + "\n\nGreen."


@pytest.mark.asyncio
async def test_prose_waiting_on_the_timer_goes_out_before_a_card():
    """Cards go out at once and prose waits for the flush timer, so
    without this the prose landed one card late in the timeline."""
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    consumer.edit_interval_ms = 10_000
    await _feed(consumer, [
        {"type": "text_delta", "text": "Checking the stack."},
        {"type": "tool_use_start", "id": "t1", "name": "bash"},
        {"type": "tool_input_end", "id": "t1", "arguments": {"command": "gh pr list"}},
        {"type": "tool_result", "id": "t1", "preview": "8 open"},
        {"type": "text_delta", "text": "Eight PRs. "},
        {"type": "thinking_delta", "thinking": "which first"},
        {"type": "text_delta", "text": "Starting with #1371."},
        {"type": "turn_complete"},
    ])
    assert adapter.order == ["text", "card", "card", "text", "card", "card", "text"]
    assert adapter.body == "Checking the stack.\n\nEight PRs. \n\nStarting with #1371."
