"""Gateway daemon entry point.

Runs forever as a launchd-managed service. Hosts a single
``_BridgeState`` and one or more platform adapters (Slack today;
Telegram/Discord later). Routes inbound messages from any adapter to
the right Freyja session and streams responses back through the
originating adapter.

Lifecycle:
  · PID lock acquisition (one daemon per ``FREYJA_HOME``)
  · Signal handlers: SIGTERM → graceful drain; SIGINT → same
  · Load ``~/.freyja/.env`` so platform tokens + provider keys are
    available
  · Construct ``_BridgeState`` (existing Freyja machinery)
  · Instantiate + connect configured platform adapters
  · For each inbound message: route to a session, register a
    per-turn ``SlackStreamConsumer``, enqueue via
    ``_schedule_or_queue_turn``
  · Block on shutdown event; on shutdown, disconnect adapters,
    release PID lock, exit 0 (planned) or 1 (crash)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from bridge.gateway.pid import (
    acquire_lock,
    consume_takeover_marker,
    freyja_home,
    gateway_log_path,
    release_lock,
)
from bridge.gateway.control_channel import ControlChannelReader
from bridge.gateway.platforms.base import IncomingMessage, Platform
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.session_router import (
    gateway_source_block,
    route as route_message,
)
from bridge.gateway.setup.env_writer import read_env
from bridge.gateway.stream_consumer import SlackStreamConsumer

logger = logging.getLogger("freyja.gateway")


def _help_card_text() -> str:
    """The text returned for `/freyja` and `/freyja help`."""
    return (
        "*Freyja on Slack*\n"
        "I'm a multi-agent assistant. I can help with coding, research, "
        "writing, and analysis. A few things that make me different:\n"
        "\n"
        "• I spawn *specialized sub-agents* (explore, code, verify, judge) "
        "to work on parts of your task in parallel. They publish findings "
        "to a shared bus and I synthesize.\n"
        "• I have a *goal mode* with an autonomous judge that reviews my "
        "work each turn and decides whether to keep iterating.\n"
        "• I have persistent *memory + skills* that compound across sessions.\n"
        "\n"
        "*Slash commands*\n"
        "• `/freyja help`   — this card\n"
        "• `/freyja status` — show session info (model, mode, in-flight)\n"
        "• `/freyja perms`  — show tool permissions for this session\n"
        "• `/goal <obj>`    — arm a goal loop\n"
        "• `/mode <s>`      — switch coordination (bus / goal / kanban / isolated)\n"
        "• `/model <id>`    — switch the agent model\n"
        "• `/stop`          — interrupt the current turn\n"
        "• `/reset`         — start a fresh conversation\n"
        "• `/perms`         — show tool permissions\n"
        "\n"
        "*Channels*: @mention me to start a thread, then keep replying in "
        "the thread without re-mentioning.\n"
        "*DMs*: just talk.\n"
        "*Files*: drop in any image, code, or document — I'll use it."
    )


_IMAGE_EXT_PATTERNS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")


def _looks_like_image_name(name: str) -> bool:
    """Conservative ext check used as a fallback when Slack's
    ``mimetype`` field is missing or wrong (it occasionally is).
    Avoids hardcoding behavior to MIME strings that vary by upload
    path."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXT_PATTERNS)


