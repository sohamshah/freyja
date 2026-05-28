"""Platform adapter Protocol + shared dataclasses for the messaging gateway.

A platform adapter is a thin shim between an external chat surface
(Slack today; Telegram, Discord, Matrix later) and the Freyja bridge.
It owns the platform-specific connection + auth + protocol handling and
translates incoming events into a normalized ``IncomingMessage`` shape
that the gateway's session router knows how to handle.

Every adapter must:

  · ``connect()`` — establish the platform connection, start receiving
    events, return True on success
  · ``disconnect()`` — clean shutdown
  · ``send()`` — deliver a fresh message to a chat
  · ``edit()`` — replace an existing message (used by the stream
    consumer for in-place progressive edits)
  · ``upload_file()`` — attach a file to a chat (image / doc / voice)

Adapters are async-first. The gateway calls ``await adapter.connect()``
once at daemon start and ``await adapter.disconnect()`` on shutdown.
Inbound messages flow through the ``on_event`` callback the gateway
provides to ``connect()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


class Platform(Enum):
    """Stable platform identifier used in session keys + logs.

    Append new platforms here; never renumber. The string value is what
    persists into session keys on disk, so changing one would orphan
    every conversation routed through that platform.
    """

    SLACK = "slack"
    # TELEGRAM = "telegram"
    # DISCORD = "discord"
    # MATRIX = "matrix"


@dataclass
class MessageSource:
    """Structured identifier for the conversation a message belongs to.

    Every field has clear semantics and the session router uses the
    combination to build a deterministic session key (see
    ``session_router.session_key_for``). Mirrors Hermes's SessionSource
    field-for-field for parity.

    chat_type values:
      · "dm"      — 1:1 conversation with the bot (Slack: D... channel
                    id, or user_id as the chat_id depending on event)
      · "channel" — public or private channel (Slack: C... / G...)
      · "group"   — group DM (Slack: G... mpim)
      · "thread"  — used when the parent is a channel but the message
                    is in a thread (some platforms surface thread as
                    its own chat_type; Slack does not — we always set
                    chat_type to the parent's type and use thread_id
                    to differentiate)
    """

    platform: Platform
    workspace_id: str                # Slack team_id, Discord guild_id, etc.
    chat_type: str                   # "dm" | "channel" | "group" | "thread"
    chat_id: str                     # platform-native channel/DM id
    user_id: str | None = None       # who sent the message
    user_name: str | None = None     # human-readable display name
    chat_name: str | None = None     # channel name (#general) or DM partner
    thread_id: str | None = None     # Slack thread_ts, Discord thread id
    message_id: str | None = None    # Slack ts of the triggering message
    is_bot: bool = False             # whether the sender is itself a bot


@dataclass
class IncomingMessage:
    """Normalized inbound message handed to the gateway by adapters."""

    source: MessageSource
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    received_at: float = 0.0
    # When True, the message originated as a slash command (e.g. /goal).
    # The adapter is responsible for parsing the command name + args out
    # of text and surfacing them here.
    is_slash_command: bool = False
    slash_command_name: str | None = None
    slash_command_args: str = ""
    # Adapter-specific opaque payload — passed through to send() in
    # case it needs platform-native context (e.g. Slack response_url
    # for ephemeral slash replies).
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """Result of sending or editing a platform message."""

    ok: bool
    message_id: str | None = None
    error: str | None = None
    raw: Any = None


# Type alias for the inbound event callback adapters receive from the
# gateway on connect. Adapters call this for every message they want
# routed; the gateway handles session lookup and agent dispatch.
EventCallback = Callable[[IncomingMessage], Awaitable[None]]


@runtime_checkable
class PlatformAdapter(Protocol):
    """Minimum interface every platform adapter must implement."""

    @property
    def name(self) -> str:
        """Stable adapter name, matches Platform enum value."""
        ...

    async def connect(self, on_event: EventCallback) -> bool:
        """Establish platform connection. Call on_event for inbound
        messages. Return True on success, False if connection failed
        (e.g. missing tokens, auth rejection). On False the gateway
        will retry with backoff."""
        ...

    async def disconnect(self) -> None:
        """Clean shutdown. Idempotent."""
        ...

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
        """Send a new message.

        ``thread_id`` posts in the thread (required for in-thread
        replies). ``ephemeral_user_id`` makes the message visible only
        to that user (Slack ephemeral). ``raw_hint`` carries any
        platform-specific context the adapter stashed on the inbound
        message and needs back to deliver correctly (e.g. Slack slash
        command response_url).
        """
        ...

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Replace an existing message in place. Used by the stream
        consumer for progressive edits so the operator sees the agent's
        response grow live."""
        ...

    async def upload_file(
        self,
        chat_id: str,
        path: str,
        *,
        thread_id: str | None = None,
        filename: str | None = None,
        title: str | None = None,
    ) -> SendResult:
        """Attach a file to a chat (image, doc, voice). Path must
        exist on disk; the adapter handles the multipart upload."""
        ...

    async def upload_files(
        self,
        chat_id: str,
        items: list["UploadItem"],
        *,
        thread_id: str | None = None,
        initial_comment: str | None = None,
    ) -> SendResult:
        """Upload one or more files in a single platform call so they
        share one initial comment / attachment group. Each ``UploadItem``
        carries either a path or raw bytes (mutually exclusive). Adapters
        that don't support batch uploads fall back to N serial
        ``upload_file`` calls; the SendResult.message_id points at the
        first message in that case."""
        ...

    async def send_typing(
        self,
        chat_id: str,
        *,
        thread_id: str | None = None,
        status: str = "is thinking...",
    ) -> SendResult:
        """Render a platform-native typing/status indicator for the
        next turn. Best-effort: adapters that don't have a native
        status surface should no-op silently and return ok. Slack:
        uses ``assistant.threads.setStatus``."""
        ...

    async def stop_typing(
        self,
        chat_id: str,
        *,
        thread_id: str | None = None,
    ) -> SendResult:
        """Clear any active typing/status indicator. Always called at
        turn boundaries, including on error paths, to avoid dangling
        indicators."""
        ...


@dataclass
class UploadItem:
    """One file to upload in an ``upload_files`` batch.

    Provide either ``path`` (a filesystem path Python can read) OR
    ``data`` (raw bytes). If both are set, ``data`` wins. ``filename``
    is what the recipient sees; defaults to the basename of ``path``.
    ``title`` is an optional human-readable label.
    """

    path: str | None = None
    data: bytes | None = None
    filename: str | None = None
    title: str | None = None
    mime_type: str | None = None


# ─── helpers shared across adapters ──────────────────────────────────


def truncate_for_platform(content: str, max_chars: int) -> list[str]:
    """Split a long message into chunks bounded by ``max_chars``,
    preferring boundaries at fenced-code-block edges and double newlines.

    Slack's hard limit is 40,000 chars per message; most platforms have
    similar limits. We default to slightly under the cap to leave room
    for any per-platform prefix/suffix the adapter adds.
    """
    if len(content) <= max_chars:
        return [content]

    chunks: list[str] = []
    remaining = content
    while len(remaining) > max_chars:
        # Try to split at the latest fenced code boundary or paragraph
        # break that fits within max_chars. Falls back to a hard cut.
        cut = max_chars
        for marker in ("\n```\n", "\n\n", "\n", " "):
            idx = remaining.rfind(marker, 0, max_chars)
            if idx > max_chars // 2:
                cut = idx + len(marker)
                break
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
