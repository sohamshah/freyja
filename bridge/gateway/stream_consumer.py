"""Bridge event → one Slack streaming message per turn (Thinking Steps).

Subscribes to events for a specific session id via the bridge's
``register_session_listener``. Each new turn instantiates a fresh
consumer that produces ONE Slack message in the user's thread, opened
with ``chat.startStream`` and grown with ``chat.appendStream``:

  · prose streams live as ``MarkdownTextChunk`` every ~500 ms, in the
    order it was written, with tables, task lists and fences rendered
    natively by Slack
  · thinking and each tool call are ``TaskUpdateChunk`` cards, inline
    between the prose, collapsible, with a status
  · widgets the agent sends through the ``slack_blocks`` tool (data
    tables, charts, cards) are ``BlocksChunk`` entries in the same message
  · ``chat.stopStream`` seals it with the tail prose, inline images,
    the feedback buttons, ``freyja_session`` metadata and the session
    status

Live probe on 2026-10-20: markdown, task and blocks chunks mix in one
stream in both ``timeline`` and ``plan`` mode, and stopStream takes
chunks, blocks, metadata and session_status together. The June
``streaming_mode_mismatch`` error was the ``markdown_text`` parameter
mixed with ``chunks``, never two chunk types. The consumer never passes
``markdown_text`` as a parameter.

Events are handled strictly in order by one worker task. Each handler
takes the lock several times on its way through, so with one task per
event two deltas could swap places around a slow append, a tool card's
paragraph break could land between two words of the next sentence, and
finalize could seal the message before the last deltas were in the
buffer (the tail then went out as a second message). One worker, one
event at a time, removes all of that.

Slack caps a streamed message at 12,000 characters of content (prose,
and once any prose exists the task card titles and outputs too). The
consumer keeps a running estimate and rolls over to a continuation
stream in the same thread before the cap; when Slack still answers
``msg_too_long`` it rolls over then and sends the piece again. The old
message is sealed with the session still ``processing``; the feedback
buttons go on the last one only.

Session lifecycle (Slack Agent Sessions API):
  · first event of the turn → ``processing``
  · finalize / abort / turn failure → ``active``
  · stopped by the native Stop button → the gateway cancels the turn
    and calls ``abort(stopped_by_user=True)``; Slack seals the streams
    itself and the consumer makes no further API calls

Verbosity (off/new/all/verbose) controls how much detail lands in the
task cards. ``off`` skips cards entirely; prose still streams.

If start_stream fails (rate limit, API change, workspace tier), the
``cards_disabled`` flag flips, nothing more is appended, and the prose
is posted once at finalize with ``chat.postMessage``. If the stream
dies mid-turn (idle timeout, deleted), the prose not yet streamed goes
out the same way.

A mid-stream provider retry (``stream_reset``) cannot take back text
Slack already shows. The unsent part is dropped; when some of the
failed attempt was already visible, a short note marks the retry.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.gateway.platforms.base import (
    MessageSource,
    PlatformAdapter,
    SendResult,
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

# Slack caps one streamed message at 12,000 characters of content and
# answers ``msg_too_long`` to every append after that. Measured in the
# operator's DM on 2026-10-20: prose alone fails at the fifth
# 3,000-character append; once any prose exists every task card chunk
# counts too (its title and output, plus some overhead per chunk), so
# 1,000 characters of prose and five 2,000-character tool outputs fill
# a message. Cards alone do not count. The consumer keeps a running
# estimate and rolls over to a continuation stream before the cap.
_STREAM_CHAR_CAP = 12_000
_STREAM_BUDGET = 10_000
_CHUNK_OVERHEAD = 40
# One prose or thinking append. The cap is on the total, not the call,
# but small pieces keep the rollover point close to the budget.
_TEXT_PIECE_CAP = 3000
_THINKING_DELTA_CAP = 3000
# Card content is collapsed in Slack and shown in full in the desktop
# app, and it is paid for out of the same 12k as the prose, so it gets
# the short version here.
_THINKING_CARD_CAP = 600
_TOOL_OUTPUT_CAP = 300
_TOOL_ERROR_OUTPUT_CAP = 800
_TOOL_TITLE_CAP = 120
# Output of a tool card that was still running when its message filled
# up; the card is reopened in the next message.
_CONTINUED_NOTE = "Still running. The result is in the next message."
# Note appended when a provider retry happens after part of the failed
# attempt was already streamed.
_RETRY_NOTE = "_(the model call failed mid-reply; retrying)_"

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
    "web_search":     "Researching",
    "web_fetch":      "Researching",
    "web_research":   "Researching",
    "twitter_search": "Searching X/Twitter",
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
    "call_tool":         "Running a tool",
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
    elif tool_name in {"web_search", "twitter_search"}:
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


def _stream_is_dead(error: str | None) -> bool:
    """Slack has two ways of saying "that streaming message is gone".

    ``message_not_in_streaming_state`` — it exists but stopStream already
    sealed it. ``message_not_found`` — it is gone entirely (deleted, or the
    ts belongs to a message this token can no longer see).

    Only the first was ever checked, so a ``message_not_found`` stream left
    cards enabled and every subsequent event re-attempted the append: a
    2026-09-09 Slack turn logged 44 identical failures with full tracebacks
    while the card sat frozen. Either error means the same thing to us —
    stop trying.
    """
    e = (error or "").lower()
    return "message_not_in_streaming_state" in e or "message_not_found" in e


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
    # start_stream failed (or a later card emit hit
    # message_not_in_streaming_state). Card emission becomes a no-op;
    # body delivery is independent and proceeds normally as a separate
    # chat.postMessage at finalize.
    cards_disabled: bool = False

    # Body prose buffer. Deltas accumulate here for the whole turn;
    # finalize() drains it once via chat.postMessage in the same thread.
    # Body never lives inside the streaming envelope (task-mode streams
    # reject MarkdownTextChunk), so there's no need for live streaming,
    # throttling, or a separate text_buffer / fallback_committed split.
    body_buffer: str = ""
    # Where the current model call's prose starts in body_buffer. A
    # stream_reset (the engine retries a call that failed mid-stream)
    # cuts back to it, so the reply is not posted as partial + whole.
    body_mark: int = 0
    body_mark_break: bool = False
    # A non-text event (tool call, thinking) fired since the last body
    # append. The model's next text block is a NEW paragraph — the raw
    # deltas carry no separator between blocks, so without this flag a
    # multi-phase turn renders as one run-on line ("…checks.Now let me
    # see…"). _append_text inserts a blank line when the flag is set.
    body_break_pending: bool = False
    # Prefix of body_buffer already streamed to Slack. Everything past it
    # is pending; finalize ships it as the stopStream tail.
    body_sent: int = 0
    last_text_flush_ms: float = 0.0
    text_flush_task: Any = None
    # Characters Slack counts against the current message's 12k cap:
    # prose plus card titles and outputs (reset on rollover).
    stream_chars: int = 0
    stream_count: int = 0
    # A rollover is in progress; appends made while sealing the old
    # message and opening the new one must not start another.
    rolling_over: bool = False
    # Tool cards opened in the current message and not yet resolved
    # (id -> title). A rollover closes them in the old message and
    # reopens them in the new one.
    inflight_cards: dict[str, str] = field(default_factory=dict)
    # Set by abort(stopped_by_user=True): Slack sealed the streams and
    # asked for no further calls.
    stopped_by_user: bool = False
    # Widgets appended through append_blocks (for the feedback gate).
    widgets_emitted: int = 0

    # Thinking-card accumulator. `thinking_buffer` is the FULL cumulative
    # reasoning text; `thinking_sent` tracks the prefix already emitted
    # so each TaskUpdateChunk carries only the new delta. Slack APPENDS
    # the `output` field on consecutive task updates with the same id,
    # so sending the cumulative buffer would concatenate the entire
    # reasoning text repeatedly into the card.
    thinking_card_id: str | None = None
    thinking_buffer: str = ""
    thinking_sent: str = ""
    # The card reached _THINKING_CARD_CAP and carries its marker.
    thinking_capped: bool = False
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
    # Calls kept out of the chat (heartbeats). A call_tool call sends two
    # tool_input_end events for one id, so the second must know the first
    # was dropped.
    suppressed_tool_ids: set[str] = field(default_factory=set)

    # Inline image attachments accumulated during the turn for final
    # delivery via stop_stream(blocks=…). Each entry is a dict with
    # keys: data (bytes), mime, filename. Limited to a small budget so
    # one chat.stopStream call doesn't blow past Slack's payload cap.
    pending_image_blocks: list[dict[str, Any]] = field(default_factory=list)

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

        # "timeline" interleaves prose and cards in the order they happen;
        # "plan" groups cards under titled plan headers (the pre-October
        # layout). Both accept mixed chunks.
        self.display_mode = "timeline"
        # Thumbs up / down under the sealed message.
        self.feedback_enabled = True

        self._state = _StreamState()
        self._lock = asyncio.Lock()
        # Events wait here for the one worker task that handles them in
        # order (see _drain).
        self._queue: deque[dict[str, Any]] = deque()
        self._worker: asyncio.Task | None = None
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
        """Sync entry point: queues the event for the ordered worker.

        Called synchronously by ``emit()`` from the engine thread, so it
        must NOT block. The loop thread appends it to the queue and the
        worker handles it after everything queued before it.
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
            "stream_start",
            "stream_reset",
            "system_event",
        }:
            return
        if etype == "system_event" and event.get("subtype") != "verification_nudge":
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            # The loop is no longer running — silently drop.
            pass

    def _enqueue(self, event: dict[str, Any]) -> None:
        """Loop-thread half of on_event: queue the event and make sure a
        worker is draining the queue."""
        if self._state.finalized:
            return
        self._queue.append(event)
        if self._worker is None or self._worker.done():
            self._worker = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        """Handle queued events one at a time, in the order they came.

        A handler takes the lock several times on its way through, so
        one task per event let deltas overtake each other around a slow
        append and let finalize run before the last deltas were in the
        buffer. One worker keeps the prose, the cards and the seal in
        the order the agent produced them.
        """
        while self._queue:
            event = self._queue.popleft()
            if self._state.finalized:
                continue
            try:
                await self._handle_event_async(event)
            except Exception:  # noqa: BLE001
                logger.exception("[slack] stream event %s failed", event.get("type"))

    async def _handle_event_async(self, event: dict[str, Any]) -> None:
        etype = event.get("type")

        # Markers only move the body mark; they are not activity, so they
        # must not open the stream or set the typing status.
        if etype == "stream_start":
            async with self._lock:
                self._state.body_mark = len(self._state.body_buffer)
                self._state.body_mark_break = self._state.body_break_pending
            return
        if etype == "stream_reset":
            async with self._lock:
                st = self._state
                if st.body_sent > st.body_mark:
                    # Part of the failed attempt is already on screen and
                    # cannot be taken back. Keep it, mark the retry, and
                    # let the retried text continue as a new paragraph.
                    st.body_buffer = st.body_buffer[: st.body_sent]
                    st.body_mark = st.body_sent
                    st.body_break_pending = True
                    self._append_text_locked(_RETRY_NOTE)
                    st.body_break_pending = True
                else:
                    st.body_buffer = st.body_buffer[: st.body_mark]
                    # The failed call's text used up the pending paragraph
                    # break; the retried text needs it again.
                    st.body_break_pending = st.body_mark_break
            return
        if etype == "system_event":
            # The engine asked the agent to check its work before ending
            # (a hidden message). Its next words are a new paragraph, not
            # a run-on of the reply it meant to end with.
            async with self._lock:
                self._state.body_break_pending = True
            return

        # The session is working: ``processing`` on the first event of
        # the turn. finalize/abort set it back to ``active``.
        if not self._state.typing_set:
            self._state.typing_set = True
            await self._set_status("processing")

        if etype in {"thinking_delta", "tool_use_start", "tool_input_end", "tool_result"}:
            self._state.body_break_pending = True

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
        elif etype == "turn_failed":
            await self._handle_turn_failed(event)

    async def _handle_turn_failed(self, event: dict) -> None:
        """Tell the operator the turn died, then close the stream.

        Without this the failure is invisible: the bridge logs it and
        emits a session-scoped event, but nothing reaches Slack, so the
        bot simply stops answering. A thread pinned to a model whose API
        key the daemon lacks fails this way on EVERY message, which
        reads as the gateway being down rather than as one bad setting.
        """
        detail = str(event.get("message") or "").strip() or "unknown error"
        hint = str(event.get("hint") or "")
        model = event.get("model")
        where = f" on `{model}`" if model else ""
        text = f":warning: That turn failed{where}: {detail}{hint}"
        try:
            await self.adapter.send(
                self.source.chat_id,
                text,
                thread_id=self._reply_thread_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[slack] could not post turn failure", exc_info=True)
        # Always finalize: the stream card is open and no turn_complete
        # is coming, so without this it stays spinning until the stale
        # consumer teardown notices on the NEXT message.
        try:
            await self.finalize()
        except Exception:  # noqa: BLE001
            logger.debug("finalize after turn failure failed", exc_info=True)

    # ── stream lifecycle ──

    async def _ensure_stream_opened(self) -> None:
        """Idempotent: open the stream on first call, no-op after."""
        async with self._lock:
            if self._state.stream_opened or self._state.cards_disabled:
                return
            self._state.stream_opened = True
            if not self._reply_thread_id and self.source.chat_type != "code":
                logger.warning("[slack] no thread anchor — start_stream skipped")
                self._state.cards_disabled = True
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
                thread_id=self._reply_thread_id or None,
                task_display_mode=self.display_mode,
                recipient_team_id=self.source.workspace_id or None,
                recipient_user_id=self.source.user_id or None,
            )
            if result.ok and result.message_id:
                self._state.stream_ts = result.message_id
                self._state.stream_count += 1
            else:
                self._state.cards_disabled = True
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
            if self._state.cards_disabled or not self._state.stream_ts:
                return
            if self._state.phase_open:
                return
            if self.display_mode != "plan":
                # Timeline mode has no plan headers; prose between card
                # batches is the grouping. Keep the phase bookkeeping.
                self._state.phase_open = True
                self._state.phase_title = title
                self._state.cards_emitted_this_phase = 0
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
                if _stream_is_dead(result.error):
                    self._state.cards_disabled = True

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

    # ── text body ──

    def _append_text_locked(self, text: str) -> None:
        st = self._state
        if st.body_break_pending:
            st.body_break_pending = False
            if st.body_buffer and not st.body_buffer.endswith("\n"):
                st.body_buffer += "\n\n"
        st.body_buffer += text

    async def _append_text(self, text: str) -> None:
        """Append a body delta and schedule a flush into the stream.

        Flushes are throttled to ``edit_interval_ms``; the first delta
        of a quiet period goes out at once so the operator sees the
        reply start. Whatever is still unsent at finalize ships as the
        stopStream tail.
        """
        async with self._lock:
            self._append_text_locked(text)
            if self._state.cards_disabled or not self._state.stream_ts:
                return
            self._schedule_text_flush_locked()

    def _schedule_text_flush_locked(self) -> None:
        st = self._state
        if st.text_flush_task is not None and not st.text_flush_task.done():
            return
        now_ms = time.monotonic() * 1000
        wait_ms = max(0.0, self.edit_interval_ms - (now_ms - st.last_text_flush_ms))

        async def _later() -> None:
            if wait_ms:
                await asyncio.sleep(wait_ms / 1000)
            async with self._lock:
                if self._state.finalized:
                    return
                await self._flush_text_locked()

        try:
            st.text_flush_task = asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            st.text_flush_task = None

    async def _flush_text_locked(self, *, keep_tail: int = 0) -> None:
        """Stream the unsent prose. Caller holds the lock.

        ``keep_tail`` leaves that many trailing characters unsent so the
        caller can ship them on stopStream instead. Pieces are capped at
        ``_TEXT_PIECE_CAP`` and go through ``_stream_append_locked``,
        which rolls the message over when a piece would not fit. A dead
        stream flips ``cards_disabled`` and the rest of the prose falls
        back to one post at finalize.
        """
        st = self._state
        if st.cards_disabled or not st.stream_ts:
            return
        try:
            from slack_sdk.models.messages.chunk import MarkdownTextChunk
        except ImportError:
            st.cards_disabled = True
            return
        end = len(st.body_buffer) - keep_tail
        while st.body_sent < end:
            piece = st.body_buffer[st.body_sent : min(end, st.body_sent + _TEXT_PIECE_CAP)]
            if not piece:
                break
            result = await self._stream_append_locked(
                [MarkdownTextChunk(text=piece)], chars=len(piece), prose_first=False,
            )
            if not result.ok:
                logger.debug("[slack] prose appendStream failed: %s", result.error)
                return
            st.body_sent += len(piece)
        st.last_text_flush_ms = time.monotonic() * 1000

    async def _stream_append_locked(
        self, chunks: list[Any], *, chars: int, prose_first: bool = True,
    ) -> SendResult:
        """One ``chat.appendStream`` against the live message, with the
        12k cap in mind. Caller holds the lock.

        ``chars`` is what Slack will count for these chunks. When the
        running total would pass the budget, the message is sealed and a
        continuation stream opened first. When Slack still answers
        ``msg_too_long`` (the estimate was short), the same happens once
        and the chunks are sent again. A dead stream flips
        ``cards_disabled``; after the Stop button nothing is sent.

        With ``prose_first`` (every chunk but prose itself), the prose
        still waiting on the flush timer goes out before the chunk, so a
        card or widget lands after the words the agent wrote before it
        and not one card ahead of them.
        """
        st = self._state
        if st.cards_disabled or not st.stream_ts or st.stopped_by_user:
            return SendResult(ok=False, error="no live stream")
        if prose_first and st.body_sent < len(st.body_buffer):
            await self._flush_text_locked()
            if st.cards_disabled or not st.stream_ts:
                return SendResult(ok=False, error="no live stream")
        may_roll = st.stream_chars > 0 and not st.rolling_over
        if may_roll and st.stream_chars + chars + _CHUNK_OVERHEAD > _STREAM_BUDGET:
            await self._rollover_locked()
            if st.cards_disabled or not st.stream_ts:
                return SendResult(ok=False, error="no live stream")
        result = await self.adapter.append_stream(self.source.chat_id, st.stream_ts, chunks=chunks)
        err = (result.error or "").lower()
        if not result.ok and "msg_too_long" in err and may_roll:
            await self._rollover_locked()
            if st.cards_disabled or not st.stream_ts:
                return result
            result = await self.adapter.append_stream(
                self.source.chat_id, st.stream_ts, chunks=chunks,
            )
            err = (result.error or "").lower()
        if result.ok:
            st.stream_chars += chars + _CHUNK_OVERHEAD
        elif _stream_is_dead(err) or "stopped_by_user" in err:
            st.cards_disabled = True
        return result

    async def _rollover_locked(self) -> None:
        """Seal the current message and continue in a fresh stream.

        The old message keeps the session ``processing`` (the turn is
        still running) and closes its open cards: the thinking card
        completes, and a tool still running gets a note that its result
        is in the next message, where the card is reopened. Without
        that, Slack shows a warning icon on any card left in progress
        when a stream is sealed.
        """
        st = self._state
        if st.rolling_over:
            return
        st.rolling_over = True
        try:
            await self._close_thinking_card_locked()
            inflight = dict(st.inflight_cards)
            st.inflight_cards.clear()
            try:
                from slack_sdk.models.messages.chunk import TaskUpdateChunk
            except ImportError:
                TaskUpdateChunk = None  # type: ignore[assignment]
            if TaskUpdateChunk is not None and not st.cards_disabled and st.stream_ts:
                for card_id, title in inflight.items():
                    chunk = TaskUpdateChunk(
                        id=card_id, title=title, status="complete", output=_CONTINUED_NOTE,
                    )
                    await self.adapter.append_stream(
                        self.source.chat_id, st.stream_ts, chunks=[chunk],
                    )
            try:
                await self.adapter.stop_stream(
                    self.source.chat_id, st.stream_ts,
                    metadata=self._metadata(), session_status="processing",
                )
            except Exception:  # noqa: BLE001
                logger.debug("[slack] rollover stop_stream failed", exc_info=True)
            result = await self.adapter.start_stream(
                self.source.chat_id,
                thread_id=self._reply_thread_id or None,
                task_display_mode=self.display_mode,
                recipient_team_id=self.source.workspace_id or None,
                recipient_user_id=self.source.user_id or None,
            )
            if result.ok and result.message_id:
                st.stream_ts = result.message_id
                st.stream_count += 1
                st.stream_chars = 0
                st.phase_open = False
                st.phase_title = None
                st.cards_emitted_this_phase = 0
                if TaskUpdateChunk is not None:
                    for card_id, title in inflight.items():
                        chunk = TaskUpdateChunk(id=card_id, title=title, status="in_progress")
                        res = await self.adapter.append_stream(
                            self.source.chat_id, st.stream_ts, chunks=[chunk],
                        )
                        if res.ok:
                            st.stream_chars += len(title) + _CHUNK_OVERHEAD
                            st.inflight_cards[card_id] = title
            else:
                logger.warning("[slack] continuation startStream failed: %s", result.error)
                st.cards_disabled = True
        finally:
            st.rolling_over = False

    async def append_blocks(self, blocks: list[dict[str, Any]], *, label: str = "") -> bool:
        """Put Block Kit widgets into the live message, in order.

        Called by the ``slack_blocks`` tool from inside the turn. Pending
        prose is flushed first so the widget lands where the agent put
        it. Returns False when there is no live stream (the caller then
        posts the blocks as their own message).
        """
        async with self._lock:
            st = self._state
            if st.finalized or st.cards_disabled or not st.stream_ts:
                return False
            try:
                from slack_sdk.models.messages.chunk import BlocksChunk
            except ImportError:
                return False
            result = await self._stream_append_locked(
                [BlocksChunk(blocks=blocks)], chars=_text_chars_in(blocks),
            )
            if not result.ok:
                logger.debug("[slack] blocks chunk append failed: %s", result.error)
                return False
            st.widgets_emitted += len(blocks)
            # The next prose is a new paragraph under the widget.
            st.body_break_pending = True
            return True

    def _metadata(self) -> dict[str, Any]:
        """``freyja_session`` metadata stamped on every message of the turn,
        so a Slack message maps back to its session without a lookup."""
        return {
            "event_type": "freyja_session",
            "event_payload": {
                "session_key": self.session_key,
                "turn_ts": str(self.source.message_id or ""),
            },
        }

    async def _set_status(self, status: str) -> None:
        """Best-effort agent session status through the adapter."""
        if self._state.stopped_by_user:
            return
        setter = getattr(self.adapter, "set_session_status", None)
        try:
            if setter is not None:
                await setter(
                    self.source.chat_id, thread_id=self._reply_thread_id or None, status=status,
                )
            elif status == "processing":
                await self.adapter.send_typing(self.source.chat_id, thread_id=self._reply_thread_id)
            else:
                await self.adapter.stop_typing(self.source.chat_id, thread_id=self._reply_thread_id)
        except Exception:  # noqa: BLE001
            logger.debug("session status %s failed (non-fatal)", status, exc_info=True)

    # ── thinking card (TaskUpdateChunk) ──

    async def _append_thinking(self, text: str) -> None:
        """Open or extend the current Thinking task card."""
        async with self._lock:
            if self._state.cards_disabled:
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
        consecutive updates with the same card id, so we send only
        the unsent suffix on each flush.

        The card holds at most ``_THINKING_CARD_CAP`` characters: it is
        collapsed in Slack, counted against the message cap, and the
        desktop app has the full text. Past the cap the rest is dropped
        and the card ends in a marker. Larger deltas go out as several
        TaskUpdateChunks (Slack appends them). The final
        ``status="complete"`` flush always fires so the card resolves
        visually.
        """
        if self._state.cards_disabled or not self._state.stream_ts:
            return
        if not self._state.thinking_card_id:
            return
        try:
            from slack_sdk.models.messages.chunk import TaskUpdateChunk
        except ImportError:
            return
        st = self._state
        delta = st.thinking_buffer[len(st.thinking_sent):]
        room = _THINKING_CARD_CAP - len(st.thinking_sent)
        if st.thinking_capped:
            delta = ""
        elif len(delta) > room:
            delta = delta[: max(0, room)].rstrip() + " …"
            st.thinking_capped = True
        # Skip pure status-mutations only when there's no content AND
        # we're still in_progress — they'd be no-op churn. Always
        # send the final "complete" status so the card resolves.
        if not delta and status == "in_progress":
            self._state.last_thinking_flush_ms = time.monotonic() * 1000
            return
        # Split the delta into appendStream-sized pieces. Each piece
        # carries an in_progress status; only the LAST piece carries
        # the requested status (so a "complete" final flush actually
        # marks the card complete).
        pieces = (
            [delta[i : i + _THINKING_DELTA_CAP] for i in range(0, len(delta), _THINKING_DELTA_CAP)]
            if delta
            else [""]
        )
        for idx, piece in enumerate(pieces):
            is_last = idx == len(pieces) - 1
            chunk = TaskUpdateChunk(
                id=self._state.thinking_card_id,
                title="Thinking",
                status=status if is_last else "in_progress",
                output=piece or None,
            )
            result = await self._stream_append_locked(
                [chunk], chars=len("Thinking") + len(piece),
            )
            if not result.ok:
                logger.debug("[slack] thinking appendStream failed: %s", result.error)
                break
        self._state.thinking_sent = self._state.thinking_buffer
        self._state.last_thinking_flush_ms = time.monotonic() * 1000

    async def _close_thinking_card(self) -> None:
        """Finalize the open Thinking card (status=complete) so the
        next thinking phase starts a new card."""
        async with self._lock:
            await self._close_thinking_card_locked()

    async def _close_thinking_card_locked(self) -> None:
        if not self._state.thinking_card_id:
            return
        await self._flush_thinking_locked(status="complete")
        self._state.thinking_card_id = None
        self._state.thinking_buffer = ""
        self._state.thinking_sent = ""
        self._state.thinking_capped = False

    # ── tool cards ──

    async def _handle_tool_input_end(self, event: dict[str, Any]) -> None:
        """Emit a TaskUpdateChunk(status=in_progress) for the tool whose
        args just landed. The completion chunk fires later on
        tool_result."""
        tool_id = str(event.get("id") or "")
        if tool_id in self._state.suppressed_tool_ids:
            return
        args = event.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        tool_name = self._state.pending_tool_names.get(tool_id, "?")
        # A call_tool call is shown as the tool it runs. Keep that name for
        # the result card too.
        tool_name, args = _unwrap_call_tool(tool_name, args, str(event.get("toolName") or ""))
        if tool_id in self._state.pending_tool_names:
            self._state.pending_tool_names[tool_id] = tool_name

        # Heartbeat suppression — pure noise in chat.
        if self._is_heartbeat(tool_name, args):
            # Drop the entry from pending tracking so tool_result
            # doesn't render a completion card for it either.
            self._state.pending_tool_names.pop(tool_id, None)
            self._state.suppressed_tool_ids.add(tool_id)
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
        # Tool output displayed inside the expanded card. Short: it is
        # paid for out of the message's 12k cap, and the desktop app has
        # the whole thing. An error gets more room than a success.
        cap = _TOOL_ERROR_OUTPUT_CAP if is_error else _TOOL_OUTPUT_CAP
        output_text = _short(preview, cap) if preview else None
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
            if self._state.cards_disabled or not self._state.stream_ts:
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
            result = await self._stream_append_locked(
                [chunk], chars=len(title) + len(output or ""),
            )
            # Count only the FIRST time a card opens (in_progress) so
            # the in_progress → complete transition on the same card
            # doesn't double-count. The counter drives phase-close
            # detection on the next text_delta.
            if status == "in_progress":
                self._state.cards_emitted_this_phase += 1
                if result.ok:
                    self._state.inflight_cards[card_id] = title
            else:
                self._state.inflight_cards.pop(card_id, None)
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
        # with extra glyphs. A simple "name: preview" reads cleanly, and
        # one line is all Slack shows of it.
        return _short(f"{name}: {preview}", _TOOL_TITLE_CAP)

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

    @property
    def finalized(self) -> bool:
        return self._state.finalized

    def abort(self, *, stopped_by_user: bool = False) -> None:
        """Neutralize an orphaned or cancelled consumer WITHOUT delivering
        its unsent body.

        finalize() only runs on turn_complete. A turn that dies before
        emitting it (provider init failure, cancellation) leaves its
        consumer registered forever, and every event of every later turn
        then fans out to BOTH consumers. The gateway calls this when it
        tears a stale consumer down at the next turn's registration, and
        when the operator presses Stop.

        Sets the finalized gate synchronously (on_event drops everything
        from here on) and best-effort seals any open stream and sets the
        session ``active``. With ``stopped_by_user`` Slack has already
        sealed the streams and asked for no further calls, so nothing is
        sent.
        """
        if self._state.finalized:
            return
        self._state.finalized = True
        self._state.stopped_by_user = stopped_by_user
        task = self._state.text_flush_task
        if task is not None and not task.done():
            task.cancel()
        if stopped_by_user:
            return

        async def _seal() -> None:
            if self._state.stream_ts and not self._state.cards_disabled:
                try:
                    await self.adapter.stop_stream(
                        self.source.chat_id, self._state.stream_ts,
                        metadata=self._metadata(), session_status="active",
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("[slack] abort stop_stream failed", exc_info=True)
            else:
                await self._set_status("active")

        try:
            asyncio.run_coroutine_threadsafe(_seal(), self._loop)
        except RuntimeError:
            pass

    async def finalize(self) -> None:
        """Seal the turn's message: tail prose, images, feedback, status."""
        async with self._lock:
            if self._state.finalized:
                return
            self._state.finalized = True
            task = self._state.text_flush_task
            if task is not None and not task.done():
                task.cancel()

        # Close any open thinking card.
        await self._close_thinking_card()

        # Build Block Kit image blocks for images whose unshared
        # upload succeeded during the turn — these render INLINE at
        # the bottom of the streaming message via stop_stream(blocks=).
        inline_blocks = self._build_inline_image_blocks()

        st = self._state
        sealed = False
        unsent = ""
        if st.stream_ts and not st.cards_disabled:
            async with self._lock:
                # Everything but the last piece goes out as appends; the
                # last piece rides on stopStream so the seal and the tail
                # are one call. The tail has to fit the message too.
                tail_len = min(len(st.body_buffer) - st.body_sent, _TEXT_PIECE_CAP)
                await self._flush_text_locked(keep_tail=max(0, tail_len))
                tail_len = len(st.body_buffer) - st.body_sent
                if (
                    tail_len > 0
                    and st.stream_chars > 0
                    and not st.cards_disabled
                    and st.stream_chars + tail_len + _CHUNK_OVERHEAD > _STREAM_BUDGET
                ):
                    await self._rollover_locked()
            tail = st.body_buffer[st.body_sent :] if not st.cards_disabled else ""
            chunks: list[Any] | None = None
            if tail and not st.cards_disabled:
                try:
                    from slack_sdk.models.messages.chunk import MarkdownTextChunk
                    chunks = [MarkdownTextChunk(text=tail)]
                except ImportError:
                    chunks = None
            if not st.cards_disabled:
                has_content = bool(st.body_buffer) or st.widgets_emitted > 0 or st.stream_count > 0
                blocks: list[dict[str, Any]] = list(inline_blocks)
                if self.feedback_enabled and has_content:
                    from bridge.gateway import slack_blocks as _blocks
                    blocks.append(_blocks.feedback_block(self.session_key, str(self.source.message_id or "")))
                stop_result = await self.adapter.stop_stream(
                    self.source.chat_id,
                    st.stream_ts,
                    chunks=chunks,
                    blocks=blocks or None,
                    metadata=self._metadata(),
                    session_status="active",
                )
                if stop_result.ok:
                    sealed = True
                    if chunks:
                        st.body_sent = len(st.body_buffer)
                else:
                    logger.warning("[slack] chat.stopStream failed: %s", stop_result.error)
                    too_long = "msg_too_long" in (stop_result.error or "").lower()
                    if too_long and st.stream_chars > 0:
                        # The estimate was short. Continue in a fresh
                        # message and seal that one with the tail.
                        async with self._lock:
                            await self._rollover_locked()
                        if st.stream_ts and not st.cards_disabled:
                            retry = await self.adapter.stop_stream(
                                self.source.chat_id, st.stream_ts, chunks=chunks,
                                blocks=blocks or None, metadata=self._metadata(),
                                session_status="active",
                            )
                        else:
                            retry = stop_result
                    else:
                        # A malformed block can trip stopStream — retry
                        # with the tail only; images fall through to
                        # files_upload.
                        retry = await self.adapter.stop_stream(
                            self.source.chat_id, st.stream_ts, chunks=chunks,
                            metadata=self._metadata(), session_status="active",
                        )
                        inline_blocks = []
                    if retry.ok:
                        sealed = True
                        if chunks:
                            st.body_sent = len(st.body_buffer)
            unsent = st.body_buffer[st.body_sent :]
        else:
            unsent = st.body_buffer[st.body_sent :]

        # Fallback delivery: the stream never opened, died, or refused
        # the tail. One chat.postMessage with whatever Slack has not
        # shown yet. Cap at 10k chars; run.py already truncates
        # per-message context to 1500 chars so anything beyond that
        # is excess. Skip when there's no prose.
        final_text = unsent[:10_000].strip()
        if final_text:
            send_result = await self.adapter.send(
                self.source.chat_id,
                final_text,
                thread_id=self._reply_thread_id or None,
                raw_hint=self.raw_hint,
                metadata=self._metadata(),
            )
            if not send_result.ok:
                logger.warning(
                    "[slack] body postMessage failed: %s",
                    send_result.error,
                )

        # Fallback files_upload: covers images whose inline path
        # didn't reach Slack (unshared upload failed OR stop_stream
        # rejected the blocks). When inline_blocks is non-empty the
        # successfully-uploaded images already rendered, so we skip
        # those entries and only push the rest.
        await self._upload_pending_images(skip_with_file_id=bool(inline_blocks))

        # The session is idle. stopStream already said so when it
        # sealed; cover the paths that never sealed.
        if not sealed:
            await self._set_status("active")

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


