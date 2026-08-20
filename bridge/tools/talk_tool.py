"""Inter-agent + operator-to-agent messaging tools.

Two tools registered on every parent + child registry:

- `talk(to, content, *, force=False, wait_for_reply=False,
   reply_timeout_s=60)` — send a message to one or more agent sessions.
- `list_agent_sessions(connected=True)` — directory of addressable
  sessions for the caller.

Both depend on the shared `TalkRouter` injected at registry build time.
The router holds references to:
  - the bridge `BridgeState` (for global session lookup + re-wake)
  - the caller's session id (so "parent" / "siblings" / "children"
    aliases resolve correctly)
  - the caller's parent session id (for "parent" alias)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from engine.tools import Tool, ToolDefinition, ToolResult, ToolTier
from bridge.inbox import InboxMessage, new_message_id


# Default timeout for wait_for_reply, in seconds. Long enough to span a
# few iterations of the responder; not so long the caller permanently
# hangs.
DEFAULT_REPLY_TIMEOUT_S = 60
# Cap on the wait_for_reply timeout to prevent unbounded blocking.
MAX_REPLY_TIMEOUT_S = 600


def _is_gateway_session_id(session_id: str) -> bool:
    """``freyja:<platform>:…`` ids are owned by the gateway daemon (the
    process holding the gateway PID lock), NOT by whatever local mirror
    this process may have open. Mirrors freyja_bridge's check; kept
    local to avoid importing the heavy bridge module at call time."""
    if not session_id.startswith("freyja:"):
        return False
    rest = session_id[len("freyja:") :]
    return ":" in rest and len(rest.split(":", 1)[0]) > 0


def _gateway_owner_pid() -> int | None:
    """PID of the live gateway daemon, or None when none is running."""
    try:
        from bridge.gateway.pid import get_running_pid

        return get_running_pid()
    except Exception:  # noqa: BLE001
        return None


def _session_exists_on_disk(session_id: str) -> bool:
    """True when a persisted transcript or inbox sidecar exists for the
    session — i.e. it has actually run somewhere and can be resumed."""
    try:
        from bridge.transcript_persistence import (
            _inbox_path,
            _transcript_path,
        )

        return _transcript_path(session_id).exists() or _inbox_path(
            session_id
        ).exists()
    except Exception:  # noqa: BLE001
        return False


def queue_to_inbox_sidecar(session_id: str, msg: InboxMessage) -> str:
    """Persist a message into a session's on-disk inbox sidecar.

    Used when no process currently has the recipient loaded — the
    sidecar is rehydrated by try_restore_transcript (Step 2c) the next
    time the session loads, and the pre-iteration drain delivers the
    message on its first turn.
    """
    try:
        from bridge.transcript_persistence import (
            load_inbox_state,
            save_inbox_state,
        )
    except Exception as exc:  # noqa: BLE001
        return f"sidecar queue failed: {exc}"
    data = load_inbox_state(session_id) or {
        "sessionId": session_id,
        "unread": [],
        "delivered": [],
    }
    unread = list(data.get("unread") or [])
    unread.append(msg.to_dict())
    data["unread"] = unread
    try:
        save_inbox_state(session_id, data)
    except Exception as exc:  # noqa: BLE001
        return f"sidecar queue failed: {exc}"
    return (
        "queued to inbox sidecar — recipient isn't loaded anywhere right "
        "now; it will see the message when it next runs"
    )


@dataclass
class TalkRouterContext:
    """Per-agent context the routing tools need at execution time."""

    caller_session_id: str
    caller_label: str
    caller_role: str  # "operator" | "agent"
    parent_session_id: str | None


class TalkRouter:
    """Bridge-side dispatcher used by both TalkTool and ListAgentSessionsTool.

    Provided by the bridge at child-registry build time. Keeps a
    reference to the BridgeState so it can find any session by id.
    """

    def __init__(
        self,
        *,
        bridge_state: Any,
        get_running_sessions: Callable[[], dict[str, Any]],
        resolve_archived_sub: Callable[[str], dict[str, Any] | None],
        wake_archived_sub: Callable[[str, InboxMessage], Any],
    ) -> None:
        self._bridge_state = bridge_state
        self._get_running_sessions = get_running_sessions
        # Returns the persisted sub-agent state for a saved session id,
        # or None if no sidecar exists. Used by re-wake.
        self._resolve_archived_sub = resolve_archived_sub
        # Coroutine: re-wakes an archived sub-agent and delivers the
        # message via its inbox. Called by talk() when the recipient
        # has no live runner.
        self._wake_archived_sub = wake_archived_sub

    @property
    def bridge_state(self) -> Any:
        return self._bridge_state

    # ------ Sub-agent lookup -----------------------------------------

    def find_subagent_record(self, sub_id: str) -> Any | None:
        """Walk every root session's subagent_registry looking for the
        sub-agent id. Returns the SubAgentRecord or None."""
        for root in self.running_sessions().values():
            reg = getattr(root, "subagent_registry", None)
            if reg is None:
                continue
            try:
                rec = reg.get(sub_id)
            except Exception:
                rec = None
            if rec is not None:
                return rec
        return None

    def all_subagent_records(self) -> list[Any]:
        out: list[Any] = []
        for root in self.running_sessions().values():
            reg = getattr(root, "subagent_registry", None)
            if reg is None:
                continue
            try:
                out.extend(reg.list_all())
            except Exception:
                continue
        return out

    # ------ Lookup ----------------------------------------------------

    def running_sessions(self) -> dict[str, Any]:
        return self._get_running_sessions()

    def session_for(self, session_id: str) -> Any | None:
        return self.running_sessions().get(session_id)

    def archived_subagent(self, session_id: str) -> dict[str, Any] | None:
        return self._resolve_archived_sub(session_id)

    # ------ Addressing aliases ---------------------------------------

    def resolve_ref(
        self, ref: str, ctx: TalkRouterContext
    ) -> tuple[str, Any | None, Any | None, dict[str, Any] | None]:
        """Resolve a reference (id, label, or alias) to a recipient.

        Returns (resolved_session_id, live_root | None, sub_record | None,
        archived_state | None). At most one of the three optional slots
        will be set; all four-tuple None means unresolved.
        """
        ref_clean = (ref or "").strip()
        if not ref_clean:
            return "", None, None, None

        running = self.running_sessions()

        # --- Aliases ---
        if ref_clean == "parent":
            parent_id = ctx.parent_session_id
            if not parent_id:
                return "", None, None, None
            # Parent may be a root session OR another sub-agent (nested)
            if parent_id in running:
                return parent_id, running[parent_id], None, None
            sub = self.find_subagent_record(parent_id)
            if sub is not None:
                return parent_id, None, sub, None
            archived = self.archived_subagent(parent_id)
            if archived is not None:
                return parent_id, None, None, archived
            return parent_id, None, None, None
        if ref_clean in ("main", "operator"):
            roots = [s for s in running.values() if s.parent_session_id is None]
            if len(roots) == 1:
                return roots[0].id, roots[0], None, None
            return "", None, None, None

        # --- Direct session-id match ---
        if ref_clean in running:
            return ref_clean, running[ref_clean], None, None
        sub_rec = self.find_subagent_record(ref_clean)
        if sub_rec is not None:
            return ref_clean, None, sub_rec, None
        archived = self.archived_subagent(ref_clean)
        if archived is not None:
            return ref_clean, None, None, archived

        # --- Label lookup (root sessions + sub-agents) ---
        label_root_matches = [
            s for s in running.values()
            if (getattr(s, "title", None) == ref_clean)
        ]
        label_sub_matches = [
            r for r in self.all_subagent_records()
            if getattr(r, "label", "") == ref_clean
        ]
        if len(label_root_matches) == 1 and not label_sub_matches:
            return label_root_matches[0].id, label_root_matches[0], None, None
        if len(label_sub_matches) == 1 and not label_root_matches:
            r = label_sub_matches[0]
            return r.id, None, r, None
        return "", None, None, None

    # ------ Cross-process routing ------------------------------------

    def forward_cross_process(
        self,
        target_id: str,
        msg: InboxMessage,
        *,
        live_local: bool,
        archived_state: dict[str, Any] | None,
    ) -> str | None:
        """Route the message to the process that actually owns the
        recipient, when that isn't us. Returns a status string when the
        message was handed off (or queued), None when local delivery
        should proceed.

        Ownership rules:
          · ``freyja:<platform>:…`` gateway ids belong to the process
            holding the gateway PID lock. A desktop-side mirror of a
            Slack session is NOT authoritative — pushing into it is how
            the 2026-08-13 rewrite brief silently missed the live Slack
            agent for 2h14m.
          · Archived sub-agents belong to the process that hosts their
            parent: gateway-shaped parent → daemon, else → desktop.
          · Everything else resolvable locally is local.
        """
        owner = _gateway_owner_pid()
        am_owner = owner is not None and owner == os.getpid()

        if _is_gateway_session_id(target_id):
            if am_owner:
                return None  # we are the authority — deliver locally
            if owner is not None:
                try:
                    from bridge.gateway.control_channel import append_command

                    append_command({
                        "type": "talk_deliver",
                        "to": target_id,
                        "message": msg.to_dict(),
                        "origin_pid": os.getpid(),
                    })
                except Exception as exc:  # noqa: BLE001
                    return f"forward to gateway daemon failed: {exc}"
                return (
                    f"forwarded to the gateway daemon (pid {owner}) which "
                    "owns this session — it will deliver and wake the "
                    "recipient"
                )
            # No live gateway daemon. A live local mirror (desktop-only
            # deployment where the operator continued the thread in the
            # app) is the best available surface; otherwise persist.
            if live_local:
                return None
            return queue_to_inbox_sidecar(target_id, msg)

        # Non-gateway recipient. If we're the gateway daemon and can't
        # serve it live, it lives in the desktop bridge — forward there.
        # Archived sub-agents route by their parent's shape.
        if archived_state is not None and not live_local:
            parent_id = str(archived_state.get("parentSessionId") or "")
            parent_is_gateway = _is_gateway_session_id(parent_id)
            if parent_is_gateway and not am_owner and owner is not None:
                return self._forward_to_daemon(target_id, msg, owner)
            if not parent_is_gateway and am_owner:
                return self._forward_to_desktop(target_id, msg)
            return None  # local re-wake is the right call

        if am_owner and not live_local:
            return self._forward_to_desktop(target_id, msg)
        return None

    def _forward_to_daemon(
        self, target_id: str, msg: InboxMessage, owner: int
    ) -> str:
        try:
            from bridge.gateway.control_channel import append_command

            append_command({
                "type": "talk_deliver",
                "to": target_id,
                "message": msg.to_dict(),
                "origin_pid": os.getpid(),
            })
        except Exception as exc:  # noqa: BLE001
            return f"forward to gateway daemon failed: {exc}"
        return f"forwarded to the gateway daemon (pid {owner}) which hosts it"

    def _forward_to_desktop(self, target_id: str, msg: InboxMessage) -> str:
        try:
            from bridge.gateway.control_channel import (
                append_command,
                desktop_commands_path,
            )

            append_command(
                {
                    "type": "talk_deliver",
                    "to": target_id,
                    "message": msg.to_dict(),
                    "origin_pid": os.getpid(),
                },
                path=desktop_commands_path(),
            )
        except Exception as exc:  # noqa: BLE001
            return f"forward to desktop bridge failed: {exc}"
        return (
            "forwarded to the desktop bridge — the recipient session lives "
            "there (delivery happens when the desktop app is running)"
        )

    # ------ Delivery -------------------------------------------------

    async def deliver(
        self,
        recipient_id: str,
        live_root: Any | None,
        sub_record: Any | None,
        archived_state: dict[str, Any] | None,
        msg: InboxMessage,
    ) -> str:
        """Drop the message into the recipient's inbox. Routes by
        recipient kind: root session inbox, sub-agent record inbox, or
        re-wake (archived sub-agent)."""
        if live_root is not None and getattr(live_root, "inbox", None) is not None:
            live_root.inbox.push(msg)
            if msg.force:
                # For root sessions, cancel currently has no clean hook
                # (no SubAgentRecord). The runner's pre-iteration drain
                # will pick the message up at the next iteration; for
                # mid-stream interruption on the root we'd need a
                # bridge-level cancel — Phase 3 task.
                pass
            # An IDLE root session never drains its inbox on its own —
            # the pre-iteration drain only runs during an active turn.
            # wake_for_inbox schedules a synthetic turn so the message
            # is processed now instead of at the next operator poke.
            wake_status = ""
            wake = getattr(live_root, "wake_for_inbox", None)
            if callable(wake):
                try:
                    wake_status = wake() or ""
                except Exception:  # noqa: BLE001
                    wake_status = ""
            return "delivered to root session" + (
                f" ({wake_status})" if wake_status else ""
            )

        if sub_record is not None and getattr(sub_record, "inbox", None) is not None:
            # Terminal sub-agents stay in the registry post-completion so
            # the renderer can still see them, but their runners are gone
            # and nothing will ever drain their inbox. Push-to-dead-inbox
            # silently swallows messages and any wait_for_reply hangs.
            # Detect terminal state and fall through to the re-wake path
            # — the saved sidecar gets spawned afresh with the message
            # queued for iteration 1.
            from bridge.tools.sub_agent_registry import SubAgentState
            is_terminal = sub_record.state in (
                SubAgentState.DONE,
                SubAgentState.FAILED,
                SubAgentState.CANCELLED,
            )
            if not is_terminal:
                sub_record.inbox.push(msg)
                if msg.force:
                    self._signal_force_cancel_record(sub_record)
                return "delivered to sub-agent"
            # Terminal — try re-wake. Look up sidecar from disk; if no
            # sidecar exists (very early-failed agent that never wrote
            # one), fall back to pushing to the dead inbox + emit a
            # warning in the status so the operator sees it.
            archived_state = archived_state or self._resolve_archived_sub(recipient_id)
            if archived_state is None:
                sub_record.inbox.push(msg)
                return (
                    "delivered to terminal sub-agent (no sidecar — message "
                    "may not be processed)"
                )

        if archived_state is not None:
            try:
                result = self._wake_archived_sub(recipient_id, msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                return f"re-wake failed: {exc}"
            return "queued for re-wake (recipient archived)"

        return "recipient not found"

    def _signal_force_cancel_record(self, record: Any) -> None:
        """Trip the SubAgentRecord's cancel events so the runner exits
        the in-flight LLM stream / tool call ASAP. The runner's own
        compliance-iteration logic (Phase 3) handles the recovery."""
        try:
            record.cancel_event.set()
        except Exception:
            pass
        loop = getattr(record, "loop", None)
        evt = getattr(record, "asyncio_cancel", None)
        if loop is not None and evt is not None:
            try:
                loop.call_soon_threadsafe(evt.set)
            except Exception:
                pass


# ============================================================================
# talk tool
# ============================================================================

class TalkTool:
    """Push a message into another agent (or the operator) session's inbox."""

    def __init__(self, router: TalkRouter, ctx: TalkRouterContext) -> None:
        self._router = router
        self._ctx = ctx

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="talk",
            summary="Send a message to another agent session (parent / sibling / child / by id).",
            tier=ToolTier.HOT,
            description=(
                "Send a message to one or more agent sessions. Used for "
                "coordination, asking clarifying questions, redirecting work, "
                "or sharing context that would be useful to a sibling.\n\n"
                "Addressing:\n"
                "  - 'parent' — your spawning session (root agent or another sub-agent)\n"
                "  - 'main' / 'operator' — the root operator session\n"
                "  - '<session_id>' — any session you have a concrete id for "
                "(gateway ids like 'freyja:slack:…' work with or without the "
                "'freyja:' prefix)\n"
                "  - '<label>' — a sibling or child by display label (unique only)\n"
                "  - or a list of any of the above for multi-cast\n\n"
                "Use `list_agent_sessions` first to see who is addressable.\n\n"
                "Delivery: idle recipients are WOKEN — a delivery turn runs "
                "immediately so the message is processed now, not at the "
                "recipient's next operator poke. Sessions owned by another "
                "Freyja process (e.g. Slack threads served by the gateway "
                "daemon) are forwarded there automatically; the tool result "
                "states exactly how the message was delivered. A result "
                "saying 'queued' or 'sidecar' means the recipient has NOT "
                "seen it yet.\n\n"
                "Flags:\n"
                "  - force=true: interrupts the recipient mid-operation. The "
                "recipient's current LLM stream / tool call is cancelled, "
                "they're given one compliance iteration to react to your "
                "message, then they exit. Use sparingly — for stop signals "
                "or critical redirects, not routine FYI.\n"
                "  - wait_for_reply=true: blocks YOUR turn until the "
                "recipient sends a reply tagged to this message (they must "
                "call talk with reply_to=<your message id>, which they see "
                "in the delivered header). Times out after reply_timeout_s "
                "seconds (default 60). A timeout does NOT mean failure — "
                "the message stays queued; don't repeat it blindly.\n\n"
                "Messages to non-running sub-agents will RE-WAKE them — "
                "the recipient picks up where it left off with your "
                "message prepended."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Recipient ref(s) — id, label, or alias.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The message body. Be specific; the recipient is another agent who will read this in-context.",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Interrupt the recipient mid-operation. Use for urgent stops/redirects.",
                    },
                    "wait_for_reply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Block your turn until the recipient replies (tagged to this message).",
                    },
                    "reply_timeout_s": {
                        "type": "integer",
                        "default": DEFAULT_REPLY_TIMEOUT_S,
                        "description": f"Max seconds to wait when wait_for_reply=true. Max {MAX_REPLY_TIMEOUT_S}.",
                    },
                    "reply_to": {
                        "type": "string",
                        "description": "If this message IS a reply to a previous talk(), set this to the source message id. Lets the original sender's wait_for_reply unblock.",
                    },
                },
                "required": ["to", "content"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        to_arg = arguments.get("to")
        content = (arguments.get("content") or "").strip()
        force = bool(arguments.get("force") or False)
        wait_for_reply = bool(arguments.get("wait_for_reply") or False)
        reply_timeout_s = arguments.get("reply_timeout_s") or DEFAULT_REPLY_TIMEOUT_S
        reply_to = arguments.get("reply_to")

        if not content:
            return ToolResult(
                call_id=call_id,
                content="Error: `content` is required",
                is_error=True,
            )

        recipients: list[str]
        if isinstance(to_arg, str):
            recipients = [to_arg]
        elif isinstance(to_arg, list):
            recipients = [str(r) for r in to_arg if r]
        else:
            return ToolResult(
                call_id=call_id,
                content="Error: `to` must be a string or list of strings",
                is_error=True,
            )
        if not recipients:
            return ToolResult(
                call_id=call_id,
                content="Error: `to` is empty",
                is_error=True,
            )

        if wait_for_reply and len(recipients) > 1:
            return ToolResult(
                call_id=call_id,
                content="Error: wait_for_reply requires a single recipient",
                is_error=True,
            )

        msg_id = new_message_id()
        results: list[str] = []
        # Messages that plausibly reached a recipient (locally or via a
        # cross-process forward) — a reply to any of them can land in
        # our inbox, so wait_for_reply is meaningful for all of these.
        awaitable: list[InboxMessage] = []

        for ref in recipients:
            ref_clean = (ref or "").strip()
            resolved_id, live_root, sub_rec, archived = self._router.resolve_ref(
                ref_clean, self._ctx
            )
            # Gateway ids are routinely written without the "freyja:"
            # prefix (Slack surfaces them as "slack:T…:channel:…").
            # Normalize so both spellings address the same session.
            target_id = resolved_id
            if not target_id and ref_clean:
                cand = (
                    ref_clean
                    if ref_clean.startswith("freyja:")
                    else f"freyja:{ref_clean}"
                )
                if _is_gateway_session_id(cand):
                    resolved_id, live_root, sub_rec, archived = (
                        self._router.resolve_ref(cand, self._ctx)
                    )
                    target_id = resolved_id or cand
                else:
                    # Unresolved non-gateway ref — it may still be a real
                    # but closed session; the on-disk existence gate in
                    # the cold-delivery branch below decides.
                    target_id = ref_clean
            if not target_id:
                results.append(
                    f"'{ref}': unresolved — use list_agent_sessions to find "
                    "addressable ids/labels"
                )
                continue

            msg = InboxMessage(
                id=msg_id if len(recipients) == 1 else new_message_id(),
                from_session=self._ctx.caller_session_id,
                from_label=self._ctx.caller_label,
                from_role=self._ctx.caller_role,
                content=content,
                force=force,
                reply_to=(str(reply_to) if reply_to else None),
            )

            # Cross-process routing first: a local mirror of a session
            # owned by another process must NOT swallow the message.
            fwd = self._router.forward_cross_process(
                target_id,
                msg,
                live_local=(live_root is not None or sub_rec is not None),
                archived_state=archived,
            )
            if fwd is not None:
                results.append(f"'{ref}' → {target_id}: {fwd}")
                if "failed" not in fwd:
                    awaitable.append(msg)
                continue

            if live_root is None and sub_rec is None and archived is None:
                # Nothing loaded locally. Deliver cold ONLY when the
                # session verifiably exists on disk (gateway session
                # after a daemon restart, or a closed desktop session).
                # A typo'd id must stay a hard "unresolved", not a
                # sidecar file for a session that will never run.
                if not _session_exists_on_disk(target_id):
                    results.append(
                        f"'{ref}': unresolved — no such session; use "
                        "list_agent_sessions to find addressable ids/labels"
                    )
                    continue
                try:
                    from bridge.freyja_bridge import (
                        deliver_talk_message_locally,
                    )

                    status = await deliver_talk_message_locally(
                        self._router.bridge_state, target_id, msg
                    )
                except Exception as exc:  # noqa: BLE001
                    status = f"delivery failed: {exc}"
                results.append(f"'{ref}' → {target_id}: {status}")
                if "failed" not in status:
                    awaitable.append(msg)
                continue

            status = await self._router.deliver(
                target_id, live_root, sub_rec, archived, msg
            )
            results.append(f"'{ref}' ({target_id[:24]}): {status}")
            if "not found" not in status and "failed" not in status:
                awaitable.append(msg)

        # Handle wait_for_reply (single-recipient case enforced above)
        if wait_for_reply and len(awaitable) == 1:
            sent_msg = awaitable[0]
            timeout = max(1, min(int(reply_timeout_s), MAX_REPLY_TIMEOUT_S))
            reply = await self._await_reply(
                source_msg_id=sent_msg.id,
                timeout_s=timeout,
            )
            delivery_note = results[0] if results else "sent"
            if reply is None:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Sent (id={sent_msg.id}); delivery: {delivery_note}. "
                        f"No reply within {timeout}s. The message stays in "
                        "the recipient's inbox and will be read on its next "
                        "turn — but nothing was acknowledged, so do NOT "
                        "assume it acted. Check back later, or watch for "
                        "its reply arriving as a \"[message from …]\" block."
                    ),
                    is_error=False,
                )
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Sent (id={sent_msg.id}); delivery: {delivery_note}. "
                    f"Reply from {reply.from_label}: {reply.content}"
                ),
                is_error=False,
            )

        return ToolResult(
            call_id=call_id,
            content="; ".join(results) + f" (msg id={msg_id})",
            is_error=False,
        )

    def _caller_inbox(self) -> Any:
        """Find this caller's inbox. Caller may be a root session or a
        sub-agent — TalkRouter knows how to find either."""
        root = self._router.session_for(self._ctx.caller_session_id)
        if root is not None and getattr(root, "inbox", None) is not None:
            return root.inbox
        sub = self._router.find_subagent_record(self._ctx.caller_session_id)
        if sub is not None and getattr(sub, "inbox", None) is not None:
            return sub.inbox
        return None

    async def _await_reply(
        self,
        *,
        source_msg_id: str,
        timeout_s: int,
    ) -> InboxMessage | None:
        """Block the caller until a reply tagged with reply_to=source_msg_id
        lands in OUR inbox. Caller can be either a root session or a
        sub-agent — TalkRouter knows how to find either's inbox."""
        inbox = self._caller_inbox()
        if inbox is None:
            return None

        # take_reply (not peek) — the reply's content is returned inline
        # in this tool result, so it must leave the unread queue or the
        # next pre-iteration drain would deliver the same content twice.
        got = inbox.take_reply(source_msg_id)
        if got is not None:
            return got

        event = asyncio.Event()
        inbox.add_reply_waiter(source_msg_id, event)
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return None
            return inbox.take_reply(source_msg_id)
        finally:
            inbox.remove_reply_waiter(source_msg_id)


