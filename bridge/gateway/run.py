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


def _scheduler_help_card() -> str:
    return (
        "*Scheduler — `/freyja schedule|remind|loop|daemon`*\n\n"
        "*Create*\n"
        "• `/freyja remind <when> <prompt>` — one-shot reminder\n"
        "• `/freyja schedule add <when> <prompt> [--to <sinks>] [--in <execution>] [--name <label>]`\n"
        "• `/freyja loop <interval> <prompt>` — fixed-cadence loop\n"
        "• `/freyja loop until <cond> <prompt>` — runs until cond satisfied\n"
        "• `/freyja loop <prompt>` — self-paced; agent picks its own delays\n\n"
        "*Manage*\n"
        "• `/freyja schedule list [--mine] [--tag <tag>]`\n"
        "• `/freyja schedule get|pause|resume|remove|run|runs <id_or_prefix>`\n"
        "• `/freyja schedule metrics`\n\n"
        "*Daemon*\n"
        "• `/freyja daemon install|uninstall|status`\n"
        "  (auto-installs on first scheduled job — so jobs fire even with Freyja closed)\n\n"
        "*Sinks (`--to ...`, comma-separated)*\n"
        "`here`, `slack`, `desktop`, `session`, `laptop:/path/{date}.md`, "
        "`https://hook.url`, `noop`\n\n"
        "*Examples*\n"
        "• `/freyja remind in 30 minutes ping me about the deploy`\n"
        "• `/freyja schedule add every weekday at 9am summarize new PRs in repo X --to slack`\n"
        "• `/freyja schedule add every 5 minutes check uptime --to webhook:https://hook.example/uptime`\n"
        "• `/freyja loop 5m clean stale tabs`\n"
    )


def _arg_value(body: str, flag: str) -> str | None:
    """Pluck the value of a `--flag <val>` pair out of free-form text.
    Handles `--flag val`, `--flag=val`. Returns None if absent."""
    import re as _re
    m = _re.search(rf"{_re.escape(flag)}(?:[=\s]+)([^\s][^\-]*?)(?=\s+--|\s*$)", body)
    if not m:
        return None
    return m.group(1).strip().rstrip(",")


def _strip_args(body: str, flags: list[str]) -> str:
    """Remove `--flag value` pairs from body so the remainder is just
    the schedule + prompt text. Naive but covers our slash-command
    grammar."""
    import re as _re
    for flag in flags:
        body = _re.sub(
            rf"{_re.escape(flag)}(?:[=\s]+)[^\s][^\-]*?(?=\s+--|\s*$)",
            "",
            body,
        )
    return _re.sub(r"\s+", " ", body).strip()


def _split_when_prompt(body: str) -> tuple[str, str, str]:
    """Split a body like ``every weekday at 9am summarize my PRs`` into
    (when, separator, prompt). The boundary is hard — we try several
    heuristics in order of specificity.

    Returns (when, '', prompt) where prompt is everything after the
    matched when-phrase. Both can be empty if parsing fails."""
    import re as _re
    body = body.strip()
    if not body:
        return "", "", ""
    # Patterns: explicit colon ("every weekday at 9am: do X")
    if ":" in body:
        when, _, prompt = body.partition(":")
        return when.strip(), ":", prompt.strip()
    # "every <pattern> at <time> <prompt>"
    m = _re.match(
        r"(every\s+(?:weekday|weekdays|day|"
        r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:s)?"
        r"(?:\s+(?:and|,)\s+(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:s)?)*"
        r")\s+at\s+\S+)\s+(.+)$",
        body, _re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), " ", m.group(2).strip()
    # "every N <unit> <prompt>"
    m = _re.match(r"(every\s+\d+\s*[a-z]+)\s+(.+)$", body, _re.IGNORECASE)
    if m:
        return m.group(1).strip(), " ", m.group(2).strip()
    # "in N <unit> <prompt>"
    m = _re.match(r"(in\s+\d+\s*[a-z]+)\s+(.+)$", body, _re.IGNORECASE)
    if m:
        return m.group(1).strip(), " ", m.group(2).strip()
    # "tomorrow at X <prompt>" / "today at X <prompt>"
    m = _re.match(
        r"((?:tomorrow|today)\s+at\s+\S+)\s+(.+)$",
        body, _re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), " ", m.group(2).strip()
    # "at X <prompt>"
    m = _re.match(r"(at\s+\S+)\s+(.+)$", body, _re.IGNORECASE)
    if m:
        return m.group(1).strip(), " ", m.group(2).strip()
    # Fall through: assume the whole thing is the prompt, no schedule.
    return "", "", body


def _find_job_by_token(jobs: list[Any], token: str) -> Any | None:
    """Lookup by full id, id prefix (>=4 chars), or exact name."""
    token = (token or "").strip()
    if not token:
        return None
    for j in jobs:
        if j.id == token or j.name == token:
            return j
    if len(token) >= 4:
        for j in jobs:
            if j.id.startswith(token):
                return j
    return None


def _format_jobs_table(jobs: list[Any]) -> str:
    if not jobs:
        return "_No scheduled jobs yet. Try `/freyja schedule add …` or `/freyja remind …`._"
    lines = [f"*Scheduled jobs ({len(jobs)})*"]
    for j in jobs:
        next_at = _format_ts(j.next_fire_at)
        from bridge.scheduler.scheduling import cadence_label
        lines.append(
            f"• `{j.id}` *{j.name}* — {cadence_label(j.schedule)}\n"
            f"  status: {j.status} · next: {next_at} · sinks: "
            f"{','.join(s.kind for s in j.sinks) or 'none'}"
        )
    return "\n".join(lines)


