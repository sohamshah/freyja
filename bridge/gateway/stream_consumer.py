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

Additionally — borrowed from Hermes:

  · **Tool-progress bubbles.** On ``tool_use_start`` / ``tool_input_end``
    we maintain a separate Slack message anchor that grows as tools
    run. When the agent starts emitting user-facing text, the progress
    bubble is "closed" (finalized in place) and any further tool
    progress opens a fresh bubble BELOW the text, preserving chronology.
  · **Image uploads.** On ``tool_result`` events that carry inline
    image data (e.g. from ``generate_image``) or that mention a
    ``File saved to`` path, we ship the image(s) back to Slack via
    ``upload_files`` — batched up to 10 per call.
  · **"Is thinking…" status.** On turn start we set the Slack
    Assistant Threads status; on turn complete we clear it.

Slack doesn't have a generic typing indicator for bots, so the
streaming-edit-in-place pattern + the Assistant status combine to
give live feedback while reasoning is in flight.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.gateway.platforms.base import (
    MessageSource,
    PlatformAdapter,
    UploadItem,
    truncate_for_platform,
)

logger = logging.getLogger(__name__)


# Slack hard limit per message is 40k chars; leave some headroom for
# our own framing (a finalize marker, etc.).
DEFAULT_MAX_CHARS = 39_000

# Throttle for tool-progress bubble edits — Hermes uses 1.5s. Tools
# can fire in tight loops; throttling protects Slack's chat.update
# rate limit (≈1 update/sec/thread).
DEFAULT_PROGRESS_INTERVAL_MS = 1500

# Cap progress lines per bubble so we don't blow past the per-message
# char limit. A reasonable run = 10-20 tool calls; older lines fold
# into a "+ N more" summary.
PROGRESS_LINE_CAP = 12

# Path-extraction regex for tools that report "File saved to `/path`"
# in their text preview (generate_image being the canonical example).
_FILE_SAVED_RE = re.compile(r"File saved to `([^`]+)`")

# Image file extensions we consider worth sending back to Slack.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass
class _Anchor:
    """A single Slack message we're either growing in place or have
    finalized. A long response becomes a chain of anchors."""

    message_id: str | None = None
    content: str = ""


@dataclass
class _ToolProgress:
    """The current open tool-progress bubble — one Slack message that
    grows as tools fire. Closed (and reset to None) the moment the
    agent emits user-facing text, so subsequent tool calls open a
    fresh bubble BELOW the text."""

    message_id: str | None = None
    lines: list[str] = field(default_factory=list)
    pending_tool_names: dict[str, str] = field(default_factory=dict)
    last_edit_monotonic: float = 0.0


