"""Lightweight session-local Kanban coordination tools."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


STATUSES = ("todo", "ready", "running", "blocked", "done", "cancelled")
TERMINAL_STATUSES = {"done", "cancelled"}


@dataclass
class KanbanComment:
    author: str
    body: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "body": self.body,
            "timestamp": int(self.timestamp * 1000),
        }


@dataclass
class KanbanEvent:
    kind: str
    actor: str
    message: str
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "actor": self.actor,
            "message": self.message,
            "timestamp": int(self.timestamp * 1000),
            "details": self.details,
        }


@dataclass
class KanbanTask:
    id: str
    title: str
    body: str = ""
    assignee: str = ""
    status: str = "ready"
    priority: int = 2
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    created_by: str = "parent"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: str = ""
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    comments: list[KanbanComment] = field(default_factory=list)
    events: list[KanbanEvent] = field(default_factory=list)

    def to_dict(self, *, include_history: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "assignee": self.assignee,
            "status": self.status,
            "priority": self.priority,
            "parents": self.parents,
            "children": self.children,
            "createdBy": self.created_by,
            "createdAt": int(self.created_at * 1000),
            "updatedAt": int(self.updated_at * 1000),
            "startedAt": int(self.started_at * 1000) if self.started_at else None,
            "completedAt": int(self.completed_at * 1000) if self.completed_at else None,
            "summary": self.summary,
            "result": self.result,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }
        if include_history:
            payload["comments"] = [comment.to_dict() for comment in self.comments]
            payload["events"] = [event.to_dict() for event in self.events]
        return payload


class SessionKanbanBoard:
    """Small in-memory board scoped to one Freyja session."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, KanbanTask] = {}
        self._counter = 0

    async def create(
        self,
        *,
        title: str,
        body: str = "",
        assignee: str = "",
        parents: list[str] | None = None,
        priority: int = 2,
        actor: str = "parent",
        metadata: dict[str, Any] | None = None,
    ) -> KanbanTask:
        async with self._lock:
            clean_parents = [pid for pid in parents or [] if pid in self._tasks]
            self._counter += 1
            task_id = f"card_{self._counter:03d}"
            status = "ready"
            if any(self._tasks[pid].status != "done" for pid in clean_parents):
                status = "todo"
            task = KanbanTask(
                id=task_id,
                title=title.strip() or task_id,
                body=body.strip(),
                assignee=assignee.strip(),
                status=status,
                priority=max(0, min(int(priority or 2), 5)),
                parents=clean_parents,
                created_by=actor,
                metadata=metadata or {},
            )
            task.events.append(
                KanbanEvent(
                    "created",
                    actor,
                    f"Created {task.id}",
                    details={"parents": clean_parents},
                )
            )
            self._tasks[task_id] = task
            for parent_id in clean_parents:
                parent = self._tasks[parent_id]
                if task_id not in parent.children:
                    parent.children.append(task_id)
                    parent.updated_at = time.time()
            return task

    async def list(self) -> list[KanbanTask]:
        async with self._lock:
            order = {status: idx for idx, status in enumerate(STATUSES)}
            return sorted(
                self._tasks.values(),
                key=lambda task: (order.get(task.status, 99), task.priority, task.created_at),
            )

    async def get(self, task_id: str) -> KanbanTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update(
        self,
        task_id: str,
        *,
        actor: str,
        status: str | None = None,
        assignee: str | None = None,
        body: str | None = None,
        comment: str | None = None,
        summary: str | None = None,
        result: str | None = None,
        artifacts: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KanbanTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            now = time.time()
            changes: dict[str, Any] = {}
            if status:
                status = status.lower()
                if status not in STATUSES:
                    raise ValueError(f"invalid status {status!r}")
                if status == "running" and task.started_at is None:
                    task.started_at = now
                if status in TERMINAL_STATUSES and task.completed_at is None:
                    task.completed_at = now
                task.status = status
                changes["status"] = status
            if assignee is not None:
                task.assignee = assignee.strip()
                changes["assignee"] = task.assignee
            if body is not None:
                task.body = body.strip()
                changes["body"] = True
            if summary is not None:
                task.summary = summary.strip()
                changes["summary"] = True
            if result is not None:
                task.result = result.strip()
                changes["result"] = True
            if artifacts:
                for artifact in artifacts:
                    if artifact and artifact not in task.artifacts:
                        task.artifacts.append(artifact)
                changes["artifacts"] = len(task.artifacts)
            if metadata:
                task.metadata = {**task.metadata, **metadata}
                changes["metadata"] = sorted(metadata.keys())
            if comment:
                task.comments.append(KanbanComment(actor, comment.strip()))
                changes["comment"] = True
            task.updated_at = now
            task.events.append(
                KanbanEvent("updated", actor, f"Updated {task.id}", details=changes)
            )
            if task.status == "done":
                self._promote_unblocked_children(task.id, actor)
            return task

    async def link(self, parent_id: str, child_id: str, *, actor: str) -> tuple[KanbanTask | None, str]:
        async with self._lock:
            if parent_id == child_id:
                return None, "cannot link a card to itself"
            parent = self._tasks.get(parent_id)
            child = self._tasks.get(child_id)
            if parent is None or child is None:
                return None, "parent or child card not found"
            if self._would_create_cycle(parent_id, child_id):
                return None, "link would create a dependency cycle"
            if child_id not in parent.children:
                parent.children.append(child_id)
            if parent_id not in child.parents:
                child.parents.append(parent_id)
            child.status = "ready" if self._parents_done(child) else "todo"
            parent.updated_at = child.updated_at = time.time()
            parent.events.append(KanbanEvent("linked", actor, f"Linked to {child_id}"))
            child.events.append(KanbanEvent("linked", actor, f"Depends on {parent_id}"))
            return child, "linked"

    async def exists(self, task_id: str) -> bool:
        async with self._lock:
            return task_id in self._tasks

    def _parents_done(self, task: KanbanTask) -> bool:
        return all(self._tasks[parent_id].status == "done" for parent_id in task.parents)

    def _promote_unblocked_children(self, parent_id: str, actor: str) -> None:
        parent = self._tasks[parent_id]
        for child_id in parent.children:
            child = self._tasks.get(child_id)
            if child and child.status == "todo" and self._parents_done(child):
                child.status = "ready"
                child.updated_at = time.time()
                child.events.append(
                    KanbanEvent("promoted", actor, "All dependencies complete")
                )

    def _would_create_cycle(self, parent_id: str, child_id: str) -> bool:
        stack = [parent_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == child_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._tasks.get(current, KanbanTask(current, current)).parents)
        return False


KanbanEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


class KanbanTool:
    """Tool wrapper around a session-local Kanban board."""

    def __init__(
        self,
        board: SessionKanbanBoard,
        *,
        actor_id: str,
        actor_label: str,
        emit_event: KanbanEventCb | None = None,
        parent_session_id: str = "",
    ) -> None:
        self._board = board
        self._actor_id = actor_id
        self._actor_label = actor_label
        self._emit_event = emit_event
        self._parent_session_id = parent_session_id

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="kanban",
            summary="Coordinate multi-agent work through a shared Kanban board",
            tier=ToolTier.HOT,
            description="""Create, inspect, and update a session-local Kanban board.

Use this in kanban coordination mode to decompose a mission into cards, link
dependency gates, assign work to agent profiles, and leave durable handoffs.
Workers should call `show` first for their assigned card, then heartbeat/comment,
complete, or block the card with useful detail.""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "create",
                            "show",
                            "claim",
                            "update",
                            "comment",
                            "complete",
                            "block",
                            "heartbeat",
                            "link",
                        ],
                    },
                    "task_id": {"type": "string", "description": "Kanban card id"},
                    "title": {"type": "string", "description": "New card title"},
                    "body": {"type": "string", "description": "Card body or updated body"},
                    "assignee": {"type": "string", "description": "Profile/worker assignment"},
                    "parents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Parent card ids this card depends on",
                    },
                    "parent_id": {"type": "string", "description": "Parent id for link"},
                    "child_id": {"type": "string", "description": "Child id for link"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
                    "comment": {"type": "string", "description": "Progress note or blocker reason"},
                    "summary": {"type": "string", "description": "Completion/handoff summary"},
                    "result": {"type": "string", "description": "Final result details"},
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Verified artifact paths produced for this card",
                    },
                    "metadata": {"type": "object", "description": "Optional structured metadata"},
                    "created_cards": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Card ids referenced by a completion handoff; verified against the board",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            payload = await self._execute_action(action, arguments)
        except ValueError as exc:
            return ToolResult(call_id=call_id, content=f"Error: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call_id=call_id, content=f"Kanban error: {exc}", is_error=True)
        await self._emit(action, payload)
        return ToolResult(
            call_id=call_id,
            content=json.dumps(payload, indent=2, sort_keys=True),
            is_error=False,
        )

    async def _execute_action(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        actor = f"{self._actor_label} ({self._actor_id})"
        if action == "list":
            tasks = [task.to_dict(include_history=False) for task in await self._board.list()]
            return {"action": action, "tasks": tasks, "count": len(tasks)}

        if action == "create":
            task = await self._board.create(
                title=str(arguments.get("title") or "").strip(),
                body=str(arguments.get("body") or "").strip(),
                assignee=str(arguments.get("assignee") or "").strip(),
                parents=[str(pid) for pid in arguments.get("parents") or []],
                priority=int(arguments.get("priority") or 2),
                actor=actor,
                metadata=dict(arguments.get("metadata") or {}),
            )
            return {"action": action, "task": task.to_dict()}

        if action == "show":
            task = await self._require_task(arguments)
            return {"action": action, "task": task.to_dict()}

        if action == "claim":
            task = await self._require_task(arguments)
            updated = await self._board.update(
                task.id,
                actor=actor,
                status="running",
                assignee=str(arguments.get("assignee") or self._actor_label),
                comment=str(arguments.get("comment") or "Claimed card").strip(),
            )
            return {"action": action, "task": updated.to_dict() if updated else None}

        if action in {"update", "comment", "complete", "block", "heartbeat"}:
            task = await self._require_task(arguments)
            status = arguments.get("status")
            comment = str(arguments.get("comment") or "").strip()
            summary = arguments.get("summary")
            result = arguments.get("result")
            metadata = dict(arguments.get("metadata") or {})
            artifacts = [str(item) for item in arguments.get("artifacts") or []]
            if action == "comment":
                if not comment:
                    raise ValueError("comment is required")
            elif action == "complete":
                status = "done"
                missing = [
                    str(card_id)
                    for card_id in arguments.get("created_cards") or []
                    if not await self._board.exists(str(card_id))
                ]
                if missing:
                    raise ValueError(f"created_cards not found: {', '.join(missing)}")
            elif action == "block":
                status = "blocked"
                if not comment:
                    raise ValueError("comment is required when blocking a card")
            elif action == "heartbeat":
                comment = comment or "Heartbeat"
                metadata = {**metadata, "heartbeatAt": int(time.time() * 1000)}
            updated = await self._board.update(
                task.id,
                actor=actor,
                status=str(status).lower() if status else None,
                assignee=arguments.get("assignee"),
                body=arguments.get("body"),
                comment=comment or None,
                summary=str(summary).strip() if summary is not None else None,
                result=str(result).strip() if result is not None else None,
                artifacts=artifacts,
                metadata=metadata,
            )
            return {"action": action, "task": updated.to_dict() if updated else None}

        if action == "link":
            parent_id = str(arguments.get("parent_id") or "").strip()
            child_id = str(arguments.get("child_id") or "").strip()
            if not parent_id or not child_id:
                raise ValueError("parent_id and child_id are required")
            child, message = await self._board.link(parent_id, child_id, actor=actor)
            if child is None:
                raise ValueError(message)
            return {"action": action, "message": message, "task": child.to_dict()}

        raise ValueError(f"unknown action {action!r}")

    async def _require_task(self, arguments: dict[str, Any]) -> KanbanTask:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        task = await self._board.get(task_id)
        if task is None:
            raise ValueError(f"card {task_id!r} not found")
        return task

    async def _emit(self, action: str, payload: dict[str, Any]) -> None:
        if self._emit_event is None:
            return
        message = f"Kanban {action}"
        task = payload.get("task")
        if isinstance(task, dict):
            message = f"Kanban {action}: {task.get('id', '')} {task.get('title', '')}".strip()
        event = {
            "type": "system_event",
            "sessionId": self._parent_session_id or self._actor_id,
            "subtype": f"kanban_{action}",
            "message": message,
            "details": payload,
        }
        result = self._emit_event(event)
        if asyncio.iscoroutine(result):
            await result
