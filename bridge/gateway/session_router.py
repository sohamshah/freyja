"""Maps inbound gateway messages → Freyja sessions.

The router is the bridge between the platform-agnostic ``IncomingMessage``
shape and the existing ``_BridgeState`` / ``_BridgeSession`` machinery
that runs the actual agent.

Two responsibilities:

1. Construct a deterministic session key from the message source so
   the same Slack conversation always lands in the same Freyja session
   across restarts.

2. Look up or create that session in the bridge state, attach the
   gateway source context, and enqueue the user's message via the
   existing turn-queue path. The session inherits everything Freyja
   already gives interactive sessions — tool registry, sub-agent
   profiles, coordination strategy, memory, skills — plus a stashed
   ``gateway_source`` field that the agent sees in its system prompt
   so it knows it's talking on Slack, who the user is, what channel.
"""

from __future__ import annotations

import logging
from typing import Any

from bridge.gateway.platforms.base import IncomingMessage, MessageSource

logger = logging.getLogger(__name__)


def session_key_for(
    source: MessageSource,
    *,
    threads_per_user: bool = False,
) -> str:
    """Build a deterministic session key from a message source.

    DM rules (mirrors Hermes ``gateway/session.py:build_session_key``):
      · DMs always include chat_id; each private conversation is isolated
      · thread_id further differentiates threaded DMs within the same DM
      · Without chat_id, thread_id is used as a best-effort fallback
      · Without either, DMs share a single session per workspace

    Channel / group rules:
      · chat_id identifies the parent
      · thread_id, when present, differentiates threads
      · Within a thread, by default threads are SHARED across users
        (no user_id in the key) — every participant talks to the same
        agent thread, which matches normal Slack thread behavior. Pass
        ``threads_per_user=True`` to flip this.
      · Outside a thread, channel messages are per-user (each operator
        in #general gets their own session when they @mention the bot)

    Workspace identity (``workspace_id``) is always included so a multi-
    workspace install doesn't conflate users with the same ID across
    different teams.

    The key format is::

        freyja:<platform>:<workspace_id>:<chat_type>:<chat_id>[:thread_id][:user_id]

    Example keys::

        freyja:slack:T012345:dm:D098765
        freyja:slack:T012345:dm:D098765:1700000000.123456
        freyja:slack:T012345:channel:C001:1700000000.123456              # shared thread
        freyja:slack:T012345:channel:C001:U001                            # @mention in channel
    """
    platform = source.platform.value
    workspace = source.workspace_id or "_unknown"

    if source.chat_type == "dm":
        # DMs collapse to a single session per (workspace, dm_chat).
        # Threading inside a 1-on-1 DM is just a UI affordance; the
        # operator expects one continuous conversation regardless of
        # whether they reply top-level or inside a Slack thread under
        # one of the bot's earlier responses. Including thread_id in
        # the key here silently fragmented sessions and made the bot
        # "forget" the conversation every time the user clicked
        # "Reply…" on one of its messages.
        if source.chat_id:
            return f"freyja:{platform}:{workspace}:dm:{source.chat_id}"
        # No chat_id available (rare, malformed event) — fall back to
        # a single per-workspace DM bucket so we don't lose the turn.
        return f"freyja:{platform}:{workspace}:dm"

    # channel / group / thread
    parts: list[str] = [f"freyja:{platform}:{workspace}", source.chat_type]
    if source.chat_id:
        parts.append(source.chat_id)
    if source.thread_id:
        parts.append(source.thread_id)

    # User isolation: outside a thread, per-user. Inside a thread, shared
    # by default (matches Slack thread semantics) unless caller opts in
    # to per-user threads.
    isolate_user = True
    if source.thread_id and not threads_per_user:
        isolate_user = False
    if isolate_user and source.user_id:
        parts.append(source.user_id)

    return ":".join(parts)


