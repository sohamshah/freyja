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

# Fallback Plan title when we can't derive one from the upcoming tool
# (the phase opens with thinking before any tool name is known, or the
# tool category isn't mapped). Slack renders the plan as a collapsible
# header above its task cards.
PLAN_TITLE_DEFAULT = "Working"
PLAN_TITLE_THINKING = "Thinking"

# Tool → phase title map. The plan that wraps a tool batch takes its
# title from the FIRST tool in the batch — gives the operator a
# semantic header ("Researching") instead of a generic "Working" on
# every group. Tools not listed here fall through to the default.
_PHASE_TITLE_BY_TOOL = {
    # Research / external data
    "web_search":   "Researching",
    "web_fetch":    "Researching",
    "web_research": "Researching",
    # Local filesystem reads
    "read_file":      "Reading",
    "list_directory": "Reading",
    "glob":           "Searching",
    "grep":           "Searching",
    "artifacts":      "Reading",
    # Local filesystem writes
    "write_file": "Writing",
    "edit_file":  "Editing",
    "edit_json":  "Editing",
    # Shell
    "bash":  "Running",
    "shell": "Running",
    # Browser + computer-use
    "browser_execute_js":  "Browsing",
    "browser_screenshot":  "Browsing",
    "computer_use":        "Driving the screen",
    "click":               "Driving the screen",
    "move_mouse":          "Driving the screen",
    "type_text":           "Driving the screen",
    "press_key":           "Driving the screen",
    "screenshot":          "Taking screenshots",
    # Generative / analysis
    "generate_image": "Generating",
    "analyze_video":  "Analyzing video",
    # Sub-agent fan-out
    "sub_agent":  "Delegating",
    "subagents":  "Delegating",
    # Knowledge / config
    "tasks":             "Planning",
    "kanban":            "Planning",
    "memory":            "Recalling",
    "load_skill":        "Loading a skill",
    "search_skills":     "Searching skills",
    "list_skills":       "Searching skills",
    "tool_search":       "Searching tools",
    "summarize_context": "Compacting context",
    # Outbound
    "send_attachment": "Sharing",
}


def _phase_title_for_tool(tool_name: str) -> str:
    """Derive a Plan section title from the first tool of a phase."""
    return _PHASE_TITLE_BY_TOOL.get(tool_name, PLAN_TITLE_DEFAULT)