# ============================================================================
# list_agent_sessions tool
# ============================================================================

class ListAgentSessionsTool:
    """Directory of addressable agent sessions for the caller.

    Default `connected=True` returns only the caller's parent, siblings,
    and children. `connected=False` returns every session known to the
    bridge — useful for cross-mission coordination but should be opt-in.
    """

    def __init__(self, router: TalkRouter, ctx: TalkRouterContext) -> None:
        self._router = router
        self._ctx = ctx

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_agent_sessions",
            summary="List agent sessions you can address with `talk`.",
            tier=ToolTier.HOT,
            description=(
                "Returns a directory of agent sessions you can address with "
                "the `talk` tool. By default returns only sessions related to "
                "you (parent + siblings + children); pass connected=false to "
                "see every session known to the bridge.\n\n"
                "Each entry includes the session id, display label, agent "
                "profile, relationship to you, status, and a short task "
                "preview so you can disambiguate similar-looking siblings.\n\n"
                "The full directory can run to hundreds of archived "
                "sessions — pass `query` (case-insensitive substring over "
                "id, label, and task preview) and/or `limit` instead of "
                "paging through everything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "connected": {
                        "type": "boolean",
                        "default": True,
                        "description": "True (default) = only parent + siblings + children. False = every visible session.",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include sub-agents that have completed but are still re-wakeable via talk().",
                    },
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring filter over id, label, and task preview.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 40,
                        "description": "Max entries returned (running sessions sort first). Use with query to find one session cheaply.",
                    },
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        connected = bool(arguments.get("connected", True))
        include_archived = bool(arguments.get("include_archived", True))
        query = str(arguments.get("query") or "").strip().lower()
        try:
            limit = max(1, int(arguments.get("limit") or 40))
        except (TypeError, ValueError):
            limit = 40
        entries = self._enumerate(connected=connected, include_archived=include_archived)
        if query:
            entries = [
                e for e in entries
                if query in str(e.get("id", "")).lower()
                or query in str(e.get("label", "")).lower()
                or query in str(e.get("task_preview", "")).lower()
            ]
        total = len(entries)
        if total > limit:
            # Running sessions are almost always what the caller wants;
            # keep them ahead of archived ones before cutting.
            entries.sort(key=lambda e: 0 if e.get("status") == "running" else 1)
            entries = entries[:limit]
        payload: dict[str, Any] = {
            "sessions": entries,
            "count": total,
            "shown": len(entries),
        }
        if total > len(entries):
            payload["note"] = (
                f"{total - len(entries)} more matched — narrow with `query` "
                "or raise `limit`."
            )
        return ToolResult(
            call_id=call_id,
            content=json.dumps(payload, indent=2),
            is_error=False,
        )

    def _enumerate(
        self, *, connected: bool, include_archived: bool
    ) -> list[dict[str, Any]]:
        running = self._router.running_sessions()
        out: list[dict[str, Any]] = []
        caller_id = self._ctx.caller_session_id
        caller_parent = self._ctx.parent_session_id

        # Root sessions
        for sess_id, sess in running.items():
            if sess_id == caller_id:
                continue
            relationship = self._classify_root(sess_id, sess, caller_id, caller_parent)
            if connected and relationship == "unrelated":
                continue
            out.append({
                "id": sess_id,
                "label": _session_label(sess),
                "agent_type": getattr(sess, "agent_type", None) or "general",
                "relationship": relationship,
                "status": "running",
                "task_preview": _task_preview(sess),
                "unread_messages": (
                    len(sess.inbox.unread) if getattr(sess, "inbox", None) else 0
                ),
            })

        # Live sub-agent records (siblings + children of caller)
        for rec in self._router.all_subagent_records():
            if rec.id == caller_id:
                continue
            relationship = self._classify_sub(rec, caller_id, caller_parent)
            if connected and relationship == "unrelated":
                continue
            status = "running" if rec.state.name == "RUNNING" else "completed"
            inbox = getattr(rec, "inbox", None)
            out.append({
                "id": rec.id,
                "label": getattr(rec, "label", "") or rec.id,
                "agent_type": getattr(rec, "agent_type_name", "") or "general",
                "relationship": relationship,
                "status": status,
                "task_preview": _truncate(getattr(rec, "task", "") or "", 120),
                "unread_messages": len(inbox.unread) if inbox else 0,
            })

        if include_archived:
            for entry in self._enumerate_archived(caller_id, caller_parent, connected):
                out.append(entry)

        # Sort by relationship priority then label for predictability
        rel_rank = {"parent": 0, "sibling": 1, "child": 2, "unrelated": 3}
        out.sort(key=lambda e: (rel_rank.get(e["relationship"], 4), e["label"]))
        return out

    def _classify_root(
        self,
        sess_id: str,
        sess: Any,
        caller_id: str,
        caller_parent: str | None,
    ) -> str:
        sess_parent = getattr(sess, "parent_session_id", None)
        if sess_id == caller_parent:
            return "parent"
        if sess_parent == caller_id:
            return "child"
        if sess_parent and caller_parent and sess_parent == caller_parent:
            return "sibling"
        return "unrelated"

    def _classify_sub(
        self,
        rec: Any,
        caller_id: str,
        caller_parent: str | None,
    ) -> str:
        sub_parent = getattr(rec, "parent_session_id", "") or ""
        if rec.id == caller_parent:
            return "parent"
        if sub_parent == caller_id:
            return "child"
        if sub_parent and caller_parent and sub_parent == caller_parent:
            return "sibling"
        return "unrelated"

    def _enumerate_archived(
        self,
        caller_id: str,
        caller_parent: str | None,
        connected: bool,
    ) -> list[dict[str, Any]]:
        """Walk the on-disk sub-agent sidecars to find addressable
        completed sessions. Lightweight — only reads files matching
        *.subagent.json under the sessions dir."""
        from bridge.transcript_persistence import SESSIONS_DIR

        out: list[dict[str, Any]] = []
        try:
            for path in SESSIONS_DIR.glob("*.subagent.json"):
                try:
                    import json as _json
                    data = _json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sess_id = str(data.get("sessionId") or "")
                if not sess_id or sess_id == caller_id:
                    continue
                parent_id = data.get("parentSessionId") or None
                # Relationship classification mirrors the live path
                if sess_id == caller_parent:
                    rel = "parent"
                elif parent_id == caller_id:
                    rel = "child"
                elif parent_id and caller_parent and parent_id == caller_parent:
                    rel = "sibling"
                else:
                    rel = "unrelated"
                if connected and rel == "unrelated":
                    continue
                out.append({
                    "id": sess_id,
                    "label": str(data.get("label") or sess_id),
                    "agent_type": str(data.get("agentType") or "general"),
                    "relationship": rel,
                    "status": "archived",
                    "task_preview": _truncate(str(data.get("task") or ""), 120),
                    "unread_messages": 0,
                })
        except Exception:
            pass
        return out


# ---- helpers ----

def _session_label(sess: Any) -> str:
    rec = getattr(sess, "subagent_record", None)
    if rec is not None:
        return getattr(rec, "label", "") or sess.id
    return getattr(sess, "title", None) or sess.id


def _task_preview(sess: Any) -> str:
    rec = getattr(sess, "subagent_record", None)
    if rec is not None:
        return _truncate(getattr(rec, "task", "") or "", 120)
    return ""


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"
