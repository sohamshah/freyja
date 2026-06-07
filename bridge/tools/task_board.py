"""Lightweight task ledger — the universal planning primitive available
in every coordination mode.

The parent in any mode uses this for personal planning (synthesis steps,
multi-section deliverables, gating user requests). Workers that are
explicitly assigned a task_id at spawn time also receive the tool so
they can heartbeat, complete, and block their own task directly.

Persistence is via TaskJournal (see bridge/task_journal.py): an
append-only JSONL log replayed on session restore. The journal makes
tasks survive bridge restart, which matters because long-running
missions can outlive the runtime.
"""

from __future__ import annotations

import asyncio
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
    # Dependency graph — ids of tasks this one waits on and ids of
    # tasks that wait on this one. Symmetric: linking A→B adds B to
    # A.blocks and A to B.blocked_by. Cycle detection at link time.
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    # The stale-reminder system uses this to figure out "has the agent
    # made meaningful tool-call progress since this task was last
    # touched?" Set by TaskBoardTool to the session-wide tool-call
    # counter at the moment of each task action. Not persisted in the
    # journal (it's a session-local cue, regenerated on replay).
    last_touched_tool_index: int = 0

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
            "blockedBy": list(self.blocked_by),
            "blocks": list(self.blocks),
        }
        if include_history:
            payload["events"] = [event.to_dict() for event in self.events]
        return payload


