"""Regression tests for ``SlackAdapter.fetch_thread_context``.

The bug being pinned: the original implementation did one call to
``conversations.replies`` with ``limit=50`` and returned the response
as-is. Slack returns messages chronologically oldest-first, so on a
long thread the agent saw the *first* 50 messages and was blind to
everything that happened since. When a user @mentioned Freyja deep in
a thread, the context the agent reasoned over was the thread's
opening framing rather than its recent state.

The new behavior: walk pages forward, keep a rolling buffer of the
last ``limit`` replies, pin the parent (page-1 / index-0), and inject
a synthetic ``role: "system_note"`` marker between parent and tail
when the thread is longer than the window.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from bridge.gateway.platforms.slack import SlackAdapter


class _FakeSlackClient:
    """Fakes ``AsyncWebClient.conversations_replies`` for an arbitrary
    thread length. Slack-shape: returns up to ``limit`` messages per
    call, paginated via ``response_metadata.next_cursor``."""

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
    """Stand up a SlackAdapter with just enough glue to call
    fetch_thread_context — bypass the connect / Socket Mode path."""
    adapter = SlackAdapter()
    adapter._app = MagicMock()  # type: ignore[attr-defined]
    adapter._get_client = lambda *_a, **_k: client  # type: ignore[assignment]
    adapter._bot_user_id = "Ubot"
    return adapter


def test_short_thread_returns_all_messages_no_marker() -> None:
    """A 30-reply thread fits in the window — no gap, no marker."""
    client = _FakeSlackClient(total_replies=30)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    # Parent + 30 replies = 31 messages, all visible, no synthetic marker.
    assert len(out) == 31
    assert out[0]["text"] == "PARENT"
    assert all(m["role"] != "system_note" for m in out)
    assert out[-1]["text"] == "reply 30"
    # Only one API call needed (well under PAGE_SIZE).
    assert len(client.calls) == 1


def test_long_thread_returns_parent_plus_marker_plus_last_100() -> None:
    """A 300-reply thread should yield: parent + system_note + 100 tail."""
    client = _FakeSlackClient(total_replies=300)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    # Parent at the front …
    assert out[0]["text"] == "PARENT"
    # … then the gap marker …
    assert out[1]["role"] == "system_note"
    assert "earlier replies" in out[1]["text"]
    # … then exactly the last 100 replies.
    tail = out[2:]
    assert len(tail) == 100
    assert tail[0]["text"] == "reply 201"
    assert tail[-1]["text"] == "reply 300"
    # No `system_note` should leak into the tail.
    assert all(m["role"] != "system_note" for m in tail)


def test_long_thread_skipped_count_is_accurate() -> None:
    """Marker should report the number of messages NOT in the output."""
    client = _FakeSlackClient(total_replies=300)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    marker = out[1]
    # 301 total - 1 (parent) - 100 (tail) = 200 skipped.
    assert "200" in marker["text"], (
        f"expected skipped count of 200 in marker, got: {marker['text']!r}"
    )
    # Not truncated — we exhausted pagination, so no "+".
    assert "+" not in marker["text"], (
        f"expected definite count (no '+'), got: {marker['text']!r}"
    )


def test_pathological_thread_caps_at_max_pages_and_marks_truncated() -> None:
    """Threads beyond MAX_PAGES * PAGE_SIZE (5000 msgs) get a "N+"
    marker so the agent knows it's seeing a bounded approximation."""
    # 6000 replies → 6001 total messages → exceeds 5 * 1000 = 5000.
    client = _FakeSlackClient(total_replies=6000)
    adapter = _make_adapter(client)

    out = asyncio.run(adapter.fetch_thread_context("C1", "1000.000000"))

    assert out[0]["text"] == "PARENT"
    marker = out[1]
    assert marker["role"] == "system_note"
    # "+" indicates we capped before exhausting the thread.
    assert "+" in marker["text"], (
        f"expected '+' suffix on capped marker, got: {marker['text']!r}"
    )
    # Tail is still the last 100 of what we DID fetch.
    tail = out[2:]
    assert len(tail) == 100
    # We stopped after MAX_PAGES (5) pages of PAGE_SIZE (1000); the
    # last reply in our buffer is reply #4999 (parent + 4999 replies =
    # 5000 messages fetched).
    assert tail[-1]["text"] == "reply 4999"
    # Exactly MAX_PAGES API calls.
    assert len(client.calls) == 5


def test_excludes_the_trigger_message() -> None:
    """``exclude_ts`` should drop a specific message from the output —
    the @mention that's already being fed in separately."""
    client = _FakeSlackClient(total_replies=10)
    adapter = _make_adapter(client)

    # Pretend reply #5 is the @mention that triggered this turn.
    trigger_ts = "1005.000000"
    out = asyncio.run(adapter.fetch_thread_context(
        "C1", "1000.000000", exclude_ts=trigger_ts,
    ))

    # The trigger should not appear anywhere.
    assert all(m["ts"] != trigger_ts for m in out)
    # Other messages still present.
    assert any(m["text"] == "reply 4" for m in out)
    assert any(m["text"] == "reply 6" for m in out)


def test_oldest_ts_filters_out_already_seen_history() -> None:
    """``oldest_ts`` is a lower bound — used when the bot already
    responded earlier in the thread and we want only the delta since
    its last reply."""
    client = _FakeSlackClient(total_replies=10)
    adapter = _make_adapter(client)

    # Only return messages strictly newer than reply #6.
    out = asyncio.run(adapter.fetch_thread_context(
        "C1", "1000.000000", oldest_ts="1006.000000",
    ))

    # Parent (ts 1000) is older than 1006 → also filtered out.
    # Only replies 7-10 should survive.
    texts = [m["text"] for m in out if m["role"] != "system_note"]
    assert texts == ["reply 7", "reply 8", "reply 9", "reply 10"], texts


if __name__ == "__main__":
    test_short_thread_returns_all_messages_no_marker()
    print("✓ test_short_thread_returns_all_messages_no_marker")
    test_long_thread_returns_parent_plus_marker_plus_last_100()
    print("✓ test_long_thread_returns_parent_plus_marker_plus_last_100")
    test_long_thread_skipped_count_is_accurate()
    print("✓ test_long_thread_skipped_count_is_accurate")
    test_pathological_thread_caps_at_max_pages_and_marks_truncated()
    print("✓ test_pathological_thread_caps_at_max_pages_and_marks_truncated")
    test_excludes_the_trigger_message()
    print("✓ test_excludes_the_trigger_message")
    test_oldest_ts_filters_out_already_seen_history()
    print("✓ test_oldest_ts_filters_out_already_seen_history")
    print("\nAll thread-pagination regression tests passed.")