def _format_job_detail(j: Any) -> str:
    from bridge.scheduler.scheduling import cadence_label
    return (
        f"*{j.name}* (`{j.id}`)\n"
        f"• Status: {j.status} · enabled: {j.enabled}\n"
        f"• Cadence: {cadence_label(j.schedule)}\n"
        f"• Next fire: {_format_ts(j.next_fire_at)}\n"
        f"• Last fire: {_format_ts(j.last_fire_at)} · fires: {j.fire_count}\n"
        f"• Execution: {j.execution.kind}\n"
        f"• Sinks: {', '.join(s.kind for s in j.sinks) or 'none'}\n"
        f"• Tags: {', '.join(j.tags) or '—'}\n"
        f"• Prompt: ```{j.prompt[:1000]}```"
    )


def _format_runs(job: Any, runs: list[Any]) -> str:
    if not runs:
        return f"_No runs yet for {job.name} ({job.id})._"
    lines = [f"*Recent runs for {job.name} (`{job.id}`)*"]
    for r in runs:
        lines.append(
            f"• `{r.run_id}` {r.status} · "
            f"started {_format_ts(r.started_at)} · "
            f"{r.duration_seconds:.1f}s · "
            f"sinks ok={sum(1 for d in r.delivery_reports if d.success)}/"
            f"{len(r.delivery_reports)}"
        )
    return "\n".join(lines)


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _autoname(prompt: str) -> str:
    words = (prompt or "").split()[:8]
    name = " ".join(words)
    return (name[:60] + "…") if len(name) > 60 else (name or "scheduled job")


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
        "• `/freyja help`    — this card\n"
        "• `/freyja status`  — show session info (model, mode, in-flight)\n"
        "• `/freyja perms`   — show tool permissions for this session\n"
        "• `/freyja models`  — list all models, harnesses, and modes\n"
        "• `/freyja verbose` — cycle tool-progress detail (off/new/all/verbose)\n"
        "• `/freyja remind <when> <prompt>` — one-shot reminder\n"
        "• `/freyja schedule add <when> <prompt>` — recurring scheduled job\n"
        "• `/freyja schedule list|pause|resume|remove|runs` — manage jobs\n"
        "• `/freyja loop <prompt>` — self-paced loop\n"
        "• `/freyja daemon install|status` — background scheduler daemon\n"
        "• `/goal <obj>`     — arm a goal loop\n"
        "• `/mode <s>`       — switch coordination (bus / goal / kanban / isolated)\n"
        "• `/model <id>`     — switch the agent model\n"
        "• `/stop`           — interrupt the current turn\n"
        "• `/reset`          — start a fresh conversation\n"
        "• `/perms`          — show tool permissions\n"
        "\n"
        "*Inline flags* (work inside threads where slash commands don't):\n"
        "  Append `--verbose`, `--all`, `--new`, or `--silent` to any "
        "message to set tool-progress detail. Sticky on the session.\n"
        "\n"
        "*Channels*: @mention me to start a thread, then keep replying in "
        "the thread without re-mentioning.\n"
        "*DMs*: just talk.\n"
        "*Files*: drop in any image, code, or document — I'll use it."
    )


_IMAGE_EXT_PATTERNS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")


def _format_ctx_window(n: int) -> str:
    """Compact context-window label.

    Avoids "1.0M" when the value is just a tweak above a round number
    (e.g. 1_048_576 → "1M", 1_050_000 → "1M"). Falls back to one
    decimal place only when the value is meaningfully off-round
    (1_500_000 → "1.5M", 1_100_000 → "1.1M")."""
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{round(val)}M" if abs(val - round(val)) < 0.08 else f"{val:.1f}M"
    if n >= 1_000:
        val = n / 1_000
        return f"{round(val)}k" if abs(val - round(val)) < 0.5 else f"{val:.1f}k"
    return str(n)


