"""Pin the SlackAdapter's hard refusal of oversized content.

Both ``send`` and ``edit`` historically had silent failure modes: send used to split-and-post (losing message-id tracking for all but the last chunk), edit used to truncate to a "(continued)" stub. Both were sources of visible data loss. The current contract is to return ``SendResult(ok=False, error="…max_message_chars…")`` so callers must chunk before calling.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from bridge.gateway.platforms.slack import SlackAdapter


def test_send_and_edit_reject_oversized_content() -> None:
    adapter = SlackAdapter()
    # Reach the size guard without a real Slack connection.
    adapter._app = MagicMock()  # type: ignore[attr-defined]
    adapter._get_client = lambda *_a, **_k: MagicMock()  # type: ignore[assignment]
    adapter._pop_slash_context_for = lambda *_a, **_k: None  # type: ignore[assignment]

    huge = "x" * (adapter.max_message_chars + 100)
    sent = asyncio.run(adapter.send("C123", huge))
    assert not sent.ok and "max_message_chars" in (sent.error or ""), sent.error
    edited = asyncio.run(adapter.edit("C123", "ts.1", huge))
    assert not edited.ok and "max_message_chars" in (edited.error or ""), edited.error
