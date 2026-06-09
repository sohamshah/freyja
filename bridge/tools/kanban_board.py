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
    "review",
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
# `review` is in-flight from the operator's perspective — the judge subagent
# is reading the artifacts and deciding pass/fail. Terminal statuses are the
# ones a card stops moving from on its own.
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
    # `done` and `done_unverified` are both allowed from triage/ready/blocked
    # because the parent can legitimately seal a card via the `complete`
    # action without ever spinning up a sub-agent. Whether complete writes
    # `done` or `done_unverified` depends on the card's `requires_verification`
    # flag. Worker writes are constrained separately by the worker-tool
    # slim-down (Move E), not by this table.
    "triage": {"ready", "done", "done_unverified", "cancelled"},
    "ready": {"running", "done", "done_unverified", "cancelled", "blocked"},
    "running": {
        "done_unverified",
        "done",  # parent agents that don't use the verifier skip straight here
        "review",  # default-on judge review path (Move R)
        "blocked",
        "crashed",
        "timed_out",
        # `failed` is reachable when the circuit breaker rewrites a
        # crashed/timed_out transition past threshold (Move F). Also
        # available as an operator emergency override.
        "failed",
        "cancelled",
    },
    "done_unverified": {"done", "running", "cancelled", "review"},
    "review": {
        # Judge verdict pass → sealed done.
        "done",
        # Verdict fail + iter<5 → worker rewoken, card back in flight.
        "running",
        # Rework-from-scratch path: judge verdict says the approach
        # itself was wrong (not just the execution), so the card goes
        # back to the ready queue for re-planning before another
        # worker picks it up. Without this, the agent can only retry
        # with the same worker mid-flight (``running``) or hard-block
        # — there's no way to surface "this needs to be redone from
        # scratch" without the operator manually moving the card.
        "ready",
        # 5th failed verdict → blocked, operator territory.
        "blocked",
        # Escape hatch.
        "cancelled",
    },
    "blocked": {"running", "ready", "done", "done_unverified", "review", "cancelled"},
    "crashed": {"ready", "running", "failed", "cancelled", "review"},
    "timed_out": {"ready", "running", "failed", "cancelled", "review"},
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
# Caps used by `_build_worker_context` to keep the rendered ground-
# truth block tail-able in a single LLM context window. Each cap has a
# corresponding "[truncated …]" marker emitted when content exceeds it
# so the worker can decide to fetch more deliberately.
_CTX_MAX_BODY_BYTES = 8000
_CTX_MAX_FIELD_BYTES = 2000
_CTX_MAX_COMMENTS = 12
_CTX_MAX_COMMENT_BYTES = 800


def _ctx_clip(text: str, cap: int) -> str:
    """Clip `text` to `cap` characters, appending a visible truncation
    marker when content was cut. The marker is part of the contract —
    the worker sees `… [truncated, N chars omitted]` and knows it's
    working off a subset, so it can `show_history` for more if needed."""
    if not text:
        return ""
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + f"\n… [truncated, {len(text) - cap + 1} chars omitted]"