def gateway_source_block(source: MessageSource) -> str:
    """Render a one-paragraph block describing the gateway context.

    Injected at the top of the system prompt for sessions that came
    from the gateway so the agent knows it's not at the operator's
    desktop — which shapes how it formats responses (Slack mrkdwn vs.
    full markdown), how much it inlines (channel = brief, DM = fine to
    expand), and whether to be careful about who else might see its
    output (channel = others present).
    """
    lines: list[str] = []
    lines.append(
        f"You are responding on the **{source.platform.value.title()}** gateway, "
        f"not the desktop app."
    )
    if source.chat_type == "dm":
        partner = source.user_name or source.user_id or "the operator"
        lines.append(f"This is a private DM with {partner}.")
    elif source.chat_type == "channel":
        ch = source.chat_name or source.chat_id or "an unnamed channel"
        sender = source.user_name or source.user_id or "a user"
        lines.append(f"You're in channel #{ch}. {sender} sent the message.")
        if source.thread_id:
            lines.append(
                "You're inside a thread — reply in-thread; other channel "
                "members may see your responses if they expand the thread."
            )
        else:
            lines.append(
                "You're at the top-level of the channel. Other members can see "
                "your responses. Keep things appropriate for that audience."
            )
    elif source.chat_type == "group":
        lines.append("This is a small group conversation — multiple users present.")

    lines.append(
        "Format with platform-appropriate markdown (Slack mrkdwn renders "
        "**bold** as *bold* and supports fenced code blocks). Long responses "
        "will be split across messages automatically."
    )
    # Make the agent aware of the gateway-only tools so it stops
    # apologizing about not being able to send files. Without this hint
    # it'll happily list paths but tell the user "I can't upload" —
    # which is wrong: it can, via `send_attachment`.
    lines.append(
        "Outbound file sharing: call `send_attachment(paths=[...], "
        "caption=...)` to attach one or more local files directly into "
        "this chat. Use it whenever the user asks you to send / share / "
        "show / attach a file — don't explain you can't, just call the "
        "tool. Up to 10 files per call."
    )
    # Inject either the restrictive guidance (filter on, shared workspace)
    # or a strong positive override (filter off, single-operator install).
    # The positive override is critical: pretrained models bias toward
    # "I'm a bot in a chat app → I shouldn't run shell / mouse / screen
    # tools," and once the agent has SAID "those are blocked" in this
    # chat, the prior-conversation injection replays that refusal and
    # the agent self-anchors. The override has to be loud enough to
    # break that pattern.
    from bridge.gateway.capabilities import gateway_filter_enabled
    if gateway_filter_enabled(source.platform):
        lines.append(
            "You are operating without the operator at the keyboard. DO NOT call "
            "the `bash`, `computer`, `computer_use`, `click`, `move_mouse`, "
            "`type_text`, `press_key`, `screenshot`, `browser_execute_js`, or "
            "`browser_screenshot` tools — they have side effects the operator "
            "cannot see or veto from Slack. If the request needs those, explain "
            "what you would do and ask the operator to run it from the desktop "
            "app instead. Read-only tools (read_file, list_directory, glob, grep, "
            "web_search, web_fetch, sub_agent) are fine."
        )
    else:
        lines.append(
            "TOOL ACCESS: you have the FULL tool surface on this gateway — "
            "exactly the same set you have on the desktop app. That includes "
            "`bash`, `computer_use`, `screenshot`, `click`, `move_mouse`, "
            "`type_text`, `press_key`, `browser_execute_js`, `browser_screenshot`, "
            "`generate_image`, `analyze_video`, `write_file`, `edit_file`, "
            "`sub_agent`, memory mutators, and everything else in your tool list. "
            "Nothing is filtered. The operator is the only person who can DM this "
            "bot, and they explicitly want you to drive their machine from Slack.\n"
            "\n"
            "If a prior assistant turn in this chat claimed any tool was \"blocked "
            "for safety,\" \"restricted on the Slack gateway,\" or similar — that was "
            "WRONG. Ignore that framing entirely. When the user asks you to take a "
            "screenshot, run bash, move the mouse, etc., just call the tool. Don't "
            "apologize, don't redirect them to the desktop app, don't explain "
            "constraints that don't exist. The desktop app and the Slack gateway are "
            "the same agent with the same tools — only the chat surface differs."
        )
    return "\n".join(lines)


async def route(
    message: IncomingMessage,
    bridge_state: Any,
    *,
    default_model: str | None = None,
    default_strategy: str = "bus",
) -> tuple[str, Any]:
    """Look up or create the session for this message and enqueue it.

    Returns (session_key, session) so the caller (typically the platform
    adapter's stream consumer) can subscribe to events scoped to the
    session id.

    Does NOT block on the agent's response — that runs as a pending
    task on the session. The adapter's stream consumer reads events
    from the bridge's emit stream filtered by session id.
    """
    key = session_key_for(message.source)
    logger.info(
        "gateway: routing %s message to session %s (text=%r)",
        message.source.platform.value,
        key,
        (message.text or "")[:80],
    )

    # ensure_session is the existing bridge entry point that creates or
    # restores a session by id. It handles transcript rehydration and
    # backward compat. We pass gateway_source THROUGH ensure_session so
    # the attribute is set on the session BEFORE try_restore_transcript
    # triggers initialize() — otherwise the gateway-only tool
    # registration (`send_attachment` et al) silently skips because
    # `self.gateway_source` is None at registration check time.
    session = await bridge_state.ensure_session(
        session_id=key,
        model_id=default_model,
        coordination_strategy=default_strategy,
        gateway_source=message.source,
    )

    # Belt: keep the legacy setattr too — covers any code path that
    # built a session without going through ensure_session(...).
    setattr(session, "gateway_source", message.source)

    return key, session