# Cap citations per card. More than this is visual noise — Slack
# renders sources as a single row of pills, and >4 wraps unevenly
# on mobile.
_MAX_SOURCES_PER_CARD = 4
# URL extraction regex used to pull citation refs out of free-form
# tool result text (web_search, web_research). Slack-formatted URLs
# like <https://x|label> get caught too — group 1 is the bare URL,
# group 2 is the optional label.
_URL_RE = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]+))?>|(?<!<)(https?://[^\s)\]]+)")


def _build_sources_for_tool(
    tool_name: str,
    args: dict[str, Any],
    preview: str,
) -> list[Any] | None:
    """Build a UrlSourceElement list for tool cards that benefit from
    citation refs.

    Per-tool extraction:
      · web_fetch / web_research: args.url → one source
      · web_search / web_research with multiple results: parse top
        URLs from the preview text
      · browser_screenshot: parse "Tab: <url>" from preview

    Returns None when no sources apply (most tools). Capped at
    ``_MAX_SOURCES_PER_CARD``. Returns instances of
    ``slack_sdk.models.blocks.block_elements.UrlSourceElement``.
    """
    try:
        from slack_sdk.models.blocks.block_elements import UrlSourceElement
    except ImportError:
        return None

    sources: list[Any] = []
    seen_urls: set[str] = set()

    def _add(url: str, label: str | None = None) -> None:
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        text = label or _domain_of(url) or url
        sources.append(UrlSourceElement(url=url, text=_short(text, 60)))

    if tool_name in {"web_fetch", "web_research"}:
        url = str(args.get("url") or "").strip()
        if url.startswith("http"):
            _add(url, str(args.get("objective") or "").strip() or None)
        # web_research often pulls multiple URLs into the preview; harvest them too.
        if tool_name == "web_research":
            for m in _URL_RE.finditer(preview):
                u = (m.group(1) or m.group(3) or "").strip()
                lbl = (m.group(2) or "").strip() or None
                if u:
                    _add(u, lbl)
                if len(sources) >= _MAX_SOURCES_PER_CARD:
                    break
    elif tool_name == "web_search":
        # Pull the top URLs from the result preview. Slack renders the
        # citation pills underneath the card title.
        for m in _URL_RE.finditer(preview):
            u = (m.group(1) or m.group(3) or "").strip()
            lbl = (m.group(2) or "").strip() or None
            if u:
                _add(u, lbl)
            if len(sources) >= _MAX_SOURCES_PER_CARD:
                break
    elif tool_name in {"browser_screenshot", "browser_execute_js"}:
        # Look for "Tab: <url>" first, fall back to args.url.
        m = re.search(r"Tab:\s+<?(https?://[^\s>|]+)", preview)
        if m:
            _add(m.group(1))
        else:
            url = str(args.get("url") or args.get("tab_url") or "").strip()
            if url.startswith("http"):
                _add(url)

    return sources or None


def _domain_of(url: str) -> str:
    """Strip URL down to its hostname for compact citation labels."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


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

    # Multi-Plan phasing. Each "phase" is a contiguous batch of
    # thinking + tool calls between prose emissions. When the agent
    # transitions from "doing things" → "reporting what it did" by
    # emitting user-facing text, the current phase ends; the next
    # tool/thinking after that opens a fresh PlanUpdateChunk with a
    # title derived from the upcoming tool's category. Net result:
    # one streaming message with multiple titled card groups stitched
    # by interstitial prose, e.g.:
    #     Researching: web_fetch ✓, web_fetch ✓
    #     "I found two relevant sources..."
    #     Writing: write_file ✓
    #     "Saved to /tmp/draft.md"
    phase_open: bool = False
    cards_emitted_this_phase: int = 0
    # Title of the currently open Plan. Used to detect when the next
    # tool belongs to a different semantic category ("Researching" →
    # "Planning" → "Delegating") so we can close the current Plan and
    # open a fresh one. Without this every tool in a turn collapses
    # into one giant card regardless of what the agent is doing.
    phase_title: str | None = None

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
            # Body text arriving means any in-flight thinking phase is
            # over — close that card before we start emitting prose.
            await self._close_thinking_card()
            # Prose AFTER tool cards marks the end of a phase. The next
            # tool / thinking will open a fresh PlanUpdateChunk with a
            # new title. If no cards have been emitted yet (text-only
            # response), this is a no-op.
            async with self._lock:
                self._close_phase_if_after_cards()
            await self._append_text(text)
        elif etype == "thinking_delta":
            if self.verbosity == "off":
                return
            text = str(event.get("thinking") or event.get("text") or "")
            if not text:
                return
            await self._ensure_stream_opened()
            # If this is the first card-worthy event of a phase, open
            # a Plan. Thinking-led phases get the generic "Thinking"
            # title — the tool-derived title (if any) takes over when
            # the first tool_input_end fires within the same phase.
            await self._ensure_phase_open(title=PLAN_TITLE_THINKING)
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
            # recipient_team_id + recipient_user_id are what Slack uses
            # to actually put the message into streaming state — without
            # them, chat.startStream returns 200/ts but the message stays
            # in non-streaming state and every subsequent appendStream
            # rejects with "message_not_in_streaming_state". The SDK's
            # ChatStream wrapper docs say "Required when streaming to
            # channels"; empirically required for DMs too.
            result = await self.adapter.start_stream(
                self.source.chat_id,
                thread_id=self._reply_thread_id,
                task_display_mode="plan",
                recipient_team_id=self.source.workspace_id or None,
                recipient_user_id=self.source.user_id or None,
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
            # NOTE: Plan emission deferred until the first card-worthy
            # event arrives — see _ensure_phase_open. If the turn
            # produces ONLY prose (no tools, no thinking), we never
            # emit a plan at all and the message reads as a clean
            # text response.

    async def _ensure_phase_open(self, *, title: str) -> None:
        """Open a new Plan section with the given title if none is
        currently open. Idempotent within a phase.

        Called at the first card-worthy event of each phase (first
        tool_use_start or first thinking_delta after stream is open).
        The phase closes when prose lands between tool batches (see
        _close_phase_if_after_cards), so the next card-worthy event
        opens a fresh Plan with its own title.
        """
        async with self._lock:
            if self._state.stream_failed or not self._state.stream_ts:
                return
            if self._state.phase_open:
                return
            try:
                from slack_sdk.models.messages.chunk import PlanUpdateChunk
            except ImportError:
                logger.warning("[slack] PlanUpdateChunk not available")
                self._state.phase_open = True  # avoid retrying forever
                return
            chunk = PlanUpdateChunk(title=title)
            result = await self.adapter.append_stream(
                self.source.chat_id,
                self._state.stream_ts,
                chunks=[chunk],
            )
            self._state.phase_open = True
            self._state.phase_title = title
            self._state.cards_emitted_this_phase = 0
            if not result.ok:
                logger.debug("[slack] plan chunk append failed: %s", result.error)
                if "message_not_in_streaming_state" in (result.error or "").lower():
                    self._state.stream_failed = True

    def _close_phase_if_after_cards(self) -> None:
        """End the current phase if at least one card has been emitted
        in it. The NEXT card-worthy event will open a fresh phase with
        its own title. Called from the text-emission path so prose
        between tool batches naturally splits the cards into groups."""
        if self._state.phase_open and self._state.cards_emitted_this_phase > 0:
            self._state.phase_open = False
            self._state.phase_title = None
            self._state.cards_emitted_this_phase = 0
            # Also reset the same-tool coalesce tracking — a new phase
            # is a fresh batch, the prior tool shouldn't coalesce into
            # the next phase's cards.
            self._state.coalesce_last_tool = None
            self._state.coalesce_count = 0

    def _rotate_phase_on_title_change(self, new_title: str) -> None:
        """Close the current Plan if the new card belongs to a
        different semantic category, so multi-stage turns produce
        multiple labelled cards instead of one giant "Delegating"
        block. Only rotates after at least one card has actually
        been emitted in the current phase — we never close an empty
        phase, and we never close because of the very first card.
        """
        if (
            self._state.phase_open
            and self._state.phase_title is not None
            and self._state.phase_title != new_title
            and self._state.cards_emitted_this_phase > 0
        ):
            self._close_phase_if_after_cards()

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
        """Send any buffered body text. Caller holds the lock.

        We open the stream with ``task_display_mode="plan"`` so the
        message lives in "task" streaming mode — once that's set the
        server rejects subsequent ``markdown_text`` top-level appends
        with ``streaming_mode_mismatch``. Body text in this mode has
        to ride as a ``MarkdownTextChunk`` inside the chunks array
        instead, which renders the same prose between/after the
        plan + task cards.
        """
        if self._state.stream_failed or not self._state.stream_ts:
            return
        if not self._state.text_buffer:
            return
        delta = self._state.text_buffer
        self._state.text_buffer = ""
        try:
            from slack_sdk.models.messages.chunk import MarkdownTextChunk
        except ImportError:
            # Old SDK without the chunk type — fall back to top-level
            # markdown_text (works on text-mode streams; will error on
            # task-mode streams, but we have no better option).
            result = await self.adapter.append_stream(
                self.source.chat_id,
                self._state.stream_ts,
                markdown_text=delta,
            )
        else:
            chunk = MarkdownTextChunk(text=delta)
            result = await self.adapter.append_stream(
                self.source.chat_id,
                self._state.stream_ts,
                chunks=[chunk],
            )
        self._state.last_text_flush_ms = time.monotonic() * 1000
        if not result.ok:
            # Slack closes a streaming message after an idle window
            # (~empirically ~minutes; we hit this every time a long
            # ``subagents`` wait sits between LLM iterations). Once
            # that happens every future appendStream rejects with
            # "message_not_in_streaming_state" and nothing the model
            # produces afterwards reaches the user. Detect the
            # closed-stream case AND any other appendStream failure
            # and switch to the chat.postMessage / chat.update
            # fallback path that ``_append_text`` and
            # ``_flush_fallback_locked`` already implement — at least
            # the prose body lands in Slack, even if task cards stop
            # updating. Restore the delta we already drained from
            # text_buffer into fallback_buffer so nothing is lost.
            error_str = (result.error or "").lower()
            is_closed = "message_not_in_streaming_state" in error_str
            logger.warning(
                "[slack] text appendStream failed (%s) — %s",
                result.error,
                "stream closed, switching to postMessage fallback"
                if is_closed
                else "marking stream failed and falling back",
            )
            # Restore the drained delta to text_buffer — the fallback
            # flush reads from there. Without this, every byte we
            # drained on the failing call is silently dropped.
            self._state.text_buffer = delta + self._state.text_buffer
            self._state.stream_failed = True
            # Kick the fallback flush immediately so the user sees
            # the in-flight text now rather than waiting for finalize.
            await self._flush_fallback_locked()

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
            if "message_not_in_streaming_state" in (result.error or "").lower():
                self._state.stream_failed = True

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
        # First card-worthy event of the phase opens a Plan titled
        # after the upcoming tool's category. If the phase was already
        # opened by a thinking_delta (with "Thinking" title), the
        # already-emitted Plan keeps that title — Slack doesn't let us
        # mutate an existing Plan's title — and subsequent tools in
        # the same phase land under it. Trade-off: the title reflects
        # the phase's OPENING activity, not the dominant one.
        # Close + reopen the phase when the tool's semantic category
        # shifts (e.g. Researching → Planning → Delegating). Without
        # this, every tool in a multi-stage turn lands in whichever
        # title the FIRST tool set — three sub_agents plus a tasks
        # call plus a subagents call all collapse into one "Delegating"
        # card. Splitting on category gives the operator a sense of
        # the turn's structure at a glance.
        new_title = _phase_title_for_tool(tool_name)
        self._rotate_phase_on_title_change(new_title)
        await self._ensure_phase_open(title=new_title)

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
            await self._collect_images_for_finalize(event)
            return
        args = self._state.pending_tool_args.pop(tool_id, {})

        if self.verbosity == "off":
            await self._collect_images_for_finalize(event)
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
        # URL citations for web / browser tools.
        sources = _build_sources_for_tool(tool_name, args, preview)

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
                sources=sources,
            )
        else:
            await self._emit_task_chunk(
                tool_id, title, status,
                output=output_text, sources=sources,
            )

        await self._collect_images_for_finalize(event)

    async def _emit_task_chunk(
        self,
        card_id: str,
        title: str,
        status: str,
        *,
        output: str | None = None,
        sources: list[Any] | None = None,
    ) -> None:
        """Append a TaskUpdateChunk to the stream. Status transitions
        on the same card_id mutate the existing card.

        ``sources`` attaches one or more clickable references
        (UrlSourceElement) to the card — used to surface where the
        agent looked when running web_fetch / web_search / browser
        tools."""
        async with self._lock:
            if self._state.stream_failed or not self._state.stream_ts:
                return
            try:
                from slack_sdk.models.messages.chunk import TaskUpdateChunk
            except ImportError:
                return
            kwargs: dict[str, Any] = {
                "id": card_id,
                "title": title,
                "status": status,
            }
            if output is not None:
                kwargs["output"] = output
            if sources:
                kwargs["sources"] = sources
            chunk = TaskUpdateChunk(**kwargs)
            result = await self.adapter.append_stream(
                self.source.chat_id,
                self._state.stream_ts,
                chunks=[chunk],
            )
            # Count only the FIRST time a card opens (in_progress) so
            # the in_progress → complete transition on the same card
            # doesn't double-count. The counter drives phase-close
            # detection on the next text_delta.
            if status == "in_progress":
                self._state.cards_emitted_this_phase += 1
            if not result.ok:
                logger.debug("[slack] task chunk append failed: %s", result.error)
                # If the stream went out of streaming state (idle
                # timeout during a long tool wait), mark it failed so
                # subsequent body-text flushes route through the
                # chat.postMessage fallback path and the user still
                # sees the agent's prose response.
                if "message_not_in_streaming_state" in (result.error or "").lower():
                    self._state.stream_failed = True

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

    async def _collect_images_for_finalize(self, event: dict[str, Any]) -> None:
        """Eagerly upload any image content from a tool_result via the
        unshared-upload path, capturing the file_id for later inline
        rendering in stop_stream's blocks.

        Uploading immediately (rather than batched at finalize) keeps
        the API surface small (one upload per image, no batch) and
        means each image's file_id is ready by the time the agent
        emits the final response. The upload is unshared so the file
        only appears inside the streaming message's Block Kit blocks,
        not as a separate thread message.

        Falls back to the legacy "stash bytes, batch-upload at
        finalize via files_upload" path if the unshared upload fails
        for any reason — the image still lands, just as an adjacent
        thread message instead of inline.
        """
        # Collect raw image bytes from both surfaces — inline anthropic
        # image blocks AND "File saved to" path refs.
        candidates: list[dict[str, Any]] = []
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
            candidates.append({
                "data": raw,
                "mime": mime,
                "filename": f"{label.replace(' ', '_')}{_mime_to_ext(mime)}",
            })
        if not candidates:
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
                candidates.append({
                    "data": raw,
                    "mime": _ext_to_mime(p.suffix.lower()),
                    "filename": p.name,
                })

        if not candidates:
            return

        for img in candidates:
            file_id = await self._upload_image_unshared(img)
            if file_id:
                # Recorded for stop_stream(blocks=…) construction.
                img["file_id"] = file_id
            # Either way (file_id present or not), keep the entry so
            # the finalize fallback can re-try via post-stop
            # files_upload if the inline path didn't land.
            self._state.pending_image_blocks.append(img)

    async def _upload_image_unshared(self, img: dict[str, Any]) -> str | None:
        """Push one image via the adapter's unshared upload path.
        Returns the Slack file_id (``F...``) on success, None on
        failure or if the adapter doesn't implement the method."""
        if not hasattr(self.adapter, "upload_file_unshared"):
            return None
        item = UploadItem(
            data=img["data"],
            filename=img["filename"],
            mime_type=img["mime"],
        )
        try:
            result = await self.adapter.upload_file_unshared(item)
        except Exception:  # noqa: BLE001
            logger.exception("unshared image upload failed")
            return None
        if not result.ok or not result.message_id:
            logger.debug(
                "[slack] unshared upload returned %s — falling back to "
                "post-stream files_upload for this image",
                result.error,
            )
            return None
        return result.message_id

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

        # Build Block Kit image blocks for images whose unshared
        # upload succeeded during the turn — these render INLINE at
        # the bottom of the streaming message via stop_stream(blocks=).
        # Images whose unshared upload failed get re-uploaded the
        # legacy way (post-stream files_upload to the thread) so the
        # user still sees them, just in an adjacent message.
        inline_blocks = self._build_inline_image_blocks()

        # Stop the stream. No more content; this finalizes the
        # message visually (Slack removes the streaming indicator).
        # Skipped on the fallback path — there's no stream to stop.
        if self._state.stream_ts and not self._state.stream_failed:
            stop_result = await self.adapter.stop_stream(
                self.source.chat_id,
                self._state.stream_ts,
                blocks=inline_blocks or None,
            )
            if not stop_result.ok:
                logger.warning("[slack] chat.stopStream failed: %s", stop_result.error)
                # The inline blocks may have been the cause (e.g. a
                # malformed image block). Retry without them so the
                # stream at least finalizes — the image will then land
                # via the fallback files_upload below.
                if inline_blocks:
                    await self.adapter.stop_stream(
                        self.source.chat_id,
                        self._state.stream_ts,
                    )
                    inline_blocks = None  # signal fallback to upload all

        # Fallback files_upload: covers images whose inline path
        # didn't reach Slack (unshared upload failed OR stop_stream
        # rejected the blocks). When inline_blocks is non-empty the
        # successfully-uploaded images already rendered, so we skip
        # those entries and only push the rest.
        await self._upload_pending_images(skip_with_file_id=bool(inline_blocks))

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

    def _build_inline_image_blocks(self) -> list[dict[str, Any]]:
        """Construct Block Kit image blocks for images already uploaded
        via the unshared path. Used by stop_stream's blocks param so
        the images render INSIDE the streaming message envelope, not
        as a separate adjacent thread message."""
        blocks: list[dict[str, Any]] = []
        for img in self._state.pending_image_blocks:
            file_id = img.get("file_id")
            if not file_id:
                continue
            blocks.append({
                "type": "image",
                "slack_file": {"id": file_id},
                "alt_text": img.get("filename") or "image",
            })
        return blocks

    async def _upload_pending_images(self, *, skip_with_file_id: bool = False) -> None:
        """Fallback uploader for images whose inline-block path
        didn't reach Slack (unshared upload failed earlier, or
        stop_stream rejected the blocks payload). Places them in the
        thread as an adjacent message — uglier than the inline
        rendering but at least the user sees the image.

        When ``skip_with_file_id`` is True, images that DID land
        inline are skipped here to avoid duplicate rendering."""
        items: list[UploadItem] = []
        for img in self._state.pending_image_blocks:
            if not img.get("data"):
                continue
            if skip_with_file_id and img.get("file_id"):
                continue
            items.append(UploadItem(
                data=img["data"],
                filename=img["filename"],
                mime_type=img["mime"],
            ))
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