def _build_worker_context(
    task: Any,
    parent_context: list[dict[str, Any]],
    recent_by_assignee: list[dict[str, Any]],
) -> str:
    """Render a worker's ground-truth markdown block for a card.

    Sections, all conditional on content:
      1. Header — id / title / assignee / status / iteration / artifacts
      2. Body — the task spec, capped at _CTX_MAX_BODY_BYTES
      3. Parent task results — for each dependency parent, the most
         recent summary + artifact list (1-level only)
      4. Recent work by @assignee — implicit role continuity
      5. Comments — capped at _CTX_MAX_COMMENTS, oldest collapsed

    Mirrors the Hermes `build_worker_context` pattern. The worker calls
    `kanban` action=show on its card and the `workerContext` field can
    be treated as ground truth — header / parents / history all
    pre-resolved so the worker doesn't need follow-up tool calls just
    to orient.
    """
    lines: list[str] = []

    # 1. Header
    lines.append(f"# Kanban card `{task.id}`: {task.title}")
    lines.append("")
    lines.append(f"Assignee:   {task.assignee or '(unassigned)'}")
    lines.append(f"Status:     {task.status}")
    if getattr(task, "review_iteration", 0):
        lines.append(f"Review iter: {task.review_iteration} / 5")
    if task.priority is not None and task.priority != 2:
        lines.append(f"Priority:   {task.priority}")
    if task.artifacts:
        lines.append(f"Artifacts:  {len(task.artifacts)} file(s)")
        for path in task.artifacts[:10]:
            lines.append(f"  · {path}")
        if len(task.artifacts) > 10:
            lines.append(
                f"  · … [truncated, {len(task.artifacts) - 10} more not shown]"
            )

    # 2. Body
    if task.body:
        lines.append("")
        lines.append("## Body")
        lines.append(_ctx_clip(task.body, _CTX_MAX_BODY_BYTES))

    # 3. Parent results
    if parent_context:
        lines.append("")
        lines.append("## Parent card handoffs")
        for parent in parent_context:
            pid = parent.get("id", "?")
            title = parent.get("title", "")
            status = parent.get("status", "")
            summary = parent.get("summary") or ""
            artifacts = parent.get("artifacts") or []
            lines.append(f"### `{pid}` — {title} [{status}]")
            if summary:
                lines.append(_ctx_clip(summary, _CTX_MAX_FIELD_BYTES))
            if artifacts:
                lines.append("")
                lines.append("Artifacts:")
                for path in artifacts[:5]:
                    lines.append(f"  · {path}")
                if len(artifacts) > 5:
                    lines.append(
                        f"  · … [truncated, {len(artifacts) - 5} more not shown]"
                    )

    # 4. Recent work by this assignee
    if recent_by_assignee:
        lines.append("")
        lines.append(f"## Recent work by @{task.assignee}")
        for entry in recent_by_assignee:
            eid = entry.get("id", "?")
            etitle = entry.get("title", "")
            esum = entry.get("summary") or ""
            line = f"- `{eid}` — {etitle}"
            if esum:
                line += f": {esum}"
            lines.append(line)

    # 5. Comments (tail)
    comments = list(getattr(task, "comments", []) or [])
    if comments:
        lines.append("")
        if len(comments) > _CTX_MAX_COMMENTS:
            omitted = len(comments) - _CTX_MAX_COMMENTS
            lines.append(
                f"## Comment thread (showing {_CTX_MAX_COMMENTS} of {len(comments)};"
                f" {omitted} earlier omitted — use `show_history` for full log)"
            )
            comments = comments[-_CTX_MAX_COMMENTS:]
        else:
            lines.append(f"## Comment thread ({len(comments)})")
        for c in comments:
            # Backtick-strip author to defeat any future author-forgery
            # surface; matches the Hermes hardening pattern.
            author = (getattr(c, "author", None) or "").replace("`", "")
            body = _ctx_clip(getattr(c, "body", "") or "", _CTX_MAX_COMMENT_BYTES)
            lines.append("")
            lines.append(f"comment from `{author}`:")
            lines.append(body)

    return "\n".join(lines)


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
    # Opt-in verification (Move C). When True, the worker's `complete`
    # call (or the bridge's automatic terminal-marking) routes to
    # `done_unverified` so the dispatcher's verifier lane picks it up
    # for a sign-off pass. When False — the default — `complete` seals
    # the card directly to `done`. Cheap/ambiguous work (quick web
    # lookup, image gen, exploratory bash) doesn't need verification;
    # the parent flips this on for cards whose definition_of_done is
    # checkable enough to be worth the extra spawn.
    requires_verification: bool = False
    # Circuit breaker. Crashes and timeouts increment this; a successful
    # run resets to zero. Past `FAILURE_THRESHOLD`, `update()` rewrites
    # the next crashed/timed_out transition to `failed` so the
    # dispatcher stops respawning a flapping card.
    consecutive_failures: int = 0
    # Default-on judge-review machinery (Move R). Each time the worker
    # terminates, the card lands in `review`, the dispatcher spawns or
    # wakes the sticky judge subagent, and the verdict routes the card
    # forward. `review_iteration` increments on each entry to review;
    # at MAX_REVIEW_ITERATIONS (5) a fail verdict routes to `blocked`
    # instead of rewaking the worker.
    review_iteration: int = 0
    # Sticky session ids. Set once on first dispatch / first judge spawn
    # and reused for every subsequent rework / re-review cycle so the
    # worker has continuity of context AND the judge remembers its prior
    # verdicts when re-evaluating.
    worker_session_id: str = ""
    judge_session_id: str = ""
    # What state the worker exited at — done / failed / cancelled /
    # crashed / timed_out. The judge factors this in: a worker that
    # crashed mid-flight may still have produced enough to satisfy the
    # card, but the judge should know it didn't terminate cleanly.
    worker_terminal_state: str = ""

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
            "requiresVerification": self.requires_verification,
            "reviewIteration": self.review_iteration,
            "workerSessionId": self.worker_session_id,
            "judgeSessionId": self.judge_session_id,
            "workerTerminalState": self.worker_terminal_state,
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

    def __init__(self, journal: Any | None = None) -> None:
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
        # Append-only JSONL journal for cross-restart persistence (Move G).
        # When set, every board mutation writes a single line. `None`
        # keeps the board purely in-memory — used by tests and by code
        # paths that don't need durability.
        self._journal = journal
        # While replaying from a journal we don't want to re-emit the
        # exact same events back to the journal. Toggled by `replay_from`.
        self._replaying = False

    def _journal_append(self, event: dict[str, Any]) -> None:
        if self._journal is None or self._replaying:
            return
        try:
            self._journal.append(event)
        except Exception:  # noqa: BLE001
            # The journal is best-effort — never let a disk hiccup
            # corrupt the in-memory board.
            pass

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
        requires_verification: bool = False,
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
                requires_verification=bool(requires_verification),
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
            self._journal_append({"kind": "create", "task": task.to_dict()})
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
        requires_verification: bool | None = None,
        review_iteration: int | None = None,
        worker_session_id: str | None = None,
        judge_session_id: str | None = None,
        worker_terminal_state: str | None = None,
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
                    # Include the valid next-states in the error so the
                    # agent doesn't burn a turn on trial-and-error.
                    # Without the hint, the agent in session-mq67ogk0
                    # tried review→ready (rejected), then considered
                    # review→blocked, then settled on review→running —
                    # all while doing extra digest calls to map the
                    # state machine. Surfacing the valid options inline
                    # collapses that into one turn.
                    valid = sorted(ALLOWED_TRANSITIONS.get(task.status, set()))
                    valid_str = (
                        ", ".join(repr(s) for s in valid)
                        if valid
                        else "(terminal — no transitions allowed)"
                    )
                    raise ValueError(
                        f"invalid transition {task.status!r} -> {next_status!r}. "
                        f"From {task.status!r} you can move to: {{{valid_str}}}"
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
                # Reset ``started_at`` whenever the card transitions
                # INTO ``running`` from a non-running state, not just on
                # the first time ever. Previously only the first dispatch
                # stamped it, so a card going review→running for rework
                # (or blocked→running on unstick) reported cumulative
                # wall time since the original spawn — the trace showed
                # a "running for 1h55m" phantom for a card whose new
                # worker had only been alive for seconds.
                if next_status == "running" and task.status != "running":
                    task.started_at = now
                    changes["startedAt"] = int(now * 1000)
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
            if requires_verification is not None:
                next_flag = bool(requires_verification)
                if next_flag != task.requires_verification:
                    task.requires_verification = next_flag
                    changes["requiresVerification"] = next_flag
            if review_iteration is not None:
                task.review_iteration = int(review_iteration)
                changes["reviewIteration"] = task.review_iteration
            if worker_session_id is not None:
                task.worker_session_id = worker_session_id.strip()
                changes["workerSessionId"] = task.worker_session_id
            if judge_session_id is not None:
                task.judge_session_id = judge_session_id.strip()
                changes["judgeSessionId"] = task.judge_session_id
            if worker_terminal_state is not None:
                task.worker_terminal_state = worker_terminal_state.strip()
                changes["workerTerminalState"] = task.worker_terminal_state
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
            # Snapshot the full task state on update. Replay applies the
            # snapshot directly, which is simpler than reconstructing per-
            # field deltas and identical in outcome.
            self._journal_append(
                {
                    "kind": "update",
                    "id": task.id,
                    "actor": actor,
                    "task": task.to_dict(),
                }
            )
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
            self._journal_append(
                {
                    "kind": "link",
                    "parent": parent_id,
                    "child": child_id,
                    "actor": actor,
                }
            )
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
                # Skip mission_root — see the comment in the list
                # action handler. Agent-facing surfaces don't include
                # the synthetic conversation container.
                if task.metadata.get("role") == "mission_root":
                    continue
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

    async def recent_work_by_assignee(
        self,
        assignee: str,
        *,
        exclude_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return recent completed cards by the same assignee on OTHER
        cards. Used in `show`'s `worker_context` to give the worker
        implicit role continuity ("I'm the explore profile and my recent
        runs were research-flavored"). Sorted newest-first by
        `completed_at`."""
        if not assignee:
            return []
        async with self._lock:
            done = [
                t
                for t in self._tasks.values()
                if t.status == "done"
                and t.assignee == assignee
                and (exclude_id is None or t.id != exclude_id)
            ]
            done.sort(key=lambda t: t.completed_at or t.updated_at or 0, reverse=True)
            return [
                {
                    "id": t.id,
                    "title": t.title,
                    "completedAt": int((t.completed_at or t.updated_at or 0) * 1000),
                    "summary": (t.summary or "")[:200],
                }
                for t in done[:limit]
            ]

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
            self._journal_append(
                {"kind": "unblock", "id": task.id, "actor": actor}
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

    def replay_events(self, events: list[dict[str, Any]]) -> None:
        """Rebuild board state by replaying a journal log. Idempotent —
        applies each event to the in-memory state without firing the
        journal write-back. Should be called on a fresh board before
        any tool calls land."""
        self._replaying = True
        try:
            for event in events:
                kind = event.get("kind")
                if kind == "create":
                    self._apply_create_snapshot(event.get("task") or {})
                elif kind == "update":
                    snapshot = event.get("task") or {}
                    if snapshot.get("id") in self._tasks:
                        self._apply_update_snapshot(snapshot)
                elif kind == "link":
                    self._apply_link(
                        str(event.get("parent") or ""),
                        str(event.get("child") or ""),
                    )
                elif kind == "unblock":
                    cid = str(event.get("id") or "")
                    task = self._tasks.get(cid)
                    if task is not None:
                        task.status = "ready"
        finally:
            self._replaying = False

    def _apply_create_snapshot(self, snapshot: dict[str, Any]) -> None:
        cid = snapshot.get("id")
        if not cid or cid in self._tasks:
            return
        task = self._task_from_snapshot(snapshot)
        self._tasks[task.id] = task
        # Counter must track the highest assigned id so the next create
        # generates a fresh, non-clashing id.
        suffix = task.id.rsplit("_", 1)[-1]
        if suffix.isdigit():
            self._counter = max(self._counter, int(suffix))
        if task.metadata.get("role") == "mission_root" and self._mission_root_id is None:
            self._mission_root_id = task.id

    def _apply_update_snapshot(self, snapshot: dict[str, Any]) -> None:
        existing = self._tasks.get(snapshot.get("id") or "")
        if existing is None:
            return
        merged = self._task_from_snapshot(snapshot)
        existing.title = merged.title
        existing.body = merged.body
        existing.assignee = merged.assignee
        existing.status = merged.status
        existing.priority = merged.priority
        existing.parents = list(merged.parents)
        existing.children = list(merged.children)
        existing.created_by = merged.created_by
        existing.created_at = merged.created_at
        existing.updated_at = merged.updated_at
        existing.started_at = merged.started_at
        existing.completed_at = merged.completed_at
        existing.result = merged.result
        existing.summary = merged.summary
        existing.artifacts = list(merged.artifacts)
        existing.metadata = dict(merged.metadata)
        existing.comments = list(merged.comments)
        existing.events = list(merged.events)
        existing.comment_count = merged.comment_count
        existing.event_count = merged.event_count
        existing.consecutive_failures = merged.consecutive_failures
        existing.requires_verification = merged.requires_verification

    def _apply_link(self, parent_id: str, child_id: str) -> None:
        parent = self._tasks.get(parent_id)
        child = self._tasks.get(child_id)
        if parent is None or child is None:
            return
        if child_id not in parent.children:
            parent.children.append(child_id)
        if parent_id not in child.parents:
            child.parents.append(parent_id)

    def _task_from_snapshot(self, snapshot: dict[str, Any]) -> KanbanTask:
        # `to_dict()` emits timestamps in milliseconds and a few field
        # name changes (createdBy etc.). Translate back to the dataclass
        # shape. Unknown fields are dropped silently so a future schema
        # bump doesn't break replay.
        def _seconds(ms: Any) -> float | None:
            if ms is None:
                return None
            try:
                return float(ms) / 1000.0
            except (TypeError, ValueError):
                return None

        comments = [
            KanbanComment(
                author=c.get("author", ""),
                body=c.get("body", ""),
                timestamp=(_seconds(c.get("timestamp")) or time.time()),
            )
            for c in (snapshot.get("comments") or [])
        ]
        events = [
            KanbanEvent(
                kind=e.get("kind", "updated"),
                actor=e.get("actor", ""),
                message=e.get("message", ""),
                timestamp=(_seconds(e.get("timestamp")) or time.time()),
                details=e.get("details") or {},
            )
            for e in (snapshot.get("events") or [])
        ]
        return KanbanTask(
            id=str(snapshot.get("id") or ""),
            title=str(snapshot.get("title") or ""),
            body=str(snapshot.get("body") or ""),
            assignee=str(snapshot.get("assignee") or ""),
            status=str(snapshot.get("status") or "ready"),
            priority=int(snapshot.get("priority") or 2),
            parents=list(snapshot.get("parents") or []),
            children=list(snapshot.get("children") or []),
            created_by=str(snapshot.get("createdBy") or "parent"),
            created_at=(_seconds(snapshot.get("createdAt")) or time.time()),
            updated_at=(_seconds(snapshot.get("updatedAt")) or time.time()),
            started_at=_seconds(snapshot.get("startedAt")),
            completed_at=_seconds(snapshot.get("completedAt")),
            result=str(snapshot.get("result") or ""),
            summary=str(snapshot.get("summary") or ""),
            artifacts=list(snapshot.get("artifacts") or []),
            metadata=dict(snapshot.get("metadata") or {}),
            comments=comments,
            events=events,
            comment_count=int(snapshot.get("commentCount") or len(comments)),
            event_count=int(snapshot.get("eventCount") or len(events)),
            consecutive_failures=int(snapshot.get("consecutiveFailures") or 0),
            requires_verification=bool(snapshot.get("requiresVerification") or False),
            review_iteration=int(snapshot.get("reviewIteration") or 0),
            worker_session_id=str(snapshot.get("workerSessionId") or ""),
            judge_session_id=str(snapshot.get("judgeSessionId") or ""),
            worker_terminal_state=str(snapshot.get("workerTerminalState") or ""),
        )


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
        autopilot_state_provider: Callable[[], bool] | None = None,
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
        # Callback returning the parent session's current autopilot
        # state. The create action surfaces this in its result so the
        # agent can see when a newly-created card has no dispatcher
        # ready to pick it up — and tell the operator instead of
        # silently becoming the worker.
        self._autopilot_state_provider = autopilot_state_provider

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
                "to agent profiles, and leave durable handoffs.\n\n"
                "VERIFICATION is OPT-IN per card. Set "
                "`requires_verification=true` at create-time (or via "
                "update) on cards whose `definition_of_done` is checkable "
                "enough to be worth a second-pass review — typically code "
                "changes with tests, schema migrations, or anything where "
                "a wrong-but-plausible answer would cost more than the "
                "extra spawn. LEAVE IT FALSE (the default) for quick web "
                "lookups, image generation, exploratory bash, or tasks "
                "whose success criteria are ambiguous. The worker's "
                "`complete` call routes to `done_unverified` when the flag "
                "is true (verifier picks up automatically) and to `done` "
                "when it's false."
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
                    "requires_verification": {
                        "type": "boolean",
                        "description": (
                            "When True, the worker's `complete` action sends the card to "
                            "`done_unverified` for a verifier sign-off pass instead of sealing "
                            "directly to `done`. Set this on cards whose `definition_of_done` "
                            "is checkable enough to be worth the extra spawn cost (e.g. code "
                            "changes with tests). Leave False (default) for quick lookups, "
                            "image generation, ambiguous tasks, or anything cheap to redo."
                        ),
                    },
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
        else:
            # Parent / orchestrator mode. The board is for cross-agent
            # handoffs — the parent's job is routing, not executing. Block
            # the parent from acting as its own worker by gating
            # complete / block / heartbeat against the card's
            # `worker_session_id` claim. If the parent genuinely needs to
            # do a card itself, it should explicitly `claim` it first,
            # which sets `worker_session_id` to the parent's session and
            # makes the intent visible on the board.
            if action in {"complete", "block", "heartbeat"}:
                target = str(arguments.get("task_id") or "").strip()
                if target:
                    card = await self._board.get(target)
                    if card is None:
                        return ToolResult(
                            call_id=call_id,
                            content=f"Error: card {target!r} not found.",
                            is_error=True,
                        )
                    owner = (card.worker_session_id or "").strip()
                    if not owner:
                        return ToolResult(
                            call_id=call_id,
                            content=(
                                f"Error: card {target!r} has no worker — "
                                "the orchestrator does not execute cards "
                                "itself. Either let autopilot dispatch a "
                                "worker, or `claim` the card explicitly "
                                "before calling " + action + "."
                            ),
                            is_error=True,
                        )
                    if owner != self._actor_id:
                        return ToolResult(
                            call_id=call_id,
                            content=(
                                f"Error: card {target!r} is owned by "
                                f"session {owner!r}; refusing "
                                f"{action!r} from session "
                                f"{self._actor_id!r}."
                            ),
                            is_error=True,
                        )
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
            # Hide mission_root from the agent-facing list. It's a
            # synthetic container the bridge auto-creates for the
            # conversation's mission objective; it is not a unit of
            # work and surfacing it to the agent was leading to
            # rationalizations like "this work falls under the mission
            # root" that skipped real card creation. The renderer
            # still sees it for visualization purposes since the board
            # state isn't filtered at storage time.
            tasks = [
                task.to_dict(include_history=False)
                for task in await self._board.list()
                if task.metadata.get("role") != "mission_root"
            ]
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
                requires_verification=bool(arguments.get("requires_verification") or False),
            )
            payload: dict[str, Any] = {"action": action, "task": task.to_dict()}
            # Surface autopilot state so the agent can tell when a
            # freshly-created card has no dispatcher to claim it. If
            # autopilot is off, the operator needs to either flip it
            # on or explicitly claim the card — the agent should
            # surface that to them, not silently do the work itself.
            if self._autopilot_state_provider is not None:
                try:
                    enabled = bool(self._autopilot_state_provider())
                except Exception:  # noqa: BLE001
                    enabled = False
                payload["autopilotEnabled"] = enabled
                if not enabled:
                    payload["autopilotNote"] = (
                        "Autopilot is OFF — this card will sit in `ready` "
                        "until a worker claims it manually or the operator "
                        "enables autopilot. Surface this to the operator "
                        "rather than executing the card yourself."
                    )
            return payload

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
            # `worker_context` — a pre-formatted markdown block the worker
            # can treat as ground truth for its first orient-pass. Header
            # + body + parent handoffs + recent work by this assignee +
            # truncated comment thread, with explicit truncation markers
            # so the worker knows when to fetch more. Modeled after the
            # Hermes `build_worker_context` pattern.
            recent_by_assignee = await self._board.recent_work_by_assignee(
                task.assignee, exclude_id=task.id, limit=5,
            ) if task.assignee else []
            payload["workerContext"] = _build_worker_context(
                task, parent_context, recent_by_assignee
            )
            return {"action": action, "task": payload}

        if action == "show_history":
            task = await self._require_task(arguments)
            history = await self._board.history(task.id)
            return {"action": action, "history": history}

        if action == "claim":
            task = await self._require_task(arguments)
            # Record the claiming session on the card so the ownership
            # check on subsequent complete / block / heartbeat calls can
            # match the right caller. Without this, claim was a no-op
            # for the ownership gate and the orchestrator could mutate
            # any card just by calling claim then complete.
            updated = await self._board.update(
                task.id,
                actor=actor,
                status="running",
                assignee=str(arguments.get("assignee") or self._actor_label),
                comment=str(arguments.get("comment") or "Claimed card").strip(),
                worker_session_id=self._actor_id,
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
            # Move R bookkeeping for `complete` — track review iteration
            # bump and sticky worker id when the worker's own call lands
            # the card in review. Set inside the elif below and threaded
            # into the update() call so we don't have to re-fetch.
            review_iteration_override: int | None = None
            worker_session_id_override: str | None = None
            worker_terminal_state_override: str | None = None
            if action == "comment":
                if not comment:
                    raise ValueError("comment is required")
            elif action == "complete":
                # Move R: every worker completion routes to `review`, not
                # straight to `done`. The dispatcher's review lane spawns
                # (or wakes the sticky) judge-deep against the card, the
                # judge renders a verdict via structured output, and the
                # outcome decides done / rework / blocked.
                #
                # Iteration bump here is critical — without it,
                # `_mark_kanban_terminal` (which fires after the worker
                # subagent terminates) would ALSO bump iteration, and we'd
                # double-count. Doing it once at the explicit complete call
                # makes the path deterministic.
                #
                # `created_cards` verification: two checks.
                #   1. Each id must exist on the board (catches phantom
                #      references).
                #   2. Each card's `created_by` must include this caller's
                #      session id (catches the "worker claims credit for
                #      a sibling's card" failure class). Orchestrators
                #      bypass check 2 since they legitimately route many
                #      cards' creation.
                created_cards = [
                    str(card_id) for card_id in arguments.get("created_cards") or []
                ]
                missing: list[str] = []
                stolen: list[str] = []
                for card_id in created_cards:
                    card_obj = await self._board.get(card_id)
                    if card_obj is None:
                        missing.append(card_id)
                        continue
                    if self._owned_task_id:
                        creator = (card_obj.created_by or "")
                        # `created_by` looks like "<label> (<session_id>)".
                        # Worker is identified by its session id, so we
                        # substring-match. False positives are extremely
                        # unlikely because session ids are random.
                        if self._actor_id and self._actor_id not in creator:
                            stolen.append(card_id)
                if missing:
                    raise ValueError(
                        f"created_cards not found: {', '.join(missing)}"
                    )
                if stolen:
                    raise ValueError(
                        f"created_cards not created by this worker: "
                        f"{', '.join(stolen)}. Only list cards your own "
                        f"session created during this run."
                    )
                status = "review"
                review_iteration_override = task.review_iteration + 1
                worker_session_id_override = task.worker_session_id or self._actor_id
                worker_terminal_state_override = "done"
            elif action == "block":
                status = "blocked"
                if not comment:
                    raise ValueError("comment is required when blocking a card")
                # Sticky-block bookkeeping: capture the worker's session
                # id and "terminal state" so the renderer can show why
                # the card is blocked + the dispatcher's sweep can tell
                # this came from the worker, not from the iteration cap.
                worker_session_id_override = task.worker_session_id or self._actor_id
                worker_terminal_state_override = "blocked-by-worker"
            elif action == "heartbeat":
                comment = comment or "Heartbeat"
                metadata = {**metadata, "heartbeatAt": int(time.time() * 1000)}
            requires_verification = arguments.get("requires_verification")
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
                requires_verification=(
                    bool(requires_verification) if requires_verification is not None else None
                ),
                review_iteration=review_iteration_override,
                worker_session_id=worker_session_id_override,
                worker_terminal_state=worker_terminal_state_override,
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
