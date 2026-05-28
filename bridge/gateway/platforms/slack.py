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
            await self._handle_message(event)

        @self._app.event("app_mention")
        async def _handle_app_mention(event, say):  # noqa: ARG001
            # Some Slack app configurations deliver @mentions ONLY as
            # app_mention (not as message events). Forward through the
            # same pipeline. Dedup on event_ts prevents double-fire when
            # both events arrive.
            await self._handle_message(event)

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

    async def _handle_message(self, event: dict[str, Any]) -> None:
        # Dedup: Socket Mode can redeliver after reconnect.
        event_ts = str(event.get("ts") or "")
        if event_ts and event_ts in self._dedup:
            return
        if event_ts:
            now = time.monotonic()
            self._dedup[event_ts] = now
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
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")
        user_id = event.get("user", "")
        if (bot_id or subtype == "bot_message") and user_id == self._bot_user_id:
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

        formatted = _markdown_to_slack(content)
        chunks = truncate_for_platform(formatted, SLACK_MAX_MESSAGE_LENGTH)
        last_result: Any = None
        try:
            for i, chunk in enumerate(chunks):
                kwargs: dict[str, Any] = {
                    "channel": chat_id,
                    "text": chunk,
                    "mrkdwn": True,
                }
                if thread_id:
                    kwargs["thread_ts"] = thread_id
                last_result = await client.chat_postMessage(**kwargs)
            sent_ts = (last_result or {}).get("ts") if last_result else None
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
                raw=last_result,
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
        formatted = _markdown_to_slack(content)
        # If the formatted content exceeds the Slack cap, we can't fit
        # it into one edit. Truncate the edit and let the caller send a
        # follow-up message for the overflow.
        if len(formatted) > SLACK_MAX_MESSAGE_LENGTH:
            formatted = formatted[: SLACK_MAX_MESSAGE_LENGTH - 50] + "\n…(continued)"
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

    async def fetch_thread_context(
        self,
        chat_id: str,
        thread_ts: str,
        *,
        limit: int = 50,
        exclude_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pull prior messages from a Slack thread or top-level DM.

        Returns a chronological list of ``{role, user_id, user_name,
        text, ts}`` dicts. Used by the gateway router to prepend
        thread context to the framed prompt so the agent sees what was
        said before it was triggered (esp. important when the bot is
        @mentioned mid-conversation and the user expects it to read
        the thread first, or when the user replies in a thread the
        bot wasn't actively listening to).

        ``exclude_ts`` filters out a specific message (typically the
        one that triggered the current turn — no point feeding it
        back twice).

        Best-effort: any API failure returns []. Caller should fall
        back to "no prior context" rather than failing the turn.
        """
        if not self._app or not chat_id or not thread_ts:
            return []
        client = self._get_client(chat_id)
        if client is None:
            return []
        try:
            res = await client.conversations_replies(
                channel=chat_id,
                ts=thread_ts,
                limit=max(1, min(limit, 200)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[slack] conversations.replies failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for m in res.get("messages") or []:
            ts = str(m.get("ts") or "")
            if exclude_ts and ts == str(exclude_ts):
                continue
            text = str(m.get("text") or "")
            # Prefer rich-text blocks when present (preserves quotes/lists).
            blocks_text = extract_text_from_slack_blocks(m.get("blocks"))
            if blocks_text and len(blocks_text) > len(text):
                text = blocks_text
            if not text:
                continue
            user_id = str(m.get("user") or "")
            is_bot = bool(m.get("bot_id")) or user_id == (self._bot_user_id or "")
            out.append({
                "role": "assistant" if is_bot else "user",
                "user_id": user_id,
                "text": text,
                "ts": ts,
            })
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
            if not text:
                continue
            user_id = str(m.get("user") or "")
            is_bot = bool(m.get("bot_id")) or user_id == (self._bot_user_id or "")
            out.append({
                "role": "assistant" if is_bot else "user",
                "user_id": user_id,
                "text": text,
                "ts": ts,
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


