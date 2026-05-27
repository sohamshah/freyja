"""Bridge event → Slack progressive edits.

Subscribes to events for a specific session id via the bridge's
``register_session_listener``. Each new turn instantiates a fresh
consumer that:

  · accumulates ``text_delta`` events into a buffer
  · sends a first message (``adapter.send``) the first time the buffer
    has visible content
  · throttles subsequent edits to ``adapter.edit`` at most every
    ``edit_interval_ms`` (default 500ms)
  · splits long responses across multiple messages when the buffer
    exceeds the platform's max-chars cap
  · finalizes on ``turn_complete`` by emitting one final edit with the
    full text so the operator sees the complete response
  · unregisters itself on finalize so the next turn gets a fresh
    consumer + fresh Slack message anchor

Slack doesn't have a typing indicator, so the streaming-edit-in-place
pattern IS the typing indicator. Operators see the bot's response grow
as the agent generates it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bridge.gateway.platforms.base import (
    MessageSource,
    PlatformAdapter,
    truncate_for_platform,
)

logger = logging.getLogger(__name__)


# Slack hard limit per message is 40k chars; leave some headroom for
# our own framing (a finalize marker, etc.).
DEFAULT_MAX_CHARS = 39_000


@dataclass
class _Anchor:
    """A single Slack message we're either growing in place or have
    finalized. A long response becomes a chain of anchors."""

    message_id: str | None = None
    content: str = ""


@dataclass
class _State:
    """Mutable consumer state. Held inside an asyncio.Lock for safe
    concurrent flush/append."""

    buffer: str = ""
    anchors: list[_Anchor] = field(default_factory=list)
    last_emit_monotonic: float = 0.0
    finalized: bool = False


class SlackStreamConsumer:
    """One per turn. Lifetime: from message-received → turn_complete.

    Usage::

        consumer = SlackStreamConsumer(adapter, source)
        register_session_listener(session_key, consumer.on_event)
        # ... gateway routes inbound message, agent runs ...
        # consumer auto-finalizes on turn_complete and unregisters
    """

    def __init__(
        self,
        adapter: PlatformAdapter,
        source: MessageSource,
        *,
        session_key: str,
        edit_interval_ms: int = 500,
        max_chars: int = DEFAULT_MAX_CHARS,
        raw_hint: dict[str, Any] | None = None,
        on_complete: Any = None,
    ) -> None:
        self.adapter = adapter
        self.source = source
        self.session_key = session_key
        self.edit_interval_ms = edit_interval_ms
        self.max_chars = max_chars
        self.raw_hint = raw_hint
        self.on_complete = on_complete  # called when finalize() runs

        self._state = _State()
        self._lock = asyncio.Lock()
        # Capture the loop the consumer was constructed on. emit() is
        # called synchronously from inside the runner's stream callbacks
        # (which run on this same loop), and we use run_coroutine_threadsafe
        # to schedule the async flush — works from inside or outside the
        # loop's call stack.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

    # ── public event ingress (sync callback fired by emit()) ──

    def on_event(self, event: dict[str, Any]) -> None:
        """Sync entry point — schedules async handling on the loop.

        Called synchronously by ``emit()`` so it must NOT block. We
        forward to an async task to do the actual Slack API work.
        """
        # If the consumer is already finalized, drop further events
        # (a sibling consumer is already active for the next turn).
        if self._state.finalized:
            return
        etype = event.get("type")
        # We only care about a small subset of event types.
        if etype not in {
            "text_delta",
            "turn_complete",
            "turn_start",
            "thinking_delta",
            "tool_use_start",
        }:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._handle_event_async(event), self._loop
            )
        except RuntimeError:
            # The loop is no longer running — silently drop.
            pass

    async def _handle_event_async(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "text_delta":
            text = str(event.get("text") or "")
            if text:
                await self._append(text)
        elif etype == "turn_complete":
            await self.finalize()
        # text_delta carries the agent's user-visible response. We
        # deliberately suppress thinking_delta and tool_use_start over
        # Slack — they're noise for the chat surface. Future: post
        # them as a collapsed "🔧 running tool X..." status reaction.

    # ── core flush loop ──

    async def _append(self, text: str) -> None:
        async with self._lock:
            self._state.buffer += text
            now = time.monotonic() * 1000
            since_last = now - self._state.last_emit_monotonic
            if since_last >= self.edit_interval_ms:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Push the current buffer to Slack. Must be called with the
        lock held."""
        if not self._state.buffer.strip():
            return

        # If the latest anchor's full content (current anchor's stored
        # content + new buffer) would exceed max_chars, finalize the
        # current anchor and start a new one.
        # We rebuild from "all anchors combined + buffer" so split
        # boundaries land cleanly.
        full = self._combined_content()
        chunks = truncate_for_platform(full, self.max_chars)

        # Map chunks → anchors. Existing anchors get edited; new
        # anchors get sent fresh.
        for idx, chunk in enumerate(chunks):
            if idx < len(self._state.anchors):
                anchor = self._state.anchors[idx]
                if anchor.content == chunk:
                    continue  # unchanged
                if anchor.message_id:
                    result = await self.adapter.edit(
                        self.source.chat_id,
                        anchor.message_id,
                        chunk,
                    )
                    if not result.ok:
                        logger.warning(
                            "slack edit failed for anchor %s: %s; "
                            "starting a new anchor",
                            anchor.message_id,
                            result.error,
                        )
                        # Fall back: open a new anchor for this chunk
                        # by appending one. The old (broken) anchor
                        # stays in the list with its stale content.
                        new_result = await self.adapter.send(
                            self.source.chat_id,
                            chunk,
                            thread_id=self.source.thread_id,
                            raw_hint=self.raw_hint,
                        )
                        if new_result.ok:
                            self._state.anchors.append(
                                _Anchor(message_id=new_result.message_id, content=chunk)
                            )
                        continue
                anchor.content = chunk
            else:
                # New chunk — send as a new Slack message.
                result = await self.adapter.send(
                    self.source.chat_id,
                    chunk,
                    thread_id=self.source.thread_id,
                    raw_hint=self.raw_hint if idx == 0 else None,
                )
                if result.ok:
                    self._state.anchors.append(
                        _Anchor(message_id=result.message_id, content=chunk)
                    )
                else:
                    logger.warning("slack send failed: %s", result.error)

        # Buffer is now fully reflected in anchors; consolidate.
        self._state.buffer = ""
        # Rebuild buffer to "what's not yet in an anchor" — which is
        # nothing after a successful flush. The implementation above
        # works on full-text reslicing each time which is O(N) per
        # flush; for a 39k cap and ~500ms cadence that's ~80
        # operations per second worst case, well within budget.
        self._state.last_emit_monotonic = time.monotonic() * 1000

    def _combined_content(self) -> str:
        # Concatenate any text already split across anchors + the
        # pending buffer. Used to recompute chunk boundaries each
        # flush, so re-slicing on a different boundary never silently
        # truncates content.
        parts = [a.content for a in self._state.anchors] + [self._state.buffer]
        return "".join(parts)

    # ── finalize ──

    async def finalize(self) -> None:
        """Final flush + listener unregister. Idempotent."""
        async with self._lock:
            if self._state.finalized:
                return
            await self._flush_locked()
            self._state.finalized = True
        # Notify the gateway that this turn is done (so the per-turn
        # listener registration can be cleared).
        if self.on_complete:
            try:
                result = self.on_complete()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("on_complete callback raised")
