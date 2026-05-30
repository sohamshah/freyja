"""Bridge event → Slack streaming message (Thinking Steps).

Subscribes to events for a specific session id via the bridge's
``register_session_listener``. Each new turn instantiates a fresh
consumer that opens ONE streaming message via Slack's chat.startStream
+ chat.appendStream + chat.stopStream API trio (slack-sdk 3.40+),
delivering both the agent's prose response AND its tool calls /
thinking through native Slack Block Kit primitives:

  · ``markdown_text`` chunks for the agent's user-facing prose body
  · ``TaskUpdateChunk`` per tool call — renders as a collapsible card
    with a status indicator (``in_progress`` → ``complete`` / ``error``)
    that the user can expand to see args + output
  · ``TaskUpdateChunk`` per thinking phase — surfaces the model's
    reasoning as another collapsible card titled "Thinking"
  · ``PlanUpdateChunk`` once near the start to give the task-card
    group a header
  · ``chat.stopStream(blocks=…)`` for inline image attachments
    rendered at the end of the message

Replaces the prior dual-message pattern (a primary text message edited
via chat.update + a separate "tool progress" bubble with emoji lines).
Slack's native collapsing means we no longer need our own emoji-based
progress visual — the chevron + timeline is rendered by Slack.

Verbosity (off/new/all/verbose) controls how much detail lands in the
task cards. Default is ``all`` (every tool call gets a card) since
native collapse means the visual cost is near-zero. ``off`` skips
task cards entirely (markdown_text only). Heartbeat tool calls are
suppressed in all levels except ``verbose``.

Lifecycle:
  · First event of the turn → set Slack Assistant Threads typing status
  · First substantive event (text/tool/thinking) → start_stream opens
    the message; capture the ts for subsequent calls
  · text_delta → buffer locally; throttled flush to append_stream
  · thinking_delta → open/extend the "Thinking" task card
  · tool_use_start → record name; (rendering deferred until tool_input_end
    so we can heartbeat-filter)
  · tool_input_end → emit TaskUpdateChunk(status="in_progress")
  · tool_result → emit TaskUpdateChunk(status="complete", output=…);
    handle inline image data
  · turn_complete → flush remaining text; close any open thinking card;
    call stop_stream (with image blocks if any pending)
  · stop_typing + on_complete callback → unregisters this consumer

Backwards compat note: this code REPLACES the prior chat.postMessage +
chat.update flow entirely. If start_stream fails (rate limit, API
change, workspace tier limitation), the fallback path posts a
minimal error message via send() — see T20 for the planned graceful
chat.postMessage fallback.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.gateway.platforms.base import (
    MessageSource,
    PlatformAdapter,
    UploadItem,
)

logger = logging.getLogger(__name__)


# ── Streaming throttle ────────────────────────────────────────────────
# Slack's chat.update rate limit is documented as ~1/sec/thread. The
# stream methods are newer and not documented as tightly, but we
# defensively throttle text + thinking-card appends to the same
# cadence so we don't burst-rate-limit on a fast-streaming model.
# Tool-card chunks (one per tool transition) fire infrequently enough
# that we send them immediately.
DEFAULT_TEXT_FLUSH_INTERVAL_MS = 500
DEFAULT_THINKING_FLUSH_INTERVAL_MS = 800

# Image extension allowlist for the "File saved to `/path`" → upload
# path extraction. Used by _handle_tool_result.
_FILE_SAVED_RE = re.compile(r"File saved to `([^`]+)`")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Plan title that wraps the per-turn task cards. Slack renders this
# above the cards when task_display_mode="plan" is set on startStream.
# Deliberately bland — the cards themselves carry the actual content.
PLAN_TITLE = "Working"


@dataclass
class _StreamState:
    """Per-turn mutable state.

    Replaces the old _State / _Anchor / _ToolProgress trio. The new
    model is much simpler: one stream ts, two text buffers (body +
    thinking), and dicts tracking which tools are in flight.
    """

    # Set after start_stream succeeds. Subsequent append_stream /
    # stop_stream calls use this as their handle.
    stream_ts: str | None = None
    # We have attempted start_stream (success or failure). Idempotent
    # gating for _ensure_stream_opened.
    stream_opened: bool = False
    # start_stream failed; downstream calls degrade to a fallback send.
    stream_failed: bool = False

    # Body text accumulator. Deltas land here; the flush loop drains
    # them to append_stream(markdown_text=…) every throttle window.
    text_buffer: str = ""
    last_text_flush_ms: float = 0.0

    # Thinking-card accumulator. Same pattern as text but routes to a
    # TaskUpdateChunk(title="Thinking") instead of markdown_text.
    # `thinking_buffer` is the FULL cumulative reasoning text; we
    # track `thinking_sent` separately so each TaskUpdateChunk we
    # emit carries only the unsent delta. Slack APPENDS the `output`
    # field on consecutive task updates with the same id (the same
    # semantics as markdown_text on appendStream), so sending the
    # cumulative buffer on every flush concatenates the entire
    # reasoning text repeatedly into the card.
    thinking_card_id: str | None = None
    thinking_buffer: str = ""
    thinking_sent: str = ""
    last_thinking_flush_ms: float = 0.0

    # Lifecycle gates.
    finalized: bool = False
    typing_set: bool = False
    plan_chunk_sent: bool = False

    # Tool-call tracking. tool_use_start stashes name → cached for
    # tool_result's completion chunk so it can render the same title.
    # tool_input_end caches args for the same reason (and for the
    # heartbeat filter).
    pending_tool_names: dict[str, str] = field(default_factory=dict)
    pending_tool_args: dict[str, dict] = field(default_factory=dict)

    # Inline image attachments accumulated during the turn for final
    # delivery via stop_stream(blocks=…). Each entry is a dict with
    # keys: data (bytes), mime, filename. Limited to a small budget so
    # one chat.stopStream call doesn't blow past Slack's payload cap.
    pending_image_blocks: list[dict[str, Any]] = field(default_factory=list)

    # Graceful fallback when chat.startStream fails (rate limit, API
    # change, workspace tier). The fallback path uses chat.postMessage
    # for the first delta and chat.update for subsequent deltas — the
    # old streaming pattern. Tool calls are NOT rendered as cards in
    # fallback mode (Slack post/update doesn't support task cards);
    # they're either dropped or appended as a text suffix. The user
    # still gets the prose response.
    fallback_message_id: str | None = None
    fallback_buffer: str = ""
    fallback_committed: str = ""

    # Same-tool coalescing under verbosity="new". Records the last
    # tool-name + repeat count so we can decorate the card title with
    # ×N instead of emitting N separate cards.
    coalesce_last_tool: str | None = None
    coalesce_count: int = 0


class SlackStreamConsumer:
    """One per turn. Lifetime: from message-received → turn_complete.

    Usage::

        consumer = SlackStreamConsumer(adapter, source, session_key=…)
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
        edit_interval_ms: int = DEFAULT_TEXT_FLUSH_INTERVAL_MS,
        max_chars: int | None = None,
        raw_hint: dict[str, Any] | None = None,
        on_complete: Any = None,
        permission_resolver: Any = None,
        verbosity: str = "all",
    ) -> None:
        self.adapter = adapter
        self.source = source
        self.session_key = session_key
        self.edit_interval_ms = edit_interval_ms
        # Retained for potential future use (oversized response splitting
        # via multiple streams), but Thinking Steps lets one message
        # carry far more than the old 39k cap because cards collapse.
        adapter_cap = int(getattr(adapter, "max_message_chars", 39_000))
        self.max_chars = max_chars if max_chars is not None else adapter_cap
        self.raw_hint = raw_hint
        self.on_complete = on_complete
        self.permission_resolver = permission_resolver
        # Tracking for the destructive-command approval flow — preserved
        # for parity even though chat.startStream / appendStream paths
        # don't themselves issue approval prompts.
        self._pending_permission_ids: set[str] = set()

        # Verbosity ladder. New default is "all" since native task
        # cards collapse — visual noise no longer scales with detail.
        if verbosity not in {"off", "new", "all", "verbose"}:
            verbosity = "all"
        self.verbosity = verbosity

        self._state = _StreamState()
        self._lock = asyncio.Lock()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        # Resolve thread anchor for start_stream. Slack rejects
        # thread_ts=None on chat.startStream, so we ALWAYS need a
        # thread anchor — for top-level messages we use the inbound
        # message's own ts so the streaming reply becomes the first
        # message in a new thread rooted on the user's message.
        self._reply_thread_id: str = self._resolve_thread_anchor()

    def _resolve_thread_anchor(self) -> str:
        """Pick the thread_ts to pass to chat.startStream.

        Order of preference:
          1. ``source.thread_id`` — the inbound was already in a thread
          2. For DMs with reply_in_thread enabled: ``source.message_id``
             — anchor a fresh thread on the user's message
          3. ``source.message_id`` — same as 2 but unconditional;
             chat.startStream requires SOME thread_ts and rooting on
             the user's message is the least-surprising default for
             any platform that doesn't natively support threadless
             streaming replies.
        """
        src = self.source
        if src.thread_id:
            return str(src.thread_id)
        # Read the adapter's slack_config if exposed.
        cfg = getattr(self.adapter, "slack_config", None)
        reply_in_thread = bool(getattr(cfg, "reply_in_thread", True))
        if reply_in_thread and src.chat_type == "dm" and src.message_id:
            return str(src.message_id)
        # Fall through — anchor on the message anyway. Better than
        # failing the start_stream entirely.
        return str(src.message_id or "")

    # ── public event ingress (sync callback fired by emit()) ──

    def on_event(self, event: dict[str, Any]) -> None:
        """Sync entry point — schedules async handling on the loop.

        Called synchronously by ``emit()`` so it must NOT block. We
        forward to an async task to do the actual Slack API work.
        """
        if self._state.finalized:
            return
        etype = event.get("type")
        # Event types we care about. thinking_delta is new in this
        # consumer — we route it into a Task Card so reasoning shows
        # up alongside tool calls in the collapsible widget.
        if etype not in {
            "text_delta",
            "thinking_delta",
            "turn_start",
            "turn_complete",
            "tool_use_start",
            "tool_input_end",
            "tool_result",
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

        # Set the Slack Assistant Threads typing status on the first
        # event of the turn (best-effort; harmless if it fails on
        # non-Assistant threads).
        if not self._state.typing_set:
            self._state.typing_set = True
            try:
                await self.adapter.send_typing(
                    self.source.chat_id,
                    thread_id=self._reply_thread_id,
                )
            except Exception:  # noqa: BLE001
                logger.debug("send_typing failed (non-fatal)", exc_info=True)

        if etype == "text_delta":
            text = str(event.get("text") or "")
            if not text:
                return
            await self._ensure_stream_opened()
            # Body text arriving means any in-flight thinking phase
            # is over — close that card before we start emitting prose.
            await self._close_thinking_card()
            await self._append_text(text)
        elif etype == "thinking_delta":
            if self.verbosity == "off":
                return
            text = str(event.get("thinking") or event.get("text") or "")
            if not text:
                return
            await self._ensure_stream_opened()
            await self._append_thinking(text)
        elif etype == "tool_use_start":
            if self.verbosity == "off":
                return
            tool_id = str(event.get("id") or "")
            tool_name = str(event.get("name") or "?")
            if tool_id:
                self._state.pending_tool_names[tool_id] = tool_name
            # Defer the in_progress card until tool_input_end fires —
            # we need the args to (a) heartbeat-filter and (b) render
            # a meaningful title. The brief gap (tens of ms typically)
            # is covered by the typing indicator we set above.
        elif etype == "tool_input_end":
            if self.verbosity == "off":
                return
            await self._handle_tool_input_end(event)
        elif etype == "tool_result":
            await self._handle_tool_result(event)
        elif etype == "turn_complete":
            await self.finalize()

    # ── stream lifecycle ──

    async def _ensure_stream_opened(self) -> None:
        """Idempotent: open the stream on first call, no-op after."""
        async with self._lock:
            if self._state.stream_opened or self._state.stream_failed:
                return
            self._state.stream_opened = True
            if not self._reply_thread_id:
                logger.warning("[slack] no thread anchor — start_stream skipped")
                self._state.stream_failed = True
                return
            result = await self.adapter.start_stream(
                self.source.chat_id,
                thread_id=self._reply_thread_id,
                task_display_mode="plan",
            )
            if result.ok and result.message_id:
                self._state.stream_ts = result.message_id
            else:
                self._state.stream_failed = True
                logger.warning(
                    "[slack] chat.startStream failed: %s — falling back to "
                    "chat.postMessage / chat.update path. Task cards will "
                    "NOT render (Slack non-streaming pathway doesn't support "
                    "them); the agent's prose response still posts.",
                    result.error,
                )
                return
            # First chunk after startStream: emit the Plan header so
            # the task cards render under a stable group.
            await self._send_plan_chunk_locked()

    async def _send_plan_chunk_locked(self) -> None:
        """Emit one PlanUpdateChunk to title the task-card group.
        Caller holds the lock."""
        if self._state.plan_chunk_sent or self._state.stream_failed:
            return
        if not self._state.stream_ts:
            return
        try:
            from slack_sdk.models.messages.chunk import PlanUpdateChunk
        except ImportError:
            logger.warning("[slack] PlanUpdateChunk not available; skipping plan header")
            self._state.plan_chunk_sent = True
            return
        chunk = PlanUpdateChunk(title=PLAN_TITLE, others={})
        result = await self.adapter.append_stream(
            self.source.chat_id,
            self._state.stream_ts,
            chunks=[chunk],
        )
        self._state.plan_chunk_sent = True
        if not result.ok:
            logger.debug("[slack] plan chunk append failed: %s", result.error)

    # ── text body (markdown_text deltas) ──

    async def _append_text(self, text: str) -> None:
        """Accumulate body text and throttle-flush.

        Two paths:
          · streaming OK → buffer + append_stream(markdown_text=…)
          · streaming failed → buffer + post/update via send/edit.
            The user still gets the prose response; only the
            collapsible task cards are missing.
        """
        async with self._lock:
            self._state.text_buffer += text
            now_ms = time.monotonic() * 1000
            if now_ms - self._state.last_text_flush_ms < self.edit_interval_ms:
                return
            if self._state.stream_failed:
                await self._flush_fallback_locked()
            else:
                await self._flush_text_locked()

    async def _flush_fallback_locked(self) -> None:
        """Send the accumulated text via the legacy post/update path.

        First flush: chat.postMessage and capture the ts.
        Subsequent flushes: chat.update with the cumulative content.
        Caller holds the lock.
        """
        if not self._state.text_buffer:
            return
        self._state.fallback_committed += self._state.text_buffer
        self._state.text_buffer = ""
        content = self._state.fallback_committed
        try:
            if self._state.fallback_message_id is None:
                result = await self.adapter.send(
                    self.source.chat_id,
                    content,
                    thread_id=self._reply_thread_id,
                    raw_hint=self.raw_hint,
                )
                if result.ok and result.message_id:
                    self._state.fallback_message_id = result.message_id
                else:
                    logger.warning(
                        "[slack] fallback send failed: %s", result.error,
                    )
            else:
                result = await self.adapter.edit(
                    self.source.chat_id,
                    self._state.fallback_message_id,
                    content,
                )
                if not result.ok:
                    logger.warning(
                        "[slack] fallback edit failed: %s", result.error,
                    )
        finally:
            self._state.last_text_flush_ms = time.monotonic() * 1000

    async def _flush_text_locked(self) -> None:
        """Send any buffered body text. Caller holds the lock."""
        if self._state.stream_failed or not self._state.stream_ts:
            return
        if not self._state.text_buffer:
            return
        delta = self._state.text_buffer
        self._state.text_buffer = ""
        result = await self.adapter.append_stream(
            self.source.chat_id,
            self._state.stream_ts,
            markdown_text=delta,
        )
        self._state.last_text_flush_ms = time.monotonic() * 1000
        if not result.ok:
            logger.warning("[slack] text appendStream failed: %s", result.error)

    # ── thinking card (TaskUpdateChunk) ──

    async def _append_thinking(self, text: str) -> None:
        """Open or extend the current Thinking task card."""
        async with self._lock:
            if self._state.stream_failed:
                return
            if not self._state.thinking_card_id:
                self._state.thinking_card_id = f"thinking-{uuid.uuid4().hex[:8]}"
                self._state.thinking_buffer = ""
            self._state.thinking_buffer += text
            now_ms = time.monotonic() * 1000
            if now_ms - self._state.last_thinking_flush_ms < DEFAULT_THINKING_FLUSH_INTERVAL_MS:
                return
            await self._flush_thinking_locked(status="in_progress")

    async def _flush_thinking_locked(self, *, status: str) -> None:
        """Push the unsent delta of the thinking buffer into the
        Thinking card. Caller holds the lock.

        Slack's TaskUpdateChunk.output is APPEND-semantics across
        consecutive updates with the same card id — same as how
        markdown_text appends to the streaming body. To avoid
        duplicating the cumulative reasoning text on every flush
        we send only what hasn't been sent yet (delta), tracked
        via ``thinking_sent``. The final ``status="complete"``
        flush still fires even if no new content has arrived since
        the last in_progress flush, so the card resolves visually.
        """
        if self._state.stream_failed or not self._state.stream_ts:
            return
        if not self._state.thinking_card_id:
            return
        try:
            from slack_sdk.models.messages.chunk import TaskUpdateChunk
        except ImportError:
            return
        delta = self._state.thinking_buffer[len(self._state.thinking_sent):]
        # Skip pure status-mutations only when there's no content AND
        # we're still in_progress — they'd be no-op churn. Always
        # send the final "complete" status so the card resolves.
        if not delta and status == "in_progress":
            return
        chunk = TaskUpdateChunk(
            id=self._state.thinking_card_id,
            title="Thinking",
            status=status,
            output=delta or None,
        )
        result = await self.adapter.append_stream(
            self.source.chat_id,
            self._state.stream_ts,
            chunks=[chunk],
        )
        self._state.thinking_sent = self._state.thinking_buffer
        self._state.last_thinking_flush_ms = time.monotonic() * 1000
        if not result.ok:
            logger.debug("[slack] thinking appendStream failed: %s", result.error)

    async def _close_thinking_card(self) -> None:
        """Finalize the open Thinking card (status=complete) so the
        next thinking phase starts a new card."""
        async with self._lock:
            if not self._state.thinking_card_id:
                return
            await self._flush_thinking_locked(status="complete")
            self._state.thinking_card_id = None
            self._state.thinking_buffer = ""
            self._state.thinking_sent = ""

    # ── tool cards ──

    async def _handle_tool_input_end(self, event: dict[str, Any]) -> None:
        """Emit a TaskUpdateChunk(status=in_progress) for the tool whose
        args just landed. The completion chunk fires later on
        tool_result."""
        tool_id = str(event.get("id") or "")
        args = event.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        tool_name = self._state.pending_tool_names.get(tool_id, "?")

        # Heartbeat suppression — pure noise in chat.
        if self._is_heartbeat(tool_name, args):
            # Drop the entry from pending tracking so tool_result
            # doesn't render a completion card for it either.
            self._state.pending_tool_names.pop(tool_id, None)
            return

        # Cache args for the later tool_result chunk.
        self._state.pending_tool_args[tool_id] = args

        await self._ensure_stream_opened()
        await self._close_thinking_card()

        # Same-tool coalescing under "new". The card uses a stable id
        # — when we emit a second TaskUpdateChunk with the same id,
        # Slack updates the existing card rather than adding a new one.
        if self.verbosity == "new" and tool_name == self._state.coalesce_last_tool:
            self._state.coalesce_count += 1
            card_id = f"{tool_name}-coalesce"
            title = f"{tool_name} ×{self._state.coalesce_count}"
            await self._emit_task_chunk(card_id, title, "in_progress")
            return
        if self.verbosity == "new":
            self._state.coalesce_last_tool = tool_name
            self._state.coalesce_count = 1

        # Build the card title with an argument preview.
        title = self._format_tool_title(tool_name, args)
        await self._emit_task_chunk(tool_id, title, "in_progress")

    async def _handle_tool_result(self, event: dict[str, Any]) -> None:
        """Mark the tool's card complete and harvest any inline images
        for inclusion in the final stop_stream(blocks=…) call."""
        tool_id = str(event.get("id") or "")
        if not tool_id:
            return
        tool_name = self._state.pending_tool_names.pop(tool_id, None)
        if tool_name is None:
            # The tool was filtered (heartbeat) — its in_progress card
            # was never emitted, so nothing to complete here. Still
            # process images though (a heartbeat shouldn't generate
            # images, but be defensive).
            self._collect_images_for_finalize(event)
            return
        args = self._state.pending_tool_args.pop(tool_id, {})

        if self.verbosity == "off":
            self._collect_images_for_finalize(event)
            return

        preview = str(event.get("preview") or "")
        is_error = bool(event.get("isError"))
        # Slack's task widget status enum is {pending, in_progress,
        # complete, error}. NOT "completed" — that string is silently
        # ignored and the card never resolves to a finished state.
        status = "error" if is_error else "complete"
        # Tool output displayed inside the expanded card. Trim to a
        # reasonable size — Slack truncates long task outputs anyway.
        output_text = _short(preview, 2000) if preview else None
        title = self._format_tool_title(tool_name, args)

        # Same-tool coalescing under "new" — keep the same coalesced
        # card and update its status to in_progress so it stays a
        # single visual line until the LAST result lands.
        if (
            self.verbosity == "new"
            and tool_name == self._state.coalesce_last_tool
            and self._state.coalesce_count > 1
        ):
            card_id = f"{tool_name}-coalesce"
            await self._emit_task_chunk(
                card_id,
                f"{tool_name} ×{self._state.coalesce_count}",
                "complete" if not is_error else "error",
                output=output_text,
            )
        else:
            await self._emit_task_chunk(tool_id, title, status, output=output_text)

        self._collect_images_for_finalize(event)

    async def _emit_task_chunk(
        self,
        card_id: str,
        title: str,
        status: str,
        *,
        output: str | None = None,
    ) -> None:
        """Append a TaskUpdateChunk to the stream. Status transitions
        on the same card_id mutate the existing card."""
        async with self._lock:
            if self._state.stream_failed or not self._state.stream_ts:
                return
            try:
                from slack_sdk.models.messages.chunk import TaskUpdateChunk
            except ImportError:
                return
            chunk = TaskUpdateChunk(
                id=card_id,
                title=title,
                status=status,
                output=output,
                others={},
            )
            result = await self.adapter.append_stream(
                self.source.chat_id,
                self._state.stream_ts,
                chunks=[chunk],
            )
            if not result.ok:
                logger.debug("[slack] task chunk append failed: %s", result.error)

    def _format_tool_title(self, name: str, args: dict[str, Any]) -> str:
        """Build a Task Card title: ``name`` plus a short arg preview
        when one fits. No emoji — the card has its own status indicator.
        """
        if not args:
            return name
        preview = _summarize_tool_args(name, args)
        if not preview:
            return name
        # The card already shows status (spinner/check), so don't pad
        # with extra glyphs. A simple "name: preview" reads cleanly.
        return f"{name}: {preview}"

    def _is_heartbeat(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name != "tasks" or self.verbosity == "verbose":
            return False
        action = str(args.get("action") or "").lower()
        return action == "heartbeat"

    # ── images → stop_stream(blocks=…) ──

    def _collect_images_for_finalize(self, event: dict[str, Any]) -> None:
        """Stash any image content from a tool_result event so we can
        attach it to the message's final state via stop_stream's
        ``blocks`` param. Slack supports Block Kit only on stopStream
        (per the docs)."""
        # Inline base64 images (generate_image et al).
        for img in event.get("images") or []:
            data_b64 = img.get("dataBase64")
            if not data_b64:
                continue
            try:
                raw = base64.b64decode(data_b64)
            except (binascii.Error, ValueError):
                continue
            mime = str(img.get("mimeType") or "image/png")
            label = str(img.get("label") or "image")
            self._state.pending_image_blocks.append({
                "data": raw,
                "mime": mime,
                "filename": f"{label.replace(' ', '_')}{_mime_to_ext(mime)}",
            })
        # "File saved to `<path>`" references — only if no inline data
        # for this event (avoids double-attach for generate_image).
        if not (event.get("images") or []):
            preview = str(event.get("preview") or "")
            for match in _FILE_SAVED_RE.finditer(preview):
                p = Path(match.group(1))
                try:
                    if not p.exists() or not p.is_file():
                        continue
                except OSError:
                    continue
                if p.suffix.lower() not in _IMAGE_EXTS:
                    continue
                try:
                    raw = p.read_bytes()
                except OSError:
                    continue
                self._state.pending_image_blocks.append({
                    "data": raw,
                    "mime": _ext_to_mime(p.suffix.lower()),
                    "filename": p.name,
                })

    # ── finalize ──

    async def finalize(self) -> None:
        """Flush remaining text, close cards, call stop_stream."""
        async with self._lock:
            if self._state.finalized:
                return
            self._state.finalized = True

        # Flush whatever's still in the text buffer. Don't honour the
        # throttle — this is the last chance. Routes to the streaming
        # or fallback path based on which one we ended up on.
        async with self._lock:
            self._state.last_text_flush_ms = 0.0
            if self._state.stream_failed:
                await self._flush_fallback_locked()
            else:
                await self._flush_text_locked()

        # Close any open thinking card.
        await self._close_thinking_card()

        # Upload any pending images via files_upload (separate from
        # stopStream blocks). Slack's Block Kit image block only
        # accepts public URLs, but our images live in-process as
        # base64 — uploading via files_upload_v2 with thread_ts
        # places them in the same thread as the streaming response,
        # which is the closest equivalent to "inline at the end".
        await self._upload_pending_images()

        # Stop the stream. No more content; this finalizes the
        # message visually (Slack removes the streaming indicator).
        # Skipped on the fallback path — there's no stream to stop.
        if self._state.stream_ts and not self._state.stream_failed:
            stop_result = await self.adapter.stop_stream(
                self.source.chat_id,
                self._state.stream_ts,
            )
            if not stop_result.ok:
                logger.warning("[slack] chat.stopStream failed: %s", stop_result.error)

        # Stop the typing indicator.
        try:
            await self.adapter.stop_typing(
                self.source.chat_id,
                thread_id=self._reply_thread_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("stop_typing failed (non-fatal)", exc_info=True)

        # Notify the gateway that this turn is done.
        if self.on_complete:
            try:
                result = self.on_complete()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("on_complete callback raised")

    async def _upload_pending_images(self) -> None:
        """Ship any images collected during the turn to the same thread
        the streaming response lives in. Uses upload_files for batch +
        one initial_comment.

        We deliberately upload AFTER stopStream rather than via the
        blocks param because Block Kit image blocks require a publicly
        accessible URL; our generated images are in-memory bytes.
        files_upload_v2 with thread_ts puts them directly under the
        streaming message — close enough to inline."""
        items = [
            UploadItem(
                data=img["data"],
                filename=img["filename"],
                mime_type=img["mime"],
            )
            for img in self._state.pending_image_blocks
            if img.get("data")
        ]
        if not items:
            return
        try:
            result = await self.adapter.upload_files(
                self.source.chat_id,
                items,
                thread_id=self._reply_thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("upload_files failed: %s", exc)
            return
        if not result.ok:
            logger.warning("[slack] upload_files failed: %s", result.error)


# ─── module-level helpers (preserved from prior implementation) ────────


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    """Render a chat-friendly preview of tool arguments.

    Returns a string suitable as a Task Card title suffix. Per-tool
    special cases pull the most useful fields and label them; generic
    fallback returns the first scalar arg. Output is kept short since
    Task Card titles render in a single line.
    """
    if not isinstance(args, dict) or not args:
        return ""
    if name in {"web_search", "web_research"}:
        return _short(str(args.get("query") or ""), 120)
    if name == "web_fetch":
        url = str(args.get("url") or "")
        obj = str(args.get("objective") or "")
        if obj:
            return f"{_short(url, 80)} — {_short(obj, 100)}"
        return _short(url, 140)
    if name in {"read_file", "write_file", "edit_file"}:
        return _short(str(args.get("path") or ""), 120)
    if name in {"bash", "shell"}:
        cmd = str(args.get("command") or args.get("cmd") or "")
        summary = str(args.get("summary") or "")
        if summary:
            return f"{_short(summary, 80)} — `{_short(cmd, 120)}`"
        return _short(cmd, 180)
    if name == "glob":
        return _short(str(args.get("pattern") or ""), 120)
    if name == "grep":
        pattern = str(args.get("pattern") or args.get("query") or "")
        path = str(args.get("path") or "")
        if path:
            return f"`{_short(pattern, 80)}` in `{_short(path, 60)}`"
        return _short(pattern, 160)
    if name == "sub_agent":
        agent_type = str(args.get("agent_type") or args.get("type") or "")
        label = str(args.get("label") or args.get("name") or "")
        task = str(args.get("task") or args.get("prompt") or "")
        parts: list[str] = []
        if agent_type:
            parts.append(f"[{agent_type}]")
        if label:
            parts.append(label)
        if task:
            parts.append(_short(task, 160))
        return " — ".join(parts) if parts else "(no task)"
    if name == "tasks":
        action = str(args.get("action") or "")
        title = str(args.get("title") or "")
        bits: list[str] = []
        if action:
            bits.append(f"action={action}")
        if title:
            bits.append(_short(title, 100))
        return " · ".join(bits)
    if name == "kanban":
        action = str(args.get("action") or "")
        title = str(args.get("title") or args.get("card_title") or "")
        bits: list[str] = []
        if action:
            bits.append(f"action={action}")
        if title:
            bits.append(_short(title, 120))
        return " · ".join(bits)
    if name == "generate_image":
        return _short(str(args.get("prompt") or ""), 180)
    if name == "memory_update":
        return _short(
            str(args.get("text") or args.get("name") or args.get("title") or ""),
            140,
        )
    if name == "send_attachment":
        paths = args.get("paths") or []
        caption = str(args.get("caption") or "")
        if isinstance(paths, list) and paths:
            files = ", ".join(Path(str(p)).name for p in paths[:4])
            more = "" if len(paths) <= 4 else f" (+{len(paths) - 4})"
            return f"{files}{more}" + (f" — {_short(caption, 120)}" if caption else "")
        return _short(caption, 160)
    # Generic fallback — show the FIRST 2 scalar args.
    pairs: list[str] = []
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)) and (v or v == 0):
            pairs.append(f"{k}={_short(str(v), 80)}")
            if len(pairs) >= 2:
                break
    return " · ".join(pairs)


def _short(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _mime_to_ext(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime.lower(), ".png")


def _ext_to_mime(ext: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext.lower(), "image/png")


def _first_meaningful_line(text: str, max_chars: int = 180) -> str:
    """Return the first non-empty line of ``text``, capped at
    ``max_chars``. Preserved from the prior implementation for any
    caller still using it."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[Image:") or line.startswith("[Image URL:"):
            continue
        if len(line) > max_chars:
            line = line[: max_chars - 1] + "…"
        return line
    return ""
