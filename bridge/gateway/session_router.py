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
import re
from typing import Any

from bridge.gateway.platforms.base import IncomingMessage, MessageSource

logger = logging.getLogger(__name__)


# ─── verbosity inline-flag parsing ────────────────────────────────────
#
# Slack slash commands don't fire inside threads, so users need a way
# to control verbosity in a thread-friendly form. We accept CLI-style
# `--flag` tokens at the start or end of a message, strip them out,
# and apply the corresponding level. Mirrors Hermes's 4-level model
# (off/new/all/verbose) — see ~/work/services/hermes-agent for prior
# art — but adds the inline-token surface so threads work too.

VERBOSITY_LEVELS: tuple[str, ...] = ("off", "new", "all", "verbose")
# Default bumped from "new" → "all" once tool progress moved to native
# Slack Thinking Steps (collapsible Task Cards). Native rendering
# collapses by default, so showing every call adds no visual cost.
# Set per-session via `/freyja verbose` or inline `--off / --new /
# --all / --verbose` flags.
DEFAULT_VERBOSITY = "all"

_VERBOSITY_FLAG_MAP = {
    "--off": "off",
    "--silent": "off",
    "--quiet": "off",
    "-q": "off",
    "--new": "new",
    "--brief": "new",
    "--all": "all",
    "--verbose": "verbose",
    "-v": "verbose",
}

_FLAG_TOKEN_RE = re.compile(
    r"(?:^|\s)(" + "|".join(
        re.escape(f) for f in sorted(_VERBOSITY_FLAG_MAP.keys(), key=len, reverse=True)
    ) + r")(?=\s|$)",
    re.IGNORECASE,
)


def parse_verbosity_flags(text: str) -> tuple[str | None, str]:
    """Strip verbosity flags from ``text``; return (level, cleaned_text).

    Only tokens at the start or end of the message are considered, so
    natural-language uses of "verbose" or pasted shell commands
    containing ``--verbose`` mid-string don't accidentally trigger a
    mode change. Case-insensitive. Multiple flags: last (rightmost in
    original text) wins.

    Examples:
        ``"--verbose what does this do?"`` → ``("verbose", "what does this do?")``
        ``"fix the build --silent"``       → ``("off", "fix the build")``
        ``"explain bash --verbose --new"`` → ``("new", "explain bash")``
        ``"normal question"``              → ``(None, "normal question")``
        ``"paste: cmd --verbose --foo"``   → ``(None, "paste: cmd --verbose --foo")``
    """
    if not text:
        return None, ""
    raw = text

    # Peel matching flag tokens off the END of the message until we
    # hit a non-flag token. ``raw`` is mutated so subsequent peels see
    # the trimmed string.
    while True:
        m = re.search(r"(?:\s+|^)(\S+)\s*$", raw)
        if not m:
            break
        candidate = m.group(1)
        if candidate.lower() in _VERBOSITY_FLAG_MAP:
            raw = raw[: m.start(1)].rstrip()
        else:
            break

    # Same idea from the START.
    while True:
        m = re.match(r"^\s*(\S+)(?:\s+|$)", raw)
        if not m:
            break
        candidate = m.group(1)
        if candidate.lower() in _VERBOSITY_FLAG_MAP:
            raw = raw[m.end():].lstrip()
        else:
            break

    if raw == text:
        return None, text  # no flags consumed
    # Last-flag-wins. Scan the original message left-to-right for any
    # recognized flag tokens; the rightmost one's level is the result.
    found: list[str] = []
    for m in _FLAG_TOKEN_RE.finditer(text):
        found.append(m.group(1).lower())
    if not found:
        return None, text
    level = _VERBOSITY_FLAG_MAP[found[-1]]
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return level, cleaned


