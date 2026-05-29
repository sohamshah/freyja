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
    finalized. A long response becomes a chain of anchors.

    ``last_formatted`` is the FORMATTED string (post ``format_content``)
    that Slack currently displays for this anchor. We compare against
    it on the next flush to skip no-op edits, and we send it verbatim
    to ``adapter.edit``/``adapter.send`` — the adapter does not
    re-chunk, so what we record here is exactly what Slack shows.
    """

    message_id: str | None = None
    last_formatted: str = ""


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
    concurrent flush/append.

    ``buffer`` holds ALL raw (pre-format) text emitted since the last
    progress-bubble reset. It is **not** cleared on flush — every
    flush re-formats the entire buffer and re-chunks it, comparing
    each formatted chunk to ``anchors[i].last_formatted`` to decide
    edit vs. skip. This is the source-of-truth representation of
    unfrozen content; anchors store only what Slack currently shows,
    not what they "should" contain.

    Why not clear buffer per flush? Anchors store the FORMATTED chunk
    (because that's what we compare against to dedupe edits), and
    ``format_content`` is lossy (``[t](u)`` → ``<u|t>`` can't be
    inverted). Keeping the raw stream in ``buffer`` lets us re-format
    + re-chunk deterministically each flush.

    The buffer is cleared only when ``_reset_progress_bubble`` freezes
    the current anchors — at that point everything in the buffer has
    been committed to anchors above the progress bubble, and any new
    text will start a fresh anchor below.
    """

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
        max_chars: int | None = None,
        raw_hint: dict[str, Any] | None = None,
        on_complete: Any = None,
        permission_resolver: Any = None,
    ) -> None:
        self.adapter = adapter
        self.source = source
        self.session_key = session_key
        self.edit_interval_ms = edit_interval_ms
        # Default to the adapter's hard per-message cap. Allowing an
        # override is useful for tests; production always uses what the
        # adapter says is safe, since the adapter's send/edit will
        # reject anything larger.
        adapter_cap = int(getattr(adapter, "max_message_chars", DEFAULT_MAX_CHARS))
        self.max_chars = max_chars if max_chars is not None else adapter_cap
        self.raw_hint = raw_hint
        self.on_complete = on_complete  # called when finalize() runs
        # Callback ``(request_id, approved) -> bool`` used by the Slack
        # Block Kit approval click handler to resolve the daemon's
        # DesktopPermissionHandler future. Wired through ``run.py`` from
        # the session's permission_handler. None when the session has no
        # in-process handler (shouldn't happen for live Slack sessions
        # but we fail open rather than hang).
        self.permission_resolver = permission_resolver
        # Pending in-flight permission ids minted in this consumer's
        # lifetime — used to clean up external resolvers on finalize()
        # so we don't leak resolver entries across turns.
        self._pending_permission_ids: set[str] = set()

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
            "permission_request",
            "permission_resolved",
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
        elif etype == "permission_request":
            await self._handle_permission_request(event)
        elif etype == "permission_resolved":
            await self._handle_permission_resolved(event)
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
        lock held.

        Pipeline (in order):

          1. Format the entire unfrozen raw buffer via
             ``adapter.format_content`` once. Lossy transforms (markdown
             link → ``<u|t>``) happen here, not inside ``send`` / ``edit``.
          2. Chunk the FORMATTED string with ``truncate_for_platform``
             using the adapter's strict ``max_message_chars`` cap. Each
             chunk is by construction within Slack's per-message limit,
             so the adapter accepts it without splitting.
          3. Map chunks to anchors at or after ``anchors_frozen_at``.
             If ``anchor.last_formatted == chunk``, skip — no edit
             needed. Otherwise edit (existing anchor) or send (new
             anchor, append).

        The contract change vs. the old code: anchors store the
        FORMATTED text that Slack currently shows, not the raw
        markdown. The chunk boundary the consumer picks is the chunk
        boundary Slack actually receives — eliminating the orphan
        path where ``send`` used to internally split and only return
        the last ts (leaving leading messages untracked).
        """
        if not self._state.buffer.strip():
            return

        # Format → chunk → diff against currently-deployed formatted
        # anchors. Frozen anchors are NOT touched: they sit above a
        # closed progress bubble and editing them would shuffle text
        # above the chip, breaking chronology.
        formatted = self.adapter.format_content(self._state.buffer)
        chunks = truncate_for_platform(formatted, self.max_chars)
        base = self._state.anchors_frozen_at

        for idx, chunk in enumerate(chunks):
            anchor_idx = base + idx
            if anchor_idx < len(self._state.anchors):
                anchor = self._state.anchors[anchor_idx]
                if anchor.last_formatted == chunk:
                    continue  # no-op edit — Slack already shows this
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
                        # broken) and append yet another. Bounded by
                        # in-place replace.
                        new_result = await self.adapter.send(
                            self.source.chat_id,
                            chunk,
                            thread_id=self._reply_thread_id,
                            raw_hint=self.raw_hint,
                        )
                        if new_result.ok:
                            self._state.anchors[anchor_idx] = _Anchor(
                                message_id=new_result.message_id,
                                last_formatted=chunk,
                            )
                        else:
                            logger.warning(
                                "slack replace-after-edit-failure also failed: %s",
                                new_result.error,
                            )
                        continue
                    anchor.last_formatted = chunk
                else:
                    # Anchor exists but lost its message_id (post failure
                    # earlier in the turn). Try to send it now so
                    # subsequent flushes can edit it.
                    new_result = await self.adapter.send(
                        self.source.chat_id,
                        chunk,
                        thread_id=self._reply_thread_id,
                        raw_hint=self.raw_hint,
                    )
                    if new_result.ok:
                        anchor.message_id = new_result.message_id
                        anchor.last_formatted = chunk
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
                        _Anchor(message_id=result.message_id, last_formatted=chunk)
                    )
                else:
                    # Record the anchor anyway with no message_id so the
                    # next flush retries the send rather than perma-
                    # losing the chunk. Failure mode otherwise: dropped
                    # send leaves a hole in the visible response.
                    logger.warning("slack send failed: %s", result.error)
                    self._state.anchors.append(
                        _Anchor(message_id=None, last_formatted="")
                    )

        # NOTE: buffer is NOT cleared here. Anchors track formatted text;
        # the raw stream stays in buffer so next flush re-formats from
        # the canonical source. Cleared only on _reset_progress_bubble
        # (when frozen anchors above the new progress chip take over).
        self._state.last_emit_monotonic = time.monotonic() * 1000

    def _combined_content(self) -> str:
        """Raw text awaiting flush.

        Just the buffer — unfrozen anchors no longer carry raw
        content (they carry the FORMATTED chunk that Slack
        currently shows). The raw stream is the single source of
        truth for "what should be visible past the frozen
        frontier"; formatting + chunking happens fresh each flush.
        """
        return self._state.buffer

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
            # Clear the raw buffer — every character it held has been
            # absorbed into one of the now-frozen anchors. Without this
            # the next flush would re-format the same content and try
            # to land it past the freeze, duplicating it below the
            # progress chip.
            self._state.buffer = ""

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

    # ── permission flow ──

    async def _handle_permission_request(self, event: dict[str, Any]) -> None:
        """Forward an in-process ``permission_request`` to Slack.

        We bridge the daemon's per-session ``DesktopPermissionHandler``
        prompt (which would otherwise emit an event nobody listens to
        on this side of the process boundary) to a Block Kit message in
        the originating Slack thread. The button click then resolves
        the daemon's pending Future via ``permission_resolver``.

        Doing this here means the same code path serves *every* permission
        prompt the engine emits — bash network egress, write-into-protected-
        path, image-write, etc. — not just the ``approval.py`` destructive-
        command list. If the consumer can't post (no resolver wired, no
        Slack chat id), we log and let the awaiter's hard timeout settle
        the request — failing closed rather than hanging.
        """
        request_id = str(event.get("requestId") or "")
        if not request_id:
            return
        prompt = str(event.get("prompt") or "permission required")
        reason = str(event.get("reason") or "") or str(event.get("details") or "")
        level = str(event.get("level") or "medium")
        if self.permission_resolver is None:
            logger.warning(
                "permission_request %s arrived without a resolver — "
                "will hard-timeout in the daemon (session=%s)",
                request_id, self.session_key,
            )
            return
        from bridge.gateway.approval import register_external_resolver
        # Tie the Slack button -> daemon future glue together.
        register_external_resolver(
            request_id,
            lambda approved: bool(self.permission_resolver(request_id, approved)),
        )
        self._pending_permission_ids.add(request_id)
        # Post the Block Kit message — reuses the exact same adapter
        # affordance used by destructive-tool approvals, so the operator
        # UI is identical.
        try:
            result = await self.adapter.send_approval_request(
                chat_id=self.source.chat_id,
                request_id=request_id,
                tool_name=f"permission · {level}",
                command_preview=prompt,
                reason=reason,
                thread_id=self._reply_thread_id,
            )
            if not getattr(result, "ok", False):
                logger.warning(
                    "Block Kit approval post failed for %s: %s",
                    request_id, getattr(result, "error", "?"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("send_approval_request raised for %s", request_id)

    async def _handle_permission_resolved(self, event: dict[str, Any]) -> None:
        """Drop the external resolver entry once the prompt is settled.

        The button-click path already replaces the message Block Kit with
        a "✅ approved by @X" / "❌ denied by @X" footer (see
        ``slack.py:_handle_approval_click``). We only have to free the
        process-wide resolver table entry so it doesn't leak.
        """
        request_id = str(event.get("requestId") or "")
        if not request_id:
            return
        from bridge.gateway.approval import unregister_external_resolver
        unregister_external_resolver(request_id)
        self._pending_permission_ids.discard(request_id)

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
        # Drop any external approval resolvers minted during the turn
        # that weren't already cleaned up by ``permission_resolved`` —
        # protects against a turn that completes while a prompt is still
        # in flight (e.g. operator never clicked, daemon timed out).
        if self._pending_permission_ids:
            from bridge.gateway.approval import unregister_external_resolver
            for rid in list(self._pending_permission_ids):
                unregister_external_resolver(rid)
            self._pending_permission_ids.clear()
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
    """Render a chat-friendly preview of tool arguments.

    Returns a string that may span multiple lines for richer tools (the
    progress bubble joins entries with ``\n``, so embedded newlines in
    a single entry just render as a multi-line block for that tool
    call). Per-tool special cases pull the most useful fields and label
    them; generic fallback returns the first scalar arg.

    Wider previews for tools where the structure matters (``sub_agent``,
    ``tasks``) — the operator wants to see ``what`` the agent is doing,
    not just ``which tool``. Headline + indented detail on a second
    line keeps it scannable.
    """
    if not isinstance(args, dict) or not args:
        return ""
    if name in {"web_search", "web_research"}:
        q = str(args.get("query") or "")
        return _short(q, 160)
    if name == "web_fetch":
        url = str(args.get("url") or "")
        obj = str(args.get("objective") or "")
        if obj:
            return f"{_short(url, 100)}\n     ↳ _{_short(obj, 160)}_"
        return _short(url, 160)
    if name in {"read_file", "write_file", "edit_file"}:
        return _short(str(args.get("path") or ""), 140)
    if name in {"bash", "shell"}:
        cmd = str(args.get("command") or args.get("cmd") or "")
        summary = str(args.get("summary") or "")
        if summary:
            return f"{summary}\n     ↳ `{_short(cmd, 160)}`"
        return _short(cmd, 200)
    if name == "glob":
        return _short(str(args.get("pattern") or ""), 140)
    if name == "grep":
        pattern = str(args.get("pattern") or args.get("query") or "")
        path = str(args.get("path") or "")
        if path:
            return f"`{_short(pattern, 100)}`  in  `{_short(path, 80)}`"
        return _short(pattern, 180)
    if name == "sub_agent":
        agent_type = str(args.get("agent_type") or args.get("type") or "")
        mode = str(args.get("mode") or "")
        label = str(args.get("label") or args.get("name") or "")
        model = str(args.get("model") or "")
        task = str(args.get("task") or args.get("prompt") or "")
        # Build a structured 2-line preview: headline (config) + task quote
        config_bits: list[str] = []
        if agent_type:
            mode_suffix = f" · {mode}" if mode else ""
            config_bits.append(f"[{agent_type}{mode_suffix}]")
        if model:
            config_bits.append(f"({model})")
        if label:
            config_bits.append(label)
        header = " ".join(config_bits)
        if task:
            task_line = _short(task.replace("\n", " "), 240)
            if header:
                return f"{header}\n     ↳ _{task_line}_"
            return f"_{task_line}_"
        return header or "(no task)"
    if name == "tasks":
        action = str(args.get("action") or "")
        title = str(args.get("title") or "")
        task_id = str(args.get("task_id") or args.get("id") or "")
        body = str(args.get("body") or args.get("description") or "")
        bits: list[str] = []
        if action:
            bits.append(f"action=`{action}`")
        if title:
            bits.append(_short(title, 140))
        elif task_id:
            bits.append(f"id=`{task_id}`")
        head = " · ".join(bits) if bits else ""
        if body:
            return f"{head}\n     ↳ _{_short(body, 200)}_" if head else _short(body, 240)
        return head
    if name == "kanban":
        action = str(args.get("action") or "")
        title = str(args.get("title") or args.get("card_title") or "")
        card_id = str(args.get("card_id") or args.get("id") or "")
        head = f"action=`{action}`" if action else ""
        if title:
            head = f"{head} · {_short(title, 140)}" if head else _short(title, 180)
        elif card_id:
            head = f"{head} · id=`{card_id}`" if head else f"id=`{card_id}`"
        return head
    if name == "generate_image":
        return _short(str(args.get("prompt") or ""), 220)
    if name == "memory_update":
        return _short(
            str(args.get("text") or args.get("name") or args.get("title") or ""),
            180,
        )
    if name == "send_attachment":
        paths = args.get("paths") or []
        caption = str(args.get("caption") or "")
        if isinstance(paths, list) and paths:
            files = ", ".join(Path(str(p)).name for p in paths[:4])
            more = "" if len(paths) <= 4 else f" (+{len(paths) - 4})"
            head = f"files: {files}{more}"
            if caption:
                return f"{head}\n     ↳ _{_short(caption, 180)}_"
            return head
        return _short(caption, 200)
    # Generic fallback — show the FIRST 2 scalar args so a tool like
    # ``tasks(action=create, title=...)`` still shows the title even
    # if we forgot to add a per-tool branch above. The previous code
    # only showed the first arg, which is why ``tasks`` looked like
    # ``action=create`` with no context.
    pairs: list[str] = []
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)) and (v or v == 0):
            pairs.append(f"{k}={_short(str(v), 100)}")
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