@dataclass
class _State:
    """Mutable consumer state. Held inside an asyncio.Lock for safe
    concurrent flush/append."""

    buffer: str = ""
    anchors: list[_Anchor] = field(default_factory=list)
    last_emit_monotonic: float = 0.0
    finalized: bool = False
    # Tool-progress bubble lifecycle.
    progress: _ToolProgress = field(default_factory=_ToolProgress)
    # Whether we've issued send_typing yet on this turn (sent on
    # first event, cleared at turn_complete).
    typing_set: bool = False
    # When a progress bubble closes (text resumes after tools), all
    # anchors authored BEFORE the progress are visually above it in
    # Slack. New text must land BELOW the progress message — which
    # means it has to go into a NEW anchor, not extend the last one.
    # Track how many anchors are frozen (no longer editable); the
    # flush path operates only on anchors past this index. Initialized
    # to 0 (no anchors frozen at turn start); advances whenever
    # _reset_progress_bubble finalizes a real bubble.
    anchors_frozen_at: int = 0


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

        # Resolve where the bot's outbound messages should be threaded.
        # Computed once at consumer creation (per turn), since the
        # source's thread state doesn't change mid-turn.
        #
        # · If the inbound message is already a thread reply
        #   (source.thread_id is set), reply in the SAME thread —
        #   matches user expectation: "I asked in this thread, the
        #   answer should be here too."
        # · Else, for DMs, if the platform config says
        #   ``reply_in_thread: true`` (default), reply in a NEW
        #   thread anchored to the user's top-level message
        #   (source.message_id). Without this, every top-level DM
        #   spawns a separate top-level bot reply, producing an
        #   ugly flat history of disconnected interactions instead
        #   of cleanly threaded turns.
        # · Else (channels, non-DM with thread_in_reply off), no
        #   thread — reply top-level.
        self._reply_thread_id: str | None = self._compute_reply_thread_id()

    def _compute_reply_thread_id(self) -> str | None:
        src = self.source
        if src.thread_id:
            return src.thread_id
        if src.chat_type == "dm":
            # Read slack_config off the adapter if present (slack
            # adapter exposes it). Other adapters may not — default
            # to True so the behavior matches the gateway.yaml
            # default of reply_in_thread: true.
            cfg = getattr(self.adapter, "slack_config", None)
            reply_in_thread = getattr(cfg, "reply_in_thread", True)
            if reply_in_thread and src.message_id:
                return src.message_id
        return None

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
        # Event types we care about. text_delta drives the chat
        # surface; tool_use_start + tool_input_end + tool_result drive
        # the progress bubble + image upload; turn_start/turn_complete
        # gate the typing indicator + finalize lifecycle.
        if etype not in {
            "text_delta",
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
        # Set Slack's "is thinking..." status on the FIRST event of
        # the turn (whichever event type happens to arrive first —
        # usually tool_use_start or turn_start). Best-effort; if it
        # fails (e.g. not in an Assistant thread) we silently move on.
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
            if text:
                # First user-facing text means: close the in-flight
                # tool-progress bubble (if any) so the next tool call
                # opens a fresh bubble BELOW the text. Preserves
                # chronological reading order in the chat.
                await self._reset_progress_bubble()
                await self._append(text)
        elif etype == "tool_use_start":
            tool_id = str(event.get("id") or "")
            tool_name = str(event.get("name") or "?")
            if tool_id:
                # Stash the name keyed by id so tool_input_end can
                # render the full "name(args)" preview when args land.
                self._state.progress.pending_tool_names[tool_id] = tool_name
            # Emit a provisional "running" line; refined by
            # tool_input_end once args arrive.
            await self._append_progress(f"⚙️ {tool_name}…")
        elif etype == "tool_input_end":
            tool_id = str(event.get("id") or "")
            args = event.get("arguments") or {}
            tool_name = self._state.progress.pending_tool_names.pop(
                tool_id,
                "?",
            )
            preview = _summarize_tool_args(tool_name, args)
            line = f"⚙️ `{tool_name}`"
            if preview:
                line += f": {preview}"
            await self._replace_last_progress(line)
        elif etype == "tool_result":
            await self._handle_tool_result(event)
        elif etype == "turn_complete":
            await self.finalize()

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

        # Chunk only the UNFROZEN content (anchors past
        # anchors_frozen_at + the live buffer). Frozen anchors are
        # already-finalized text that sits above a closed progress
        # bubble — editing them with new text would re-author content
        # ABOVE the progress chip, breaking chronology. New text
        # always lands in fresh anchors past the frozen mark.
        base = self._state.anchors_frozen_at
        full = self._combined_content()
        chunks = truncate_for_platform(full, self.max_chars)

        # Map chunks → anchors AT OR AFTER base. Existing unfrozen
        # anchors get edited; new chunks get sent as fresh messages.
        for idx, chunk in enumerate(chunks):
            anchor_idx = base + idx
            if anchor_idx < len(self._state.anchors):
                anchor = self._state.anchors[anchor_idx]
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
                            "replacing with a fresh message",
                            anchor.message_id,
                            result.error,
                        )
                        # Replace the broken anchor in-place rather
                        # than appending — appending would leave the
                        # broken anchor in the list and each
                        # subsequent flush would re-target it (still
                        # broken) and append yet another. List grows
                        # unbounded. Replace = bounded.
                        new_result = await self.adapter.send(
                            self.source.chat_id,
                            chunk,
                            thread_id=self._reply_thread_id,
                            raw_hint=self.raw_hint,
                        )
                        if new_result.ok:
                            self._state.anchors[anchor_idx] = _Anchor(
                                message_id=new_result.message_id,
                                content=chunk,
                            )
                        continue
                anchor.content = chunk
            else:
                # New chunk — send as a new Slack message and append.
                result = await self.adapter.send(
                    self.source.chat_id,
                    chunk,
                    thread_id=self._reply_thread_id,
                    raw_hint=self.raw_hint if anchor_idx == 0 else None,
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
        # Only the UNFROZEN suffix of anchors + the live buffer
        # participates in flushes. Frozen anchors (text written
        # before the most recent progress bubble closed) are
        # excluded so their content can't be re-edited and the
        # chunk-boundary recompute can't shuffle their text into
        # the new anchor below the progress bubble.
        active_anchors = self._state.anchors[self._state.anchors_frozen_at:]
        parts = [a.content for a in active_anchors] + [self._state.buffer]
        return "".join(parts)

    # ── tool-progress bubble ──

    async def _append_progress(self, line: str) -> None:
        """Add a new line to the open progress bubble (creating it
        if needed). Throttled to DEFAULT_PROGRESS_INTERVAL_MS."""
        async with self._lock:
            if self._state.finalized:
                return
            self._state.progress.lines.append(line)
            await self._flush_progress_locked()

    async def _replace_last_progress(self, line: str) -> None:
        """Replace the last progress line (used when tool_input_end
        arrives with full args, refining a provisional 'running' line)."""
        async with self._lock:
            if self._state.finalized:
                return
            if self._state.progress.lines:
                self._state.progress.lines[-1] = line
            else:
                self._state.progress.lines.append(line)
            await self._flush_progress_locked()

    async def _flush_progress_locked(self) -> None:
        """Send / edit the progress bubble. Caller holds the lock."""
        now_ms = time.monotonic() * 1000
        last = self._state.progress.last_edit_monotonic
        if last and (now_ms - last) < DEFAULT_PROGRESS_INTERVAL_MS:
            # Within throttle window — skip; the next event or the
            # finalize sweep will flush.
            return
        lines = self._state.progress.lines
        if not lines:
            return
        # Cap visible lines so a runaway tool loop doesn't blow past
        # the chat.update char limit. Older lines fold into a summary.
        if len(lines) > PROGRESS_LINE_CAP:
            hidden = len(lines) - PROGRESS_LINE_CAP
            visible = ["_…+ {n} earlier tool call(s)_".format(n=hidden)]
            visible.extend(lines[-PROGRESS_LINE_CAP:])
        else:
            visible = list(lines)
        body = "\n".join(visible)
        if self._state.progress.message_id:
            try:
                await self.adapter.edit(
                    self.source.chat_id,
                    self._state.progress.message_id,
                    body,
                )
            except Exception:  # noqa: BLE001
                logger.debug("progress edit failed", exc_info=True)
        else:
            try:
                res = await self.adapter.send(
                    self.source.chat_id,
                    body,
                    thread_id=self._reply_thread_id,
                    raw_hint=self.raw_hint,
                )
                if res.ok and res.message_id:
                    self._state.progress.message_id = res.message_id
            except Exception:  # noqa: BLE001
                logger.debug("progress send failed", exc_info=True)
        self._state.progress.last_edit_monotonic = now_ms

    async def _reset_progress_bubble(self) -> None:
        """Close the open progress bubble (if any) and pin the cursor
        so subsequent text starts a NEW anchor BELOW it.

        Called when the agent starts emitting user-facing text after
        a series of tool calls. Two correctness moves before we
        return:

        1. Force-flush any pre-tool text still sitting in
           ``self._state.buffer`` so it lands in the anchor ABOVE
           the progress message (its rightful chronological home),
           not in the new anchor we're about to start BELOW.
        2. Advance ``anchors_frozen_at`` to the current anchor
           count. ``_combined_content`` / ``_flush_locked`` ignore
           frozen anchors, so the next text_delta opens a brand-new
           Slack message past the progress chip.

        Without #2, the new text would be appended to the last
        existing anchor (which sits ABOVE the progress bubble),
        producing the visual "tool calls shown after the final
        answer that got edited last" bug.
        """
        async with self._lock:
            if not self._state.progress.message_id and not self._state.progress.lines:
                return
            # Pre-tool text waiting in the buffer belongs in the OLD
            # anchor — flush it before we close the progress, bypass
            # the throttle so it actually lands.
            if self._state.buffer.strip():
                self._state.last_emit_monotonic = 0
                await self._flush_locked()
            # Final progress flush.
            now_ms = time.monotonic() * 1000
            self._state.progress.last_edit_monotonic = 0
            await self._flush_progress_locked()
            self._state.progress = _ToolProgress()
            self._state.progress.last_edit_monotonic = now_ms
            # Freeze the cursor. Next text_delta lands in a new
            # anchor authored AFTER the closed progress bubble.
            self._state.anchors_frozen_at = len(self._state.anchors)

    # ── tool_result handling: image uploads ──

    async def _handle_tool_result(self, event: dict[str, Any]) -> None:
        """If the tool result includes image data (inline base64 or
        a saved file path), upload it back to Slack so the operator
        actually sees what the agent produced.

        Inline + path are TWO surfaces for the same data — ``generate_image``
        returns both an ``image`` content block AND a "File saved to
        \\`...\\`" line in its text preview. We prefer inline (base64) since
        it works even when the on-disk file isn't readable by the daemon,
        and only fall back to path extraction when no inline images were
        emitted (e.g. tools that wrote a file but didn't return an image
        block, like a hypothetical ``screenshot`` adapter).
        """
        items: list[UploadItem] = []
        # Inline image blocks (e.g. anthropic ``image`` content blocks).
        # Freyja's bridge extracted these into ``event["images"]`` with
        # base64 data + mime type via _tool_content_preview_and_images.
        inline_images = event.get("images") or []
        for img in inline_images:
            data_b64 = img.get("dataBase64")
            if not data_b64:
                continue
            try:
                raw = base64.b64decode(data_b64)
            except (binascii.Error, ValueError):
                continue
            mime = str(img.get("mimeType") or "image/png")
            ext = _mime_to_ext(mime)
            label = str(img.get("label") or "image")
            items.append(UploadItem(
                data=raw,
                filename=f"{label.replace(' ', '_')}{ext}",
                mime_type=mime,
            ))
        # Path references in the preview text — ONLY if no inline images.
        # Otherwise we'd double-upload (same image as inline + as path).
        preview = str(event.get("preview") or "")
        if not items:
            for match in _FILE_SAVED_RE.finditer(preview):
                p = Path(match.group(1))
                if not p.exists() or not p.is_file():
                    continue
                if p.suffix.lower() not in _IMAGE_EXTS:
                    continue
                items.append(UploadItem(path=str(p), filename=p.name))
        if not items:
            return
        # Caption: use the preview's first non-empty line as a context
        # blurb on the upload so the image isn't a bare attachment.
        # Falls back to a generic line if the preview is empty.
        caption = _first_meaningful_line(preview) or "Generated image"
        try:
            result = await self.adapter.upload_files(
                self.source.chat_id,
                items,
                thread_id=self._reply_thread_id,
                initial_comment=caption,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("upload_files failed")
            await self._notify_upload_failure(str(exc))
            return
        if not result.ok:
            logger.warning("upload_files returned error: %s", result.error)
            await self._notify_upload_failure(result.error or "unknown error")

    async def _notify_upload_failure(self, reason: str) -> None:
        """Send a fallback text message when an image upload fails so
        the user knows the agent's output didn't actually land. Slack
        rejects oversize files, unauthorized scopes, and rate-limited
        uploads — without this the agent appears to have responded but
        the user sees nothing."""
        # Trim the reason to keep the chat clean — Slack errors can be
        # multi-line API dumps. Keep just the first line, capped.
        short = reason.split("\n", 1)[0].strip()
        if len(short) > 200:
            short = short[:197] + "…"
        try:
            await self.adapter.send(
                self.source.chat_id,
                f"_couldn't upload the image: {short}_",
                thread_id=self._reply_thread_id,
                raw_hint=self.raw_hint,
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to send upload-failure notice", exc_info=True)

    # ── finalize ──

    async def finalize(self) -> None:
        """Final flush + listener unregister. Idempotent."""
        async with self._lock:
            if self._state.finalized:
                return
            await self._flush_locked()
            # Force a final unthrottled flush of progress so the
            # closed bubble matches reality.
            self._state.progress.last_edit_monotonic = 0
            await self._flush_progress_locked()
            self._state.finalized = True
        # Clear the typing indicator (best-effort).
        try:
            await self.adapter.stop_typing(
                self.source.chat_id,
                thread_id=self._reply_thread_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("stop_typing failed (non-fatal)", exc_info=True)
        # Notify the gateway that this turn is done (so the per-turn
        # listener registration can be cleared).
        if self.on_complete:
            try:
                result = self.on_complete()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("on_complete callback raised")


# ─── module-level helpers ────────────────────────────────────────────


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    """Render a short, chat-friendly preview of tool arguments.

    Per-tool special cases for the noisy / verbose tools; generic
    repr for the rest. Kept terse — the progress bubble lives next
    to the agent's text, not in a debugger.
    """
    if not isinstance(args, dict) or not args:
        return ""
    if name in {"web_search", "web_fetch", "web_research"}:
        q = str(args.get("query") or args.get("url") or "")
        return _short(q, 80)
    if name in {"read_file", "write_file", "edit_file"}:
        return _short(str(args.get("path") or ""), 80)
    if name in {"bash", "shell"}:
        return _short(str(args.get("command") or args.get("cmd") or ""), 80)
    if name == "glob":
        return _short(str(args.get("pattern") or ""), 80)
    if name == "grep":
        return _short(str(args.get("pattern") or args.get("query") or ""), 80)
    if name == "sub_agent":
        return _short(str(args.get("task") or args.get("prompt") or ""), 80)
    if name == "generate_image":
        return _short(str(args.get("prompt") or ""), 80)
    # Generic fallback — first scalar arg, truncated.
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)) and v:
            return _short(f"{k}={v}", 80)
    return ""


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


def _first_meaningful_line(text: str, max_chars: int = 180) -> str:
    """Return the first non-empty line of ``text``, capped at
    ``max_chars``. Used to derive an image-upload caption from a tool's
    preview string. Skips lines that are pure metadata bracket markers
    like ``[Image: image/png, 1024x1024]`` since those duplicate the
    visible attachment thumbnail."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Skip the bridge's synthetic image-placeholder lines.
        if line.startswith("[Image:") or line.startswith("[Image URL:"):
            continue
        if len(line) > max_chars:
            line = line[: max_chars - 1] + "…"
        return line
    return ""
