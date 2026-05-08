"""Lightweight task ledger for task-first solo coordination."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


TASK_STATUSES = ("todo", "active", "blocked", "done", "cancelled")
TERMINAL_TASK_STATUSES = {"done", "cancelled"}


@dataclass
class TaskEvent:
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
class TaskItem:
    id: str
    title: str
    body: str = ""
    status: str = "todo"
    assignee: str = ""
    priority: int = 2
    progress: int = 0
    created_by: str = "parent"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    summary: str = ""
    result: str = ""
    artifacts: list[str] = field(default_factory=list)
    events: list[TaskEvent] = field(default_factory=list)

    def to_dict(self, *, include_history: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "progress": self.progress,
            "createdBy": self.created_by,
            "createdAt": int(self.created_at * 1000),
            "updatedAt": int(self.updated_at * 1000),
            "startedAt": int(self.started_at * 1000) if self.started_at else None,
            "completedAt": int(self.completed_at * 1000) if self.completed_at else None,
            "summary": self.summary,
            "result": self.result,
            "artifacts": self.artifacts,
        }
        if include_history:
            payload["events"] = [event.to_dict() for event in self.events]
        return payload


class SessionTaskBoard:
    """Small in-memory task ledger scoped to one Freyja session."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskItem] = {}
        self._counter = 0

    async def create(
        self,
        *,
        title: str,
        body: str = "",
        assignee: str = "",
        priority: int = 2,
        actor: str = "parent",
    ) -> TaskItem:
        async with self._lock:
            self._counter += 1
            task_id = f"task_{self._counter:03d}"
            task = TaskItem(
                id=task_id,
                title=title.strip() or task_id,
                body=body.strip(),
                assignee=assignee.strip(),
                priority=max(0, min(int(priority or 2), 5)),
                created_by=actor,
            )
            task.events.append(TaskEvent("created", actor, f"Created {task.id}"))
            self._tasks[task_id] = task
            return task

    async def list(self) -> list[TaskItem]:
        async with self._lock:
            order = {status: idx for idx, status in enumerate(TASK_STATUSES)}
            return sorted(
                self._tasks.values(),
                key=lambda task: (order.get(task.status, 99), task.priority, task.created_at),
            )

    async def get(self, task_id: str) -> TaskItem | None:
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
        progress: int | None = None,
        summary: str | None = None,
        result: str | None = None,
        artifacts: list[str] | None = None,
        note: str | None = None,
    ) -> TaskItem | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            now = time.time()
            changes: dict[str, Any] = {}
            if status:
                clean_status = status.lower()
                if clean_status not in TASK_STATUSES:
                    raise ValueError(f"invalid status {status!r}")
                if clean_status == "active" and task.started_at is None:
                    task.started_at = now
                if clean_status in TERMINAL_TASK_STATUSES and task.completed_at is None:
                    task.completed_at = now
                task.status = clean_status
                changes["status"] = clean_status
            if assignee is not None:
                task.assignee = assignee.strip()
                changes["assignee"] = task.assignee
            if body is not None:
                task.body = body.strip()
                changes["body"] = True
            if progress is not None:
                task.progress = max(0, min(int(progress), 100))
                changes["progress"] = task.progress
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
            if note:
                changes["note"] = True
            task.updated_at = now
            task.events.append(
                TaskEvent("updated", actor, note or f"Updated {task.id}", details=changes)
            )
            return task


TaskEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


class TaskBoardTool:
    """Tool wrapper around a session-local task ledger."""

    def __init__(
        self,
        board: SessionTaskBoard,
        *,
        actor_id: str,
        actor_label: str,
        emit_event: TaskEventCb | None = None,
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
            name="tasks",
            summary="Track parent-led work through a lightweight task ledger",
            tier=ToolTier.HOT,
            description="""Create, inspect, and update a session-local task ledger.

Use this in task coordination mode to keep solo work visible. Create tasks for
meaningful units of work, claim active work, heartbeat during long tasks, and
complete/block with concise handoffs. Sub-agents may receive a task_id and
should update that task as they work.""",
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
                            "heartbeat",
                            "complete",
                            "block",
                            "cancel",
                        ],
                    },
                    "task_id": {"type": "string", "description": "Task id"},
                    "title": {"type": "string", "description": "New task title"},
                    "body": {"type": "string", "description": "Task details or replacement body"},
                    "assignee": {"type": "string", "description": "Worker/profile assignment"},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                    "note": {"type": "string", "description": "Progress note or blocker reason"},
                    "summary": {"type": "string", "description": "Short handoff summary"},
                    "result": {"type": "string", "description": "Final result details"},
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths or artifact refs produced for this task",
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
            return ToolResult(call_id=call_id, content=f"Tasks error: {exc}", is_error=True)
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
                priority=int(arguments.get("priority") or 2),
                actor=actor,
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
                status="active",
                assignee=str(arguments.get("assignee") or self._actor_label),
                progress=int(arguments.get("progress") or max(task.progress, 5)),
                note=str(arguments.get("note") or "Claimed task").strip(),
            )
            return {"action": action, "task": updated.to_dict() if updated else None}

        if action in {"update", "heartbeat", "complete", "block", "cancel"}:
            task = await self._require_task(arguments)
            status = arguments.get("status")
            note = str(arguments.get("note") or "").strip()
            progress = arguments.get("progress")
            if action == "heartbeat":
                note = note or "Heartbeat"
            elif action == "complete":
                status = "done"
                progress = 100
            elif action == "block":
                status = "blocked"
                if not note:
                    raise ValueError("note is required when blocking a task")
            elif action == "cancel":
                status = "cancelled"
            updated = await self._board.update(
                task.id,
                actor=actor,
                status=str(status).lower() if status else None,
                assignee=arguments.get("assignee"),
                body=arguments.get("body"),
                progress=int(progress) if progress is not None else None,
                summary=str(arguments.get("summary")).strip() if arguments.get("summary") is not None else None,
                result=str(arguments.get("result")).strip() if arguments.get("result") is not None else None,
                artifacts=[str(item) for item in arguments.get("artifacts") or []],
                note=note or None,
            )
            return {"action": action, "task": updated.to_dict() if updated else None}

        raise ValueError(f"unknown action {action!r}")

    async def _require_task(self, arguments: dict[str, Any]) -> TaskItem:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        task = await self._board.get(task_id)
        if task is None:
            raise ValueError(f"task {task_id!r} not found")
        return task

    async def _emit(self, action: str, payload: dict[str, Any]) -> None:
        if self._emit_event is None:
            return
        message = f"Task {action}"
        task = payload.get("task")
        if isinstance(task, dict):
            message = f"Task {action}: {task.get('id', '')} {task.get('title', '')}".strip()
        event = {
            "type": "system_event",
            "sessionId": self._parent_session_id or self._actor_id,
            "subtype": f"task_{action}",
            "message": message,
            "details": payload,
        }
        result = self._emit_event(event)
        if asyncio.iscoroutine(result):
            await result
