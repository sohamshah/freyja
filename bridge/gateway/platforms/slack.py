"""Freyja's Slack platform adapter.

Socket Mode via ``slack-bolt``. No public URL required, all connections
outbound — works on home networks, behind NAT, behind corporate
proxies. Mirrors the proven Hermes pattern but focused on what we
need for v1:

  · multi-workspace tokens (comma-split SLACK_BOT_TOKEN, plus optional
    slack_tokens.json file)
  · mention gating in channels (DMs always respond, channels need
    @mention or to be inside a thread the bot already participated in)
  · streaming responses via send-then-edit progressive updates
  · slash command routing with ephemeral acks
  · file download for inbound attachments
  · file upload for outbound images / docs
  · markdown → Slack mrkdwn conversion
  · Socket Mode reconnect handled by slack-bolt; we dedup events to
    avoid double-processing on reconnect

Out of scope for v1 (additions later as needed):
  · Block Kit approval buttons (we don't need them — the agent's
    interactivity is in DMs)
  · Voice messages
  · Slack Assistant AI Cards (typing status workaround)
  · HTTP proxy support
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    import aiohttp
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient
    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False
    aiohttp = None  # type: ignore
    AsyncApp = Any  # type: ignore
    AsyncSocketModeHandler = Any  # type: ignore
    AsyncWebClient = Any  # type: ignore

from bridge.gateway.config import GatewayConfig, SlackConfig
from bridge.gateway.pid import freyja_home
from bridge.gateway.platforms.base import (
    EventCallback,
    IncomingMessage,
    MessageSource,
    Platform,
    PlatformAdapter,
    SendResult,
    UploadItem,
    truncate_for_platform,
)
from bridge.gateway.platforms.slack_manifest import known_slash_command_names

logger = logging.getLogger(__name__)


SLACK_MAX_MESSAGE_LENGTH = 39_000   # Slack hard cap is 40k; leave room


# Map of standard markdown → Slack mrkdwn for the bits where they
# diverge. Slack supports a subset of CommonMark with non-standard
# emphasis markers (`*bold*` vs `**bold**`, `_italic_` vs `__italic__`).
_MD_TO_SLACK_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_TO_SLACK_ITAL = re.compile(r"__([^_\n]+?)__")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def extract_text_from_slack_blocks(blocks: list[dict[str, Any]] | None) -> str:
    """Walk Slack's Block Kit ``blocks`` JSON and reconstruct markdown.

    Slack's modern composer sends rich text as a nested ``blocks``
    tree. The raw ``text`` field is lossy — quoted messages, ordered
    lists, and code blocks degrade to plain paragraphs or vanish.
    Reading the structured tree preserves them so the agent gets the
    fidelity the user actually typed.

    Mirrors the Hermes implementation. Outputs:
      · ``rich_text_section`` → inline text (concat children)
      · ``rich_text_quote``   → ``> `` prefix per nesting level
      · ``rich_text_list``    → ``• `` (bullet) or ``1. 2. 3.`` (ordered)
      · ``rich_text_preformatted`` → fenced ``` code blocks
      · plain ``section`` blocks with ``text.text`` → as-is

    Returns "" if blocks is empty / None — the caller should fall
    back to the message's raw ``text`` field in that case.
    """
    if not blocks:
        return ""
    out_lines: list[str] = []

    def render_elements(els: list[dict[str, Any]] | None) -> str:
        if not els:
            return ""
        parts: list[str] = []
        for el in els:
            t = el.get("type")
            if t == "text":
                parts.append(str(el.get("text") or ""))
            elif t == "link":
                url = str(el.get("url") or "")
                label = str(el.get("text") or url)
                parts.append(f"<{url}|{label}>" if label != url else url)
            elif t == "emoji":
                name = str(el.get("name") or "")
                parts.append(f":{name}:" if name else "")
            elif t == "user":
                uid = str(el.get("user_id") or "")
                parts.append(f"<@{uid}>" if uid else "")
            elif t == "channel":
                cid = str(el.get("channel_id") or "")
                parts.append(f"<#{cid}>" if cid else "")
            elif t == "broadcast":
                rng = str(el.get("range") or "channel")
                parts.append(f"<!{rng}>")
            else:
                # Unknown element type — try to extract a text field.
                inner = el.get("text")
                if isinstance(inner, str):
                    parts.append(inner)
        return "".join(parts)

    def walk_rich_text(block: dict[str, Any], quote_depth: int = 0) -> None:
        for inner in block.get("elements") or []:
            t = inner.get("type")
            prefix = ">" * quote_depth + (" " if quote_depth else "")
            if t == "rich_text_section":
                line = render_elements(inner.get("elements"))
                if line:
                    out_lines.append(prefix + line)
            elif t == "rich_text_quote":
                walk_rich_text(inner, quote_depth=quote_depth + 1)
            elif t == "rich_text_list":
                style = inner.get("style") or "bullet"
                ordered = style == "ordered"
                for idx, item in enumerate(inner.get("elements") or [], start=1):
                    line = render_elements(item.get("elements"))
                    bullet = f"{idx}. " if ordered else "• "
                    if line:
                        out_lines.append(prefix + bullet + line)
            elif t == "rich_text_preformatted":
                code = render_elements(inner.get("elements"))
                if code:
                    out_lines.append("```")
                    for ln in code.splitlines() or [""]:
                        out_lines.append(ln)
                    out_lines.append("```")

    for block in blocks:
        btype = block.get("type")
        if btype == "rich_text":
            walk_rich_text(block)
        elif btype == "section":
            txt = (block.get("text") or {}).get("text")
            if isinstance(txt, str) and txt:
                out_lines.append(txt)
    return "\n".join(out_lines).strip()


def _markdown_to_slack(content: str) -> str:
    """Convert common CommonMark idioms to Slack mrkdwn.

    Slack uses:
      *bold*        instead of **bold**
      _italic_      instead of __italic__ (single underscores already work)
      <url|text>    instead of [text](url)
      `inline`      same
      ```block```   same
    """
    # Replace **bold** with *bold*. Iterate from inside out by
    # operating on non-greedy matches.
    content = _MD_TO_SLACK_BOLD.sub(r"*\1*", content)
    content = _MD_TO_SLACK_ITAL.sub(r"_\1_", content)
    content = _MD_LINK.sub(r"<\2|\1>", content)
    return content


def _safe_token_preview(token: str) -> str:
    if not token:
        return "(empty)"
    if len(token) < 16:
        return f"{token[:4]}…"
    return f"{token[:6]}…{token[-4:]}"


