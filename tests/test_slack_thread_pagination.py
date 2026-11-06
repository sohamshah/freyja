"""Regression tests for ``SlackAdapter.fetch_thread_context``.

The bug being pinned: the original implementation did one call to ``conversations.replies`` with ``limit=50`` and returned the response as-is. Slack returns messages chronologically oldest-first, so on a long thread the agent saw the *first* 50 messages and was blind to everything that happened since. When a user @mentioned Freyja deep in a thread, the context the agent reasoned over was the thread's opening framing rather than its recent state.

The new behavior: walk pages forward, keep a rolling buffer of the last ``limit`` replies, pin the parent (page-1 / index-0), and inject a synthetic ``role: "system_note"`` marker between parent and tail when the thread is longer than the window.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from bridge.gateway.platforms.slack import SlackAdapter


class _FakeSlackClient:
    """Fakes ``AsyncWebClient.conversations_replies`` for an arbitrary thread length. Slack-shape: returns up to ``limit`` messages per call, paginated via ``response_metadata.next_cursor``."""

    def __init__(self, total_replies: int, *, parent_text: str = "PARENT") -> None:
        # Build the canonical reply list. Index 0 is the parent.
        self.messages: list[dict[str, Any]] = [
            {"ts": "1000.000000", "user": "Uparent", "text": parent_text}
        ]
        for i in range(1, total_replies + 1):
            self.messages.append({
                "ts": f"{1000 + i}.000000",
                "user": f"U{i % 5}",
                "text": f"reply {i}",
            })
        self.calls: list[dict[str, Any]] = []  # captures every paginated call

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        limit = int(kwargs.get("limit") or 200)
        cursor = kwargs.get("cursor")
        start = int(cursor) if cursor else 0
        end = min(start + limit, len(self.messages))
        page = self.messages[start:end]
        out: dict[str, Any] = {"messages": page, "has_more": end < len(self.messages)}
        if end < len(self.messages):
            out["response_metadata"] = {"next_cursor": str(end)}
        return out


def _make_adapter(client: _FakeSlackClient) -> SlackAdapter:
    """Stand up a SlackAdapter with just enough glue to call fetch_thread_context, bypassing the connect / Socket Mode path."""
    adapter = SlackAdapter()
    adapter._app = MagicMock()  # type: ignore[attr-defined]
    adapter._get_client = lambda *_a, **_k: client  # type: ignore[assignment]
    adapter._bot_user_id = "Ubot"
    return adapter


def test_short_thread_returns_all_messages_no_marker() -> None:
    """A 30-reply thread fits in the window: no gap, no marker, one API call."""
    client = _FakeSlackClient(total_replies=30)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    assert len(out) == 31
    assert out[0]["text"] == "PARENT"
    assert all(m["role"] != "system_note" for m in out)
    assert out[-1]["text"] == "reply 30"
    assert len(client.calls) == 1


def test_long_thread_returns_parent_plus_an_accurate_marker_plus_last_100() -> None:
    """A 300-reply thread yields parent + system_note + the last 100 replies; the marker reports the 200 messages not in the output, with no "+" because pagination was exhausted."""
    client = _FakeSlackClient(total_replies=300)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    assert out[0]["text"] == "PARENT"
    marker = out[1]
    assert marker["role"] == "system_note"
    assert "earlier replies" in marker["text"]
    assert "200" in marker["text"], marker["text"]
    assert "+" not in marker["text"], marker["text"]
    tail = out[2:]
    assert len(tail) == 100
    assert tail[0]["text"] == "reply 201"
    assert tail[-1]["text"] == "reply 300"
    assert all(m["role"] != "system_note" for m in tail)


def test_pathological_thread_caps_at_max_pages_and_marks_truncated() -> None:
    """Threads beyond MAX_PAGES * PAGE_SIZE (5000 msgs) get a "N+" marker so the agent knows it's seeing a bounded approximation."""
    # 6000 replies → 6001 total messages → exceeds 5 * 1000 = 5000.
    client = _FakeSlackClient(total_replies=6000)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    assert out[0]["text"] == "PARENT"
    marker = out[1]
    assert marker["role"] == "system_note"
    assert "+" in marker["text"], marker["text"]
    # Tail is still the last 100 of what we DID fetch: parent + 4999 replies = 5000 messages over MAX_PAGES (5) calls.
    tail = out[2:]
    assert len(tail) == 100
    assert tail[-1]["text"] == "reply 4999"
    assert len(client.calls) == 5


def test_exclude_ts_drops_the_trigger_and_oldest_ts_drops_seen_history() -> None:
    """``exclude_ts`` removes the @mention that is already being fed in separately; ``oldest_ts`` is a lower bound used when the bot already responded earlier in the thread and only the delta since its last reply is wanted (the parent is older too, so it goes)."""
    client = _FakeSlackClient(total_replies=10)
    adapter = _make_adapter(client)

    trigger_ts = "1005.000000"
    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000", exclude_ts=trigger_ts))
    assert all(m["ts"] != trigger_ts for m in out)
    assert any(m["text"] == "reply 4" for m in out)
    assert any(m["text"] == "reply 6" for m in out)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000", oldest_ts="1006.000000"))
    texts = [m["text"] for m in out if m["role"] != "system_note"]
    assert texts == ["reply 7", "reply 8", "reply 9", "reply 10"], texts
