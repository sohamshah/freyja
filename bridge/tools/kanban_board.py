"""Lightweight session-local Kanban coordination tools."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


# Status vocabulary. Forward path: triage → ready → running → done.
#   triage           — card exists but body isn't yet a specifiable plan.
#                       The specifier profile expands and promotes to ready.
#   ready            — fully specified and dispatchable.
#   running          — claimed by a worker, in flight.
#   done_unverified  — worker completed; awaiting verifier sign-off.
#   done             — verified complete.
#   blocked          — paused, waiting on user input or external decision.
#   crashed          — worker exited unclean; retry-eligible.
#   timed_out        — exceeded its budget; retry-eligible.
#   failed           — circuit-breaker tripped; dispatcher locked out.
#   cancelled        — explicitly stopped by operator.
STATUSES = (
    "triage",
    "ready",
    "running",
    "done_unverified",
    "done",
    "blocked",
    "crashed",
    "timed_out",
    "failed",
    "cancelled",
    # Legacy alias: existing callers still write `todo` to mean "exists but
    # not yet dispatchable." The new vocab calls this `triage`. Both are
    # accepted to keep backward compatibility; `todo` normalizes to `triage`
    # at write time. Listed last so it sorts after the canonical buckets.
    "todo",
)
TERMINAL_STATUSES = {"done", "cancelled", "failed"}
# Terminal states a parent can reach where its `triage`/`todo`/`ready`
# children's dependencies can never be satisfied. Children of these parents
# get `kanban_orphan` events rather than auto-promotion or auto-cancellation.
TERMINAL_FAILURE_STATUSES = {"cancelled", "failed"}
# Statuses that count as "still in the worker's hands" — claimed but not yet
# back in the operator's queue.
ACTIVE_STATUSES = {"running"}
# End states from which retry is possible (via reclaim or operator action).
RETRY_ELIGIBLE_STATUSES = {"crashed", "timed_out", "blocked"}

# Allowed transitions. Anything outside this table is rejected at write
# time so accidental backflow (e.g. `done → running`) can't happen under
# autonomy. Keep this table mechanically reviewable: the LEFT side is the
# *current* status; the RIGHT side is the set of valid next-statuses.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    # `done` is allowed from triage/ready/blocked because the parent can
    # legitimately seal a card via the `complete` action without ever
    # spinning up a sub-agent. Worker writes are constrained separately
    # by the worker-tool slim-down (Move E), not by this table.
    "triage": {"ready", "done", "cancelled"},
    "ready": {"running", "done", "cancelled", "blocked"},
    "running": {
        "done_unverified",
        "done",  # parent agents that don't use the verifier skip straight here
        "blocked",
        "crashed",
        "timed_out",
        # `failed` is reachable when the circuit breaker rewrites a
        # crashed/timed_out transition past threshold (Move F). Also
        # available as an operator emergency override.
        "failed",
        "cancelled",
    },
    "done_unverified": {"done", "running", "cancelled"},
    "blocked": {"running", "ready", "done", "cancelled"},
    "crashed": {"ready", "running", "failed", "cancelled"},
    "timed_out": {"ready", "running", "failed", "cancelled"},
    # Terminal states are absorbing — no further moves.
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}


def normalize_status(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    # Legacy: `todo` is the old name for `triage`. Accept either, persist as triage.
    if raw == "todo":
        return "triage"
    if raw not in STATUSES:
        raise ValueError(f"invalid status {value!r}")
    return raw


def is_valid_transition(current: str, next_status: str) -> bool:
    """Self-transitions (writing the same status again) are always valid —
    we use them as no-ops to update assignee/comment without status change."""
    if current == next_status:
        return True
    return next_status in ALLOWED_TRANSITIONS.get(current, set())
# Rolling-window cap on each card's `events` and `comments` lists. The full
# history is still queryable via the `show_history` tool action; this just
# bounds what rides along on every `to_dict()` payload so long missions don't
# blow up the per-event JSON the dashboard ingests.
DEFAULT_HISTORY_TAIL = 30
# Circuit-breaker threshold. The Nth crashed/timed_out transition flips
# the card to `failed` instead, locking the dispatcher out. Set low —
# in autonomy a flapping card is much cheaper to surface to the parent
# than to silently re-spawn forever.
FAILURE_THRESHOLD = 3


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
    # `comments` and `events` are append-only working lists trimmed to the
    # last `DEFAULT_HISTORY_TAIL` entries. `comment_count` and `event_count`
    # track totals so the dashboard can render "+ N older entries" affordances
    # and an explicit `show_history` action can return the full backing log.
    comments: list[KanbanComment] = field(default_factory=list)
    events: list[KanbanEvent] = field(default_factory=list)
    comment_count: int = 0
    event_count: int = 0
    # Circuit breaker. Crashes and timeouts increment this; a successful
    # run resets to zero. Past `FAILURE_THRESHOLD`, `update()` rewrites
    # the next crashed/timed_out transition to `failed` so the
    # dispatcher stops respawning a flapping card.
    consecutive_failures: int = 0

    def append_event(self, event: KanbanEvent) -> None:
        self.events.append(event)
        self.event_count += 1
        if len(self.events) > DEFAULT_HISTORY_TAIL:
            del self.events[: len(self.events) - DEFAULT_HISTORY_TAIL]

    def append_comment(self, comment: KanbanComment) -> None:
        self.comments.append(comment)
        self.comment_count += 1
        if len(self.comments) > DEFAULT_HISTORY_TAIL:
            del self.comments[: len(self.comments) - DEFAULT_HISTORY_TAIL]

    @property
    def spec(self) -> dict[str, Any]:
        """Structured spec fields populated by the specifier agent
        (Move D). Stored under `metadata` so the existing update path
        flows through unchanged; this helper surfaces them at the top
        level of `to_dict()` for the worker's convenience."""
        keys = ("definition_of_done", "references", "verify_with", "token_budget")
        return {key: self.metadata[key] for key in keys if key in self.metadata}

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
            "commentCount": self.comment_count,
            "eventCount": self.event_count,
            "consecutiveFailures": self.consecutive_failures,
        }
        spec = self.spec
        if spec:
            payload["spec"] = spec
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
        # Mission root card id. Set when the first card with
        # `metadata.role == "mission_root"` is created (Move B). Subsequent
        # cards with no explicit parents adopt the root as their parent so
        # the dashboard can render the mission top-down, but the root is
        # treated as gating-transparent in `_parents_done` — children of
        # the root start in `ready`, not `triage`.
        self._mission_root_id: str | None = None

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
            clean_meta = dict(metadata or {})
            is_root = clean_meta.get("role") == "mission_root"
            clean_parents = [pid for pid in parents or [] if pid in self._tasks]
            # Auto-adopt the mission root as parent when caller passed no
            # parents and this isn't itself the root. Explicit `parents=[]`
            # callers that want detachment can pass a sentinel via metadata
            # (`detached_from_root=True`) — we don't expect that often.
            if (
                not clean_parents
                and not is_root
                and self._mission_root_id is not None
                and not clean_meta.get("detached_from_root")
            ):
                clean_parents = [self._mission_root_id]
            self._counter += 1
            task_id = f"card_{self._counter:03d}"
            status = "ready"
            blocking_parents = [
                pid
                for pid in clean_parents
                if pid != self._mission_root_id
                and self._tasks[pid].status != "done"
            ]
            if blocking_parents:
                # Non-root parents still in flight — card sits in triage
                # until they finish. (Move D will widen `triage` to also
                # cover cards whose body hasn't been specified yet.)
                status = "triage"
            # Mission root itself starts running: the design doc treats it
            # as the active mission container the parent owns from turn 1.
            if is_root:
                status = "running"
            task = KanbanTask(
                id=task_id,
                title=title.strip() or task_id,
                body=body.strip(),
                assignee=assignee.strip(),
                status=status,
                # `priority or 2` was a footgun: priority=0 (most urgent)
                # silently fell back to 2 (default). Distinguish "not given"
                # from "given as 0" explicitly.
                priority=max(0, min(int(priority if priority is not None else 2), 5)),
                parents=clean_parents,
                created_by=actor,
                metadata=clean_meta,
            )
            if is_root:
                task.started_at = time.time()
            task.append_event(
                KanbanEvent(
                    "created",
                    actor,
                    f"Created {task.id}",
                    details={"parents": clean_parents},
                )
            )
            self._tasks[task_id] = task
            if is_root and self._mission_root_id is None:
                self._mission_root_id = task_id
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
            tripped_breaker = False
            if status:
                next_status = normalize_status(status)
                if not is_valid_transition(task.status, next_status):
                    raise ValueError(
                        f"invalid transition {task.status!r} -> {next_status!r}"
                    )
                # Circuit breaker: a crashed/timed_out transition past the
                # threshold is rewritten to `failed` so the dispatcher
                # locks the card out. Successful completion resets the
                # counter so a card recovering from a transient blip
                # doesn't carry a permanent failure budget.
                if next_status in {"crashed", "timed_out"}:
                    task.consecutive_failures += 1
                    changes["consecutiveFailures"] = task.consecutive_failures
                    if task.consecutive_failures >= FAILURE_THRESHOLD:
                        next_status = "failed"
                        tripped_breaker = True
                        changes["breakerTripped"] = True
                elif next_status == "done":
                    if task.consecutive_failures:
                        changes["consecutiveFailures"] = 0
                    task.consecutive_failures = 0
                if next_status == "running" and task.started_at is None:
                    task.started_at = now
                if next_status in TERMINAL_STATUSES and task.completed_at is None:
                    task.completed_at = now
                task.status = next_status
                changes["status"] = next_status
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
                task.append_comment(KanbanComment(actor, comment.strip()))
                changes["comment"] = True
            task.updated_at = now
            task.append_event(
                KanbanEvent("updated", actor, f"Updated {task.id}", details=changes)
            )
            if task.status == "done":
                self._promote_unblocked_children(task.id, actor)
            elif task.status in TERMINAL_FAILURE_STATUSES:
                self._mark_children_orphaned(task.id, actor)
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
            child.status = "ready" if self._parents_done(child) else "triage"
            parent.updated_at = child.updated_at = time.time()
            parent.append_event(KanbanEvent("linked", actor, f"Linked to {child_id}"))
            child.append_event(KanbanEvent("linked", actor, f"Depends on {parent_id}"))
            return child, "linked"

    async def exists(self, task_id: str) -> bool:
        async with self._lock:
            return task_id in self._tasks

    async def digest(self, *, max_per_bucket: int = 8) -> dict[str, Any]:
        """Compact triage view of the whole board.

        Buckets surface the cards the parent agent most likely needs to act
        on next: in-flight work with the assignee and age; recently-unblocked
        cards ready to dispatch; stuck (blocked) cards needing human input;
        and cards still waiting on dependencies. Caller can drill into any
        id via `show`. Intentionally returns only summary fields and id —
        ride-along payload is small enough for repeated calls."""
        async with self._lock:
            now = time.time()
            running: list[dict[str, Any]] = []
            ready: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []
            waiting: list[dict[str, Any]] = []
            for task in self._tasks.values():
                if task.status == "running":
                    running.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "assignee": task.assignee,
                            "priority": task.priority,
                            "ageSeconds": int(now - (task.started_at or task.updated_at)),
                            "lastUpdateSeconds": int(now - task.updated_at),
                        }
                    )
                elif task.status == "ready":
                    ready.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "assignee": task.assignee,
                            "priority": task.priority,
                            "promotedAtSeconds": int(now - task.updated_at),
                        }
                    )
                elif task.status == "blocked":
                    blocked.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "assignee": task.assignee,
                            "lastUpdateSeconds": int(now - task.updated_at),
                            "lastComment": (
                                task.comments[-1].body if task.comments else ""
                            ),
                        }
                    )
                elif task.status == "triage":
                    unresolved = [
                        pid
                        for pid in task.parents
                        if (parent := self._tasks.get(pid)) is None
                        or parent.status != "done"
                    ]
                    waiting.append(
                        {
                            "id": task.id,
                            "title": task.title,
                            "priority": task.priority,
                            "unresolvedParents": unresolved,
                        }
                    )

            running.sort(key=lambda r: r["lastUpdateSeconds"], reverse=True)
            ready.sort(key=lambda r: (r["priority"], r["promotedAtSeconds"]))
            blocked.sort(key=lambda r: r["lastUpdateSeconds"], reverse=True)
            waiting.sort(key=lambda r: r["priority"])
            return {
                "missionRoot": self._mission_root_id,
                "running": running[:max_per_bucket],
                "ready": ready[:max_per_bucket],
                "blocked": blocked[:max_per_bucket],
                "waiting": waiting[:max_per_bucket],
                "totals": {
                    "running": len(running),
                    "ready": len(ready),
                    "blocked": len(blocked),
                    "waiting": len(waiting),
                    "cards": len(self._tasks),
                },
            }

    async def parent_context(self, task_id: str) -> dict[str, Any] | None:
        """Compact upstream snapshot for one parent card. Returned inline
        from `show` so a worker reading its assignment can see what each
        of its parents produced without a chain of follow-up calls."""
        async with self._lock:
            parent = self._tasks.get(task_id)
            if parent is None:
                return None
            return {
                "id": parent.id,
                "title": parent.title,
                "status": parent.status,
                "assignee": parent.assignee,
                "summary": parent.summary,
                "artifacts": list(parent.artifacts),
            }

    async def history(self, task_id: str) -> dict[str, Any] | None:
        """Return the *trimmed-but-present* tail of comments/events along with
        the total counters. The board keeps only the last DEFAULT_HISTORY_TAIL
        entries in memory; that's the same tail `show` returns. This exists
        as a distinct action so the agent can ask for history explicitly
        without paying the cost on every `show` call."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return {
                "id": task.id,
                "title": task.title,
                "commentCount": task.comment_count,
                "eventCount": task.event_count,
                "tailSize": DEFAULT_HISTORY_TAIL,
                "comments": [comment.to_dict() for comment in task.comments],
                "events": [event.to_dict() for event in task.events],
            }

    def _parents_done(self, task: KanbanTask) -> bool:
        # Mission root counts as "always satisfied" so it can stay `running`
        # as the mission container without blocking its children from
        # entering `ready`.
        return all(
            parent_id == self._mission_root_id
            or self._tasks[parent_id].status == "done"
            for parent_id in task.parents
        )

    def _promote_unblocked_children(self, parent_id: str, actor: str) -> None:
        parent = self._tasks[parent_id]
        for child_id in parent.children:
            child = self._tasks.get(child_id)
            if child and child.status == "triage" and self._parents_done(child):
                child.status = "ready"
                child.updated_at = time.time()
                child.append_event(
                    KanbanEvent("promoted", actor, "All dependencies complete")
                )

    def _mark_children_orphaned(self, parent_id: str, actor: str) -> None:
        """Surface an `orphaned` event on children of a terminal-failure
        parent. We deliberately do not auto-cancel the children — the
        parent agent gets to decide via the `unblock` action whether to
        proceed without that dependency, cancel the child, or wait."""
        parent = self._tasks[parent_id]
        for child_id in parent.children:
            child = self._tasks.get(child_id)
            if not child or child.status not in {"triage", "ready"}:
                continue
            child.append_event(
                KanbanEvent(
                    "orphaned",
                    actor,
                    f"Parent {parent_id} is {parent.status}; use unblock to proceed",
                    details={"parent": parent_id, "parent_status": parent.status},
                )
            )
            child.updated_at = time.time()

    async def unblock(self, task_id: str, *, actor: str) -> tuple[KanbanTask | None, str]:
        """Operator-driven override that flips an orphaned `triage` child
        to `ready`. Only legal when at least one of the card's parents is
        in a terminal-failure state and all *other* parents are done."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None, "card not found"
            if task.status != "triage":
                return task, f"card is {task.status!r}; unblock only works on triage cards"
            if not task.parents:
                return task, "card has no parents to unblock"
            has_failure_parent = any(
                (parent := self._tasks.get(pid)) is not None
                and parent.status in TERMINAL_FAILURE_STATUSES
                for pid in task.parents
            )
            if not has_failure_parent:
                return task, "no parent has terminated in failure; nothing to unblock"
            non_failure_parents_done = all(
                (parent := self._tasks.get(pid)) is None
                or parent.status == "done"
                or parent.status in TERMINAL_FAILURE_STATUSES
                for pid in task.parents
            )
            if not non_failure_parents_done:
                return task, "other parents are still pending; cannot unblock yet"
            task.status = "ready"
            task.updated_at = time.time()
            task.append_event(
                KanbanEvent(
                    "unblocked",
                    actor,
                    "Promoted by operator override despite failed parent(s)",
                )
            )
            return task, "unblocked"

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