class SlackAdapter:
    """Slack platform adapter. Owns one ``slack-bolt`` AsyncApp +
    Socket Mode handler + N AsyncWebClient instances (one per
    authenticated workspace)."""

    name = Platform.SLACK.value

    # Per the PlatformAdapter contract: hard cap on a single
    # outbound message in formatted form. The consumer must chunk
    # below this before calling send/edit.
    max_message_chars = SLACK_MAX_MESSAGE_LENGTH

    def format_content(self, content: str) -> str:
        """CommonMark → Slack mrkdwn. Idempotent (re-running on
        already-converted text leaves it unchanged), so it's safe
        for ``send``/``edit`` to call as a defensive last pass on
        whatever the caller hands them."""
        return _markdown_to_slack(content)

    # Cap to prevent unbounded growth of tracking sets — old entries
    # get evicted when we hit these limits.
    _BOT_TS_MAX = 5000
    _MENTIONED_THREADS_MAX = 1000
    _DEDUP_MAX = 5000
    # TTLs in seconds. Past these, entries are considered stale and
    # safe to drop. Dedup uses the platform's redelivery window —
    # Slack's Socket Mode replays at most ~1 minute back on
    # reconnect, so 10 min is generous. Slash contexts use Slack's
    # response_url expiration (30 min); past that the stash is
    # useless anyway.
    _DEDUP_TTL_SEC = 600
    _SLASH_CTX_TTL_SEC = 1800
    # How long we wait after an app_mention event before processing it,
    # to give a possibly-richer message.channels twin (with file
    # attachments) a chance to arrive. Slack delivers the pair within
    # a few tens of ms; 300ms is comfortable headroom. The cost of
    # waiting is a small latency on bare @mentions (no twin arrives,
    # we process after the timeout) — barely noticeable next to the
    # multi-second LLM response time that follows.
    _APP_MENTION_DEFER_MS = 300

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        slack_config: SlackConfig | None = None,
    ) -> None:
        self.config = config or {}
        # Per-workspace allowlist + behavioral toggles. If not provided,
        # load from ~/.freyja/gateway.yaml.
        self.slack_config: SlackConfig = (
            slack_config if slack_config is not None
            else GatewayConfig.load().slack
        )

        self._on_event: EventCallback | None = None
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._socket_task: asyncio.Task | None = None
        self._running = False

        # Per-workspace state.
        self._team_clients: dict[str, AsyncWebClient] = {}
        self._team_bot_user_ids: dict[str, str] = {}
        self._channel_team: dict[str, str] = {}      # channel_id → team_id
        self._bot_user_id: str | None = None          # first workspace's bot

        # Tracking for mention-gating heuristics.
        self._bot_message_ts: set[str] = set()        # ts values we sent
        self._mentioned_threads: set[str] = set()     # thread_ts the bot joined
        self._dedup: dict[str, float] = {}            # event_ts → seen-at monotonic
        # Deferred app_mention tasks awaiting a possible message.channels
        # twin. When a user @mentions the bot in a channel WITH a file
        # attachment, Slack delivers two events: app_mention (no files)
        # and message.channels (with files). We defer app_mention by
        # DEDUP_DEFER_MS so message.channels can cancel it and provide
        # the rich payload. Keyed by event_ts so we can find the right
        # task to cancel when the twin arrives.
        self._pending_app_mention: dict[str, asyncio.Task[Any]] = {}

        # Slash command response_url stash. Multiple users can run a
        # slash concurrently in the same channel; we key by
        # (chat_id, user_id) so the response routes correctly.
        self._slash_contexts: dict[tuple[str, str], dict[str, Any]] = {}

    # ── lifecycle ──────────────────────────────────────────────

    async def connect(self, on_event: EventCallback) -> bool:
        """Authenticate with all configured workspaces + start Socket Mode."""
        if not _SLACK_AVAILABLE:
            logger.error(
                "slack-bolt not installed — run `uv pip install slack-bolt slack-sdk`"
            )
            return False

        self._on_event = on_event

        bot_token_raw = (
            self.config.get("bot_token")
            or os.environ.get("SLACK_BOT_TOKEN")
            or ""
        )
        app_token = (
            self.config.get("app_token")
            or os.environ.get("SLACK_APP_TOKEN")
            or ""
        )

        if not bot_token_raw:
            logger.error(
                "[slack] SLACK_BOT_TOKEN not set — run `freyja setup slack`"
            )
            return False
        if not app_token:
            logger.error(
                "[slack] SLACK_APP_TOKEN not set — Socket Mode requires it"
            )
            return False
        if not app_token.startswith("xapp-"):
            logger.error("[slack] SLACK_APP_TOKEN must start with xapp-")
            return False

        # Comma-split + dedup bot tokens (multi-workspace support).
        bot_tokens = [t.strip() for t in bot_token_raw.split(",") if t.strip()]

        # Merge tokens from ~/.freyja/slack_tokens.json if present (for
        # OAuth-discovered workspaces, future-proofing the path).
        tokens_file = freyja_home() / "slack_tokens.json"
        if tokens_file.exists():
            try:
                saved = json.loads(tokens_file.read_text(encoding="utf-8"))
                for team_id, entry in saved.items():
                    tok = entry.get("token") if isinstance(entry, dict) else None
                    if isinstance(tok, str) and tok and tok not in bot_tokens:
                        bot_tokens.append(tok)
                        team_label = (
                            entry.get("team_name") if isinstance(entry, dict) else None
                        ) or team_id
                        logger.info(
                            "[slack] loaded saved token for workspace %s",
                            team_label,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[slack] could not parse %s: %s", tokens_file, exc)

        # Close any prior handler (defensive — connect() can re-run on
        # reconnect / config reload paths).
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:  # noqa: BLE001
                pass
            self._handler = None
            self._app = None

        try:
            primary_token = bot_tokens[0]
            self._app = AsyncApp(token=primary_token)

            # Authenticate each token + capture team_id mapping.
            for token in bot_tokens:
                client = AsyncWebClient(token=token)
                try:
                    auth = await client.auth_test()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "[slack] auth_test failed for token %s: %s",
                        _safe_token_preview(token),
                        exc,
                    )
                    return False
                if not auth.get("ok"):
                    logger.error(
                        "[slack] auth_test rejected for token %s: %s",
                        _safe_token_preview(token),
                        auth.get("error"),
                    )
                    return False
                team_id = auth.get("team_id", "")
                bot_user_id = auth.get("user_id", "")
                team_name = auth.get("team", "?")
                bot_name = auth.get("user", "?")
                self._team_clients[team_id] = client
                self._team_bot_user_ids[team_id] = bot_user_id
                if self._bot_user_id is None:
                    self._bot_user_id = bot_user_id
                logger.info(
                    "[slack] authenticated as @%s in workspace %s (team %s)",
                    bot_name, team_name, team_id,
                )

            self._register_event_handlers()

            self._handler = AsyncSocketModeHandler(self._app, app_token)
            self._socket_task = asyncio.create_task(
                self._handler.start_async(),
                name="slack-socket-mode",
            )
            self._running = True
            logger.info(
                "[slack] Socket Mode handler started (%d workspace(s))",
                len(self._team_clients),
            )
            return True

        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[slack] handler close error: %s", exc)
            self._handler = None
        if self._socket_task is not None and not self._socket_task.done():
            self._socket_task.cancel()
            try:
                await self._socket_task
            except (asyncio.CancelledError, Exception):
                pass
            self._socket_task = None
        self._app = None
        self._team_clients.clear()
        self._team_bot_user_ids.clear()
        self._channel_team.clear()
        self._bot_user_id = None

    # ── event handler registration ────────────────────────────

    def _register_event_handlers(self) -> None:
        assert self._app is not None

        @self._app.event("message")
        async def _handle_message_event(event, say):  # noqa: ARG001
            # If an app_mention task is pending for this same event_ts,
            # cancel it — the message.channels event we just got is
            # strictly richer (carries the files array, full subtype
            # info, etc.) and is the authoritative delivery to use.
            event_ts = str(event.get("ts") or "")
            pending = self._pending_app_mention.pop(event_ts, None)
            if pending is not None and not pending.done():
                pending.cancel()
            await self._handle_message(event)

        @self._app.event("app_mention")
        async def _handle_app_mention(event, say):  # noqa: ARG001
            # Defer processing by a short window so any twin
            # message.channels event (which carries files) can
            # cancel us and handle the inbound itself. The
            # message-event handler above does the cancellation.
            #
            # If no twin arrives (some workspace configurations
            # deliver mentions ONLY as app_mention, or a thread
            # broadcast might fire just one of the two), the
            # deferred task processes the app_mention normally
            # after the window expires.
            event_ts = str(event.get("ts") or "")
            if not event_ts:
                await self._handle_message(event)
                return
            # If a deferred task is already in flight for this ts
            # (duplicate Socket Mode delivery during a reconnect
            # window), drop the redundant defer.
            if event_ts in self._pending_app_mention:
                return
            self._pending_app_mention[event_ts] = asyncio.create_task(
                self._handle_app_mention_after_defer(event_ts, event)
            )

        # Ack file lifecycle events so Slack doesn't log unhandled-event
        # warnings. The actual files we care about arrive as part of a
        # parent message event.
        @self._app.event("file_shared")
        async def _ack_file_shared(event, say):  # noqa: ARG001
            pass

        @self._app.event("file_created")
        async def _ack_file_created(event, say):  # noqa: ARG001
            pass

        @self._app.event("file_change")
        async def _ack_file_change(event, say):  # noqa: ARG001
            pass

        # Slack Assistant lifecycle events — just ack for now. Future:
        # we could use these to surface a typing-equivalent status.
        @self._app.event("assistant_thread_started")
        async def _ack_assistant_started(event, say):  # noqa: ARG001
            pass

        @self._app.event("assistant_thread_context_changed")
        async def _ack_assistant_changed(event, say):  # noqa: ARG001
            pass

        # Slash command dispatch: register one regex that matches every
        # known slash so the manifest's set is the source of truth.
        slash_names = [s.lstrip("/") for s in known_slash_command_names()]
        if slash_names:
            slash_pattern = re.compile(
                r"^/(?:" + "|".join(re.escape(n) for n in slash_names) + r")$"
            )
        else:
            slash_pattern = re.compile(r"^/freyja$")

        @self._app.command(slash_pattern)
        async def _handle_slash(ack, command):
            cmd = (command.get("command") or "").lstrip("/")
            await ack(
                response_type="ephemeral",
                text=f"Running `/{cmd}`…",
            )
            await self._handle_slash_command(command)

        # Interactive Block Kit button handlers for the destructive-
        # command approval prompt. Both handlers atomically resolve
        # the pending Future via ``approval.resolve_approval`` (first
        # call wins — subsequent double-clicks are no-ops).
        @self._app.action("freyja_approve")
        async def _on_approve(ack, body, respond):
            await ack()
            await self._handle_approval_click(body, respond, approved=True)

        @self._app.action("freyja_deny")
        async def _on_deny(ack, body, respond):
            await ack()
            await self._handle_approval_click(body, respond, approved=False)

    async def _handle_approval_click(
        self,
        body: dict[str, Any],
        respond: Any,
        *,
        approved: bool,
    ) -> None:
        actions = body.get("actions") or []
        request_id = ""
        if actions:
            request_id = str(actions[0].get("value") or "")
        user_id = str((body.get("user") or {}).get("id") or "")
        from bridge.gateway.approval import resolve_approval
        # Resolve the pending Future. If the request is unknown or
        # already resolved (operator double-clicked), bail without
        # touching the message.
        if not request_id or not resolve_approval(request_id, approved):
            logger.debug(
                "approval click for unknown/resolved request %s — ignoring",
                request_id,
            )
            return
        # Replace the buttons with a static confirmation block so the
        # operator can't click again and we leave an audit trail.
        verb = "approved" if approved else "denied"
        icon = "✅" if approved else "❌"
        try:
            await respond(
                replace_original=True,
                text=f"{verb} by <@{user_id}>",
                blocks=[
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"{icon} *{verb}* by <@{user_id}>",
                            }
                        ],
                    }
                ],
            )
        except Exception:  # noqa: BLE001
            logger.exception("approval respond failed")

    # ── inbound message processing ─────────────────────────────

    async def _handle_app_mention_after_defer(
        self,
        event_ts: str,
        event: dict[str, Any],
    ) -> None:
        """Process an app_mention after a brief delay.

        The delay window gives a possible message.channels twin
        (carrying file attachments Slack strips from app_mention
        events) a chance to arrive and cancel us. If we get past
        the sleep, no twin showed up — process normally.

        Cleanup of the ``_pending_app_mention`` slot happens here too
        so a follow-up cancellation can't race against a stale entry.
        """
        try:
            await asyncio.sleep(self._APP_MENTION_DEFER_MS / 1000.0)
        except asyncio.CancelledError:
            # message.channels arrived and cancelled us; the message
            # handler is processing the rich version, nothing to do.
            self._pending_app_mention.pop(event_ts, None)
            return
        # Clear our slot BEFORE handing off, so a later cancel during
        # the (possibly slow) handle_message call can't accidentally
        # cancel an unrelated future task that reused this ts.
        self._pending_app_mention.pop(event_ts, None)
        try:
            await self._handle_message(event)
        except Exception:  # noqa: BLE001
            logger.exception("deferred app_mention handler raised")

    async def _handle_message(self, event: dict[str, Any]) -> None:
        # Dedup: Socket Mode can redeliver after reconnect, AND Slack
        # double-fires @mentions in channels as both ``app_mention`` and
        # ``message.channels`` with the same ts. We can't just drop the
        # second event in the latter case: ``app_mention`` payloads omit
        # the ``files`` array, while ``message.channels`` includes it.
        # Naive dedup → text-only ``app_mention`` wins, attached images
        # never make it to the agent ("No image came through" responses).
        #
        # Strategy: the dedup key bakes in a coarse "richness" tag
        # (whether the event carries files). Two events with the same
        # ts but different richness pass through as separate
        # invocations, and the agent gets the richer one's content.
        # The downside is we may call the agent twice for a mention
        # with files — but the second call sees the prior call as
        # in-flight via the session's queueing mechanism, so it
        # queues behind it rather than racing.
        event_ts = str(event.get("ts") or "")
        has_files = bool(event.get("files"))
        dedup_key = f"{event_ts}|f" if has_files else event_ts
        if dedup_key and dedup_key in self._dedup:
            return
        if dedup_key:
            now = time.monotonic()
            self._dedup[dedup_key] = now
            # TTL-evict periodically rather than on every message.
            # The common case is O(1) insert; the prune sweep runs
            # only when we cross the size cap, which on a busy
            # channel happens every few thousand messages.
            if len(self._dedup) > self._DEDUP_MAX:
                cutoff = now - self._DEDUP_TTL_SEC
                self._dedup = {
                    k: v for k, v in self._dedup.items() if v >= cutoff
                }
                if len(self._dedup) > self._DEDUP_MAX:
                    # Still oversize after TTL drop (extreme volume) —
                    # fall back to oldest-half eviction so we always
                    # bound memory.
                    items = sorted(self._dedup.items(), key=lambda kv: kv[1])
                    self._dedup = dict(items[len(items) // 2 :])

        # Filter bot messages — never respond to ourselves (echo loop).
        # MUST check against the bot's user_id in EVERY workspace, not
        # just the primary one. In a multi-workspace install the
        # secondary workspace's bot has a different user_id, and a
        # naive ``user_id == self._bot_user_id`` check lets the bot's
        # own messages from that workspace flow through inbound
        # processing, triggering a response → echo loop.
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")
        user_id = event.get("user", "")
        if bot_id or subtype == "bot_message":
            known_bot_ids = set(self._team_bot_user_ids.values())
            if self._bot_user_id:
                known_bot_ids.add(self._bot_user_id)
            if user_id in known_bot_ids:
                return
        # Skip non-content messages (edits, deletes).
        if subtype in {"message_changed", "message_deleted"}:
            return

        # Prefer the structured ``blocks`` tree over the raw ``text``
        # field when both are present. Slack's modern WYSIWYG composer
        # writes rich content (quotes, lists, code) into blocks; the
        # ``text`` field is a lossy fallback that omits or flattens
        # quoted/forwarded content. If parsing yields something
        # non-trivial we use it; otherwise we fall back to ``text``.
        text = (event.get("text") or "").strip()
        blocks_text = extract_text_from_slack_blocks(event.get("blocks"))
        if blocks_text and len(blocks_text) > len(text):
            # blocks fully covers what's in text (and then some) —
            # prefer the richer rendering.
            text = blocks_text
        channel_id = event.get("channel") or ""
        ts = event_ts
        team_id = event.get("team") or event.get("team_id") or ""

        if not channel_id:
            return

        # Resolve missing team_id. Slack omits it on some Socket Mode
        # payloads (thread broadcasts, edits, file_share subtypes).
        # Three fallback tiers:
        #   1. channel→team cache populated from prior events on the
        #      same channel that DID include team
        #   2. for single-workspace installs (the common case), default
        #      to the only authenticated team — there's no ambiguity
        #      about where the message came from
        #   3. give up, allowlist will deny
        # Without (2), the FIRST message in a channel after a daemon
        # restart silently fails because the channel cache is empty
        # AND the event payload doesn't carry team. The user-visible
        # symptom is "the bot won't respond in this thread, but if I
        # start a fresh @mention thread it works" — because the new
        # top-level mention's event happens to carry team_id, which
        # populates the cache for subsequent thread replies.
        if not team_id:
            cached = self._channel_team.get(channel_id, "")
            if cached:
                team_id = cached
            elif len(self._team_clients) == 1:
                team_id = next(iter(self._team_clients.keys()))

        if team_id and channel_id:
            self._channel_team[channel_id] = team_id

        # Workspace/user allowlist enforcement. When the operator
        # configures gateway.yaml's `slack.allowed_user_ids`, we deny
        # everyone outside the allowlist before routing further.
        # Unattended Slack with no allowlist is footgun-grade — we
        # refuse to process anything from a workspace not explicitly
        # opted in.
        if not self.slack_config.user_allowed(team_id, user_id):
            logger.info(
                "[slack] denying message from team=%s user=%s — not in allowlist",
                team_id, user_id,
            )
            return

        # Diagnostic: if Slack delivered a message with no files but it
        # was a file-share-style event (subtype hints at it OR an upload
        # is in flight), log so we can tell whether the issue is "Slack
        # didn't send us files" vs "we mishandled files we received."
        # Helps debug the "user sends an image, bot says no image came
        # through" failure mode.
        files_in_event = event.get("files") or []
        if not files_in_event and subtype in {"file_share", "file_comment"}:
            logger.warning(
                "[slack] event ts=%s has subtype=%s but no files array — "
                "likely missing files:read scope OR file upload still in "
                "progress. Reinstall the app in the workspace if scope is "
                "absent.",
                event_ts, subtype,
            )
        elif files_in_event:
            logger.info(
                "[slack] event ts=%s carries %d file(s): %s",
                event_ts, len(files_in_event),
                [f.get("mimetype") or f.get("name") for f in files_in_event[:3]],
            )

        # DM vs channel detection.
        channel_type = event.get("channel_type") or ""
        if not channel_type and channel_id.startswith("D"):
            channel_type = "im"
        is_dm = channel_type in {"im", "mpim"}

        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
        event_thread_ts = event.get("thread_ts")
        is_thread_reply = bool(event_thread_ts and event_thread_ts != ts)
        is_mentioned = bool(bot_uid and f"<@{bot_uid}>" in text)

        # Mention gating in channels: respond if mentioned OR if inside
        # a thread the bot was already mentioned in OR if replying to a
        # message the bot sent.
        if not is_dm and bot_uid:
            if not is_mentioned:
                reply_to_bot_thread = (
                    is_thread_reply and event_thread_ts in self._bot_message_ts
                )
                in_mentioned_thread = (
                    event_thread_ts is not None
                    and event_thread_ts in self._mentioned_threads
                )
                if not reply_to_bot_thread and not in_mentioned_thread:
                    return

        # Strip the bot mention from the text + remember the thread so
        # future thread messages auto-respond without re-mention.
        if is_mentioned and bot_uid:
            text = text.replace(f"<@{bot_uid}>", "").strip()
            if event_thread_ts:
                self._mentioned_threads.add(event_thread_ts)
                if len(self._mentioned_threads) > self._MENTIONED_THREADS_MAX:
                    excess = list(self._mentioned_threads)[
                        : self._MENTIONED_THREADS_MAX // 2
                    ]
                    for t in excess:
                        self._mentioned_threads.discard(t)

        # Compute thread_id used as part of the session key. For DMs +
        # channel mentions outside of a thread, we use ts so each
        # top-level interaction gets its own session. For thread
        # replies, use the thread root.
        if event_thread_ts:
            thread_for_key = event_thread_ts
        elif not is_dm:
            thread_for_key = ts   # channel mention → its own thread/session
        else:
            thread_for_key = None  # DM top-level → workspace-DM session

        # Download any attached files.
        attachments: list[dict[str, Any]] = []
        files = event.get("files") or []
        if files and self._team_clients:
            client = self._get_client(channel_id, team_id)
            client_token = self._token_for_client(client) if client else None
            if client_token:
                for file_obj in files:
                    saved = await self._download_file(file_obj, client_token)
                    if saved:
                        attachments.append(saved)

        # Resolve user display name (best-effort).
        user_name = await self._resolve_user_name(user_id, team_id) if user_id else None

        chat_type = "dm" if is_dm else "channel"
        source = MessageSource(
            platform=Platform.SLACK,
            workspace_id=team_id or "_unknown",
            chat_type=chat_type,
            chat_id=channel_id,
            user_id=user_id or None,
            user_name=user_name,
            thread_id=thread_for_key,
            message_id=ts,
            is_bot=bool(bot_id),
        )

        message = IncomingMessage(
            source=source,
            text=text,
            attachments=attachments,
            received_at=time.time(),
        )

        if self._on_event is not None:
            try:
                await self._on_event(message)
            except Exception:  # noqa: BLE001
                logger.exception("[slack] on_event callback raised")

    async def _handle_slash_command(self, command: dict[str, Any]) -> None:
        cmd = (command.get("command") or "").lstrip("/")
        args_text = (command.get("text") or "").strip()
        channel_id = command.get("channel_id") or ""
        user_id = command.get("user_id") or ""
        team_id = command.get("team_id") or ""
        response_url = command.get("response_url") or ""
        trigger_id = command.get("trigger_id") or ""

        if not channel_id or not cmd:
            return

        # Same allowlist gate as inbound messages — slash commands are
        # equally privileged. Reject before routing.
        if not self.slack_config.user_allowed(team_id, user_id):
            logger.info(
                "[slack] denying /%s from team=%s user=%s — not in allowlist",
                cmd, team_id, user_id,
            )
            return

        # Stash the response_url so the next send() routes ephemerally
        # to this user (matches Hermes pattern, prevents leaking slash
        # responses to the rest of a channel).
        if response_url and user_id:
            now = time.monotonic()
            # Sweep stale stashes (>30 min — response_url has expired
            # by then anyway). Cheap because the dict only holds
            # in-flight slashes, not the full message history.
            cutoff = now - self._SLASH_CTX_TTL_SEC
            stale_keys = [
                k for k, v in self._slash_contexts.items()
                if v.get("stashed_at", 0) < cutoff
            ]
            for k in stale_keys:
                self._slash_contexts.pop(k, None)
            self._slash_contexts[(channel_id, user_id)] = {
                "response_url": response_url,
                "trigger_id": trigger_id,
                "command": cmd,
                "stashed_at": now,
            }

        # Resolve user display name.
        user_name = await self._resolve_user_name(user_id, team_id) if user_id else None

        # Slash commands are always treated as DMs in terms of
        # routing — we always respond to the issuer specifically.
        # Session key keeps the workspace + user separation correct.
        chat_type = "dm" if channel_id.startswith("D") else "channel"

        source = MessageSource(
            platform=Platform.SLACK,
            workspace_id=team_id or "_unknown",
            chat_type=chat_type,
            chat_id=channel_id,
            user_id=user_id or None,
            user_name=user_name,
            thread_id=None,
            message_id=None,
        )

        message = IncomingMessage(
            source=source,
            text=f"/{cmd} {args_text}".strip(),
            received_at=time.time(),
            is_slash_command=True,
            slash_command_name=cmd,
            slash_command_args=args_text,
            raw={"response_url": response_url},
        )

        if self._on_event is not None:
            try:
                await self._on_event(message)
            except Exception:  # noqa: BLE001
                logger.exception("[slack] slash on_event raised")

    # ── outbound: send / edit / upload ────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        ephemeral_user_id: str | None = None,
        raw_hint: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self._app:
            return SendResult(ok=False, error="not connected")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")

        # If this came from a slash command, use the response_url stash
        # to deliver as an ephemeral message replacing the "Running…" ack.
        slash_ctx = self._pop_slash_context_for(chat_id, ephemeral_user_id, raw_hint)
        if slash_ctx:
            return await self._send_slash_ephemeral(slash_ctx, content)

        # Format defensively — idempotent, so callers that already
        # formatted (the stream consumer's chunking path) pass through
        # unchanged. Callers that hand us raw markdown (slash replies,
        # short status posts in run.py) get correct conversion.
        formatted = self.format_content(content)
        # Strict single-message contract: a successful send maps to
        # EXACTLY ONE Slack message. If the formatted content is too
        # large, we return an error instead of silently splitting; the
        # old split-loop only recorded the last chunk's ts as the
        # SendResult.message_id, which made the stream consumer's
        # anchor list track only the trailing message — every leading
        # message it posted became an orphan that the consumer
        # couldn't edit on later flushes, producing the visible
        # duplication on long responses. Callers that need to send
        # more than one message worth of content must chunk first
        # (use ``format_content`` + ``truncate_for_platform`` with
        # ``max_message_chars``).
        if len(formatted) > self.max_message_chars:
            return SendResult(
                ok=False,
                error=(
                    f"content exceeds max_message_chars "
                    f"({len(formatted)} > {self.max_message_chars}); "
                    f"caller must chunk before calling send"
                ),
            )
        kwargs: dict[str, Any] = {
            "channel": chat_id,
            "text": formatted,
            "mrkdwn": True,
        }
        if thread_id:
            kwargs["thread_ts"] = thread_id
        try:
            result = await client.chat_postMessage(**kwargs)
            sent_ts = (result or {}).get("ts") if result else None
            if sent_ts:
                self._bot_message_ts.add(str(sent_ts))
                if thread_id:
                    self._bot_message_ts.add(str(thread_id))
                if len(self._bot_message_ts) > self._BOT_TS_MAX:
                    excess = list(self._bot_message_ts)[: self._BOT_TS_MAX // 2]
                    for t in excess:
                        self._bot_message_ts.discard(t)
            return SendResult(
                ok=True,
                message_id=str(sent_ts) if sent_ts else None,
                raw=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        if not self._app:
            return SendResult(ok=False, error="not connected")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        # Idempotent format pass — see ``send`` for rationale.
        formatted = self.format_content(content)
        # Strict size contract. The old code silently truncated to
        # ``[: max - 50] + "\n…(continued)"`` and never delivered the
        # tail, which produced visibly truncated responses on long
        # turns. Failing the edit gives the caller (the stream
        # consumer) a clear signal to split into a new anchor instead.
        if len(formatted) > self.max_message_chars:
            return SendResult(
                ok=False,
                error=(
                    f"content exceeds max_message_chars "
                    f"({len(formatted)} > {self.max_message_chars}); "
                    f"caller must chunk before calling edit"
                ),
            )
        try:
            result = await client.chat_update(
                channel=chat_id,
                ts=message_id,
                text=formatted,
                mrkdwn=True,
            )
            return SendResult(ok=True, message_id=message_id, raw=result)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=str(exc))

    # ── Streaming surface — Slack Thinking Steps (chat.startStream et al)

    async def start_stream(
        self,
        chat_id: str,
        *,
        thread_id: str | None = None,
        markdown_text: str | None = None,
        chunks: list[Any] | None = None,
        task_display_mode: str | None = "plan",
        raw_hint: dict[str, Any] | None = None,
    ) -> SendResult:
        """Wrap ``chat.startStream``. thread_id is required by the API
        — Slack's streaming methods don't support top-level messages.
        For DM top-level inbound where reply_in_thread is off, the
        caller should pass the inbound message's ts as thread_id;
        that anchors the stream as a thread reply on the user's
        message rather than failing."""
        if not self._app:
            return SendResult(ok=False, error="not connected")
        if not thread_id:
            return SendResult(
                ok=False,
                error="thread_id required for chat.startStream",
            )
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        kwargs: dict[str, Any] = {
            "channel": chat_id,
            "thread_ts": thread_id,
        }
        if markdown_text is not None:
            kwargs["markdown_text"] = markdown_text
        if chunks:
            kwargs["chunks"] = chunks
        if task_display_mode:
            kwargs["task_display_mode"] = task_display_mode
        try:
            res = await client.chat_startStream(**kwargs)
            ts = res.get("ts") if hasattr(res, "get") else None
            if ts:
                self._bot_message_ts.add(str(ts))
            return SendResult(ok=bool(res.get("ok")), message_id=ts, raw=getattr(res, "data", None) or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] chat.startStream failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def append_stream(
        self,
        chat_id: str,
        stream_id: str,
        *,
        markdown_text: str | None = None,
        chunks: list[Any] | None = None,
    ) -> SendResult:
        """Wrap ``chat.appendStream``. Either markdown_text or chunks
        (or both) should be non-empty — calling with neither is a no-op
        and we'd burn a rate-limit slot for nothing, so we short-circuit."""
        if not self._app:
            return SendResult(ok=False, error="not connected")
        if not markdown_text and not chunks:
            return SendResult(ok=True, message_id=stream_id)
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        kwargs: dict[str, Any] = {
            "channel": chat_id,
            "ts": stream_id,
        }
        if markdown_text is not None:
            kwargs["markdown_text"] = markdown_text
        if chunks:
            kwargs["chunks"] = chunks
        try:
            res = await client.chat_appendStream(**kwargs)
            return SendResult(ok=bool(res.get("ok")), message_id=stream_id, raw=getattr(res, "data", None) or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] chat.appendStream failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def stop_stream(
        self,
        chat_id: str,
        stream_id: str,
        *,
        markdown_text: str | None = None,
        chunks: list[Any] | None = None,
        blocks: list[Any] | None = None,
    ) -> SendResult:
        """Wrap ``chat.stopStream``. ``blocks`` is the only place
        Slack accepts Block Kit in the streaming envelope (per docs:
        'Blocks may be used in chat.stopStream only'). Used to attach
        final-state Block Kit content like image previews."""
        if not self._app:
            return SendResult(ok=False, error="not connected")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        kwargs: dict[str, Any] = {
            "channel": chat_id,
            "ts": stream_id,
        }
        if markdown_text is not None:
            kwargs["markdown_text"] = markdown_text
        if chunks:
            kwargs["chunks"] = chunks
        if blocks:
            kwargs["blocks"] = blocks
        try:
            res = await client.chat_stopStream(**kwargs)
            return SendResult(ok=bool(res.get("ok")), message_id=stream_id, raw=getattr(res, "data", None) or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] chat.stopStream failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def upload_file(
        self,
        chat_id: str,
        path: str,
        *,
        thread_id: str | None = None,
        filename: str | None = None,
        title: str | None = None,
    ) -> SendResult:
        if not self._app:
            return SendResult(ok=False, error="not connected")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        try:
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "file": path,
                "filename": filename or Path(path).name,
            }
            if title:
                kwargs["title"] = title
            if thread_id:
                kwargs["thread_ts"] = thread_id
            result = await client.files_upload_v2(**kwargs)
            return SendResult(
                ok=True,
                message_id=(result.get("file") or {}).get("id"),
                raw=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] file upload failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def upload_files(
        self,
        chat_id: str,
        items: list[UploadItem],
        *,
        thread_id: str | None = None,
        initial_comment: str | None = None,
    ) -> SendResult:
        """Batch-upload up to 10 files in one ``files_upload_v2`` call
        so they share a single ``initial_comment`` and group as one
        Slack attachment block. Supports either filesystem paths or
        raw bytes per item (``UploadItem.path`` xor ``UploadItem.data``).

        Splits into multiple calls of 10 if the batch is larger.
        Returns the message_id of the first batch (Slack returns one
        message anchor per upload group)."""
        if not self._app:
            return SendResult(ok=False, error="not connected")
        if not items:
            return SendResult(ok=False, error="no items to upload")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")

        first_message_id: str | None = None
        first_error: str | None = None
        # Slack's files_upload_v2 caps at 10 files per call.
        CHUNK = 10
        for offset in range(0, len(items), CHUNK):
            batch = items[offset:offset + CHUNK]
            file_uploads: list[dict[str, Any]] = []
            for it in batch:
                entry: dict[str, Any] = {
                    "filename": it.filename or (Path(it.path).name if it.path else "file.bin"),
                }
                if it.title:
                    entry["title"] = it.title
                if it.data is not None:
                    entry["content"] = it.data
                elif it.path:
                    entry["file"] = it.path
                else:
                    continue
                file_uploads.append(entry)
            if not file_uploads:
                continue
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "file_uploads": file_uploads,
            }
            # Only the first batch gets the initial_comment; later
            # batches post anonymously so the chat isn't cluttered
            # with a duplicate caption.
            if initial_comment and offset == 0:
                kwargs["initial_comment"] = initial_comment
            if thread_id:
                kwargs["thread_ts"] = thread_id
            try:
                result = await client.files_upload_v2(**kwargs)
                msg = (result.get("files") or [{}])[0]
                if first_message_id is None:
                    first_message_id = msg.get("id")
            except Exception as exc:  # noqa: BLE001
                logger.exception("[slack] batch upload failed: %s", exc)
                if first_error is None:
                    first_error = str(exc)
                # Fall back to per-file upload for this batch so we
                # don't lose everything because of one bad file.
                for it in batch:
                    if it.path:
                        await self.upload_file(
                            chat_id, it.path,
                            thread_id=thread_id,
                            filename=it.filename,
                            title=it.title,
                        )
        return SendResult(
            ok=first_message_id is not None,
            message_id=first_message_id,
            error=first_error if first_message_id is None else None,
        )

    async def send_typing(
        self,
        chat_id: str,
        *,
        thread_id: str | None = None,
        status: str = "is thinking...",
    ) -> SendResult:
        """Slack-native typing indicator via ``assistant.threads.setStatus``.

        Renders next to the bot's name as ``Bot is thinking…``.
        Works in DM threads and Assistant-context threads; in regular
        channels Slack silently ignores it. Requires the
        ``assistant:write`` scope (which our manifest already requests).
        We swallow API errors here — a missing or expired status is a
        cosmetic loss, never a correctness issue.
        """
        if not self._app or not thread_id:
            # No thread_ts → no surface to render status on.
            return SendResult(ok=True)
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=True)
        try:
            await client.assistant_threads_setStatus(
                channel_id=chat_id,
                thread_ts=thread_id,
                status=status,
            )
            return SendResult(ok=True)
        except Exception as exc:  # noqa: BLE001
            # Common cause: not in an Assistant thread context. Log
            # once at debug — these aren't user-actionable.
            logger.debug("[slack] setStatus skipped: %s", exc)
            return SendResult(ok=True)

    async def stop_typing(
        self,
        chat_id: str,
        *,
        thread_id: str | None = None,
    ) -> SendResult:
        """Clear the typing/status indicator. Sending an empty status
        is Slack's documented way to dismiss it."""
        return await self.send_typing(chat_id, thread_id=thread_id, status="")

    # conversations.replies returns messages chronologically (oldest
    # first) and pages forward via response_metadata.next_cursor. To
    # surface the most-recent ``limit`` replies we walk pages forward
    # while keeping a rolling tail; the parent stays pinned from page
    # 1. Cap total work at MAX_PAGES * PAGE_SIZE to bound latency on
    # pathological threads.
    _THREAD_PAGE_SIZE = 1000          # Slack's max for conversations.replies
    _THREAD_MAX_PAGES = 5             # ⇒ scans up to ~5000 messages
    _THREAD_DEFAULT_LIMIT = 100       # how many recent replies the agent sees

    def _normalize_thread_message(self, m: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a raw Slack message into the canonical dict the
        gateway renderer consumes. Returns ``None`` for messages with
        no text and no files (Slack joins/leaves, ephemeral system
        notices, etc.) so callers can drop them upstream."""
        text = str(m.get("text") or "")
        blocks_text = extract_text_from_slack_blocks(m.get("blocks"))
        if blocks_text and len(blocks_text) > len(text):
            text = blocks_text
        files = m.get("files") or []
        if not text and not files:
            return None
        user_id = str(m.get("user") or "")
        is_bot = bool(m.get("bot_id")) or user_id == (self._bot_user_id or "")
        return {
            "role": "assistant" if is_bot else "user",
            "user_id": user_id,
            "text": text,
            "ts": str(m.get("ts") or ""),
            "files": files,
        }

    async def fetch_thread_context(
        self,
        chat_id: str,
        thread_ts: str,
        *,
        limit: int | None = None,
        exclude_ts: str | None = None,
        oldest_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pull recent messages from a Slack thread, anchored on the
        parent.

        Slack's ``conversations.replies`` returns messages oldest-first
        with cursor-based pagination. A single page on a long thread
        only shows the original framing, so we walk pages forward and
        keep a rolling buffer of the last ``limit`` messages. The
        parent (page 1, index 0) is always pinned so the agent sees the
        thread's opening as well as the recent state. When the gap is
        non-empty, a synthetic ``role: "system_note"`` marker is
        inserted between the parent and the tail so the renderer can
        show "… N earlier replies omitted …" — keeps the agent honest
        about what it can and can't see.

        Returns a chronological list of ``{role, user_id, text, ts,
        files}`` dicts. Image/file refs survive on the ``files`` key so
        the caller can decide which to download (capped via the
        ``MAX_PRIOR_IMAGES`` budget upstream).

        ``exclude_ts`` filters out a specific message (typically the
        @mention that triggered this turn — no point feeding it back
        twice). ``oldest_ts`` is a lower bound for incremental fetches.

        Bounds: ``MAX_PAGES * PAGE_SIZE = 5 * 1000 = 5000`` messages
        scanned worst-case. Threads longer than that get the last 100
        of the first 5000 — still a major correctness improvement over
        the old "first 50" behavior; pathologically long threads are
        rare and we'd rather cap latency than chase tail messages
        across dozens of API calls.

        Best-effort: any API failure mid-pagination returns whatever
        we've collected so far so a transient hiccup degrades to "less
        context" rather than "no context".
        """
        if not self._app or not chat_id or not thread_ts:
            return []
        client = self._get_client(chat_id)
        if client is None:
            return []

        limit = limit or self._THREAD_DEFAULT_LIMIT
        if limit < 1:
            return []

        parent: dict[str, Any] | None = None
        tail: list[dict[str, Any]] = []  # rolling buffer, size ≤ limit
        total_observed = 0                # all raw messages seen across pages
        cursor: str | None = None
        truncated = False                 # True if we hit MAX_PAGES before EOF

        for page_idx in range(self._THREAD_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "ts": thread_ts,
                "limit": self._THREAD_PAGE_SIZE,
            }
            if cursor:
                kwargs["cursor"] = cursor
            try:
                res = await client.conversations_replies(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[slack] conversations.replies failed on page %d: %s",
                    page_idx, exc,
                )
                # Use whatever we already collected — better than empty.
                break
            page_msgs = res.get("messages") or []
            if page_idx == 0 and page_msgs:
                # Parent is always the first message of the first page
                # in Slack's chronological ordering.
                parent = page_msgs[0]
            total_observed += len(page_msgs)
            tail.extend(page_msgs)
            if len(tail) > limit:
                # Discard older entries — we only need the trailing
                # window. Cheap O(N) on a small N-per-page batch; the
                # alternative (deque(maxlen=limit)) hides the trim and
                # makes the count math less obvious.
                tail = tail[-limit:]
            cursor = (res.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            # ``for ... else`` runs when the loop exhausts without break.
            # Reaching here means we hit MAX_PAGES with a cursor still
            # outstanding — there are more messages we didn't fetch.
            if cursor:
                truncated = True

        if not tail:
            return []

        # Apply caller-side filters AFTER the pagination buffer so the
        # rolling window stays accurate. exclude_ts/oldest_ts are
        # usually single-message drops, so the count math below stays
        # close enough.
        def _passes(m: dict[str, Any]) -> bool:
            ts = str(m.get("ts") or "")
            if exclude_ts and ts == str(exclude_ts):
                return False
            if oldest_ts and ts <= str(oldest_ts):
                return False
            return True

        # Normalize + filter the tail. We deliberately do this AFTER
        # the rolling-buffer trim above so the buffer stays bounded
        # even if upstream calls us with a giant `limit`.
        tail_normalized: list[dict[str, Any]] = []
        for m in tail:
            if not _passes(m):
                continue
            normalized = self._normalize_thread_message(m)
            if normalized is not None:
                tail_normalized.append(normalized)

        # Decide whether the parent needs a separate entry. It's already
        # in the tail when the thread is shorter than `limit` (we never
        # discarded it), in which case there's no gap — just return the
        # normalized tail as-is.
        parent_ts = str(parent.get("ts") or "") if parent else ""
        parent_in_tail = any(m["ts"] == parent_ts for m in tail_normalized)

        if parent_in_tail or parent is None:
            return tail_normalized

        # Long-thread path: parent + gap marker + tail.
        out: list[dict[str, Any]] = []
        parent_normalized = self._normalize_thread_message(parent)
        if parent_normalized is not None and _passes(parent):
            out.append(parent_normalized)

        # Approximate the gap count. We don't know exactly how many
        # messages were skipped because the rolling buffer threw older
        # ones away, but `total_observed - len(tail) - 1` is a
        # reasonable floor (the "-1" accounts for the parent itself).
        # When we hit MAX_PAGES, signal "and counting" rather than
        # claiming a precise number we don't actually have.
        skipped = max(0, total_observed - len(tail_normalized) - 1)
        if skipped > 0 or truncated:
            label = f"{skipped}+" if truncated else str(skipped)
            out.append({
                "role": "system_note",
                "user_id": "",
                "text": f"({label} earlier replies in this thread omitted)",
                "ts": "",
                "files": [],
            })

        out.extend(tail_normalized)
        return out

    async def fetch_dm_history(
        self,
        chat_id: str,
        *,
        limit: int = 20,
        exclude_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pull recent messages from a DM channel (no threading).
        Used when a DM message arrives top-level (no thread_ts) so
        the agent still sees recent prior turns even when its own
        transcript was wiped or reset."""
        if not self._app or not chat_id:
            return []
        client = self._get_client(chat_id)
        if client is None:
            return []
        try:
            res = await client.conversations_history(
                channel=chat_id,
                limit=max(1, min(limit, 100)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[slack] conversations.history failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        # conversations.history returns NEWEST first; reverse for chronology.
        for m in reversed(res.get("messages") or []):
            ts = str(m.get("ts") or "")
            if exclude_ts and ts == str(exclude_ts):
                continue
            text = str(m.get("text") or "")
            blocks_text = extract_text_from_slack_blocks(m.get("blocks"))
            if blocks_text and len(blocks_text) > len(text):
                text = blocks_text
            files = m.get("files") or []
            if not text and not files:
                continue
            user_id = str(m.get("user") or "")
            is_bot = bool(m.get("bot_id")) or user_id == (self._bot_user_id or "")
            out.append({
                "role": "assistant" if is_bot else "user",
                "user_id": user_id,
                "text": text,
                "ts": ts,
                "files": files,
            })
        return out

    async def send_approval_request(
        self,
        chat_id: str,
        request_id: str,
        *,
        tool_name: str,
        command_preview: str,
        reason: str,
        thread_id: str | None = None,
    ) -> SendResult:
        """Post a Block Kit message with Approve / Deny buttons for a
        destructive tool call. ``request_id`` is the key that the
        action handler uses to resolve the pending Future."""
        if not self._app:
            return SendResult(ok=False, error="not connected")
        client = self._get_client(chat_id)
        if client is None:
            return SendResult(ok=False, error=f"no client for chat {chat_id}")
        # Slack section text max ~3000 chars. Trim the preview if huge
        # — the operator is mainly looking at the leading verb and
        # path, not the entire piped chain.
        preview = command_preview if len(command_preview) <= 2500 else command_preview[:2500] + "…"
        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Approve `{tool_name}`?*  "
                        f"_{reason}_"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```\n{preview}\n```",
                },
            },
            {
                "type": "actions",
                "block_id": f"freyja_approval_{request_id[:16]}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "freyja_approve",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": request_id,
                    },
                    {
                        "type": "button",
                        "action_id": "freyja_deny",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "value": request_id,
                    },
                ],
            },
        ]
        kwargs: dict[str, Any] = {
            "channel": chat_id,
            # Fallback text for notification surfaces that don't render
            # blocks (mobile lock-screen previews, etc.).
            "text": f"Approval needed for {tool_name}: {reason}",
            "blocks": blocks,
        }
        if thread_id:
            kwargs["thread_ts"] = thread_id
        try:
            result = await client.chat_postMessage(**kwargs)
            return SendResult(
                ok=True,
                message_id=result.get("ts"),
                raw=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] send_approval_request failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    # ── helpers ────────────────────────────────────────────────

    def _get_client(
        self, chat_id: str, team_id: str | None = None
    ) -> AsyncWebClient | None:
        """Return the AsyncWebClient for the workspace owning chat_id.
        Falls back to the primary client if no mapping known."""
        if team_id and team_id in self._team_clients:
            return self._team_clients[team_id]
        mapped_team = self._channel_team.get(chat_id)
        if mapped_team and mapped_team in self._team_clients:
            return self._team_clients[mapped_team]
        if self._team_clients:
            # Primary fallback.
            return next(iter(self._team_clients.values()))
        return None

    def _token_for_client(self, client: AsyncWebClient | None) -> str | None:
        if client is None:
            return None
        return getattr(client, "token", None)

    async def _resolve_user_name(
        self, user_id: str, team_id: str
    ) -> str | None:
        if not user_id:
            return None
        client = self._get_client("", team_id)
        if client is None:
            return None
        try:
            info = await client.users_info(user=user_id)
            user = info.get("user") or {}
            profile = user.get("profile") or {}
            return (
                profile.get("display_name_normalized")
                or profile.get("real_name_normalized")
                or user.get("name")
                or None
            )
        except Exception:  # noqa: BLE001
            return None

    def _pop_slash_context_for(
        self,
        chat_id: str,
        user_id: str | None,
        raw_hint: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        # Prefer the raw hint (set by the stream consumer carrying the
        # original IncomingMessage.raw payload) so we route to the right
        # response_url even if multiple slashes are in flight.
        if raw_hint and "response_url" in raw_hint:
            return {
                "response_url": raw_hint["response_url"],
                "command": raw_hint.get("command", ""),
            }
        if not user_id:
            return None
        return self._slash_contexts.pop((chat_id, user_id), None)

    async def _send_slash_ephemeral(
        self,
        slash_ctx: dict[str, Any],
        content: str,
    ) -> SendResult:
        """Deliver a slash response by POSTing to the stashed response_url.

        Replaces the "Running…" ack with the real reply, ephemeral by
        default (only the command issuer sees it)."""
        response_url = slash_ctx.get("response_url")
        if not response_url or aiohttp is None:
            return SendResult(ok=False, error="missing response_url")
        formatted = _markdown_to_slack(content)
        payload = {
            "response_type": "ephemeral",
            "text": formatted,
            "replace_original": True,
            "mrkdwn": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(response_url, json=payload) as resp:
                    text = await resp.text()
                    if resp.status >= 300:
                        logger.warning(
                            "[slack] response_url post returned %d: %s",
                            resp.status, text[:200],
                        )
                        return SendResult(
                            ok=False,
                            error=f"http {resp.status}",
                        )
                    return SendResult(ok=True, raw={"response_url_text": text})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[slack] response_url post failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    async def _download_file(
        self,
        file_obj: dict[str, Any],
        bot_token: str,
    ) -> dict[str, Any] | None:
        """Download a Slack file to disk and shape it into an
        attachment dict the bridge's user-content builder
        (``_build_user_content_blocks`` in ``freyja_bridge.py``)
        understands.

        Critical: the bridge only routes attachments whose ``type``
        is ``"image"`` or ``"video"``, and image attachments need a
        ``dataBase64`` field with the actual encoded bytes. Slack
        gives us a download URL; we fetch it, base64-encode the
        bytes for images, and emit the right shape. Everything else
        (PDFs, text docs, binaries) goes through with a stub shape
        + ``path`` so the agent can ``read_file`` it on demand and
        knows where it lives in the cache.
        """
        import base64 as _b64

        file_id = file_obj.get("id") or ""
        file_name = file_obj.get("name") or f"slack-{file_id}"
        mime_type = str(file_obj.get("mimetype") or "")
        url_private = (
            file_obj.get("url_private_download")
            or file_obj.get("url_private")
            or ""
        )
        if not url_private or aiohttp is None:
            return None
        # Cache under ~/.freyja/projects/slack-cache/<file_id>-<name>
        cache_dir = freyja_home() / "projects" / "slack-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name)
        cache_path = cache_dir / f"{file_id}-{safe_name}"
        try:
            if not cache_path.exists():
                headers = {"Authorization": f"Bearer {bot_token}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url_private, headers=headers) as resp:
                        if resp.status >= 300:
                            logger.warning(
                                "[slack] file download %s returned %d",
                                file_id, resp.status,
                            )
                            return None
                        content = await resp.read()
                cache_path.write_bytes(content)

            # Decide attachment shape based on mime type. The bridge
            # only natively understands image/* and video/* — anything
            # else still surfaces (so the agent can read_file it) but
            # doesn't get a ``dataBase64`` payload.
            is_image = mime_type.startswith("image/") or _looks_like_image(file_name)
            is_video = mime_type.startswith("video/")
            if is_image:
                try:
                    raw_bytes = cache_path.read_bytes()
                except OSError as exc:
                    logger.warning(
                        "[slack] re-read of cached image %s failed: %s",
                        cache_path, exc,
                    )
                    return None
                data_b64 = _b64.b64encode(raw_bytes).decode("ascii")
                inferred_mime = mime_type or _mime_from_filename(file_name) or "image/png"
                return {
                    "type": "image",
                    "dataBase64": data_b64,
                    "mimeType": inferred_mime,
                    "filename": file_name,
                    "name": file_name,
                    "path": str(cache_path),
                    "slack_file_id": file_id,
                }
            if is_video:
                try:
                    raw_bytes = cache_path.read_bytes()
                except OSError:
                    return None
                data_b64 = _b64.b64encode(raw_bytes).decode("ascii")
                return {
                    "type": "video",
                    "dataBase64": data_b64,
                    "mimeType": mime_type or "video/mp4",
                    "filename": file_name,
                    "name": file_name,
                    "path": str(cache_path),
                    "sizeBytes": len(raw_bytes),
                    "slack_file_id": file_id,
                }
            # Non-image, non-video (PDF, text, archive, etc.) — return
            # with type=file so the daemon's framed-text builder can
            # annotate the message ("user attached a PDF at ...") and
            # the agent can read_file it directly from the cache path.
            return {
                "type": "file",
                "path": str(cache_path),
                "name": file_name,
                "filename": file_name,
                "mime_type": mime_type,
                "mimeType": mime_type,
                "slack_file_id": file_id,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[slack] file download error %s: %s", file_id, exc)
            return None


_IMAGE_NAME_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif",
}

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".heic": "image/heic", ".heif": "image/heif",
}


def _looks_like_image(name: str) -> bool:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in _IMAGE_NAME_EXTS


def _mime_from_filename(name: str) -> str:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _MIME_BY_EXT.get(ext, "")


