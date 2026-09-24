"""Per-session message inbox for inter-agent + operator-to-agent talk.

Each `_BridgeSession` owns one `SessionInbox` instance. Messages are
pushed via the `talk` tool (agent → agent) or the existing
`send_message` IPC path (operator → agent). The runner's pre-iteration
hook drains pending messages and prepends each one as an attributed
user turn before the next provider call.

Wire shape (`InboxMessage.to_dict`):

    {
      "id":            <uuid>,                   # for reply correlation
      "from_session":  <session_id | "operator">,
      "from_label":    <human-readable sender>,
      "from_role":     "operator" | "agent",
      "content":       <free text>,
      "force":         bool,                     # urgency hint; runner
                                                 # owns the cancel mechanic
      "reply_to":      <message id | null>,      # set when this is a reply
                                                 # to a wait_for_reply message
      "timestamp":     <epoch ms>,
      "delivered_at":  <epoch ms | null>,        # set when drained
      "kind":          "talk" | "followup" | "memo",
      "clientId":      <renderer message id | null>,  # followups only
    }

Kinds:
  * ``talk``     — agent → agent (or operator → any pane) via `talk`.
  * ``followup`` — the operator typed into a session while its turn was
                   running. Slid into that turn at the next boundary
                   instead of waiting for the turn to end. May carry
                   attachments (images); ``clientId`` lets the renderer
                   move its pending bubble to where the message landed.
  * ``memo``     — a background sub-agent finished; the parent is told
                   to go look at what was done.

Bounded depth: oldest unread is dropped when the queue exceeds
`max_unread`. The drop is logged as a system_event so the operator
knows messages were lost.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


MAX_UNREAD_DEFAULT = 100  # memos from a 30-agent fan-out must not evict follow-ups
RECENT_DELIVERED_KEEP = 50  # for renderer history, kept after drain

KIND_TALK = "talk"
KIND_FOLLOWUP = "followup"
KIND_MEMO = "memo"


@dataclass
class InboxMessage:
    id: str
    from_session: str
    from_label: str
    from_role: str  # "operator" | "agent"
    content: str
    force: bool = False
    reply_to: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    delivered_at: Optional[float] = None
    kind: str = KIND_TALK
    # Operator follow-ups can carry images, in the same wire shape
    # send_message uses. Dropped once delivered (the transcript holds
    # them from then on) so the delivered-history sidecar stays small.
    attachments: Optional[list[dict[str, Any]]] = None
    client_id: Optional[str] = None
    # Structured payload for memos (sub-agent id, state, …) so the
    # renderer can draw a card without parsing the text.
    meta: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "fromSession": self.from_session,
            "fromLabel": self.from_label,
            "fromRole": self.from_role,
            "content": self.content,
            "force": self.force,
            "replyTo": self.reply_to,
            "timestamp": int(self.timestamp * 1000),
            "deliveredAt": int(self.delivered_at * 1000) if self.delivered_at else None,
            "kind": self.kind,
        }
        if self.attachments:
            out["attachments"] = self.attachments
        if self.client_id:
            out["clientId"] = self.client_id
        if self.meta:
            out["meta"] = self.meta
        return out

    def to_event_dict(self) -> dict[str, Any]:
        """``to_dict`` without attachment payloads — base64 images do not
        belong in every inbox_event the renderer receives."""
        out = self.to_dict()
        atts = out.pop("attachments", None)
        if atts:
            out["attachmentCount"] = len(atts)
        return out

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["InboxMessage"]:
        if not isinstance(payload, dict):
            return None
        try:
            ts = float(payload.get("timestamp") or 0) / 1000.0 or time.time()
        except Exception:
            ts = time.time()
        delivered_raw = payload.get("deliveredAt")
        try:
            delivered = (
                float(delivered_raw) / 1000.0
                if delivered_raw is not None
                else None
            )
        except Exception:
            delivered = None
        return cls(
            id=str(payload.get("id") or new_message_id()),
            from_session=str(payload.get("fromSession") or "operator"),
            from_label=str(payload.get("fromLabel") or "operator"),
            from_role=str(payload.get("fromRole") or "operator"),
            content=str(payload.get("content") or ""),
            force=bool(payload.get("force") or False),
            reply_to=(
                str(payload.get("replyTo"))
                if payload.get("replyTo")
                else None
            ),
            timestamp=ts,
            delivered_at=delivered,
            kind=str(payload.get("kind") or KIND_TALK),
            attachments=(
                list(payload.get("attachments"))
                if isinstance(payload.get("attachments"), list)
                else None
            ),
            client_id=(
                str(payload.get("clientId")) if payload.get("clientId") else None
            ),
            meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
        )

    def attribution_prefix(self) -> str:
        """Header line prepended when the message is injected into the
        recipient's transcript. Keeps the attribution legible to the
        receiving LLM without bloating context.

        The FULL message id is included so the recipient can reply with
        talk(reply_to=<id>) — reply correlation matches on the exact id,
        and without it in the header the recipient has no way to
        construct a reply the sender's wait_for_reply can see.
        """
        role_tag = (
            "operator"
            if self.from_role == "operator"
            else f"agent · {self.from_label}"
        )
        urgency = " · FORCE" if self.force else ""
        reply = f" · reply to {self.reply_to}" if self.reply_to else ""
        return f"[message from {role_tag} · id {self.id}{urgency}{reply}]"

    def as_user_block(self) -> str:
        """Full transcript-ready block. Memos carry their own header
        (built by build_subagent_memo); everything else gets the
        attribution line so the recipient knows who is talking."""
        if self.kind == KIND_MEMO:
            return self.content.strip()
        return f"{self.attribution_prefix()}\n{self.content.strip()}"


def new_message_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class SessionInbox:
    """In-memory inbox for a single session. Bounded by max_unread to
    cap memory growth in pathological cases."""

    session_id: str
    unread: list[InboxMessage] = field(default_factory=list)
    delivered: list[InboxMessage] = field(default_factory=list)
    max_unread: int = MAX_UNREAD_DEFAULT
    # Reply waiters: { sender_message_id: asyncio.Event } populated when
    # something is awaiting reply_to=<id>. Resolved here so the inbox is
    # the single source of truth for correlation.
    _reply_waiters: dict[str, Any] = field(default_factory=dict)
    # Callback fired (sync) whenever a message is enqueued, drained, or
    # dropped. Bridge wires it to emit telemetry events.
    on_change: Optional[Callable[[str, InboxMessage], None]] = None

    def push(self, msg: InboxMessage) -> Optional[InboxMessage]:
        """Enqueue a new message. Returns the dropped message if the
        queue had to evict an old one to stay within max_unread."""
        dropped: Optional[InboxMessage] = None
        if len(self.unread) >= self.max_unread:
            dropped = self.unread.pop(0)
            self._fire("dropped", dropped)
        self.unread.append(msg)
        self._fire("enqueued", msg)
        # Wake any reply waiter correlated to this message
        if msg.reply_to and msg.reply_to in self._reply_waiters:
            try:
                self._reply_waiters[msg.reply_to].set()
            except Exception:
                pass
        return dropped

    def drain(self) -> list[InboxMessage]:
        """Remove and return all unread messages, marking each delivered.
        Drained messages move to `delivered` (capped at
        RECENT_DELIVERED_KEEP) so the renderer can replay history."""
        if not self.unread:
            return []
        now = time.time()
        out = self.unread
        self.unread = []
        for m in out:
            m.delivered_at = now
            self.delivered.append(m)
            self._fire("delivered", m)
            # The caller holds `out` (attachments intact) for injection;
            # the history copy is the same object, so strip after the
            # caller is done — see strip_delivered_attachments().
        # Trim history
        if len(self.delivered) > RECENT_DELIVERED_KEEP:
            self.delivered = self.delivered[-RECENT_DELIVERED_KEEP:]
        return out

    def peek_unread(self) -> list[InboxMessage]:
        return list(self.unread)

    def take_reply(self, source_msg_id: str) -> Optional[InboxMessage]:
        """Remove and return the unread reply tagged reply_to=source_msg_id.

        Used by talk(wait_for_reply=True): the reply's content is
        returned inline in the tool result, so leaving it in `unread`
        would double-deliver it — the next pre-iteration drain would
        inject the same content again as a user block. Marks the
        message delivered and moves it to history like drain() does.
        """
        for i, m in enumerate(self.unread):
            if m.reply_to == source_msg_id:
                self.unread.pop(i)
                m.delivered_at = time.time()
                self.delivered.append(m)
                if len(self.delivered) > RECENT_DELIVERED_KEEP:
                    self.delivered = self.delivered[-RECENT_DELIVERED_KEEP:]
                self._fire("delivered", m)
                return m
        return None

    def has_unread(self) -> bool:
        return bool(self.unread)

    def has_force_unread(self) -> bool:
        return any(m.force for m in self.unread)

    def has_unread_kind(self, kind: str) -> bool:
        return any(m.kind == kind for m in self.unread)

    def take_kind(self, kind: str) -> list[InboxMessage]:
        """Remove and return every unread message of ``kind``, in order,
        marking each delivered (same bookkeeping as drain()). Used to
        turn queued operator follow-ups into a fresh turn's user message
        when the turn they were aimed at has already ended."""
        picked = [m for m in self.unread if m.kind == kind]
        if not picked:
            return []
        self.unread = [m for m in self.unread if m.kind != kind]
        now = time.time()
        for m in picked:
            m.delivered_at = now
            self.delivered.append(m)
            self._fire("delivered", m)
        if len(self.delivered) > RECENT_DELIVERED_KEEP:
            self.delivered = self.delivered[-RECENT_DELIVERED_KEEP:]
        return picked

    def remove(self, message_id: str) -> Optional[InboxMessage]:
        """Withdraw an unread message (the operator cancelled a queued
        follow-up before it was injected). Returns it, or None if it was
        already delivered."""
        for i, m in enumerate(self.unread):
            if m.id == message_id or (m.client_id and m.client_id == message_id):
                self.unread.pop(i)
                self._fire("withdrawn", m)
                return m
        return None

    def strip_delivered_attachments(self) -> None:
        for m in self.delivered:
            m.attachments = None

    def add_reply_waiter(self, message_id: str, event: Any) -> None:
        self._reply_waiters[message_id] = event

    def remove_reply_waiter(self, message_id: str) -> None:
        self._reply_waiters.pop(message_id, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "unread": [m.to_dict() for m in self.unread],
            "delivered": [m.to_dict() for m in self.delivered[-RECENT_DELIVERED_KEEP:]],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["SessionInbox"]:
        if not isinstance(payload, dict):
            return None
        unread_raw = payload.get("unread") or []
        delivered_raw = payload.get("delivered") or []
        unread = [m for m in (InboxMessage.from_dict(d) for d in unread_raw) if m]
        delivered = [m for m in (InboxMessage.from_dict(d) for d in delivered_raw) if m]
        return cls(
            session_id=str(payload.get("sessionId") or ""),
            unread=unread,
            delivered=delivered,
        )

    def _fire(self, action: str, msg: InboxMessage) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(action, msg)
        except Exception:
            pass