# Worker-mode surface (Move E). When a child agent's KanbanTool is built
# with `owned_task_id` set, these are the only actions it can use. The
# missing pieces are deliberate:
#   - `create` / `link` — workers don't restructure the board mid-task.
#   - `claim` — the dispatcher already assigned the card; CAS races and
#     drift from accidental re-claims are gone by construction.
#   - `unblock` — operator-only override; workers don't get to short-
#     circuit a failed parent dependency.
WORKER_ALLOWED_ACTIONS: tuple[str, ...] = (
    "list",
    "digest",
    "show",
    "show_history",
    "update",
    "comment",
    "complete",
    "block",
    "heartbeat",
)
# Mutation actions a worker is allowed to take but only on its OWNED card.
# Read-only actions (list/digest/show/show_history) stay unrestricted so
# the worker can still see how its work fits into the mission.
WORKER_OWNED_ACTIONS: frozenset[str] = frozenset(
    {"update", "comment", "complete", "block", "heartbeat"}
)


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
        owned_task_id: str | None = None,
    ) -> None:
        self._board = board
        self._actor_id = actor_id
        self._actor_label = actor_label
        self._emit_event = emit_event
        self._parent_session_id = parent_session_id
        # When set, the tool is operating in worker mode (Move E): the
        # action surface is narrowed and any mutation against a card id
        # other than `owned_task_id` is rejected.
        self._owned_task_id = (owned_task_id or "").strip() or None

    @property
    def definition(self) -> ToolDefinition:
        full_enum = [
            "list",
            "digest",
            "create",
            "show",
            "show_history",
            "claim",
            "update",
            "comment",
            "complete",
            "block",
            "heartbeat",
            "link",
            "unblock",
        ]
        # Worker-mode tools advertise a narrower enum so the model never
        # even sees the parent-only actions (Move E).
        action_enum = (
            list(WORKER_ALLOWED_ACTIONS) if self._owned_task_id else full_enum
        )
        if self._owned_task_id:
            description = (
                f"Inspect and update your assigned Kanban card "
                f"`{self._owned_task_id}`. Use `show` first to see the "
                "ask + parent context, then `heartbeat`/`comment` while "
                "you work, and `complete` or `block` when you finish. "
                "Read-only `list`/`digest`/`show_history` work on any "
                "card; mutations are restricted to your assigned card."
            )
        else:
            description = (
                "Create, inspect, and update a session-local Kanban "
                "board. Use this in kanban coordination mode to decompose "
                "a mission into cards, link dependency gates, assign work "
                "to agent profiles, and leave durable handoffs."
            )
        return ToolDefinition(
            name="kanban",
            summary="Coordinate multi-agent work through a shared Kanban board",
            tier=ToolTier.HOT,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": action_enum,
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
        # Worker-mode guardrails (Move E). Reject parent-only actions and
        # ownership violations *before* dispatching so we don't half-execute.
        if self._owned_task_id:
            if action not in WORKER_ALLOWED_ACTIONS:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Error: action {action!r} is not available to "
                        f"workers; assigned to card {self._owned_task_id!r}."
                    ),
                    is_error=True,
                )
            if action in WORKER_OWNED_ACTIONS:
                target = str(arguments.get("task_id") or "").strip()
                if target and target != self._owned_task_id:
                    return ToolResult(
                        call_id=call_id,
                        content=(
                            f"Error: worker is assigned to card "
                            f"{self._owned_task_id!r}; refusing mutation "
                            f"against {target!r}."
                        ),
                        is_error=True,
                    )
                # Empty task_id collapses to the assigned card so the worker
                # doesn't need to repeat it on every call.
                if not target:
                    arguments = {**arguments, "task_id": self._owned_task_id}
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

        if action == "digest":
            return {"action": action, "digest": await self._board.digest()}

        if action == "create":
            task = await self._board.create(
                title=str(arguments.get("title") or "").strip(),
                body=str(arguments.get("body") or "").strip(),
                assignee=str(arguments.get("assignee") or "").strip(),
                parents=[str(pid) for pid in arguments.get("parents") or []],
                priority=int(arguments.get("priority")) if arguments.get("priority") is not None else 2,
                actor=actor,
                metadata=dict(arguments.get("metadata") or {}),
            )
            return {"action": action, "task": task.to_dict()}

        if action == "show":
            task = await self._require_task(arguments)
            # Inline each direct parent's summary, status, and artifacts so
            # the worker doesn't have to make a separate `show` call per
            # parent to get the upstream context it needs. One level deep
            # only — no transitive recursion, to keep the payload bounded.
            parent_context = [
                ctx
                for pid in task.parents
                if (ctx := await self._board.parent_context(pid)) is not None
            ]
            payload = task.to_dict()
            if parent_context:
                payload["parentContext"] = parent_context
            return {"action": action, "task": payload}

        if action == "show_history":
            task = await self._require_task(arguments)
            history = await self._board.history(task.id)
            return {"action": action, "history": history}

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

        if action == "unblock":
            task = await self._require_task(arguments)
            updated, message = await self._board.unblock(task.id, actor=actor)
            if updated is None:
                raise ValueError(message)
            if message != "unblocked":
                # Surface a soft refusal as an error so the agent sees why.
                raise ValueError(message)
            return {"action": action, "message": message, "task": updated.to_dict()}

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
