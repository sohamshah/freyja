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
            return "delivered to root session"

        if sub_record is not None and getattr(sub_record, "inbox", None) is not None:
            sub_record.inbox.push(msg)
            if msg.force:
                self._signal_force_cancel_record(sub_record)
            return "delivered to sub-agent"

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
                "  - '<session_id>' — any session you have a concrete id for\n"
                "  - '<label>' — a sibling or child by display label (unique only)\n"
                "  - or a list of any of the above for multi-cast\n\n"
                "Use `list_agent_sessions` first to see who is addressable.\n\n"
                "Flags:\n"
                "  - force=true: interrupts the recipient mid-operation. The "
                "recipient's current LLM stream / tool call is cancelled, "
                "they're given one compliance iteration to react to your "
                "message, then they exit. Use sparingly — for stop signals "
                "or critical redirects, not routine FYI.\n"
                "  - wait_for_reply=true: blocks YOUR turn until the "
                "recipient sends a reply tagged to this message. Times out "
                "after reply_timeout_s seconds (default 60). Use when you "
                "genuinely cannot proceed without an answer.\n\n"
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
        delivered_sessions: list[tuple[Any, InboxMessage]] = []

        for ref in recipients:
            resolved_id, live_root, sub_rec, archived = self._router.resolve_ref(
                ref, self._ctx
            )
            if not resolved_id:
                results.append(f"'{ref}': unresolved")
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
            status = await self._router.deliver(
                resolved_id, live_root, sub_rec, archived, msg
            )
            results.append(f"'{ref}' ({resolved_id[:8]}): {status}")
            # Track live deliveries for wait_for_reply correlation
            if live_root is not None or sub_rec is not None:
                delivered_sessions.append((live_root or sub_rec, msg))

        # Handle wait_for_reply (single-recipient case enforced above)
        if wait_for_reply and len(delivered_sessions) == 1:
            _recipient, sent_msg = delivered_sessions[0]
            timeout = max(1, min(int(reply_timeout_s), MAX_REPLY_TIMEOUT_S))
            reply = await self._await_reply(
                source_msg_id=sent_msg.id,
                timeout_s=timeout,
            )
            if reply is None:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Sent (id={sent_msg.id}). No reply within {timeout}s — "
                        "proceed without."
                    ),
                    is_error=False,
                )
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Sent (id={sent_msg.id}). Reply from {reply.from_label}: "
                    f"{reply.content}"
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

        # First, check if a reply has already arrived (race)
        for m in inbox.peek_unread():
            if m.reply_to == source_msg_id:
                return m

        event = asyncio.Event()
        inbox.add_reply_waiter(source_msg_id, event)
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return None
            # Reply arrived — scan unread for the matching message
            for m in inbox.peek_unread():
                if m.reply_to == source_msg_id:
                    return m
            return None
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
                "preview so you can disambiguate similar-looking siblings."
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
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        connected = bool(arguments.get("connected", True))
        include_archived = bool(arguments.get("include_archived", True))
        entries = self._enumerate(connected=connected, include_archived=include_archived)
        return ToolResult(
            call_id=call_id,
            content=json.dumps({"sessions": entries, "count": len(entries)}, indent=2),
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