# ─── --model / --mode inline-flag parsing ─────────────────────────────
#
# Same rationale as the verbosity flags above: Slack slash commands
# don't fire inside threads, so the first @-mention that starts a
# Freyja thread has no way to set the session's model or mode. These
# flags fill that gap.
#
# Syntax: ``--model <value>`` or ``--model=<value>`` (same for --mode).
# Each consumes two tokens (flag + value) when in space-separated form.
# Only tokens at the start of the message are considered — a stray
# ``--model foo`` mid-message (e.g. a pasted CLI snippet) is left
# untouched.
#
# Returns the parsed values + cleaned text + a list of validation
# errors. The caller (run.py) surfaces errors as a Slack reply and
# bails before scheduling the agent turn.

_VALID_COORDINATION_MODES: tuple[str, ...] = ("bus", "goal", "kanban")
# Anchor at the START only — model/mode are configuration intents and
# almost always lead the message ("--model opus-4-8 fix the bug").
# Ignoring trailing positions also avoids consuming values inside
# pasted commands. Two forms accepted per flag.
_INLINE_SESSION_FLAG_RE = re.compile(
    r"^\s*(?:--(model|mode))(?:\s+|=)([^\s]+)",
    re.IGNORECASE,
)

# Standalone --models flag (no value). Matched before the value-bearing
# regex so "--models" isn't accidentally treated as "--model" with value
# "s" or similar.
_INLINE_MODELS_FLAG_RE = re.compile(r"^\s*--models\b", re.IGNORECASE)


def parse_inline_session_flags(
    text: str,
) -> tuple[str | None, str | None, str, list[str], bool]:
    """Strip ``--model <id>``, ``--mode <name>``, and ``--models`` from the
    start of ``text``.

    Returns ``(model_id, mode, cleaned_text, errors, show_models)``:

      · ``model_id``: validated model id (matches ``MODEL_REGISTRY``)
        or ``None`` if no --model flag was present
      · ``mode``: one of ``bus``/``goal``/``kanban`` or ``None``
      · ``cleaned_text``: ``text`` with the recognized flags removed
      · ``errors``: human-readable validation messages; non-empty if
        the user wrote a flag whose value didn't validate. ``cleaned_text``
        still has the bad flag stripped so the caller can echo back
        the intended message body.
      · ``show_models``: ``True`` if ``--models`` was present. The caller
        should short-circuit to the models listing and skip the agent turn.

    Examples:
        ``"--model claude-opus-4-8 fix the bug"`` →
            ``("claude-opus-4-8", None, "fix the bug", [], False)``
        ``"--mode goal --model claude-opus-4-8 ship it"`` →
            ``("claude-opus-4-8", "goal", "ship it", [], False)``
        ``"--mode banana write some code"`` →
            ``(None, None, "write some code",
               ["Invalid mode `banana`. Available: `bus`, `goal`, `kanban`."], False)``
        ``"--models"`` →
            ``(None, None, "", [], True)``
        ``"normal message"`` →
            ``(None, None, "normal message", [], False)``
    """
    if not text:
        return None, None, "", [], False

    model: str | None = None
    mode: str | None = None
    errors: list[str] = []
    raw = text

    # --models (standalone, no value) — detect and strip before the
    # value-bearing flag loop so "--models" isn't parsed as "--model s".
    show_models = False
    m_models = _INLINE_MODELS_FLAG_RE.match(raw)
    if m_models:
        show_models = True
        raw = raw[m_models.end():].lstrip()

    # Peel up to 2 leading flag tokens (one --model + one --mode in
    # either order). Use a bounded loop; >2 iterations would mean
    # duplicate flags, which we just take last-wins on.
    for _ in range(4):
        m = _INLINE_SESSION_FLAG_RE.match(raw)
        if not m:
            break
        flag = m.group(1).lower()
        value = m.group(2)
        if flag == "model":
            model = value
        else:
            mode = value
        # Strip the matched span from the front so the next iteration
        # sees the trimmed body.
        raw = raw[m.end() :].lstrip()

    cleaned = raw

    # Validation — lazy imports keep session_router import-cheap.
    if mode is not None and mode.lower() not in _VALID_COORDINATION_MODES:
        errors.append(
            f"Invalid mode `{mode}`. Available: "
            + ", ".join(f"`{m}`" for m in _VALID_COORDINATION_MODES)
            + "."
        )
        mode = None
    elif mode is not None:
        mode = mode.lower()

    if model is not None:
        try:
            from engine.providers import MODEL_REGISTRY
            valid = model in MODEL_REGISTRY
        except Exception:  # noqa: BLE001
            # If the registry can't be imported (e.g. unit-test
            # context), accept the value rather than reject blindly.
            valid = True
        if not valid:
            errors.append(
                f"Invalid model `{model}`. Use `--models` or `/freyja models` "
                "to see available options."
            )
            model = None

    return model, mode, cleaned, errors, show_models