def _setup_logging() -> None:
    """Configure root logger to write to ~/.freyja/logs/gateway.log.

    Two run contexts to support:
      · launchd daemon: stdout + stderr are redirected to
        ~/.freyja/logs/gateway.{log,err} via the plist's
        Standard{Out,Error}Path fields. Adding our own FileHandler
        targeting the same file would write every line TWICE — once
        directly, once via stdout capture. So under launchd we ONLY
        use a StreamHandler on stdout and let launchd own the file.
      · Foreground (`freyja gateway run` in a terminal): stdout is a
        TTY for live operator feedback. We add a FileHandler in
        addition so the operator gets a persistent record alongside
        live terminal output.

    Detection: stdout.isatty() is True in foreground, False under
    launchd (since launchd connects stdout to a regular file).
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Only attach the file handler in foreground mode. Under launchd
    # stdout is already piped to gateway.log; a second handler
    # writing to the same path produces the duplicate-line behavior
    # operators see in `tail -f`.
    if sys.stdout.isatty():
        try:
            file_handler = logging.FileHandler(gateway_log_path(), encoding="utf-8")
            handlers.append(file_handler)
        except OSError:
            pass
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any existing handlers so re-running doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)


def _load_env_into_os_environ() -> None:
    """Merge ``~/.freyja/.env`` into ``os.environ`` so provider SDKs +
    platform adapters see their tokens. Existing env vars take
    precedence (so the operator can override per-launch)."""
    env = read_env()
    for k, v in env.items():
        if k not in os.environ:
            os.environ[k] = v


def _render_non_media_attachments(
    attachments: list[dict[str, Any]],
) -> str:
    """Format non-image/-video attachments as a plain-text breadcrumb
    block. The agent doesn't see binary content in its context, so
    we hand it a path it can ``read_file`` on demand + the mime type
    so it knows what kind of file to expect."""
    if not attachments:
        return ""
    lines: list[str] = []
    for a in attachments:
        path = str(a.get("path") or "")
        name = str(a.get("name") or a.get("filename") or "")
        mime = str(a.get("mimeType") or a.get("mime_type") or "")
        if not path or not name:
            continue
        descr = f"`{name}`"
        if mime:
            descr += f" ({mime})"
        descr += f" — cached at `{path}`. Use read_file to read its contents."
        lines.append("- " + descr)
    return "\n".join(lines)


class GatewayDaemon:
    """Top-level daemon coordinator. One instance per process."""

    def __init__(self) -> None:
        self.state: object | None = None
        self.adapters: list[object] = []
        self.shutdown_event: asyncio.Event = asyncio.Event()
        self._planned_exit = False  # True on graceful SIGTERM or takeover
        # Reads desktop → daemon commands (permission_response, etc.)
        # from ~/.freyja/control/commands.jsonl. Started in ``start``.
        self.control_channel: ControlChannelReader | None = None

    async def _on_inbound(self, message: IncomingMessage) -> None:
        """Single callback every adapter feeds inbound messages into."""
        # Lazy import — _BridgeState lives in freyja_bridge which is a
        # heavy module. Importing it here means the daemon doesn't pay
        # the import cost until the first message arrives, and gateway
        # boot stays fast.
        from bridge.freyja_bridge import (
            _schedule_or_queue_turn,
            register_session_listener,
            unregister_session_listener,
        )

        if self.state is None:
            logger.warning("inbound message dropped — state not initialized")
            return

        # Find the originating adapter (so the stream consumer sends
        # back through the right surface).
        adapter = self._adapter_for_platform(message.source.platform)
        if adapter is None:
            logger.warning(
                "no adapter for platform %s", message.source.platform.value
            )
            return

        # In-gateway slash command handlers — these short-circuit the
        # agent path and reply directly. Anything not handled here
        # falls through to the agent as a regular text turn (the
        # framed text below carries the literal slash command).
        if message.is_slash_command:
            handled = await self._handle_slash_in_gateway(message, adapter)
            if handled:
                return

        try:
            key, session = await route_message(
                message,
                self.state,
                default_strategy="bus",
            )
        except Exception:
            logger.exception("failed to route inbound message")
            return

        # Slash commands without text body (just /status, /freyja help)
        # still get routed as agent turns — the slash text becomes the
        # user message. Future: shortcut some slashes to in-gateway
        # handlers without involving the agent.

        # Build a per-turn stream consumer. CRITICALLY we do NOT
        # register the consumer as a session listener here. Doing so
        # eagerly (back when this code did `register_session_listener`
        # right after construction) created a race when a second
        # message arrived while the first message's turn was still
        # running: the second consumer started receiving the first
        # turn's events and routed the bot's response into the WRONG
        # thread (second message's), and also wrongly finalized on
        # the first turn's turn_complete — leaving the second message
        # with no response at all.
        #
        # Instead, we hand the registration to the bridge's
        # ``_schedule_or_queue_turn`` as an ``on_turn_start`` hook. The
        # hook fires SYNCHRONOUSLY immediately before this turn's
        # ``run_turn`` begins emitting events — whether immediately
        # (no prior turn in flight) or later when the queue drains.
        # Same hook also stamps ``session.gateway_source`` so this
        # turn's tool calls (send_attachment, destructive approval
        # prompts) target the right Slack thread.
        consumer_holder: dict[str, object] = {}

        def _unregister() -> None:
            cb = consumer_holder.get("on_event")
            if cb is not None:
                unregister_session_listener(key, cb)

        # Bind the session's permission handler to a stable callable so
        # Block Kit approval clicks can resolve the daemon's pending
        # ``DesktopPermissionHandler`` future. Closing over ``session``
        # is safe — the handler outlives any single turn.
        def _resolve_permission(request_id: str, approved: bool) -> bool:
            handler = getattr(session, "permission_handler", None)
            if handler is None:
                return False
            try:
                return bool(handler.resolve(
                    request_id, approved,
                    "slack-approve" if approved else "slack-deny",
                ))
            except Exception:  # noqa: BLE001
                logger.exception("permission_handler.resolve raised")
                return False

        consumer = SlackStreamConsumer(
            adapter,  # type: ignore[arg-type]
            message.source,
            session_key=key,
            raw_hint=message.raw,
            on_complete=_unregister,
            permission_resolver=_resolve_permission,
        )
        consumer_holder["on_event"] = consumer.on_event

        # Captured into closure: this turn's source + the consumer
        # to attach. Closes over locals so the hook is self-contained.
        _turn_source = message.source
        _turn_session = session
        _turn_consumer = consumer

        def _on_turn_start() -> None:
            # Mutate session.gateway_source for the duration of THIS
            # turn — the per-turn tool resolvers (SendAttachmentTool,
            # destructive gate) read this attribute at call time.
            _turn_session.gateway_source = _turn_source
            register_session_listener(key, _turn_consumer.on_event)

        # Pull prior thread / DM context from the platform so the
        # agent sees what was said before it was triggered. Critical
        # when the bot is @mentioned mid-conversation, OR when the
        # user replies in a Slack thread the bot didn't initially
        # join — without this, the agent only sees the one message
        # that pinged it and has to guess at the rest.
        prior_block, prior_attachments = await self._fetch_prior_context(
            message, adapter,
        )

        # Annotate non-image attachments (PDFs, text docs, binaries)
        # so the agent knows they exist + where to read them from.
        # Image/video attachments flow through as native ImageBlock /
        # VideoBlock via the bridge's user-content builder; non-media
        # files only land on disk and need a textual breadcrumb. Prior
        # thread non-media files are NOT downloaded — only their text
        # refs survive (see _fetch_prior_context's file_refs_by_ts),
        # which is good enough for the agent to know they exist and
        # offer to read them on demand.
        non_media_attachments: list[dict[str, Any]] = [
            a for a in (message.attachments or [])
            if a.get("type") not in {"image", "video"}
        ]
        attach_block = _render_non_media_attachments(non_media_attachments)

        # Build the framed user text: a small context preamble (one
        # paragraph naming the platform + chat + sender), then any
        # prior thread/channel context, then the operator's actual
        # message. Lets the agent know it's on Slack without needing
        # system-prompt surgery.
        framed_parts: list[str] = [
            "[gateway context]",
            gateway_source_block(message.source),
        ]
        if prior_block:
            framed_parts.append("")
            framed_parts.append("[prior conversation in this chat/thread]")
            framed_parts.append(prior_block)
        if attach_block:
            framed_parts.append("")
            framed_parts.append("[user attached files]")
            framed_parts.append(attach_block)
        framed_parts.append("")
        framed_parts.append("[message]")
        framed_parts.append(message.text)
        framed_text = "\n".join(framed_parts)

        # Enqueue the user turn via the existing machinery (handles
        # the busy/queue case transparently). Strip non-media
        # attachments from the downstream payload — they were
        # already announced in the [user attached files] block above
        # as text, and the engine's image/video processor would drop
        # them anyway (they have no ``dataBase64`` payload). Passing
        # them through pollutes the content-block list with empty
        # entries and confuses cross-provider transcript persistence.
        # Merge the trigger message's own image/video attachments with
        # any images fetched from prior thread messages. The agent now
        # sees BOTH "what the user just attached" AND "what's been
        # floating around earlier in the thread" as proper image
        # content blocks (not text descriptions).
        downstream_attachments = [
            a for a in (message.attachments or [])
            if a.get("type") in {"image", "video"}
        ]
        downstream_attachments.extend(
            a for a in prior_attachments
            if a.get("type") in {"image", "video"}
        )
        if not downstream_attachments:
            downstream_attachments = None
        try:
            _schedule_or_queue_turn(
                session,
                framed_text,
                downstream_attachments,
                on_turn_start=_on_turn_start,
            )
        except Exception:
            logger.exception("schedule_or_queue_turn failed")
            # Defensive: if scheduling fails, the hook may or may not
            # have fired. Best-effort unregister so a half-registered
            # consumer doesn't leak.
            unregister_session_listener(key, consumer.on_event)

    def _adapter_for_platform(self, platform: Platform) -> object | None:
        for a in self.adapters:
            if getattr(a, "name", None) == platform.value:
                return a
        return None

    async def _fetch_prior_context(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Render prior conversation in this thread / DM, with images.

        Returns ``(text_block, prior_attachments)``:

          · ``text_block`` is a plain-text rendering of the thread so
            the agent has narrative context. Each message becomes one
            line: ``Alice: looks great [image: design.png]``. Image
            and file refs are annotated inline so the agent can map
            the binary attachments back to their authors.

          · ``prior_attachments`` is a list of attachment dicts
            (same shape as live inbound files — see
            ``slack._download_file``) ready to be merged with the
            current message's attachments and handed to the model.

        Adapter-aware: uses ``fetch_thread_context`` for threads
        and ``fetch_dm_history`` for DMs. Both gracefully return []
        on Slack API failure; the caller treats that as "no prior
        context".

        Image budget: capped at MAX_PRIOR_IMAGES total, each must be
        under MAX_PER_IMAGE_BYTES. Most recent images win when the
        thread has more than the budget — older ones get text refs
        only. This keeps a meme-laden thread from blowing the
        context window or driving a 30-second download cliff.
        """
        source = message.source
        # Don't bother fetching for our own outbound messages.
        if getattr(source, "is_bot", False):
            return "", []
        # Identify the trigger message ts so we exclude it from the
        # fetched block (the agent gets it as `[message]` already).
        trigger_ts = getattr(source, "message_id", None)
        msgs: list[dict[str, Any]] = []
        try:
            if source.thread_id and hasattr(adapter, "fetch_thread_context"):
                msgs = await adapter.fetch_thread_context(  # type: ignore[attr-defined]
                    source.chat_id,
                    source.thread_id,
                    limit=50,
                    exclude_ts=trigger_ts,
                )
            elif source.chat_type == "dm" and hasattr(adapter, "fetch_dm_history"):
                msgs = await adapter.fetch_dm_history(  # type: ignore[attr-defined]
                    source.chat_id,
                    limit=15,
                    exclude_ts=trigger_ts,
                )
        except Exception:
            logger.exception("prior-context fetch raised")
            return "", []
        if not msgs:
            return "", []

        # ── Download images from prior messages ──
        # Most-recent wins: walk msgs in reverse so if we hit the cap,
        # we keep the freshest visual references. Result list is
        # reversed back to chronological order at the end so the agent
        # sees images in the same order as the text.
        prior_attachments: list[dict[str, Any]] = []
        MAX_PRIOR_IMAGES = 8
        MAX_PER_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
        # ts → list of file-name refs we'll splice into the text block.
        # Built alongside the download pass so a file that exceeded the
        # size cap is still mentioned by name (just without inline data).
        file_refs_by_ts: dict[str, list[str]] = {}

        # Pull the per-channel client + token once, used for each
        # _download_file call. Bail out early if the adapter doesn't
        # expose the hooks (e.g. some future non-Slack adapter).
        client = None
        token = None
        if hasattr(adapter, "_get_client") and hasattr(adapter, "_token_for_client"):
            try:
                client = adapter._get_client(source.chat_id)  # type: ignore[attr-defined]
                if client is not None:
                    token = adapter._token_for_client(client)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                client = None
                token = None

        seen_file_ids: set[str] = set()
        for m in reversed(msgs):
            files = m.get("files") or []
            if not files:
                continue
            ts = str(m.get("ts") or "")
            refs = file_refs_by_ts.setdefault(ts, [])
            for f in files:
                name = str(f.get("name") or "(unnamed)")
                mime = str(f.get("mimetype") or "")
                is_image = mime.startswith("image/") or _looks_like_image_name(name)
                # Always annotate the text block with the file reference.
                if is_image:
                    refs.append(f"[image: {name}]")
                else:
                    refs.append(f"[file: {name}]")
                # Stop downloading once we hit the image budget; just
                # annotate the rest by name.
                if len(prior_attachments) >= MAX_PRIOR_IMAGES:
                    continue
                if not is_image:
                    continue
                if token is None or not hasattr(adapter, "_download_file"):
                    continue
                size = f.get("size") or 0
                try:
                    size_int = int(size)
                except (TypeError, ValueError):
                    size_int = 0
                if 0 < MAX_PER_IMAGE_BYTES < size_int:
                    continue
                fid = str(f.get("id") or "")
                if fid and fid in seen_file_ids:
                    continue
                if fid:
                    seen_file_ids.add(fid)
                try:
                    saved = await adapter._download_file(f, token)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    logger.exception("prior-context image download failed")
                    saved = None
                if saved and saved.get("type") == "image":
                    prior_attachments.append(saved)
        # Restore chronological order.
        prior_attachments.reverse()

        # ── Render the text block ──
        # Best-effort user-name resolution. If the adapter exposes
        # ``_resolve_user_name``, we use it to swap raw <@U123> ids for
        # display names in the line label. Falls back to <@U123>.
        async def _label(role: str, user_id: str) -> str:
            if role == "assistant":
                return "you (assistant)"
            if not user_id:
                return "user"
            if hasattr(adapter, "_resolve_user_name"):
                try:
                    name = await adapter._resolve_user_name(user_id, None)  # type: ignore[attr-defined]
                    if name:
                        return name
                except Exception:  # noqa: BLE001
                    pass
            return f"<@{user_id}>"

        lines: list[str] = []
        for m in msgs:
            role_label = await _label(
                m.get("role", "user"),
                str(m.get("user_id") or ""),
            )
            text = (m.get("text") or "").strip().replace("\n", " ")
            if len(text) > 1500:
                text = text[:1500] + "…"
            refs = file_refs_by_ts.get(str(m.get("ts") or ""), [])
            if refs:
                refs_str = " ".join(refs)
                text = f"{text} {refs_str}" if text else refs_str
            lines.append(f"{role_label}: {text}")
        return "\n".join(lines), prior_attachments

    async def _handle_slash_in_gateway(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """Reply to certain slashes directly without involving the
        agent. Returns True if handled, False to fall through."""
        cmd = (message.slash_command_name or "").lower()

        if cmd == "freyja":
            sub = (message.slash_command_args or "").strip().lower()
            if sub in {"", "help"}:
                text = _help_card_text()
            elif sub == "status":
                text = await self._render_status(message)
            elif sub in {"perms", "permissions"}:
                text = await self._render_perms(message)
            else:
                text = (
                    "Unknown subcommand. Try `/freyja help`, "
                    "`/freyja status`, or `/freyja perms`."
                )
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "status":
            text = await self._render_status(message)
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "stop":
            # Try to cancel the session's pending_task if any.
            from bridge.gateway.session_router import session_key_for
            key = session_key_for(message.source)
            cancelled = self._cancel_session(key)
            text = (
                "Interrupting current turn." if cancelled
                else "Nothing in flight on this session."
            )
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                text,
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        # /reset → clear the session's transcript so the next message
        # starts fresh. v1: just drop the session from the dict so the
        # next message creates a new one (transcript persists on disk
        # but the in-memory pending state resets).
        if cmd == "reset":
            from bridge.gateway.session_router import session_key_for
            key = session_key_for(message.source)
            self._reset_session(key)
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                "Session reset. Next message starts fresh.",
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        if cmd == "mode":
            return await self._handle_mode_command(message, adapter)

        if cmd == "model":
            return await self._handle_model_command(message, adapter)

        if cmd == "goal":
            return await self._handle_goal_command(message, adapter)

        if cmd == "perms":
            return await self._handle_perms_command(message, adapter)

        return False

    async def _handle_mode_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/mode <strategy>` — change coordination strategy on the
        active session. Creates the session if it doesn't yet exist."""
        from bridge.gateway.session_router import session_key_for
        from bridge.tools.coordination import normalize_coordination_strategy  # noqa: F401

        target = (message.slash_command_args or "").strip().lower()
        if target not in {"bus", "goal", "kanban", "isolated"}:
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                "Usage: `/mode <bus|goal|kanban|isolated>`",
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key,
            coordination_strategy=target,
        )
        # If the session existed with a different strategy, ensure_session
        # already swapped it. Re-emit confirmation for the operator.
        prev = getattr(session, "coordination_strategy", "?")
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            f"Coordination strategy set to `{prev}`.",
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_model_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/model <id>` — change the model the session uses for the
        next turn."""
        from bridge.gateway.session_router import session_key_for

        target = (message.slash_command_args or "").strip()
        if not target:
            # Show current model for this session.
            key = session_key_for(message.source)
            sessions = getattr(self.state, "sessions", {}) if self.state else {}
            session = sessions.get(key)
            current = getattr(session, "model_id", None) if session else None
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                (
                    f"Current model: `{current}`\nUsage: `/model <id>` "
                    "(e.g. `claude-opus-4-7`, `claude-sonnet-4-6`, `gpt-5.5`)"
                    if current else
                    "No active session. Usage: `/model <id>`"
                ),
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key, model_id=target,
        )
        applied = getattr(session, "model_id", target)
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            f"Model set to `{applied}`.",
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_goal_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/goal <objective>` — flip the session into goal mode and
        arm the goal loop with the given objective. The next turn (the
        operator's follow-up, or this one's body if non-empty) will be
        evaluated by the judge each iteration."""
        from bridge.gateway.session_router import session_key_for

        objective = (message.slash_command_args or "").strip()
        if not objective:
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                (
                    "Usage: `/goal <objective>`\n"
                    "Example: `/goal write me an async http client with retry`"
                ),
                thread_id=message.source.thread_id,
                ephemeral_user_id=message.source.user_id,
                raw_hint=message.raw,
            )
            return True

        key = session_key_for(message.source)
        if self.state is None:
            return True
        session = await self.state.ensure_session(
            session_id=key, coordination_strategy="goal",
        )
        # Stash gateway_source so the next turn's framing carries it.
        setattr(session, "gateway_source", message.source)
        # _set_goal arms the loop + auto-fires the calibrator in parallel.
        if hasattr(session, "_set_goal"):
            session._set_goal(objective, source="gateway-slack")
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            (
                f"Goal loop armed:\n>>> {objective}\n\n"
                "The judge will review my work each turn. Send a follow-up "
                "to kick off the first agent turn, or just wait — "
                "calibration is running."
            ),
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _handle_perms_command(
        self,
        message: IncomingMessage,
        adapter: object,
    ) -> bool:
        """`/perms` — show the capability set this gateway session has."""
        await adapter.send(  # type: ignore[attr-defined]
            message.source.chat_id,
            await self._render_perms(message),
            thread_id=message.source.thread_id,
            ephemeral_user_id=message.source.user_id,
            raw_hint=message.raw,
        )
        return True

    async def _render_perms(self, message: IncomingMessage) -> str:
        from bridge.gateway.capabilities import (
            gateway_filter_enabled,
            tools_allowed_for_gateway,
        )
        platform = message.source.platform
        if not gateway_filter_enabled(platform):
            return (
                "*Slack agent permissions*\n"
                "Full tool surface — same as the desktop app. "
                "Bash, computer-use, browser, file write, memory mutations, "
                "image generation, sub-agents are all available.\n"
                "\n"
                "_To restrict_, set `slack.enable_tool_filter: true` in "
                "`~/.freyja/gateway.yaml` and restart the gateway "
                "(`launchctl stop co.freyja.gateway && launchctl start "
                "co.freyja.gateway`). Default allowlist is read-mostly."
            )
        allowed = sorted(tools_allowed_for_gateway(platform))
        lines = [
            "*Slack agent permissions* (restricted)",
            "Allowed tools (read-mostly):",
        ]
        for t in allowed:
            lines.append(f"  • `{t}`")
        lines.append("")
        lines.append(
            "_Not allowed over Slack:_ bash, computer, browser, mouse/keyboard, "
            "screenshot, memory mutations. To grant any of those, switch to "
            "the Freyja desktop app or flip `slack.enable_tool_filter: false`."
        )
        return "\n".join(lines)

    def _cancel_session(self, session_key: str) -> bool:
        if self.state is None:
            return False
        sessions = getattr(self.state, "sessions", {})
        session = sessions.get(session_key)
        if session is None:
            return False
        task = getattr(session, "pending_task", None)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def _reset_session(self, session_key: str) -> None:
        if self.state is None:
            return
        sessions = getattr(self.state, "sessions", {})
        if session_key in sessions:
            # Best-effort cancel before drop.
            self._cancel_session(session_key)
            sessions.pop(session_key, None)

    async def _render_status(self, message: IncomingMessage) -> str:
        from bridge.gateway.session_router import session_key_for
        key = session_key_for(message.source)
        sessions = getattr(self.state, "sessions", {}) if self.state else {}
        session = sessions.get(key)
        lines: list[str] = ["*Freyja session status*"]
        lines.append(f"• key: `{key}`")
        if session is None:
            lines.append("• status: _no active session_ (next message will create one)")
            return "\n".join(lines)
        model = getattr(session, "model_id", "?")
        strategy = getattr(session, "coordination_strategy", "?")
        pending = getattr(session, "pending_task", None)
        queued = len(getattr(session, "queued_messages", []) or [])
        in_flight = bool(pending and not pending.done())
        lines.append(f"• model: `{model}`")
        lines.append(f"• mode: `{strategy}`")
        lines.append(f"• in-flight: {'yes' if in_flight else 'no'}")
        lines.append(f"• queued messages: {queued}")
        return "\n".join(lines)

    async def start(self) -> None:
        """Stand up the bridge state + connect all configured adapters."""
        from bridge.freyja_bridge import _BridgeState

        workspace = os.environ.get("FREYJA_WORKSPACE") or str(Path.home())
        default_model = os.environ.get("FREYJA_MODEL", "claude-sonnet-4-6")
        self.state = _BridgeState(
            workspace=workspace, default_model=default_model
        )
        logger.info(
            "bridge state ready (workspace=%s, model=%s)",
            workspace, default_model,
        )

        # Slack adapter is the only one in v1. Future: read
        # ~/.freyja/gateway.yaml to enable/disable adapters.
        adapter = SlackAdapter()
        ok = await adapter.connect(self._on_inbound)
        if ok:
            self.adapters.append(adapter)
            # Register with the destructive-command approval module
            # so the bridge's tool wrapper can route approval prompts
            # to this adapter. Done after connect succeeds — no point
            # registering a dead adapter.
            try:
                from bridge.gateway.approval import register_approval_adapter
                register_approval_adapter(Platform.SLACK.value, adapter)
            except Exception:  # noqa: BLE001
                logger.exception("approval adapter registration failed")
            logger.info("slack adapter connected")
        else:
            logger.warning(
                "slack adapter did not connect — set SLACK_BOT_TOKEN + "
                "SLACK_APP_TOKEN via `freyja setup slack`"
            )

        if not self.adapters:
            logger.warning(
                "no platform adapters connected — gateway running idle. "
                "Configure at least one adapter to receive messages."
            )

        # Bring up the desktop → daemon control channel last so it's
        # only servicing commands once the bridge state and adapters are
        # ready to act on them. Without this the daemon could receive a
        # permission_response before it had a session map to look up.
        await self._start_control_channel()

        # Sweep any sessions whose last logged event was a permission_request
        # without a matching permission_resolved — those are abandoned
        # prompts left over from a previous daemon crash / forced restart.
        # We emit a synthetic permission_resolved with reason="daemon_restart"
        # so the desktop UI's pending-permission queue clears the stale
        # entry instead of carrying it across restarts forever.
        try:
            self._sweep_orphaned_permission_requests()
        except Exception:  # noqa: BLE001
            logger.exception("startup permission sweep raised")

    def _sweep_orphaned_permission_requests(self) -> None:
        """Find permission_requests that were never resolved and close them."""
        import json as _json
        sessions_dir = Path(
            os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
        ) / "sessions"
        if not sessions_dir.exists():
            return
        # Only scan recently-touched event logs — old sessions don't
        # benefit from cleanup and walking the full directory on each
        # restart would scale badly.
        cutoff = time.time() - 7 * 24 * 3600
        from bridge.freyja_bridge import emit as _emit
        orphans_cleared = 0
        for path in sessions_dir.glob("*.events.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            pending: dict[str, dict[str, Any]] = {}
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line or line[0] != "{":
                            continue
                        try:
                            ev = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        t = ev.get("type")
                        rid = ev.get("requestId")
                        if not rid:
                            continue
                        if t == "permission_request":
                            pending[rid] = ev
                        elif t == "permission_resolved":
                            pending.pop(rid, None)
            except OSError:
                continue
            for rid, ev in pending.items():
                _emit(
                    {
                        "type": "permission_resolved",
                        "sessionId": ev.get("sessionId"),
                        "requestId": rid,
                        "approved": False,
                        "response": "interrupted by daemon restart",
                        "reason": "daemon_restart",
                    }
                )
                orphans_cleared += 1
        if orphans_cleared:
            logger.info(
                "startup sweep cleared %d orphaned permission_request(s)",
                orphans_cleared,
            )

    async def _start_control_channel(self) -> None:
        reader = ControlChannelReader()
        reader.register("permission_response", self._on_permission_response)
        reader.register("set_permission_policy", self._on_set_permission_policy)
        await reader.start()
        self.control_channel = reader

    def _on_set_permission_policy(self, cmd: dict[str, Any]) -> None:
        """Adjust a per-session autonomy tier from the desktop.

        Schema:
          { "type": "set_permission_policy",
            "sessionId": "freyja:slack:...",
            "autoApprove": "low" | "medium" | "high" | "yolo" | "none" }
        """
        session_id = str(cmd.get("sessionId") or "")
        tier = str(cmd.get("autoApprove") or "").strip().lower()
        if not session_id or not tier or self.state is None:
            return
        sessions = getattr(self.state, "sessions", {}) or {}
        sess = sessions.get(session_id)
        if sess is None:
            logger.info(
                "control: set_permission_policy for unknown session %s — ignoring",
                session_id,
            )
            return
        try:
            sess.permission_tier = tier
            if getattr(sess, "permission_handler", None) is not None:
                sess.permission_handler.set_policy(tier)
            logger.info(
                "control: session %s permission tier set to %s",
                session_id, tier,
            )
        except Exception:  # noqa: BLE001
            logger.exception("control: set_permission_policy raised")

    def _on_permission_response(self, cmd: dict[str, Any]) -> None:
        """Resolve a per-session ``DesktopPermissionHandler`` future.

        Sent by the desktop renderer when the operator clicks
        approve/deny on the permission modal that appears for a
        gateway-routed session.

        Schema:
          { "type": "permission_response",
            "sessionId": "freyja:slack:...",
            "requestId": "<uuid hex>",
            "approved": bool,
            "response": "optional human note" }

        We look up the session by id (any platform — not Slack-specific)
        and resolve via the handler's ``.resolve`` method. Falls back to
        ``approval.resolve_approval`` so the same desktop click can also
        settle a Slack-originated destructive-tool prompt. Idempotent —
        a re-issued response on an already-resolved request is a no-op.
        """
        session_id = str(cmd.get("sessionId") or "")
        request_id = str(cmd.get("requestId") or "")
        approved = bool(cmd.get("approved"))
        response_text = str(cmd.get("response") or "")
        if not request_id:
            logger.warning("control: permission_response missing requestId")
            return
        resolved = False
        if session_id and self.state is not None:
            sessions = getattr(self.state, "sessions", {}) or {}
            sess = sessions.get(session_id)
            if sess is not None:
                handler = getattr(sess, "permission_handler", None)
                if handler is not None:
                    try:
                        resolved = bool(handler.resolve(
                            request_id, approved, response_text,
                        ))
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "control: permission_handler.resolve raised",
                        )
        if not resolved:
            # Fall through to the gateway-wide approval registry — the
            # request_id might belong to a tool-level destructive prompt,
            # or to an external resolver registered by a stream consumer.
            try:
                from bridge.gateway.approval import resolve_approval
                resolved = bool(resolve_approval(request_id, approved))
            except Exception:  # noqa: BLE001
                logger.exception("control: resolve_approval raised")
        if not resolved:
            logger.info(
                "control: permission_response %s ignored (unknown/already resolved)",
                request_id,
            )

    async def shutdown(self) -> None:
        logger.info("gateway shutdown beginning")
        # Stop the control channel first so a late command can't fire
        # against half-shut-down state.
        if self.control_channel is not None:
            try:
                await self.control_channel.stop()
            except Exception:  # noqa: BLE001
                logger.exception("control channel stop raised")
            self.control_channel = None
        try:
            from bridge.gateway.approval import unregister_approval_adapter
            unregister_approval_adapter(Platform.SLACK.value)
        except Exception:  # noqa: BLE001
            pass
        for adapter in self.adapters:
            try:
                await adapter.disconnect()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("adapter disconnect raised")
        self.adapters.clear()
        logger.info("gateway shutdown complete")

    def request_shutdown(self, *, planned: bool = True) -> None:
        self._planned_exit = self._planned_exit or planned
        self.shutdown_event.set()

    @property
    def planned_exit(self) -> bool:
        return self._planned_exit


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, daemon: GatewayDaemon) -> None:
    def _on_sigterm() -> None:
        # If a takeover marker addressed at us is present, this SIGTERM
        # was planned by an incoming daemon — exit 0 so launchd doesn't
        # restart us into a flap loop.
        is_takeover = consume_takeover_marker()
        if is_takeover:
            logger.info("SIGTERM acknowledged (planned takeover)")
        else:
            logger.info("SIGTERM received")
        daemon.request_shutdown(planned=True)

    def _on_sigint() -> None:
        logger.info("SIGINT received")
        daemon.request_shutdown(planned=True)

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler. We're macOS-first
        # but the daemon is portable enough that this fallback matters.
        signal.signal(signal.SIGTERM, lambda *_: daemon.request_shutdown(planned=True))
        signal.signal(signal.SIGINT, lambda *_: daemon.request_shutdown(planned=True))


async def _async_main(*, replace: bool) -> int:
    if not acquire_lock(replace=replace):
        return 1

    _setup_logging()
    _load_env_into_os_environ()

    logger.info(
        "freyja gateway starting (pid=%d, home=%s)",
        os.getpid(),
        freyja_home(),
    )

    daemon = GatewayDaemon()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, daemon)

    try:
        await daemon.start()
    except Exception:
        logger.exception("daemon start failed")
        await daemon.shutdown()
        release_lock()
        return 2

    try:
        await daemon.shutdown_event.wait()
    finally:
        await daemon.shutdown()
        release_lock()

    return 0 if daemon.planned_exit else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freyja-gateway",
        description="Freyja messaging gateway daemon.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="If another gateway is running, take over from it.",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_async_main(replace=args.replace))
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
