"""
Lightweight session-scoped message bus for inter-agent communication.

Enables sibling subagents to share findings without routing through the
parent agent's context. The bus is an append-only log (not a queue) —
agents read by cursor position, not by consuming messages.

Architecture:
- One SessionMessageBus per _BridgeSession (created in __init__)
- publish_finding / read_findings tools injected into child subagents
- Parent agent does NOT get these tools (findings flow child→child)
- Messages persist for the session lifetime, capped at MAX_MESSAGES
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)

MAX_MESSAGES = 500


@dataclass
class BusMessage:
    """A single message on the bus."""
    index: int
    topic: str
    sender_id: str
    sender_label: str
    content: str
    timestamp: float = field(default_factory=time.time)


class SessionMessageBus:
    """Append-only message log shared by all agents in a session.

    Thread-safe. Agents publish findings and read by cursor position.
    When MAX_MESSAGES is reached, oldest messages are dropped but indices
    are never reused — readers can detect gaps.
    """

    def __init__(self) -> None:
        self._messages: list[BusMessage] = []
        self._lock = threading.Lock()
        self._next_index = 0

    def publish(
        self,
        topic: str,
        sender_id: str,
        sender_label: str,
        content: str,
    ) -> int:
        """Publish a message. Returns the assigned index."""
        with self._lock:
            idx = self._next_index
            self._next_index += 1
            msg = BusMessage(
                index=idx,
                topic=topic,
                sender_id=sender_id,
                sender_label=sender_label,
                content=content,
            )
            self._messages.append(msg)
            # Cap the list
            if len(self._messages) > MAX_MESSAGES:
                self._messages = self._messages[-MAX_MESSAGES:]
            return idx

    def read(
        self,
        *,
        topic: str | None = None,
        since_index: int = 0,
        limit: int = 20,
        exclude_sender: str | None = None,
    ) -> list[BusMessage]:
        """Read messages after since_index, optionally filtered by topic.

        exclude_sender filters out the reader's own messages so agents
        don't echo back their own findings.
        """
        with self._lock:
            results = []
            for msg in self._messages:
                if msg.index < since_index:
                    continue
                if topic and msg.topic != topic:
                    continue
                if exclude_sender and msg.sender_id == exclude_sender:
                    continue
                results.append(msg)
                if len(results) >= limit:
                    break
            return results

    def count(self, topic: str | None = None) -> int:
        """Count messages, optionally filtered by topic."""
        with self._lock:
            if topic is None:
                return len(self._messages)
            return sum(1 for m in self._messages if m.topic == topic)

    @property
    def latest_index(self) -> int:
        """The index of the most recent message, or -1 if empty."""
        with self._lock:
            if not self._messages:
                return -1
            return self._messages[-1].index


def _msg_to_dict(msg: BusMessage) -> dict[str, Any]:
    return {
        "index": msg.index,
        "topic": msg.topic,
        "sender_id": msg.sender_id,
        "sender_label": msg.sender_label,
        "content": msg.content,
        "timestamp": msg.timestamp,
    }


class PublishFindingTool:
    """Tool for subagents to publish findings to the session message bus."""

    def __init__(
        self, bus: SessionMessageBus, agent_id: str, agent_label: str,
        emit_event: Any = None, parent_session_id: str = "",
    ) -> None:
        self._bus = bus
        self._agent_id = agent_id
        self._agent_label = agent_label
        self._emit = emit_event
        self._parent_session_id = parent_session_id

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="publish_finding",
            summary="Share a finding with sibling agents via the session message bus",
            tier=ToolTier.HOT,
            description="""Publish a finding to the shared message bus so sibling agents
can see it. Use this when you discover something that other agents
working on related tasks would benefit from knowing.

Keep findings concise (1-3 sentences) and actionable. Other agents
will see your finding via read_findings.""",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic category (default: findings). Use 'errors' for issues, 'progress' for status updates.",
                        "enum": ["findings", "errors", "progress"],
                    },
                    "content": {
                        "type": "string",
                        "description": "The finding to share (keep concise)",
                    },
                },
                "required": ["content"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        topic = arguments.get("topic", "findings")
        content = (arguments.get("content") or "").strip()
        if not content:
            return ToolResult(call_id=call_id, content="Error: content is required", is_error=True)

        idx = self._bus.publish(
            topic=topic,
            sender_id=self._agent_id,
            sender_label=self._agent_label,
            content=content,
        )
        total = self._bus.count()

        # Emit to the renderer so the swarm monitor can show bus activity
        if self._emit:
            import asyncio
            evt = {
                "type": "bus_message",
                "sessionId": self._parent_session_id,
                "message": {
                    "index": idx,
                    "topic": topic,
                    "senderId": self._agent_id,
                    "senderLabel": self._agent_label,
                    "content": content,
                    "timestamp": self._bus._messages[-1].timestamp if self._bus._messages else 0,
                },
            }
            result_or_coro = self._emit(evt)
            if asyncio.iscoroutine(result_or_coro):
                await result_or_coro

        return ToolResult(
            call_id=call_id,
            content=f"Published to '{topic}' (index={idx}, {total} total messages on bus)",
            is_error=False,
        )


class ReadFindingsTool:
    """Tool for subagents to read findings from sibling agents."""

    def __init__(
        self, bus: SessionMessageBus, agent_id: str,
        agent_label: str = "", emit_event: Any = None,
        parent_session_id: str = "",
    ) -> None:
        self._bus = bus
        self._agent_id = agent_id
        self._agent_label = agent_label
        self._emit = emit_event
        self._parent_session_id = parent_session_id

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_findings",
            summary="Read findings published by sibling agents on the message bus",
            tier=ToolTier.HOT,
            description="""Read messages from the shared session message bus. Use this
to see what sibling agents have discovered. Pass since_index to
only get new messages since your last read.

Returns JSON array of messages with sender, topic, content, and index.
Track the highest index you've seen and pass it as since_index next time.""",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Filter by topic (optional). Omit to read all topics.",
                    },
                    "since_index": {
                        "type": "integer",
                        "description": "Only return messages after this index (default: 0 = all)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default: 20)",
                    },
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        topic = arguments.get("topic")
        since_index = int(arguments.get("since_index", 0))
        limit = int(arguments.get("limit", 20))

        messages = self._bus.read(
            topic=topic,
            since_index=since_index,
            limit=limit,
            exclude_sender=self._agent_id,
        )

        result = {
            "messages": [_msg_to_dict(m) for m in messages],
            "count": len(messages),
            "bus_total": self._bus.count(),
            "latest_index": self._bus.latest_index,
        }

        # Emit read event to renderer so the UI shows who's reading
        if self._emit:
            import asyncio
            import time as _time
            evt = {
                "type": "bus_message",
                "sessionId": self._parent_session_id,
                "message": {
                    "index": -1,
                    "topic": "read",
                    "senderId": self._agent_id,
                    "senderLabel": self._agent_label,
                    "content": f"Read {len(messages)} message(s)" + (f" on topic '{topic}'" if topic else ""),
                    "timestamp": _time.time(),
                },
            }
            result_or_coro = self._emit(evt)
            if asyncio.iscoroutine(result_or_coro):
                await result_or_coro

        return ToolResult(
            call_id=call_id,
            content=json.dumps(result, indent=2),
            is_error=False,
        )