def normalize_verbosity(value: str | None) -> str:
    """Coerce a possibly-missing or invalid verbosity string to a valid
    level. Used when reading the sticky value off a session attribute —
    older sessions might have ``None`` or stray casing."""
    if not value:
        return DEFAULT_VERBOSITY
    low = value.strip().lower()
    if low in VERBOSITY_LEVELS:
        return low
    return DEFAULT_VERBOSITY


def cycle_verbosity(current: str | None) -> str:
    """Hermes-style cycle: off → new → all → verbose → off."""
    cur = normalize_verbosity(current)
    idx = VERBOSITY_LEVELS.index(cur)
    return VERBOSITY_LEVELS[(idx + 1) % len(VERBOSITY_LEVELS)]


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
    default_strategy: str | None = None,
) -> tuple[str, Any]:
    """Look up or create the session for this message and enqueue it.

    Returns (session_key, session) so the caller (typically the platform
    adapter's stream consumer) can subscribe to events scoped to the
    session id.

    Does NOT block on the agent's response — that runs as a pending
    task on the session. The adapter's stream consumer reads events
    from the bridge's emit stream filtered by session id.

    ``default_strategy`` semantics: seeded as ``"bus"`` only when this
    is a brand-new session AND the caller didn't pass one. For an
    EXISTING session with no caller-supplied strategy, ``None`` flows
    through to ``ensure_session`` and the existing
    ``coordination_strategy`` is left alone. Without this, every
    inbound message without an explicit ``--mode`` flag (or `/mode`
    slash) would silently overwrite a prior ``kanban``/``goal``
    session back to ``bus``.
    """
    key = session_key_for(message.source)
    logger.info(
        "gateway: routing %s message to session %s (text=%r)",
        message.source.platform.value,
        key,
        (message.text or "")[:80],
    )

    # Seed the "bus" default only on brand-new sessions. For existing
    # sessions, leave coordination_strategy alone unless the caller
    # explicitly passed one (via /mode slash or --mode inline flag).
    strategy_for_call = default_strategy
    if strategy_for_call is None:
        existing = getattr(bridge_state, "sessions", {}).get(key)
        if existing is None:
            strategy_for_call = "bus"

    # ensure_session is the existing bridge entry point that creates
    # or restores a session by id. ``gateway_source`` is only used for
    # the NEW-session path (so the session's first ``initialize()``
    # call, triggered inside ``try_restore_transcript``, can register
    # send_attachment + apply capability filter). For EXISTING
    # sessions, ensure_session deliberately does NOT mutate
    # ``existing.gateway_source`` — an in-flight earlier turn might
    # still be running, and overwriting its source mid-turn would
    # route its tool calls to the wrong Slack thread. The per-turn
    # ``on_turn_start`` hook (installed by ``_on_inbound`` below) is
    # what advances ``session.gateway_source`` at turn boundaries.
    session = await bridge_state.ensure_session(
        session_id=key,
        model_id=default_model,
        coordination_strategy=strategy_for_call,
        gateway_source=message.source,
    )

    # NOTE: do NOT setattr(session, "gateway_source", message.source)
    # here. That used to be a "belt-and-suspenders" line but it
    # silently re-introduced the concurrent-turn race we just designed
    # ensure_session to avoid — same bug, two-step. The caller
    # (_on_inbound) wires session.gateway_source via the per-turn
    # ``on_turn_start`` hook on ``_schedule_or_queue_turn`` so the
    # mutation happens at the right moment.

    return key, session