def _unwrap_call_tool(
    name: str, args: dict[str, Any], declared: str = ""
) -> tuple[str, dict[str, Any]]:
    """The tool a ``call_tool`` call runs, with that tool's own arguments.

    ``call_tool`` runs a deferred tool by name, and a card that says
    "call_tool" tells the reader nothing. The first ``tool_input_end`` for the
    call carries ``{tool_name, arguments}``. The bridge then re-enters the tool
    path for the inner tool under the same id, and that event names the tool
    (``toolName``) and carries its args. Either one is enough to name the card.
    """
    if name != "call_tool":
        return name, args
    inner = args.get("tool_name")
    if isinstance(inner, str) and inner.strip():
        inner_args = args.get("arguments")
        if isinstance(inner_args, str):
            try:
                inner_args = json.loads(inner_args) if inner_args.strip() else {}
            except ValueError:
                inner_args = {}
        return inner.strip(), inner_args if isinstance(inner_args, dict) else {}
    if declared and declared != "call_tool":
        return declared, args
    return name, args


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


def _text_chars_in(value: Any) -> int:
    """Characters of text inside a Block Kit structure: every string value
    counts. An estimate on the high side of what Slack counts a blocks
    chunk for against the message cap."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_text_chars_in(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_text_chars_in(v) for v in value)
    return 0


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