class SessionTaskBoard:
    """In-memory task ledger scoped to one Freyja session. Optionally
    backed by a TaskJournal so the ledger survives bridge restart."""

    def __init__(self, journal: Any | None = None) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskItem] = {}
        self._counter = 0
        self._journal = journal

    def _journal_append(self, event: dict[str, Any]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.append(event)
        except Exception:  # noqa: BLE001
            # Persistence is best-effort — never block the in-memory
            # mutation if the journal write fails.
            pass

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
            self._journal_append({"kind": "create", "task": task.to_dict(include_history=False)})
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
            # Two parallel deltas: `changes` is the compact in-memory event
            # payload (flags + scalar values, shown in `tasks show` output
            # — kept small so a 10KB body doesn't get duplicated into every
            # event entry). `journal_fields` carries the actual NEW values
            # for prose fields so the journal can faithfully replay them
            # after a bridge restart.
            changes: dict[str, Any] = {}
            journal_fields: dict[str, Any] = {}
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
                journal_fields["status"] = clean_status
                if task.started_at:
                    journal_fields["started_at_ms"] = int(task.started_at * 1000)
                if task.completed_at:
                    journal_fields["completed_at_ms"] = int(task.completed_at * 1000)
            if assignee is not None:
                task.assignee = assignee.strip()
                changes["assignee"] = task.assignee
                journal_fields["assignee"] = task.assignee
            if body is not None:
                task.body = body.strip()
                changes["body"] = True
                journal_fields["body"] = task.body
            if progress is not None:
                task.progress = max(0, min(int(progress), 100))
                changes["progress"] = task.progress
                journal_fields["progress"] = task.progress
            if summary is not None:
                task.summary = summary.strip()
                changes["summary"] = True
                journal_fields["summary"] = task.summary
            if result is not None:
                task.result = result.strip()
                changes["result"] = True
                journal_fields["result"] = task.result
            if artifacts:
                added: list[str] = []
                for artifact in artifacts:
                    if artifact and artifact not in task.artifacts:
                        task.artifacts.append(artifact)
                        added.append(artifact)
                if added:
                    changes["artifacts"] = len(task.artifacts)
                    journal_fields["artifacts"] = list(task.artifacts)
            if note:
                changes["note"] = True
            task.updated_at = now
            task.events.append(
                TaskEvent("updated", actor, note or f"Updated {task.id}", details=changes)
            )
            self._journal_append({
                "kind": "update",
                "id": task.id,
                "actor": actor,
                "fields": journal_fields,
                "note": note or "",
            })
            return task

    async def link(
        self,
        *,
        blocker_id: str,
        dependent_id: str,
        actor: str,
    ) -> tuple[TaskItem | None, str]:
        """Mark `dependent` as blocked by `blocker`. Symmetric write —
        dependent.blocked_by gains blocker, blocker.blocks gains
        dependent. Rejects cycles. Mirrors SessionKanbanBoard.link's
        contract so the renderer can render both ledgers identically."""
        async with self._lock:
            if blocker_id == dependent_id:
                return None, "cannot link a task to itself"
            blocker = self._tasks.get(blocker_id)
            dependent = self._tasks.get(dependent_id)
            if blocker is None or dependent is None:
                return None, "blocker or dependent task not found"
            if self._would_create_cycle(blocker_id, dependent_id):
                return None, "link would create a dependency cycle"
            changed = False
            if dependent_id not in blocker.blocks:
                blocker.blocks.append(dependent_id)
                changed = True
            if blocker_id not in dependent.blocked_by:
                dependent.blocked_by.append(blocker_id)
                changed = True
            if not changed:
                return dependent, "already linked"
            now = time.time()
            blocker.updated_at = now
            dependent.updated_at = now
            blocker.events.append(
                TaskEvent("linked", actor, f"blocks {dependent_id}",
                          details={"dependent": dependent_id})
            )
            dependent.events.append(
                TaskEvent("linked", actor, f"blocked by {blocker_id}",
                          details={"blocker": blocker_id})
            )
            self._journal_append({
                "kind": "link",
                "blocker": blocker_id,
                "dependent": dependent_id,
                "actor": actor,
            })
            return dependent, "linked"

    async def unlink(
        self,
        *,
        blocker_id: str,
        dependent_id: str,
        actor: str,
    ) -> tuple[TaskItem | None, str]:
        """Reverse of link. Idempotent — unlinking already-unlinked
        pair is a no-op success."""
        async with self._lock:
            blocker = self._tasks.get(blocker_id)
            dependent = self._tasks.get(dependent_id)
            if blocker is None or dependent is None:
                return None, "blocker or dependent task not found"
            removed = False
            if dependent_id in blocker.blocks:
                blocker.blocks.remove(dependent_id)
                removed = True
            if blocker_id in dependent.blocked_by:
                dependent.blocked_by.remove(blocker_id)
                removed = True
            if not removed:
                return dependent, "no such link"
            now = time.time()
            blocker.updated_at = now
            dependent.updated_at = now
            blocker.events.append(
                TaskEvent("unlinked", actor, f"no longer blocks {dependent_id}",
                          details={"dependent": dependent_id})
            )
            dependent.events.append(
                TaskEvent("unlinked", actor, f"no longer blocked by {blocker_id}",
                          details={"blocker": blocker_id})
            )
            self._journal_append({
                "kind": "unlink",
                "blocker": blocker_id,
                "dependent": dependent_id,
                "actor": actor,
            })
            return dependent, "unlinked"

    def _would_create_cycle(self, blocker_id: str, dependent_id: str) -> bool:
        """Walk the existing `blocks` graph from dependent_id; if it
        reaches blocker_id, adding (blocker_id → dependent_id) would
        close a cycle. Bounded by the number of tasks so this never
        loops forever even on a corrupt graph."""
        seen: set[str] = set()
        frontier = [dependent_id]
        while frontier:
            current = frontier.pop()
            if current == blocker_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            task = self._tasks.get(current)
            if task is None:
                continue
            frontier.extend(task.blocks)
        return False

    def is_blocked(self, task: TaskItem) -> bool:
        """True iff any of `task.blocked_by` points at a task that's
        not yet in a terminal status. Derived; not persisted."""
        for parent_id in task.blocked_by:
            parent = self._tasks.get(parent_id)
            if parent is None:
                continue
            if parent.status not in TERMINAL_TASK_STATUSES:
                return True
        return False

    def replay_events(self, events: list[dict[str, Any]]) -> None:
        """Rebuild the in-memory ledger from a journal event stream.
        Called once at session restore before any new mutations land.
        Idempotent against malformed entries — skips lines that don't
        parse cleanly rather than aborting the whole replay."""
        for ev in events:
            kind = ev.get("kind")
            try:
                if kind == "create":
                    self._replay_create(ev.get("task") or {})
                elif kind == "update":
                    self._replay_update(ev)
                elif kind == "link":
                    self._replay_link(ev)
                elif kind == "unlink":
                    self._replay_unlink(ev)
                # `restarted` markers exist for diagnostics; no state.
            except Exception:  # noqa: BLE001
                continue

    def _replay_create(self, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("id") or "")
        if not task_id or task_id in self._tasks:
            return
        try:
            num = int(task_id.rsplit("_", 1)[-1])
            self._counter = max(self._counter, num)
        except ValueError:
            pass
        task = TaskItem(
            id=task_id,
            title=str(payload.get("title") or task_id),
            body=str(payload.get("body") or ""),
            status=str(payload.get("status") or "todo"),
            assignee=str(payload.get("assignee") or ""),
            priority=int(payload.get("priority") or 2),
            progress=int(payload.get("progress") or 0),
            created_by=str(payload.get("createdBy") or "parent"),
            created_at=(payload.get("createdAt") or 0) / 1000.0 or time.time(),
            updated_at=(payload.get("updatedAt") or 0) / 1000.0 or time.time(),
            started_at=(payload.get("startedAt") / 1000.0) if payload.get("startedAt") else None,
            completed_at=(payload.get("completedAt") / 1000.0) if payload.get("completedAt") else None,
            summary=str(payload.get("summary") or ""),
            result=str(payload.get("result") or ""),
            artifacts=list(payload.get("artifacts") or []),
            blocked_by=list(payload.get("blockedBy") or []),
            blocks=list(payload.get("blocks") or []),
        )
        self._tasks[task_id] = task

    def _replay_update(self, ev: dict[str, Any]) -> None:
        task = self._tasks.get(str(ev.get("id") or ""))
        if task is None:
            return
        fields = ev.get("fields") or {}
        # HAND-MAINTAINED: when `update()` learns a new field, add a matching
        # restore branch here AND make sure `update()` writes the new value
        # into `journal_fields`. Otherwise the field will silently revert to
        # its create-time value after a bridge restart.
        if "status" in fields:
            task.status = str(fields["status"])
        if "assignee" in fields:
            task.assignee = str(fields["assignee"])
        if "progress" in fields:
            task.progress = int(fields["progress"])
        if "body" in fields:
            task.body = str(fields["body"])
        if "summary" in fields:
            task.summary = str(fields["summary"])
        if "result" in fields:
            task.result = str(fields["result"])
        if "artifacts" in fields and isinstance(fields["artifacts"], list):
            task.artifacts = [str(a) for a in fields["artifacts"]]
        started = fields.get("started_at_ms")
        if started:
            task.started_at = float(started) / 1000.0
        completed = fields.get("completed_at_ms")
        if completed:
            task.completed_at = float(completed) / 1000.0
        # `task.events` history is intentionally not journaled. The events
        # list rebuilds via subsequent live mutations; immediately after
        # restart `show` returns the task with an empty history list,
        # which is the smallest reasonable cost of cheap append-only
        # persistence.
        task.updated_at = (ev.get("ts") or 0) / 1000.0 or time.time()

    def _replay_link(self, ev: dict[str, Any]) -> None:
        blocker = self._tasks.get(str(ev.get("blocker") or ""))
        dependent = self._tasks.get(str(ev.get("dependent") or ""))
        if blocker is None or dependent is None:
            return
        if dependent.id not in blocker.blocks:
            blocker.blocks.append(dependent.id)
        if blocker.id not in dependent.blocked_by:
            dependent.blocked_by.append(blocker.id)

    def _replay_unlink(self, ev: dict[str, Any]) -> None:
        blocker = self._tasks.get(str(ev.get("blocker") or ""))
        dependent = self._tasks.get(str(ev.get("dependent") or ""))
        if blocker is None or dependent is None:
            return
        if dependent.id in blocker.blocks:
            blocker.blocks.remove(dependent.id)
        if blocker.id in dependent.blocked_by:
            dependent.blocked_by.remove(blocker.id)


TaskEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


class TaskBoardTool:
    """Tool wrapper around a session-local task ledger.

    Universal planning primitive available in every coordination mode.
    The parent always receives this tool. Workers receive it when they
    were explicitly assigned a task_id at spawn time — they can then
    heartbeat, complete, and block their own task directly.

    See `bridge/task_journal.py` for persistence semantics.
    """

    def __init__(
        self,
        board: SessionTaskBoard,
        *,
        actor_id: str,
        actor_label: str,
        emit_event: TaskEventCb | None = None,
        parent_session_id: str = "",
        # Session-wide monotonic tool-call counter, supplied as a
        # zero-arg callable so the tool always reads the current value
        # at the moment of each task action rather than capturing it
        # at construction time. Used by the stale-task reminder to
        # determine "has the agent made other progress since this
        # task was last touched?" None means we're in a degraded mode
        # (test fixture, sub-agent that doesn't have the wiring) —
        # task.last_touched_tool_index stays at 0 and the reminder
        # never fires for that session, which is fine.
        get_tool_call_index: Callable[[], int] | None = None,
    ) -> None:
        self._board = board
        self._actor_id = actor_id
        self._actor_label = actor_label
        self._emit_event = emit_event
        self._parent_session_id = parent_session_id
        self._get_tool_call_index = get_tool_call_index

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="tasks",
            summary="Plan and track multi-step work — a personal task ledger visible to the operator",
            tier=ToolTier.HOT,
            description="""Plan and track multi-step work through a session-local task ledger. The operator sees the list live in the activity rail; it's how they know what you intend to do and how far along you are.

## When to use
- The request has 3+ distinct steps (planning, implementation, verification)
- The user provided multiple things to do (numbered or comma-separated list)
- Synthesis-heavy work — reading 3+ findings, writing a multi-section deliverable, comparing options, producing structured output
- Long-running operations where the operator benefits from progress signals
- After receiving new instructions mid-session — capture them as tasks before they get forgotten
- Before starting work — flip the task to `active` so the operator sees what you're on right now
- After completing — flip to `done` and add any follow-ups discovered while working

## When NOT to use
- Single trivial action ("read this file and tell me what it says")
- Pure conversation or explanation
- Anything completable in fewer than 3 small steps
- The whole request is one tool call

## Status lifecycle
    todo  →  active  →  done
                ↓         ↑
             blocked  ──── (re-claim after unblock via update status=active)
                ↓
            cancelled  (work no longer relevant)

Mark a task `active` BEFORE you start it — that's what shows the operator you're working on it. Multiple tasks can be `active` simultaneously when work is genuinely parallel (e.g. waiting on a sub-agent while drafting the next section).

## Never mark `done` if
- Tests are failing
- Implementation is partial
- You hit unresolved errors
- You couldn't find required files or dependencies
- The acceptance criteria in `body` aren't satisfied

Use `block` (with a `note` explaining why) for "stuck and need input." Use `cancel` for "no longer relevant."

## Dependencies
Pass `add_blocked_by=["task_001"]` on an `update` action to gate task X on task Y. Or `add_blocks=["task_002"]` to declare that X must finish before Y starts (symmetric — sets both sides). Cycles are rejected automatically. A task with any non-terminal blocker is reported with `isBlocked=true` in list/show output; skip those when claiming next work.

## Heartbeats
For work that takes minutes, call `action=heartbeat` with optional `progress` (0-100) and `note` so the operator sees you're still active. Heartbeats are cheap; use them.

## Handoff payload on completion
`complete` accepts `summary` (one-line outcome), `result` (longer detail), and `artifacts` (file paths or refs produced). The operator sees these in the rail — it's how they know what your work produced.

## Sub-agent integration
When spawning a worker with `sub_agent`, pass `task_id` so the worker inherits the task and updates it as it works. The parent doesn't need to babysit. Workers without an explicit `task_id` don't get the `tasks` tool — the parent updates the task itself when the worker returns.

## Action summary
- `list` — see all tasks (ordered: todo → active → blocked → done → cancelled, then priority, then created)
- `show task_id=…` — full task with event history + deps
- `create title=… body=… priority=… assignee=…` — new task; starts in `todo`
- `claim task_id=…` — flip to `active`, set assignee, optional starting progress
- `update task_id=… status=… progress=… body=… add_blocked_by=[…] add_blocks=[…] remove_blocked_by=[…] remove_blocks=[…]` — generic modification
- `heartbeat task_id=… progress=… note=…` — liveness ping for long-running work
- `complete task_id=… summary=… result=… artifacts=[…]` — flip to `done`
- `block task_id=… note=…` (note required) — flip to `blocked`, record reason
- `cancel task_id=…` — flip to `cancelled`""",
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
                    "status": {
                        "type": "string",
                        "enum": list(TASK_STATUSES),
                        "description": "Explicit status override (rarely needed — prefer claim/complete/block/cancel)",
                    },
                    "add_blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids that this task should wait on. Symmetric with the other side's blocks.",
                    },
                    "add_blocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids that should wait on this task. Symmetric with the other side's blocked_by.",
                    },
                    "remove_blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids to drop from this task's blocked_by list.",
                    },
                    "remove_blocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids to drop from this task's blocks list.",
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
        # Stamp the per-task touched-counter AFTER the action completes,
        # using the live tool-call counter. The same counter snapshot
        # is shared by every task the action touched, so a multi-link
        # update is treated as one bump (correct — the agent did one
        # mental action even though it mutated multiple records).
        self._stamp_touched(payload)
        await self._emit(action, payload)
        return ToolResult(
            call_id=call_id,
            content=self._format_payload(action, payload),
            is_error=False,
        )

    def _stamp_touched(self, payload: dict[str, Any]) -> None:
        """Record the current session-wide tool-call counter on every
        task referenced by this action's payload. The stale-task
        reminder uses this to compare each task's last_touched index
        against the live counter — "agent has done N other tool calls
        since this task was last touched"."""
        if self._get_tool_call_index is None:
            return
        try:
            idx = int(self._get_tool_call_index())
        except Exception:  # noqa: BLE001
            return
        # `payload` may carry one task (under `task`) or many (`tasks`
        # for list, `linked`/`unlinked` ids for graph mutations). Grab
        # whatever ids exist + stamp the matching in-memory records.
        ids: set[str] = set()
        single = payload.get("task")
        if isinstance(single, dict) and single.get("id"):
            ids.add(str(single["id"]))
        many = payload.get("tasks")
        if isinstance(many, list):
            for t in many:
                if isinstance(t, dict) and t.get("id"):
                    ids.add(str(t["id"]))
        for extra_key in ("linked", "unlinked", "affected"):
            extra = payload.get(extra_key)
            if isinstance(extra, list):
                for item in extra:
                    if isinstance(item, str):
                        ids.add(item)
        for tid in ids:
            t = self._board._tasks.get(tid)  # noqa: SLF001
            if t is not None:
                t.last_touched_tool_index = idx

    async def _execute_action(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        actor = f"{self._actor_label} ({self._actor_id})"
        if action == "list":
            tasks = [
                self._task_view(task)
                for task in await self._board.list()
            ]
            return {"action": action, "tasks": tasks, "count": len(tasks)}

        if action == "create":
            task = await self._board.create(
                title=str(arguments.get("title") or "").strip(),
                body=str(arguments.get("body") or "").strip(),
                assignee=str(arguments.get("assignee") or "").strip(),
                priority=int(arguments.get("priority") or 2),
                actor=actor,
            )
            return {"action": action, "task": self._task_view(task)}

        if action == "show":
            task = await self._require_task(arguments)
            return {"action": action, "task": self._task_view(task, history=True)}

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
            return {"action": action, "task": self._task_view(updated) if updated else None}

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
            affected = await self._apply_dependency_edits(task.id, arguments, actor)
            view = self._task_view(updated) if updated else None
            payload: dict[str, Any] = {"action": action, "task": view}
            if affected:
                payload["linked"] = affected.get("linked", [])
                payload["unlinked"] = affected.get("unlinked", [])
            return payload

        raise ValueError(f"unknown action {action!r}")

    async def _apply_dependency_edits(
        self,
        task_id: str,
        arguments: dict[str, Any],
        actor: str,
    ) -> dict[str, list[str]]:
        """Apply add_blocked_by / add_blocks / remove_blocked_by /
        remove_blocks edits on the given task. Returns the ids that
        were actually changed (success only) so the action result can
        surface them to the model + the renderer."""
        linked: list[str] = []
        unlinked: list[str] = []

        def _ids(key: str) -> list[str]:
            raw = arguments.get(key) or []
            if not isinstance(raw, list):
                return []
            return [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]

        for blocker_id in _ids("add_blocked_by"):
            _, status = await self._board.link(
                blocker_id=blocker_id,
                dependent_id=task_id,
                actor=actor,
            )
            if status == "linked":
                linked.append(blocker_id)

        for dependent_id in _ids("add_blocks"):
            _, status = await self._board.link(
                blocker_id=task_id,
                dependent_id=dependent_id,
                actor=actor,
            )
            if status == "linked":
                linked.append(dependent_id)

        for blocker_id in _ids("remove_blocked_by"):
            _, status = await self._board.unlink(
                blocker_id=blocker_id,
                dependent_id=task_id,
                actor=actor,
            )
            if status == "unlinked":
                unlinked.append(blocker_id)

        for dependent_id in _ids("remove_blocks"):
            _, status = await self._board.unlink(
                blocker_id=task_id,
                dependent_id=dependent_id,
                actor=actor,
            )
            if status == "unlinked":
                unlinked.append(dependent_id)

        return {"linked": linked, "unlinked": unlinked}

    def _task_view(self, task: TaskItem | None, *, history: bool = False) -> dict[str, Any] | None:
        """Augment to_dict() with derived `isBlocked` + a short
        recent-events tail for show actions. Keeps the renderer's
        responsibility limited — the bridge owns the truth about
        whether a task is currently waiting on dependencies."""
        if task is None:
            return None
        view = task.to_dict(include_history=history)
        view["isBlocked"] = self._board.is_blocked(task)
        return view

    async def _require_task(self, arguments: dict[str, Any]) -> TaskItem:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        task = await self._board.get(task_id)
        if task is None:
            raise ValueError(f"task {task_id!r} not found")
        return task

    def _format_payload(self, action: str, payload: dict[str, Any]) -> str:
        """Compact human-readable result for the model. No trailing JSON
        dump — the renderer reads task state from `system_event`s (see
        `collectTaskCards` on the renderer side), not from this tool
        result, so a redundant JSON payload here is pure token tax."""
        lines: list[str] = []

        if action == "list":
            tasks = payload.get("tasks") or []
            count = payload.get("count", len(tasks))
            if count == 0:
                lines.append("No tasks on the ledger yet.")
            else:
                lines.append(f"{count} task(s):")
                for t in tasks:
                    lines.append(_format_task_line(t))
        elif action in {"create", "show", "claim", "update", "heartbeat", "complete", "block", "cancel"}:
            task = payload.get("task")
            if isinstance(task, dict):
                verb = {
                    "create": "Created",
                    "show": "Task",
                    "claim": "Claimed",
                    "update": "Updated",
                    "heartbeat": "Heartbeat for",
                    "complete": "Completed",
                    "block": "Blocked",
                    "cancel": "Cancelled",
                }.get(action, action.title())
                lines.append(f"{verb} {task.get('id', '')}.")
                lines.append(_format_task_line(task))
                if task.get("body"):
                    lines.append(f"  body: {task['body']}")
                if task.get("summary"):
                    lines.append(f"  summary: {task['summary']}")
                if task.get("result"):
                    lines.append(f"  result: {task['result']}")
                if task.get("artifacts"):
                    lines.append(f"  artifacts: {', '.join(task['artifacts'])}")
                if task.get("blockedBy"):
                    lines.append(f"  blocked by: {', '.join(task['blockedBy'])}")
                if task.get("blocks"):
                    lines.append(f"  blocks: {', '.join(task['blocks'])}")
                # `show` is the one action that asks for full history. We
                # include it inline (last 8 events, oldest→newest) so the
                # agent doesn't need to parse JSON to follow the timeline.
                if action == "show":
                    history = task.get("events") or []
                    if history:
                        lines.append("  history:")
                        for ev in history[-8:]:
                            ts = ev.get("timestamp")
                            actor = ev.get("actor", "")
                            kind = ev.get("kind", "")
                            message = (ev.get("message") or "").strip()
                            lines.append(f"    [{kind}] {actor} · {message}")
                        if len(history) > 8:
                            lines.append(f"    (... {len(history) - 8} earlier event(s))")
            if payload.get("linked"):
                lines.append(f"  newly linked: {', '.join(payload['linked'])}")
            if payload.get("unlinked"):
                lines.append(f"  removed links: {', '.join(payload['unlinked'])}")
        else:
            lines.append(f"action: {action}")

        return "\n".join(lines)

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


def _format_task_line(t: dict[str, Any]) -> str:
    """One-line task summary used in list output. Format chosen so a
    50-task board reads in under a screen and the most important
    fields (status, priority, blocked-ness) are scannable left-edge."""
    tid = t.get("id", "?")
    status = t.get("status", "?")
    title = (t.get("title") or "").strip()
    priority = t.get("priority")
    assignee = (t.get("assignee") or "").strip()
    progress = t.get("progress") or 0
    blocked = t.get("isBlocked") or False

    bits: list[str] = [f"#{tid}"]
    bits.append(f"[{status}]")
    if blocked:
        bits.append("(blocked)")
    bits.append(title or "(no title)")
    meta: list[str] = []
    if priority is not None:
        meta.append(f"p{priority}")
    if assignee:
        meta.append(assignee)
    if status == "active" and progress:
        meta.append(f"{progress}%")
    if meta:
        bits.append(f"· {' · '.join(meta)}")
    return " ".join(bits)