# Display-name overrides where ``provider.title()`` produces something
# wrong (e.g. "Openai" instead of "OpenAI"). Anything not in this map
# falls through to ``.title()``.
_PROVIDER_DISPLAY = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "cerebras": "Cerebras",
    "fireworks": "Fireworks",
    "groq": "Groq",
    "xai": "xAI",
    "parallel": "Parallel",
}


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

        # If the session's transcript is large, surface a quick
        # "catching up" notice in Slack so the operator doesn't stare
        # at an empty typing indicator while the bridge sanitizes the
        # history + the LLM warms up on a fat context. The threshold
        # is intentionally chatty — even ~30 messages is enough that
        # a multi-second startup gap is worth explaining. The pre-turn
        # sanitizer (freyja_bridge._sanitize_session_oversize_images)
        # will follow this notice, dim-aware after the most recent
        # patch — so any oversized screenshots from earlier in this
        # DM get rewritten in place before the LLM call.
        await self._maybe_send_catching_up_notice(session, adapter, message)

        # Slash commands without text body (just /status, /freyja help)
        # still get routed as agent turns — the slash text becomes the
        # user message. Future: shortcut some slashes to in-gateway
        # handlers without involving the agent.

        # Parse any inline verbosity flags (--verbose, --silent, --new
        # etc.) out of the user text. This is the only way to control
        # tool-progress verbosity inside a Slack thread, since slash
        # commands don't fire in threads. The flag is sticky on the
        # session — set once, lasts until changed.
        from bridge.gateway.session_router import (
            parse_verbosity_flags,
            normalize_verbosity,
        )
        flag_level, cleaned_text = parse_verbosity_flags(message.text or "")
        if flag_level is not None:
            session.verbosity = flag_level  # type: ignore[attr-defined]
            # Strip the flag tokens from the text so the agent's prompt
            # doesn't carry them. IncomingMessage is a mutable dataclass.
            message.text = cleaned_text
        verbosity = normalize_verbosity(getattr(session, "verbosity", None))

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

        # Permission-prompt dispatcher: registered once per session and
        # left in place for the daemon's lifetime so background-mode
        # sub-agents that fire permission_requests AFTER their parent's
        # turn completes still get a Block Kit message in the originating
        # thread. The per-turn consumer no longer handles permission
        # events — single dispatch path, no risk of duplicate posts.
        if not getattr(session, "_slack_perm_listener", None):
            from bridge.gateway.permission_listener import SlackPermissionListener

            reply_thread_id = (
                message.source.thread_id
                or (
                    message.source.message_id
                    if message.source.chat_type == "dm"
                    else None
                )
            )
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = asyncio.get_event_loop()
            perm_listener = SlackPermissionListener(
                adapter=adapter,
                session_ref=session,
                source=message.source,
                reply_thread_id=reply_thread_id,
                loop=_loop,
            )
            register_session_listener(key, perm_listener.on_event)
            setattr(session, "_slack_perm_listener", perm_listener)

        consumer = SlackStreamConsumer(
            adapter,  # type: ignore[arg-type]
            message.source,
            session_key=key,
            raw_hint=message.raw,
            on_complete=_unregister,
            verbosity=verbosity,
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
        #
        # Pass the most recent platform ts we've already ingested for
        # this session so the fetch is INCREMENTAL: only messages
        # newer than our persisted transcript get pulled. Without
        # this, the same 100 thread replies / 15 DM messages get
        # re-injected on every turn alongside the persisted
        # transcript — doubling context and letting stale prior-turn
        # topics bleed into the new turn.
        last_seen_ts = None
        try:
            inner = getattr(session, "session", None)
            meta = getattr(inner, "metadata", None) if inner else None
            if isinstance(meta, dict):
                last_seen_ts = meta.get("last_inbound_platform_ts")
        except Exception:  # noqa: BLE001
            last_seen_ts = None
        prior_block, prior_attachments = await self._fetch_prior_context(
            message, adapter, oldest_platform_ts=last_seen_ts,
        )

        # Now that we've read the old ts, advance the high-water mark
        # to the message we just ingested. Persisted alongside session
        # metadata so the next daemon restart still gets incremental
        # fetches for this thread/DM. Only write if we have a usable
        # ts — empty / None means we'd silently disable the optimisation
        # on the next turn.
        try:
            current_ts = getattr(message.source, "message_id", None)
            inner = getattr(session, "session", None)
            meta = getattr(inner, "metadata", None) if inner else None
            if (
                isinstance(meta, dict)
                and current_ts
                and (last_seen_ts is None or str(current_ts) > str(last_seen_ts))
            ):
                meta["last_inbound_platform_ts"] = str(current_ts)
        except Exception:  # noqa: BLE001
            logger.debug("failed to advance last_inbound_platform_ts", exc_info=True)

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

    # Thresholds for the "catching up" notice. Tuned so we stay quiet
    # on routine threads (no notice when the LLM call will be quick)
    # but speak up before chewing through a large transcript. Below
    # 100k tokens / 50 messages the startup gap is short enough that
    # a notice would just add noise; above 500k tokens we explicitly
    # mention compaction since the runner is likely to summarize.
    _CATCHING_UP_TOKEN_THRESHOLD = 100_000
    _CATCHING_UP_BIG_TOKEN_THRESHOLD = 500_000
    _CATCHING_UP_MSG_THRESHOLD = 50

    async def _maybe_send_catching_up_notice(
        self,
        session: Any,
        adapter: Any,
        message: IncomingMessage,
    ) -> None:
        """Emit a brief Slack message before the LLM call when the
        session's persisted transcript is large enough that the user
        would otherwise stare at a typing indicator for several
        seconds wondering if anything's happening.

        Counts come straight off the in-memory session — no extra
        traversal needed beyond a len(). Best-effort: any failure
        here must not block the actual turn from running.
        """
        try:
            # `session` here is `_BridgeSession`, which wraps the engine
            # `Session`. The transcript lives one level deeper — and
            # accessing the wrong attribute silently returned None,
            # making the notice a no-op for every session.
            inner = getattr(session, "session", None)
            transcript = (
                getattr(inner, "transcript", None)
                if inner is not None
                else getattr(session, "transcript", None)
            )
            if transcript is None:
                return
            entries = list(getattr(transcript, "entries", []) or [])
            n_msgs = sum(1 for e in entries if getattr(e, "message", None) is not None)
            # Token estimate — peak input_tokens across all assistant
            # turns. Each turn's input already includes its full prior
            # history, so the MAX across turns is the best lower bound
            # on how big the *next* prompt will be. The last turn alone
            # can be misleading right after a compaction (the kept tail
            # is tiny so input_tokens drops) even though the conversation
            # itself is still long.
            est_tokens = 0
            for e in entries:
                msg = getattr(e, "message", None)
                if msg is None:
                    continue
                in_tok = int(getattr(msg, "input_tokens", 0) or 0)
                if in_tok > est_tokens:
                    est_tokens = in_tok

            if (
                n_msgs < self._CATCHING_UP_MSG_THRESHOLD
                and est_tokens < self._CATCHING_UP_TOKEN_THRESHOLD
            ):
                return

            # Format the count compactly.
            if est_tokens >= 1_000_000:
                tok_str = f"~{est_tokens / 1_000_000:.1f}M tokens"
            elif est_tokens >= 1_000:
                tok_str = f"~{est_tokens // 1_000}k tokens"
            else:
                tok_str = f"~{est_tokens} tokens"

            big = est_tokens >= self._CATCHING_UP_BIG_TOKEN_THRESHOLD
            tail = (
                "Compacting history before responding — back to you "
                "in a moment."
            ) if big else (
                "Catching up — back to you shortly."
            )
            text = (
                f"_Looking at {n_msgs} prior messages ({tok_str}) in "
                f"this conversation. {tail}_"
            )

            src = message.source
            logger.info(
                "catching-up notice firing: chat=%s thread=%s msgs=%d tokens=%d",
                src.chat_id,
                src.thread_id,
                n_msgs,
                est_tokens,
            )
            try:
                result = await adapter.send(
                    src.chat_id,
                    text,
                    thread_id=src.thread_id,
                    raw_hint=message.raw,
                )
                if not getattr(result, "ok", True):
                    logger.warning(
                        "catching-up notice send returned not-ok: %s",
                        getattr(result, "error", "?"),
                    )
            except Exception:  # noqa: BLE001
                logger.warning("catching-up notice send raised", exc_info=True)
        except Exception:  # noqa: BLE001
            # Never block the turn on a UI notice failure.
            logger.warning("catching-up notice raised", exc_info=True)

    async def _fetch_prior_context(
        self,
        message: IncomingMessage,
        adapter: object,
        *,
        oldest_platform_ts: str | None = None,
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
                # Bumped to 100 with parent+tail pagination. The old
                # ``limit=50`` translated 1:1 into a single Slack page,
                # which on long threads returned the *first* 50 messages
                # (the original framing) and missed every recent reply.
                # The adapter now walks pages forward, keeps the rolling
                # last 100 replies, and pins the parent — so the agent
                # sees both "what this thread is about" and "what just
                # got said" instead of just the stale opening.
                msgs = await adapter.fetch_thread_context(  # type: ignore[attr-defined]
                    source.chat_id,
                    source.thread_id,
                    limit=100,
                    exclude_ts=trigger_ts,
                    oldest_ts=oldest_platform_ts,
                )
            elif source.chat_type == "dm" and hasattr(adapter, "fetch_dm_history"):
                msgs = await adapter.fetch_dm_history(  # type: ignore[attr-defined]
                    source.chat_id,
                    limit=15,
                    exclude_ts=trigger_ts,
                    oldest_ts=oldest_platform_ts,
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
            role = m.get("role", "user")
            # Synthetic gap-marker injected by fetch_thread_context when
            # the thread is longer than the per-turn window. Render as a
            # centered ellipsis rather than routing through _label
            # (which would prefix it with "user:" and make it look like
            # someone in the thread literally said "N replies omitted").
            if role == "system_note":
                marker = (m.get("text") or "").strip()
                if marker:
                    lines.append(f"  … {marker} …")
                continue
            role_label = await _label(
                role,
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
            elif sub in {"models", "model", "list", "harnesses", "harness"}:
                text = await self._render_models(message)
            elif sub.startswith("verbose") or sub.startswith("verbosity") or sub.startswith("quiet"):
                # `verbose`           → cycle
                # `verbose status`    → show current
                # `verbose off|new|all|verbose` → set explicitly
                # `quiet`             → shortcut for verbose off
                arg = sub.split(maxsplit=1)
                action = arg[1].strip().lower() if len(arg) > 1 else ""
                text = await self._handle_verbose_subcommand(
                    message,
                    action if action else ("off" if sub.startswith("quiet") else ""),
                )
            elif sub.startswith("schedule") or sub.startswith("remind") or sub.startswith("loop") or sub.startswith("daemon"):
                text = await self._handle_scheduler_subcommand(message)
            else:
                text = (
                    "Unknown subcommand. Try `/freyja help`, "
                    "`/freyja status`, `/freyja perms`, `/freyja models`, "
                    "`/freyja verbose`, `/freyja schedule`, "
                    "`/freyja remind`, `/freyja loop`, or `/freyja daemon`."
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

        if cmd == "learn-this" or cmd == "learn_this":
            # Operator-issued forced trip: bypass cadence and spawn the
            # drafter right now. The drafter consumes the conversation
            # snapshot and proposes a candidate; operator confirmation
            # still required before promotion.
            from bridge.gateway.session_router import session_key_for
            key = session_key_for(message.source)
            sess = (self.state.sessions if self.state else {}).get(key)
            if sess is None:
                reply = "No active session. Start a thread first, then /learn-this."
            elif getattr(sess, "skill_cadence_counter", None) is None:
                reply = "Skill learning is unavailable on this session."
            else:
                try:
                    sess.skill_cadence_counter.force_trip()
                    sess._spawn_drafter_review()
                    reply = "Drafter running — Block Kit card will follow when the candidate is ready."
                except Exception as exc:  # noqa: BLE001
                    logger.exception("/learn-this failed")
                    reply = f"/learn-this errored: {exc}"
            await adapter.send(  # type: ignore[attr-defined]
                message.source.chat_id,
                reply,
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

    async def _handle_verbose_subcommand(
        self,
        message: IncomingMessage,
        action: str,
    ) -> str:
        """`/freyja verbose [off|new|all|verbose|status]`.

        Without an arg: cycles off → new → all → verbose → off (Hermes
        convention — operator can rotate to the right level without
        remembering names). With ``status``: shows current level + how
        to override per-message. With an explicit level name: sets it.

        Sticky on the session. The setting also applies to subsequent
        messages in the same Slack chat/thread (since session_key is
        per chat+thread).
        """
        from bridge.gateway.session_router import (
            VERBOSITY_LEVELS,
            cycle_verbosity,
            normalize_verbosity,
            session_key_for,
        )
        sessions = getattr(self.state, "sessions", {}) if self.state else {}
        session = sessions.get(session_key_for(message.source))
        current = normalize_verbosity(
            getattr(session, "verbosity", None) if session is not None else None
        )

        descriptions = {
            "off": "no Task Cards — only the agent's prose response",
            "new": "one Task Card per unique tool — consecutive same-tool calls coalesce into one card with ×N count",
            "all": "one Task Card per call (default — Slack auto-collapses; no visual cost)",
            "verbose": "every call + thinking + heartbeat noise in cards",
        }

        if action == "status" or action == "show":
            inline_hint = (
                "_To override for one message:_ append `--verbose`, "
                "`--all`, `--new`, or `--silent` (alias for off) to any "
                "Slack message — works in threads where slash commands "
                "don't fire."
            )
            return (
                f"*Verbosity:* `{current}` — {descriptions[current]}\n\n"
                f"Cycle with `/freyja verbose` (no arg) or set explicitly: "
                f"{', '.join(f'`{lv}`' for lv in VERBOSITY_LEVELS)}.\n"
                f"{inline_hint}"
            )

        if action in VERBOSITY_LEVELS:
            new_level = action
        elif action == "" or action == "cycle":
            new_level = cycle_verbosity(current)
        else:
            return (
                f"Unknown verbosity level `{action}`. Try one of: "
                + ", ".join(f"`{lv}`" for lv in VERBOSITY_LEVELS)
                + " — or just `/freyja verbose` to cycle."
            )

        if session is not None:
            session.verbosity = new_level
            return (
                f"Verbosity set to *`{new_level}`*: {descriptions[new_level]}.\n"
                f"Override per message by appending `--{new_level}` or another "
                f"flag — flags at message start/end stick to this session."
            )
        # No session yet — store as a pending preference. Next message
        # in this chat will pick it up via the session_router's first
        # ensure_session and we'll apply it then. v1: just acknowledge.
        return (
            f"Verbosity set to *`{new_level}`*: {descriptions[new_level]}. "
            f"(Will apply on your next message in this chat.)"
        )

    async def _handle_scheduler_subcommand(
        self,
        message: IncomingMessage,
    ) -> str:
        """``/freyja schedule|remind|loop|daemon`` — wrap the
        SchedulerService API as Slack slash commands.

        Grammar (subset of the model-callable schedule tool; same
        verbs, simpler args parsed from chat text):

          /freyja schedule add <when> <prompt> [--to <sinks>] [--in <execution>]
          /freyja schedule list [--mine] [--tag <tag>]
          /freyja schedule get <id>
          /freyja schedule pause|resume|remove <id>
          /freyja schedule run <id>
          /freyja remind <when> <prompt>            # one-shot alias
          /freyja loop [interval | until <cond>] [prompt]
          /freyja daemon install|uninstall|status

        Anything ambiguous falls through to a help blurb. Heavy lifting
        is done by the SchedulerService — this is purely a translator
        from chat text to typed API calls.
        """
        from bridge.scheduler.models import (
            BudgetSpec,
            JobFilter,
            JobRecord,
            SelfPacedSchedule,
        )
        from bridge.scheduler.scheduling import cadence_label, parse_when
        from bridge.scheduler.service import (
            build_creator_ref,
            build_execution,
            build_sinks,
        )

        state = self.state  # type: ignore[union-attr]
        service = getattr(state, "scheduler", None)
        if service is None:
            return "_Scheduler not available (bridge state not ready)._"

        raw = (message.slash_command_args or "").strip()
        # Already-routed via /freyja so the first word of raw is the
        # subcommand (schedule/remind/loop/daemon).
        if not raw:
            return _scheduler_help_card()
        head, _, rest = raw.partition(" ")
        head = head.lower()
        rest = rest.strip()

        # Build a creator ref from the inbound message.
        src = message.source
        creator = build_creator_ref(
            surface=src.platform.value,
            session_id="",  # filled below from session_router
            user_id=src.user_id,
            workspace_id=src.workspace_id,
            chat_id=src.chat_id,
            thread_id=src.thread_id or src.message_id,
            user_name=src.user_name,
        )
        # Best-effort session resolution so creator.session_id is set.
        try:
            from bridge.gateway.session_router import session_key_for
            creator.session_id = session_key_for(src)
        except Exception:  # noqa: BLE001
            pass

        try:
            if head == "daemon":
                return await self._scheduler_daemon_subcommand(rest)
            if head == "remind":
                return await self._scheduler_create_oneshot(
                    rest, service=service, creator=creator,
                )
            if head == "loop":
                return await self._scheduler_create_loop(
                    rest, service=service, creator=creator,
                )
            # ``schedule …`` head
            return await self._scheduler_schedule_subcommand(
                rest, service=service, creator=creator,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduler slash handler crashed")
            return f"_Scheduler error: {exc}_"

    async def _scheduler_schedule_subcommand(
        self,
        rest: str,
        *,
        service: Any,
        creator: Any,
    ) -> str:
        """Dispatch the second word of ``/freyja schedule X …``."""
        from bridge.scheduler.models import JobFilter
        from bridge.scheduler.scheduling import cadence_label
        from bridge.scheduler.service import build_execution, build_sinks

        if not rest:
            return _scheduler_help_card()
        verb, _, body = rest.partition(" ")
        verb = verb.lower()
        body = body.strip()

        if verb in ("add", "create", "new"):
            return await self._scheduler_create_recurring(
                body, service=service, creator=creator,
            )
        if verb == "list":
            mine = "--mine" in body
            tag = _arg_value(body, "--tag")
            filt = JobFilter(
                user_id=creator.user_id if mine else None,
                tag=tag,
            )
            jobs = await service.list_jobs(filt)
            return _format_jobs_table(jobs)
        if verb in ("get", "show"):
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            return _format_job_detail(job)
        if verb in ("pause", "stop"):
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            await service.pause_job(job.id)
            return f"Paused *{job.name}* (`{job.id}`)."
        if verb in ("resume", "start"):
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            await service.resume_job(job.id)
            return f"Resumed *{job.name}* (`{job.id}`). Next fire: {_format_ts(job.next_fire_at)}"
        if verb in ("remove", "delete", "rm"):
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            await service.remove_job(job.id)
            return f"Removed *{job.name}* (`{job.id}`)."
        if verb in ("run", "run_now"):
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            run = await service.run_job_now(job.id)
            return (
                f"Ran *{job.name}* (`{job.id}`): {run.status}.\n"
                f"```{(run.output_text or '')[:1500]}```"
            )
        if verb == "runs":
            jobs = await service.list_jobs(None)
            job = _find_job_by_token(jobs, body)
            if job is None:
                return f"_No job matched `{body}`._"
            runs = await service.get_runs(job.id, limit=10)
            return _format_runs(job, runs)
        if verb == "metrics":
            m = await service.metrics()
            return (
                f"*Scheduler metrics*\n"
                f"Total jobs: {m.total_jobs} ({m.enabled_jobs} active, "
                f"{m.paused_jobs} paused, {m.disabled_jobs} disabled)\n"
                f"Last 24h: {m.runs_24h} runs ({m.succeeded_24h} ok, "
                f"{m.failed_24h} failed)\n"
                f"Avg run duration: {m.avg_run_duration_seconds:.1f}s\n"
                f"Cost (24h): ${m.total_cost_usd_24h:.4f}\n"
                f"Next fire: {_format_ts(m.next_fire_at)}"
                f"{' — ' + (m.next_fire_job_name or '') if m.next_fire_job_name else ''}"
            )
        return _scheduler_help_card()

    async def _scheduler_create_oneshot(
        self,
        body: str,
        *,
        service: Any,
        creator: Any,
    ) -> str:
        """``/freyja remind <when> <prompt>`` — one-shot reminder."""
        when, _, prompt = _split_when_prompt(body)
        if not when or not prompt:
            return (
                "Usage: `/freyja remind <when> <prompt>`\n"
                "Example: `/freyja remind in 30 minutes check the deploy status`"
            )
        return await self._create_via_service(
            service, creator,
            when=when, prompt=prompt,
            name=None, sinks=None, execution=None,
        )

    async def _scheduler_create_recurring(
        self,
        body: str,
        *,
        service: Any,
        creator: Any,
    ) -> str:
        """``/freyja schedule add <when> <prompt> [--to …] [--in …]``"""
        # Strip --to / --in pieces out of body first.
        to_arg = _arg_value(body, "--to")
        in_arg = _arg_value(body, "--in")
        name_arg = _arg_value(body, "--name")
        tag_arg = _arg_value(body, "--tag")
        stripped = _strip_args(body, ["--to", "--in", "--name", "--tag"])
        when, _, prompt = _split_when_prompt(stripped)
        if not when or not prompt:
            return (
                "Usage: `/freyja schedule add <when> <prompt> "
                "[--to <sinks>] [--in <execution>] [--name <label>]`\n"
                "Examples:\n"
                "• `/freyja schedule add every weekday at 9am summarize new PRs`\n"
                "• `/freyja schedule add in 1h check the deploy --to slack,laptop:/tmp/out/{date}.md`"
            )
        sinks_list = [s.strip() for s in to_arg.split(",")] if to_arg else None
        tags = [tag_arg] if tag_arg else []
        return await self._create_via_service(
            service, creator,
            when=when, prompt=prompt,
            name=name_arg, sinks=sinks_list,
            execution=in_arg, tags=tags,
        )

    async def _scheduler_create_loop(
        self,
        body: str,
        *,
        service: Any,
        creator: Any,
    ) -> str:
        """``/freyja loop [interval | until <cond>] [prompt]``.

        Lowers to either an IntervalSchedule (fixed cadence) or
        SelfPacedSchedule (agent-driven). 'until' clauses become the
        loop's stopping condition."""
        import re as _re
        body = body.strip()
        until = None
        m = _re.match(r"until\s+(.+?)(?:\s+(.*))?$", body, _re.IGNORECASE)
        if m:
            until = m.group(1).strip()
            prompt = (m.group(2) or "").strip() or until
            when = f"self-paced between 60s and 30m"
            return await self._create_via_service(
                service, creator,
                when=when, prompt=prompt,
                name=f"loop: {prompt[:40]}",
                sinks=["here"],
                execution="persistent_job_session",
                tags=["loop"],
                extra_kwargs={"until_condition": until},
            )
        # Fixed interval form: "/freyja loop 5m clean stale tabs"
        m = _re.match(r"(\d+\s*[a-z]+)\s+(.+)$", body, _re.IGNORECASE)
        if m:
            interval = m.group(1)
            prompt = m.group(2).strip()
            return await self._create_via_service(
                service, creator,
                when=f"every {interval}", prompt=prompt,
                name=f"loop: {prompt[:40]}",
                sinks=["here"],
                execution="persistent_job_session",
                tags=["loop"],
            )
        if body:
            # No cadence, just a prompt — default to self-paced.
            return await self._create_via_service(
                service, creator,
                when="self-paced between 60s and 30m",
                prompt=body,
                name=f"loop: {body[:40]}",
                sinks=["here"],
                execution="persistent_job_session",
                tags=["loop"],
            )
        return (
            "Usage: `/freyja loop <interval> <prompt>` or "
            "`/freyja loop until <condition> <prompt>` or "
            "`/freyja loop <prompt>` (self-paced)."
        )

    async def _create_via_service(
        self,
        service: Any,
        creator: Any,
        *,
        when: str,
        prompt: str,
        name: str | None = None,
        sinks: list[Any] | None = None,
        execution: Any = None,
        tags: list[str] | None = None,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """Shared create path used by ``schedule add``, ``remind``,
        and ``loop`` slash subcommands."""
        from bridge.scheduler.models import (
            JobRecord,
            SelfPacedSchedule,
        )
        from bridge.scheduler.scheduling import cadence_label, parse_when
        from bridge.scheduler.service import build_execution, build_sinks

        schedule = parse_when(when, timezone="UTC")
        if isinstance(schedule, SelfPacedSchedule) and extra_kwargs:
            if "until_condition" in extra_kwargs:
                schedule.until_condition = extra_kwargs["until_condition"]
        execution_spec = build_execution(execution, creator=creator)
        sink_specs = build_sinks(sinks, creator=creator, state=self.state)
        spec = JobRecord(
            id="",
            name=name or _autoname(prompt),
            description="",
            creator=creator,
            schedule=schedule,
            prompt=prompt,
            execution=execution_spec,
            permission_snapshot=getattr(self.state, "permission_tier", "low"),
            sinks=sink_specs,
            tags=tags or [],
        )
        job = await service.create_job(spec)
        return (
            f":calendar: Scheduled *{job.name}* (`{job.id}`)\n"
            f"• Cadence: {cadence_label(job.schedule)}\n"
            f"• Next fire: {_format_ts(job.next_fire_at)}\n"
            f"• Sinks: {', '.join(s.kind for s in job.sinks) or 'none'}\n"
            f"• Execution: {job.execution.kind}\n"
            f"_Use `/freyja schedule list` to see all, or "
            f"`/freyja schedule pause {job.id}` to disable._"
        )

    async def _scheduler_daemon_subcommand(self, body: str) -> str:
        verb = body.strip().lower()
        try:
            from bridge.scheduler.daemon import (
                daemon_status,
                ensure_daemon_installed,
                uninstall_daemon,
            )
        except Exception as exc:  # noqa: BLE001
            return f"_Daemon module unavailable: {exc}_"
        if verb in ("", "status"):
            st = daemon_status()
            return (
                "*Background scheduler daemon*\n"
                f"• Supported: {st.get('supported')}\n"
                f"• Installed: {st.get('installed')}\n"
                f"• Running: {st.get('running')}\n"
                f"• PID: {st.get('pid')}\n"
                f"• Log: `{st.get('log')}`"
            )
        if verb in ("install", "reinstall"):
            result = ensure_daemon_installed(reason="slash_install")
            return (
                f"Install result: `{result.get('reason')}` — "
                f"plist `{result.get('plist')}`."
            )
        if verb in ("uninstall", "remove"):
            result = uninstall_daemon()
            return f"Uninstall result: removed {result.get('removed')}"
        return "Usage: `/freyja daemon install | uninstall | status`"

    async def _render_models(self, message: IncomingMessage) -> str:
        """List all models + harnesses + coordination modes available for
        this session. Marks the currently-selected one with ✓ so the
        operator can see exactly what they're using vs. what they could
        switch to. Lazy-imports the registries so daemon startup isn't
        pinned to engine.providers' import cost."""
        from bridge.gateway.session_router import session_key_for

        try:
            from engine.providers import MODEL_REGISTRY  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't load model registry: {exc}"
        try:
            from bridge.runtimes.registry import capabilities_payload
            harnesses = capabilities_payload()
        except Exception:  # noqa: BLE001
            harnesses = []

        sessions = getattr(self.state, "sessions", {}) if self.state else {}
        session = sessions.get(session_key_for(message.source))
        current_model = getattr(session, "model_id", None) if session else None
        current_strategy = getattr(session, "coordination_strategy", None) if session else None

        # ── Models grouped by provider ──
        # Sort entries within each provider by id for stable rendering.
        by_provider: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for model_id, entry in MODEL_REGISTRY.items():
            provider = str(entry.get("provider") or "other")
            by_provider.setdefault(provider, []).append((model_id, entry))
        for entries in by_provider.values():
            entries.sort(key=lambda kv: kv[0])

        # Provider display order: Anthropic + OpenAI first (frontier),
        # then everything else alphabetically.
        provider_order = sorted(
            by_provider.keys(),
            key=lambda p: (
                0 if p == "anthropic" else 1 if p == "openai" else 2,
                p,
            ),
        )

        lines: list[str] = ["*Models* — switch with `/model <id>`"]
        for provider in provider_order:
            lines.append("")
            display = _PROVIDER_DISPLAY.get(provider, provider.title())
            lines.append(f"_{display}_")
            for model_id, entry in by_provider[provider]:
                ctx = entry.get("context_window")
                ctx_str = _format_ctx_window(ctx) if isinstance(ctx, int) else "?"
                tags: list[str] = [f"{ctx_str} ctx"]
                if entry.get("thinking"):
                    rmode = entry.get("reasoning_mode")
                    if rmode == "effort":
                        tags.append("thinking (effort)")
                    else:
                        tags.append("thinking")
                marker = "✓ " if model_id == current_model else "  "
                lines.append(f"{marker}`{model_id}` · {' · '.join(tags)}")

        # ── Harnesses ──
        if harnesses:
            lines.append("")
            lines.append(
                "*Harnesses* — the runtime that drives the agent loop. "
                "Switching requires `FREYJA_HARNESS=<id>` + daemon restart."
            )
            for h in harnesses:
                marker = "  "  # no per-session current-harness yet on gateway sessions
                hid = h.get("id") or "?"
                label = h.get("label") or hid
                desc = h.get("description") or ""
                if not h.get("available", True):
                    reason = h.get("unavailableReason") or "unavailable"
                    lines.append(f"{marker}`{hid}` ({label}) — _unavailable: {reason}_")
                else:
                    lines.append(f"{marker}`{hid}` — {desc or label}")

        # ── Coordination modes ──
        lines.append("")
        lines.append("*Coordination modes* — switch with `/mode <name>`")
        modes: list[tuple[str, str]] = [
            ("bus", "default · shared event bus across sub-agents"),
            ("goal", "autonomous judge loop until objective met"),
            ("kanban", "multi-agent board with assigned cards"),
            ("isolated", "no sub-agent fanout, single-agent run"),
        ]
        for mid, blurb in modes:
            marker = "✓ " if mid == current_strategy else "  "
            lines.append(f"{marker}`{mid}` · {blurb}")

        return "\n".join(lines)

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
        reader.register("skill_candidate_resolve", self._on_skill_candidate_resolve)
        await reader.start()
        self.control_channel = reader

    def _on_skill_candidate_resolve(self, cmd: dict[str, Any]) -> None:
        """Promote or discard a drafter candidate authored in a Slack
        session. Lives on the daemon side because that's where the
        gateway-routed session and its drafter ran. The desktop calls
        ``confirmation.promote`` / ``confirmation.discard`` directly on
        the local subprocess; this is the gateway analog.

        Schema:
          { "type": "skill_candidate_resolve",
            "sessionId": "freyja:slack:...",
            "candidateId": "<uuid hex>",
            "action": "promote" | "discard",
            "edits": { name?, description?, body? } | null }
        """
        candidate_id = str(cmd.get("candidateId") or "")
        action = str(cmd.get("action") or "")
        edits = cmd.get("edits") if isinstance(cmd.get("edits"), dict) else None
        session_id = str(cmd.get("sessionId") or "")
        if not candidate_id or action not in ("promote", "discard"):
            logger.warning(
                "control: skill_candidate_resolve missing/invalid fields"
            )
            return
        try:
            from bridge.knowledge.learning import confirmation
            if action == "promote":
                result = confirmation.promote(
                    candidate_id, actor="operator", edits=edits,
                )
            else:
                result = confirmation.discard(
                    candidate_id, actor="operator", reason="operator-rejected",
                )
        except Exception:  # noqa: BLE001
            logger.exception("control: skill_candidate_resolve raised")
            return
        # Echo via emit so the desktop tailer forwards the resolution
        # event to the renderer (clears the toast).
        try:
            from bridge.freyja_bridge import emit
            emit(
                {
                    "type": "skill_candidate_resolved",
                    "sessionId": session_id,
                    "candidateId": candidate_id,
                    "action": action,
                    "actor": "operator",
                    "skillPath": str(result.skill_path) if result.skill_path else None,
                    "reason": result.reason or "",
                }
            )
        except Exception:  # noqa: BLE001
            pass

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
