"""
Minimal sub-agent tool for the desktop bridge.

Spawns a child `AsyncAgentRunner` with a curated read-only tool set and either
blocks on its completion (foreground) or schedules it as a background task.
Replaces the CLI sub_agent_tool for the desktop use case —
no Rich console, no grouped tree rendering, just JSON events for the UI.

Callbacks let the Freyja bridge stream spawn / update / done events without
having to import this module's internals.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bridge.tools.agent_types import (
    AgentType,
    get_agent_type,
    load_agent_types,
    resolve_model_choice,
)
from bridge.tools.base import (
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolTier,
)
from bridge.tools.coordination import STRATEGY_BUS, STRATEGY_ISOLATED, STRATEGY_KANBAN
from bridge.tools.sub_agent_registry import (
    SubAgentRecord,
    SubAgentRegistry,
    SubAgentState,
)
from bridge.project_paths import project_output_dir, project_output_guidance
from engine.compaction import SummaryCompaction

logger = logging.getLogger(__name__)

# Tools sub-agents are NOT allowed to use. We strip only the recursion
# escapes and the user-directed memory writer; everything else (file edits,
# bash, web search, etc.) is inherited so sub-agents have parity with the
# parent. Permission-gated tools (bash) still run through the same handler.
DEFAULT_EXCLUDED_TOOLS = frozenset(
    {
        "sub_agent",  # no recursive nesting
        "subagents",  # no orchestration from within a sub-agent
        "record_user_preference",  # user prefs come from the user, not a sub
        "publish_finding",  # child-only: injected directly, not inherited
        "read_findings",  # child-only: injected directly, not inherited
        "kanban",  # child-only in board mode so the actor id is correct
        "tasks",  # child-only in task mode so the actor id is correct
    }
)

MAX_ACTIVE_SUBAGENTS = 30

SUB_AGENT_IDENTITY_HEADER = (
    "You are a focused sub-agent running on behalf of a parent agent.\n"
    "\n"
    "You have full read/write access to the workspace via the same tools "
    "the parent uses. Work independently on the task you've been given, "
    "use the tools to gather information and make changes, and return a "
    "tight structured summary of what you found and/or did.\n"
    "\n"
    "Do not spawn further sub-agents (`sub_agent` and `subagents` are "
    "intentionally excluded). Permission-gated tools (`bash`, etc.) honor "
    "the same approval policy as the parent.\n"
)


def _build_sub_agent_system_prompt(
    child_registry: ToolRegistry,
    *,
    parent_workspace: str,
    parent_session_id: str,
) -> str:
    """Inject the actual available tool list into the sub-agent prompt.

    Mirrors what the parent bridge does in `_BridgeSession.initialize` so
    the sub-agent knows what it can call.
    """
    from bridge.tools.coordination import current_datetime_block

    tool_lines = "\n".join(
        f"- `{name}` — {tool.definition.summary}"
        for name, tool in sorted(child_registry._tools.items())  # noqa: SLF001
    )
    return (
        f"{SUB_AGENT_IDENTITY_HEADER}\n"
        f"\n{current_datetime_block()}\n"
        f"\n{project_output_guidance(parent_session_id, parent_workspace)}\n"
        f"Available tools:\n{tool_lines}\n"
    )


SubAgentEventCb = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _emit_spawn_inbox_event(
    emit_event: SubAgentEventCb,
    *,
    parent_session_id: str,
    child_session_id: str,
    task: str,
) -> None:
    """Emit a synthetic inbox_event so the activity comm graph (and
    per-session transcript chip rail) sees the spawn-time task as a
    parent → child message.

    Spawns deliver `task` as the runner's initial user message — it
    never lands in the child's inbox, so the inbox_event channel that
    the comm visualization listens on stays silent for spawns. From the
    operator's perspective this hides half of the directional traffic
    (only post-spawn talk() replies are visible). Emitting this synthetic
    event closes the gap WITHOUT changing runtime delivery: nothing is
    pushed to record.inbox; the runner still consumes the task as
    before.

    `parent_session_id` must be set (otherwise we can't draw a sender
    lane). The deterministic message id `spawn:<child_id>` keeps
    aggregation/dedupe idempotent if a slice replays."""
    if not parent_session_id:
        return
    try:
        await _fire(
            emit_event,
            {
                "type": "inbox_event",
                "sessionId": child_session_id,
                "action": "enqueued",
                "message": {
                    "id": f"spawn:{child_session_id}",
                    "fromSession": parent_session_id,
                    "fromLabel": "parent",
                    "fromRole": "agent",
                    "content": task,
                    "force": False,
                    "replyTo": None,
                    "timestamp": int(time.time() * 1000),
                    "kind": "spawn",
                },
            },
        )
    except Exception:
        pass


def _attach_inbox_emitter(record: Any, _emit_event: SubAgentEventCb) -> None:
    """Wire the sub-agent record's SessionInbox.on_change to fire
    `inbox_event` events scoped to the record's session id. Mirrors
    what `_BridgeSession.__init__` does for root sessions.

    Without this, push/drain/drop on a SubAgentRecord's inbox is
    invisible to the renderer — no inline chips, no telemetry, no
    audit trail. Root-session inboxes have always emitted these
    events; sub-agent inboxes were silently dropped on the floor.

    Note: we bypass the async _spec.emit_event (which would force us
    to schedule a task from a sync callback) and use the top-level
    sync `emit()` from freyja_bridge. Same channel root sessions use.
    """
    if record is None or getattr(record, "inbox", None) is None:
        return

    sub_id = record.id

    def _fire_inbox(action: str, msg: Any) -> None:
        try:
            # Lazy import to dodge the circular sub_agent_tool ↔ bridge.
            from bridge.freyja_bridge import emit as _bridge_emit

            _bridge_emit({
                "type": "inbox_event",
                "sessionId": sub_id,
                "action": action,
                "message": msg.to_dict(),
            })
        except Exception:
            pass

    record.inbox.on_change = _fire_inbox


@dataclass
class SubAgentSpec:
    """Static configuration for spawning sub-agents."""

    parent_workspace: str
    parent_model: str
    build_provider: Callable[..., Any]
    """Provider factory: (model_id, thinking_effort?) -> ModelProvider."""
    parent_registry: ToolRegistry
    registry: SubAgentRegistry
    emit_event: SubAgentEventCb
    parent_reasoning_level: str = "auto"
    parent_session_id: str = ""
    max_iterations: int = 25
    child_tool_names: frozenset[str] | None = None
    # Optional wrapper that turns a plain ToolRegistry into a tracing
    # registry scoped to a given session id. The bridge passes
    # `_new_tracing_registry` so child tool calls emit tool_result events
    # with the child's sessionId.
    wrap_registry: Callable[[ToolRegistry, str], ToolRegistry] | None = None
    # Session-scoped message bus for inter-agent communication.
    message_bus: Any | None = None
    # Session-level coordination strategy.
    coordination_strategy: str = STRATEGY_BUS
    # Optional board used by kanban coordination mode.
    kanban_board: Any | None = None
    # Optional task ledger. Available in every coordination mode now —
    # in isolated, workers also get a bound TaskBoardTool; in other
    # modes, only the parent gets it (workers' updates flow through
    # the parent via the spawn lifecycle hook).
    task_board: Any | None = None
    # Reader for the session-wide tool-call counter, passed to bound
    # TaskBoardTool instances so the stale-task reminder can stamp
    # last_touched_tool_index correctly.
    task_tool_call_index_getter: Any | None = None
    # Session artifact manifest shared with parent and sibling agents.
    artifact_store: Any | None = None
    # Inter-agent messaging router. When set, each spawned child gets
    # talk + list_agent_sessions tools wired with a context bound to
    # the child's session id + parent id.
    talk_router: Any | None = None


class SubAgentTool:
    """Tool definition that launches a new child runner per invocation."""

    def __init__(self, spec: SubAgentSpec) -> None:
        self._spec = spec
        self._counter = 0

    @property
    def definition(self) -> ToolDefinition:
        type_names = sorted(load_agent_types(self._spec.parent_workspace).keys())
        return ToolDefinition(
            name="sub_agent",
            summary="Delegate a focused task to a specialized sub-agent",
            tier=ToolTier.HOT,
            description=f"""Spawn a sub-agent that independently works on a task.

Each agent type has a specialized model, thinking level, tool set, and
system prompt optimized for its role. Choose the type that fits the task.

Parameters:
- label: short human-friendly name shown in the UI
- task: the task/prompt given to the sub-agent
- agent_type: agent specialization ({', '.join(type_names)}). Defaults to general.
- kanban_task_id: optional board card id when the session is in kanban mode
- task_id: optional task ledger id when the session is in task mode
- mode: "foreground" blocks on the child (default); "background" returns
  immediately with an agent id that can be monitored with the `subagents`
  tool.""",
            parameters={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label shown in the UI (<40 chars)",
                    },
                    "task": {
                        "type": "string",
                        "description": "The task / prompt for the sub-agent",
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": type_names,
                        "description": "Agent specialization. Defaults to general.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["foreground", "background"],
                        "description": "Execution mode. Defaults to foreground.",
                    },
                    "kanban_task_id": {
                        "type": "string",
                        "description": (
                            "Optional Kanban card id this sub-agent should execute. "
                            "Only useful when the session coordination strategy is kanban."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Optional task ledger id this sub-agent should serve. The worker's "
                            "lifecycle drives status updates on this task — running on spawn, "
                            "complete with summary on success, blocked on failure. Available in "
                            "any coordination mode; in isolated mode the worker is also handed "
                            "the full `tasks` tool and instructed to focus on this id (it can "
                            "still read/mutate any task on the board)."
                        ),
                    },
                },
                "required": ["label", "task"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        label = (arguments.get("label") or "sub-agent").strip()[:60]
        task = (arguments.get("task") or "").strip()
        mode = arguments.get("mode") or "foreground"
        agent_type_name = arguments.get("agent_type") or "general"
        kanban_task_id = (arguments.get("kanban_task_id") or "").strip()
        task_id = (arguments.get("task_id") or "").strip()
        if mode not in ("foreground", "background"):
            mode = "foreground"

        if not task:
            return ToolResult(
                call_id=call_id,
                content="Error: `task` is required",
                is_error=True,
            )

        # Resolve agent type and model before creating the child session. This
        # avoids a dead/stuck sub-session when every candidate model is missing
        # the required provider configuration.
        agent_type = get_agent_type(agent_type_name, self._spec.parent_workspace)
        model_resolution = resolve_model_choice(agent_type, self._spec.parent_model)
        if not model_resolution.available:
            reasons = "; ".join(
                f"{model}: {reason}"
                for model, reason in model_resolution.unavailable
            )
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: no available model for `{agent_type.name}` "
                    f"profile ({reasons})"
                ),
                is_error=True,
            )
        child_model = model_resolution.model

        # Enforce cap on concurrent running sub-agents
        running = sum(
            1 for r in self._spec.registry.list_all() if r.is_running
        )
        if running >= MAX_ACTIVE_SUBAGENTS:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: too many active sub-agents ({running}/"
                    f"{MAX_ACTIVE_SUBAGENTS}). Wait for existing ones to finish."
                ),
                is_error=True,
            )

        self._counter += 1
        sub_id = f"sub_{int(time.time() * 1000):x}_{self._counter}"
        record = self._spec.registry.register(
            id=sub_id, label=label, task=task, mode=mode
        )
        record.agent_type_name = agent_type.name
        # Stash the resolved agent type and model on the record so
        # _run_child can use them without re-resolving.
        record.agent_type = agent_type  # type: ignore[attr-defined]
        record.child_model = child_model  # type: ignore[attr-defined]
        record.model_resolution = model_resolution  # type: ignore[attr-defined]
        record.coordination_strategy = self._spec.coordination_strategy  # type: ignore[attr-defined]
        record.parent_session_id = self._spec.parent_session_id or ""
        # Attach a fresh inbox so TalkRouter can deliver into this child.
        # Re-wake path (Phase 4) will pre-populate from the inbox sidecar
        # before spawn; for live spawns it starts empty.
        try:
            from bridge.inbox import SessionInbox
            record.inbox = SessionInbox(session_id=sub_id)
            _attach_inbox_emitter(record, self._spec.emit_event)
        except Exception:
            record.inbox = None
        if kanban_task_id:
            record.kanban_task_id = kanban_task_id  # type: ignore[attr-defined]
        # Task assignment — isolated mode auto-creates a task when the
        # caller didn't pass one (legacy behavior); other modes only
        # stamp `record.task_id` if the caller explicitly passed one
        # so the operator never sees surprise auto-created tasks they
        # didn't ask for.
        if self._spec.coordination_strategy == STRATEGY_ISOLATED:
            task_id = await self._prepare_task_assignment(
                task_id=task_id,
                label=label,
                task=task,
                agent_type=agent_type.name,
                record_id=sub_id,
            )
            if task_id:
                record.task_id = task_id  # type: ignore[attr-defined]
        elif task_id and self._spec.task_board is not None:
            # Verify the task exists + record assignee on it; emit a
            # task event so the renderer's task slice picks up the
            # assignment. No auto-create in non-isolated modes — the
            # operator's mental model is "the agent updates a task I
            # see in the rail," not "the agent invents tasks."
            try:
                item = await self._spec.task_board.update(
                    task_id,
                    actor="parent",
                    assignee=label,
                    note=f"Assigned to {label} ({agent_type.name})",
                )
                if item is not None:
                    await self._emit_task_state_event("update", item)
                    record.task_id = task_id  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug("failed to attach sub-agent to task", exc_info=True)

        type_tag = f" [{agent_type.name}]" if agent_type.name != "general" else ""
        # Legacy subagent_spawn for the existing inline card
        await _fire(
            self._spec.emit_event,
            {
                "type": "subagent_spawn",
                "record": _record_to_dict(record),
            },
        )
        # New session_spawned so the renderer treats this as a real session.
        # Parent/child linking is how we get first-class detach/attach.
        await _fire(
            self._spec.emit_event,
            {
                "type": "session_spawned",
                "sessionId": sub_id,
                "parentSessionId": self._spec.parent_session_id,
                "title": f"{label}{type_tag}",
                "model": child_model,
                "reasoningLevel": agent_type.thinking_effort,
                "modelPolicy": model_resolution.policy,
                "modelCandidates": list(model_resolution.candidates),
                "modelFallbackUsed": model_resolution.fallback_used,
                "task": task,
                "mode": mode,
                "agentType": agent_type.name,
                "coordinationStrategy": self._spec.coordination_strategy,
                "kanbanTaskId": kanban_task_id or None,
                "taskId": task_id or None,
                "workspace": self._spec.parent_workspace,
                "createdAt": int(time.time() * 1000),
            },
        )

        # Synthetic inbox_event so the activity comm graph + per-session
        # transcript reflect spawn-time intent. The task itself is fed
        # via runner.run() as the initial user message — this event
        # carries no real InboxMessage and is NOT pushed to the child's
        # inbox; it exists purely so direction-of-traffic visualization
        # captures the parent → child request that would otherwise be
        # invisible (the comm graph only sees post-spawn talk() calls).
        await _emit_spawn_inbox_event(
            self._spec.emit_event,
            parent_session_id=self._spec.parent_session_id,
            child_session_id=sub_id,
            task=task,
        )

        # Persist a profile_invocation JSONL row so the metrics dashboard's
        # Profiles view can count spawns per agent_type and link back to
        # the spawning task. We snapshot the task description (truncated)
        # rather than the live task — long-running profiles may have their
        # record edited later, and the dashboard wants the *original* prompt.
        try:
            from bridge.compaction_telemetry import append_telemetry

            task_preview = task.strip().replace("\n", " ")
            if len(task_preview) > 240:
                task_preview = task_preview[:237] + "..."
            append_telemetry({
                "type": "profile_invocation",
                "session_id": sub_id,
                "parent_session_id": self._spec.parent_session_id,
                "agent_type": agent_type.name,
                "model": child_model,
                "max_iterations": agent_type.max_iterations,
                "task_preview": task_preview,
            })
        except Exception:
            pass

        # Stash a spawn timestamp so we can compute the duration in the
        # paired profile_completion row when the run ends.
        record.spawned_at_ts = time.time()  # type: ignore[attr-defined]

        if mode == "foreground":
            return await self._run_foreground(call_id, record)

        # Background: schedule and return immediately
        asyncio.create_task(self._run_background(record))
        return ToolResult(
            call_id=call_id,
            content=(
                f"Sub-agent `{label}` queued in background "
                f"(id={sub_id}, type={agent_type.name}, model={child_model}). "
                "Use the `subagents` tool with action=wait/list/kill to manage it."
            ),
            is_error=False,
        )

    async def resume_archived(
        self,
        sidecar_data: dict[str, Any],
        *,
        woken_by: str = "agent",
    ) -> str | None:
        """Re-wake an archived sub-agent from its saved sidecar.

        Mirrors execute() but:
          - reuses the saved session id so the renderer's existing slice
            picks up where it left off
          - skips the registration counter (we keep the original id)
          - restores the saved engine transcript onto the new Session
          - hydrates the record's inbox from the inbox sidecar so any
            messages queued while the agent was asleep flow into the
            first iteration via the pre-iteration drain hook

        Returns the resumed session id, or None on failure.

        `woken_by` propagates to the session_spawned metadata so the
        renderer can show a "↻ rewoken by agent / operator" chip.
        """
        from bridge.tools.agent_types import (
            get_agent_type,
            resolve_model_choice,
        )

        sub_id = str(sidecar_data.get("sessionId") or "").strip()
        if not sub_id:
            return None
        if sub_id in {r.id for r in self._spec.registry.list_all() if r.is_running}:
            # Already running — message was already delivered via the
            # live inbox path; nothing to do.
            return sub_id

        agent_type_name = str(sidecar_data.get("agentType") or "general")
        label = str(sidecar_data.get("label") or sub_id)
        task = str(sidecar_data.get("task") or "")
        coord = str(sidecar_data.get("coordinationStrategy") or self._spec.coordination_strategy)
        transcript = sidecar_data.get("transcript") if isinstance(sidecar_data.get("transcript"), dict) else None

        agent_type = get_agent_type(agent_type_name, self._spec.parent_workspace)
        model_resolution = resolve_model_choice(agent_type, self._spec.parent_model)
        if not model_resolution.available:
            return None
        child_model = model_resolution.model

        # Concurrency cap still applies.
        running = sum(1 for r in self._spec.registry.list_all() if r.is_running)
        if running >= MAX_ACTIVE_SUBAGENTS:
            return None

        # Register using the ORIGINAL sub_id so the renderer reuses its
        # existing session slice — no new pane, the old one wakes back up.
        record = self._spec.registry.register(
            id=sub_id, label=label, task=task, mode="foreground"
        )
        record.agent_type_name = agent_type.name
        record.agent_type = agent_type  # type: ignore[attr-defined]
        record.child_model = child_model  # type: ignore[attr-defined]
        record.model_resolution = model_resolution  # type: ignore[attr-defined]
        record.coordination_strategy = coord  # type: ignore[attr-defined]
        record.parent_session_id = self._spec.parent_session_id or ""
        # Resume markers — _run_child checks these to switch into the
        # restored-transcript path instead of the fresh-spawn path.
        record.restored_transcript = transcript  # type: ignore[attr-defined]
        record.resume_mode = True  # type: ignore[attr-defined]
        record.woken_by = woken_by  # type: ignore[attr-defined]
        record.spawned_at_ts = time.time()  # type: ignore[attr-defined]

        # Attach an inbox + hydrate from the sidecar so messages queued
        # while the agent was archived flow in on the first iteration.
        try:
            from bridge.inbox import SessionInbox
            from bridge.transcript_persistence import load_inbox_state

            record.inbox = SessionInbox(session_id=sub_id)
            _attach_inbox_emitter(record, self._spec.emit_event)
            stored_inbox = load_inbox_state(sub_id)
            if isinstance(stored_inbox, dict):
                restored = SessionInbox.from_dict(stored_inbox)
                if restored and restored.unread:
                    for m in restored.unread:
                        record.inbox.push(m)
        except Exception:
            record.inbox = None

        # Fire session_spawned so the renderer wakes the existing slice.
        # task carries the resume marker as a system note so the agent
        # knows it's being re-engaged.
        await _fire(
            self._spec.emit_event,
            {
                "type": "session_spawned",
                "sessionId": sub_id,
                "parentSessionId": self._spec.parent_session_id,
                "title": f"{label} (resumed)",
                "model": child_model,
                "reasoningLevel": agent_type.thinking_effort,
                "task": task,
                "mode": "foreground",
                "agentType": agent_type.name,
                "coordinationStrategy": coord,
                "kanbanTaskId": getattr(record, "kanban_task_id", None),
                "taskId": getattr(record, "task_id", None),
                "workspace": self._spec.parent_workspace,
                "createdAt": int(time.time() * 1000),
                "wokenBy": woken_by,
                "resumed": True,
            },
        )

        # Background spawn — caller is the bridge router, not an agent
        # tool call, so there's no ToolResult to return.
        asyncio.create_task(self._run_background(record), name=f"resume-{sub_id}")
        return sub_id

    async def _prepare_task_assignment(
        self,
        *,
        task_id: str,
        label: str,
        task: str,
        agent_type: str,
        record_id: str,
    ) -> str:
        if self._spec.task_board is None:
            return task_id
        actor = "parent"
        try:
            if task_id:
                item = await self._spec.task_board.update(
                    task_id,
                    actor=actor,
                    assignee=label,
                    note=f"Assigned to {label} ({agent_type})",
                )
                if item is None:
                    return ""
                await self._emit_task_state_event("update", item)
                return task_id

            item = await self._spec.task_board.create(
                title=label,
                body=task,
                assignee=label,
                actor=actor,
            )
            await self._spec.task_board.update(
                item.id,
                actor=actor,
                assignee=label,
                note=f"Auto-created for sub-agent {record_id}",
            )
            await self._emit_task_state_event("create", item)
            return item.id
        except Exception:  # noqa: BLE001
            logger.debug("failed to prepare task assignment", exc_info=True)
            return task_id

    async def spawn_programmatically(
        self,
        *,
        agent_type_name: str,
        label: str,
        task: str,
        title: str | None = None,
        tool_filter: frozenset[str] | None = None,
        max_iterations_override: int | None = None,
        mode: str = "foreground",
    ) -> tuple[SubAgentRecord, str | None, Exception | None]:
        """Spawn a sub-agent from internal bridge code (not via a model
        tool call). Same machinery as `execute()` — same record, same
        runner, same telemetry, same inbox + cancel + force support —
        but returns raw response text rather than a ToolResult, and
        accepts dynamic per-call overrides for the tool surface and
        max_iterations cap.

        Used by `_judge_goal` / `_set_goal` to spawn judge-deep and
        judge-calibrator profiles. Replaces the previous ~200-line
        bespoke spawners that bypassed SubAgentTool entirely and
        therefore couldn't participate in the talk system.

        Returns (record, response_text, error_or_None). One of
        (response_text, error) is set; the record is always populated
        so the caller can read tokens/iterations regardless.
        """
        from bridge.compaction_telemetry import append_telemetry

        # ---- model resolution ----
        agent_type = get_agent_type(agent_type_name, self._spec.parent_workspace)
        try:
            model_resolution = resolve_model_choice(agent_type, self._spec.parent_model)
        except Exception as exc:  # noqa: BLE001
            return (
                self._spec.registry.register(
                    id=f"sub_failed_{int(time.time()*1000):x}",
                    label=label,
                    task=task,
                    mode=mode,
                ),
                None,
                exc,
            )
        if not model_resolution.available:
            reasons = "; ".join(
                f"{m}: {r}" for m, r in model_resolution.unavailable
            )
            return (
                self._spec.registry.register(
                    id=f"sub_unavail_{int(time.time()*1000):x}",
                    label=label,
                    task=task,
                    mode=mode,
                ),
                None,
                RuntimeError(
                    f"No available model for `{agent_type.name}`: {reasons}"
                ),
            )
        child_model = model_resolution.model

        # ---- concurrency cap ----
        running = sum(1 for r in self._spec.registry.list_all() if r.is_running)
        if running >= MAX_ACTIVE_SUBAGENTS:
            return (
                self._spec.registry.register(
                    id=f"sub_full_{int(time.time()*1000):x}",
                    label=label,
                    task=task,
                    mode=mode,
                ),
                None,
                RuntimeError(
                    f"Too many active sub-agents ({running}/{MAX_ACTIVE_SUBAGENTS})"
                ),
            )

        # ---- register record + attach metadata ----
        self._counter += 1
        sub_id = f"sub_{int(time.time() * 1000):x}_{self._counter}"
        record = self._spec.registry.register(
            id=sub_id, label=label, task=task, mode=mode
        )
        record.agent_type_name = agent_type.name
        record.agent_type = agent_type  # type: ignore[attr-defined]
        record.child_model = child_model  # type: ignore[attr-defined]
        record.model_resolution = model_resolution  # type: ignore[attr-defined]
        record.coordination_strategy = self._spec.coordination_strategy  # type: ignore[attr-defined]
        record.parent_session_id = self._spec.parent_session_id or ""
        record.spawned_at_ts = time.time()  # type: ignore[attr-defined]

        # Per-call overrides — _run_child reads these.
        if tool_filter is not None:
            record.tool_filter_override = tool_filter  # type: ignore[attr-defined]
        if max_iterations_override is not None:
            record.max_iterations_override = max_iterations_override  # type: ignore[attr-defined]

        # Inbox so this spawn participates in the talk system on day 1.
        try:
            from bridge.inbox import SessionInbox
            record.inbox = SessionInbox(session_id=sub_id)
            _attach_inbox_emitter(record, self._spec.emit_event)
        except Exception:
            record.inbox = None

        # ---- spawn / session_spawned events ----
        type_tag = f" [{agent_type.name}]" if agent_type.name != "general" else ""
        await _fire(
            self._spec.emit_event,
            {"type": "subagent_spawn", "record": _record_to_dict(record)},
        )
        await _fire(
            self._spec.emit_event,
            {
                "type": "session_spawned",
                "sessionId": sub_id,
                "parentSessionId": self._spec.parent_session_id,
                "title": title or f"{label}{type_tag}",
                "model": child_model,
                "reasoningLevel": agent_type.thinking_effort,
                "modelPolicy": model_resolution.policy,
                "modelCandidates": list(model_resolution.candidates),
                "modelFallbackUsed": model_resolution.fallback_used,
                "task": task,
                "mode": mode,
                "agentType": agent_type.name,
                "coordinationStrategy": self._spec.coordination_strategy,
                "workspace": self._spec.parent_workspace,
                "createdAt": int(time.time() * 1000),
            },
        )

        # Same synthetic spawn-inbox event as the public execute() path
        # — judge sub-agents, deep-search children, and other
        # programmatic spawns also issue a parent → child request that
        # should show up in the activity comm graph.
        await _emit_spawn_inbox_event(
            self._spec.emit_event,
            parent_session_id=self._spec.parent_session_id,
            child_session_id=sub_id,
            task=task,
        )

        # ---- profile_invocation telemetry ----
        try:
            task_preview = task.strip().replace("\n", " ")[:240]
            append_telemetry({
                "type": "profile_invocation",
                "session_id": sub_id,
                "parent_session_id": self._spec.parent_session_id,
                "agent_type": agent_type.name,
                "model": child_model,
                "max_iterations": max_iterations_override or agent_type.max_iterations,
                "task_preview": task_preview,
            })
        except Exception:
            pass

        # ---- run ----
        if mode != "foreground":
            asyncio.create_task(self._run_background(record), name=f"prog-{sub_id}")
            return record, None, None

        try:
            text = await self._run_child(record)
            return record, text, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("programmatic sub-agent %s failed", sub_id)
            self._spec.registry.mark_done(
                sub_id, f"Error: {exc}", SubAgentState.FAILED
            )
            await _emit_update(self._spec, record)
            return record, None, exc

    async def _run_foreground(
        self, call_id: str, record: SubAgentRecord
    ) -> ToolResult:
        try:
            summary = await self._run_child(record)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sub-agent %s failed", record.id)
            self._spec.registry.mark_done(
                record.id, f"Error: {exc}", SubAgentState.FAILED
            )
            await _emit_update(self._spec, record)
            return ToolResult(
                call_id=call_id,
                content=f"Sub-agent `{record.label}` failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            call_id=call_id,
            content=summary,
            is_error=False,
        )

    async def _run_background(self, record: SubAgentRecord) -> None:
        try:
            await self._run_child(record)
        except Exception as exc:  # noqa: BLE001
            logger.exception("background sub-agent %s failed", record.id)
            self._spec.registry.mark_done(
                record.id, f"Error: {exc}", SubAgentState.FAILED
            )
            await _emit_update(self._spec, record)

    async def _run_child(self, record: SubAgentRecord) -> str:
        """Run a real AsyncAgentRunner for this sub-agent and return its final text."""
        from engine.runner import AsyncAgentRunner
        from engine.session import Session

        agent_type: AgentType = getattr(
            record,
            "agent_type",
            get_agent_type("general", self._spec.parent_workspace),
        )
        child_model: str = getattr(record, "child_model", self._spec.parent_model)

        # Build a child registry, applying the agent type's tool filter.
        # `record.tool_filter_override` (set by spawn_programmatically)
        # wins over the agent_type's static tool_include so programmatic
        # callers (judge-deep with operator-tuned judge_tools) can pass
        # a dynamic per-call surface.
        parent_tools = self._spec.parent_registry._tools  # noqa: SLF001
        tool_filter_override = getattr(record, "tool_filter_override", None)

        if tool_filter_override is not None:
            allowed = frozenset(tool_filter_override) & frozenset(parent_tools.keys())
        elif agent_type.tool_include is not None:
            # Whitelist: only these tools (intersected with what parent has)
            allowed = agent_type.tool_include & frozenset(parent_tools.keys())
        elif self._spec.child_tool_names is not None:
            allowed = self._spec.child_tool_names
        else:
            allowed = frozenset(parent_tools.keys()) - DEFAULT_EXCLUDED_TOOLS

        # Apply additional exclusions from agent type
        if agent_type.tool_exclude:
            allowed = allowed - agent_type.tool_exclude
        # Always strip recursion escapes
        allowed = allowed - DEFAULT_EXCLUDED_TOOLS

        child_registry = ToolRegistry()
        for name in sorted(allowed):
            tool = parent_tools.get(name)
            if tool is not None:
                child_registry.register(tool)

        # Inter-agent messaging — talk + list_agent_sessions, bound to
        # this child's session id + parent. Always registered when the
        # spec carries a talk router (i.e. running under a bridge that
        # supports the messaging primitive).
        if self._spec.talk_router is not None:
            from bridge.tools.talk_tool import (
                ListAgentSessionsTool,
                TalkRouterContext,
                TalkTool,
            )
            talk_ctx = TalkRouterContext(
                caller_session_id=record.id,
                caller_label=record.label or record.id,
                caller_role="agent",
                parent_session_id=self._spec.parent_session_id or None,
            )
            child_registry.register(TalkTool(router=self._spec.talk_router, ctx=talk_ctx))
            child_registry.register(
                ListAgentSessionsTool(router=self._spec.talk_router, ctx=talk_ctx)
            )

        # Generative-UI widget tool — override the parent-bound instance
        # so widget_render events carry THIS sub-agent's session id (the
        # parent's copy would route widgets into the parent's slice
        # instead of the child's). widget_spec is stateless so the
        # inherited parent copy is fine.
        try:
            from bridge.tools.widget_tool import ShowWidgetTool

            child_registry.register(
                ShowWidgetTool(
                    session_id=record.id,
                    emit_event=self._spec.emit_event,
                )
            )
        except Exception:  # noqa: BLE001
            pass

        # Inject message bus tools BEFORE building the system prompt so
        # the tool list in the prompt includes publish_finding / read_findings.
        if (
            self._spec.coordination_strategy == STRATEGY_BUS
            and self._spec.message_bus is not None
        ):
            from bridge.tools.message_bus import PublishFindingTool, ReadFindingsTool
            child_registry.register(
                PublishFindingTool(
                    bus=self._spec.message_bus,
                    agent_id=record.id,
                    agent_label=record.label,
                    emit_event=self._spec.emit_event,
                    parent_session_id=self._spec.parent_session_id,
                )
            )
            child_registry.register(
                ReadFindingsTool(
                    bus=self._spec.message_bus,
                    agent_id=record.id,
                    agent_label=record.label,
                    emit_event=self._spec.emit_event,
                    parent_session_id=self._spec.parent_session_id,
                )
            )

        if (
            self._spec.coordination_strategy == STRATEGY_KANBAN
            and self._spec.kanban_board is not None
        ):
            from bridge.tools.kanban_board import KanbanTool
            child_registry.register(
                KanbanTool(
                    self._spec.kanban_board,
                    actor_id=record.id,
                    actor_label=record.label,
                    emit_event=self._spec.emit_event,
                    parent_session_id=self._spec.parent_session_id,
                    # Worker-mode constraint (Move E): the child sees a
                    # narrowed tool surface and can only mutate the card
                    # it was assigned to.
                    owned_task_id=getattr(record, "kanban_task_id", "") or None,
                )
            )

        if (
            self._spec.coordination_strategy == STRATEGY_ISOLATED
            and self._spec.task_board is not None
        ):
            from bridge.tools.task_board import TaskBoardTool
            child_registry.register(
                TaskBoardTool(
                    self._spec.task_board,
                    actor_id=record.id,
                    actor_label=record.label,
                    emit_event=self._spec.emit_event,
                    parent_session_id=self._spec.parent_session_id,
                    get_tool_call_index=self._spec.task_tool_call_index_getter,
                )
            )

        # Build system prompt: use agent type's specialized prompt if provided,
        # otherwise fall back to default sub-agent prompt with tool list.
        if agent_type.system_prompt:
            from bridge.tools.coordination import current_datetime_block
            tool_lines = "\n".join(
                f"- `{name}` — {tool.definition.summary}"
                for name, tool in sorted(child_registry._tools.items())  # noqa: SLF001
            )
            system_prompt = (
                f"{agent_type.system_prompt}\n"
                f"\n{current_datetime_block()}\n"
                f"\n{project_output_guidance(self._spec.parent_session_id, self._spec.parent_workspace)}\n"
                f"Available tools:\n{tool_lines}\n"
            )
            system_prompt += self._coordination_guidance(record)
        else:
            system_prompt = _build_sub_agent_system_prompt(
                child_registry,
                parent_workspace=self._spec.parent_workspace,
                parent_session_id=self._spec.parent_session_id,
            )
            system_prompt += self._coordination_guidance(record)

        system_prompt += (
            "\nProfile metadata:\n"
            f"- type: {agent_type.name}\n"
            f"- model: {child_model}\n"
            f"- thinking: {agent_type.thinking_effort}\n"
            f"- max iterations: {agent_type.max_iterations}\n"
            f"- source: {agent_type.source}\n"
        )

        # Append sibling context so this agent knows what others are
        # working on and can decide whether to check the bus.
        siblings = [
            r for r in self._spec.registry.list_all()
            if r.id != record.id and r.is_running
        ]
        if siblings and self._spec.coordination_strategy == STRATEGY_BUS:
            sibling_lines = "\n".join(
                f"- {s.label} [{s.agent_type_name}]: {s.task[:120]}"
                for s in siblings
            )
            system_prompt += (
                f"\n\nSibling agents currently running:\n{sibling_lines}\n"
                "Use `publish_finding` when you discover something relevant "
                "to their work. Use `read_findings` midway if their topics "
                "overlap with yours.\n"
            )

        # Surface the fully-resolved system prompt so the renderer's
        # SystemPromptHeader can show it on the child's pane. Goes out
        # as a system_event so the existing applyEventToSlice handler
        # (subtype='system_prompt_set') picks it up without renderer-side
        # changes. Useful for ANY sub-agent, particularly the judge /
        # calibrator profiles where the system prompt IS the contract.
        await _fire(
            self._spec.emit_event,
            {
                "type": "system_event",
                "sessionId": record.id,
                "subtype": "system_prompt_set",
                "message": "System prompt configured",
                "details": {"systemPrompt": system_prompt},
            },
        )

        # Wrap the child registry with a tracing wrapper scoped to the
        # child's session id, so tool_result events land in the child's slice.
        if self._spec.wrap_registry is not None:
            child_registry = self._spec.wrap_registry(child_registry, record.id)

        # Build provider with agent type's model and thinking config
        provider = self._spec.build_provider(child_model, agent_type.thinking_effort)
        session = Session.create(
            system_prompt=system_prompt,
            tools=list(child_registry._tools.values()),  # noqa: SLF001
            session_id=record.id,
            metadata={
                "model_id": child_model,
                "reasoning_level": agent_type.thinking_effort,
                "parent_session_id": self._spec.parent_session_id,
                "project_session_id": self._spec.parent_session_id,
                "subagent_id": record.id,
                "subagent_label": record.label,
                "agent_type": agent_type.name,
                "coordination_strategy": self._spec.coordination_strategy,
            },
        )

        # Resume path: if the record carries a saved transcript (set
        # by resume_archived), restore it onto the new Session. The
        # incoming inbox message (also pre-loaded onto record.inbox)
        # will be drained by the runner's pre-iteration hook and
        # appear as the next user turn.
        if getattr(record, "resume_mode", False) and getattr(record, "restored_transcript", None):
            try:
                session.restore_transcript(record.restored_transcript)
                logger.info("Restored transcript onto resumed sub-agent %s", record.id)
            except Exception:
                logger.exception(
                    "failed to restore transcript on resumed sub-agent %s", record.id
                )

        await self._mark_kanban_running(record)
        await self._mark_task_running(record)

        # Emit turn_start for the child session so the UI spins up a message
        # container to stream into.
        await _fire(
            self._spec.emit_event,
            {
                "type": "turn_start",
                "sessionId": record.id,
                "turnId": f"turn-1",
            },
        )

        collected_text: list[str] = []
        tool_count = 0
        current_tool_id: dict[str, str] = {"id": ""}
        cancelled = record.cancel_event

        async def on_stream(event: Any) -> None:
            nonlocal tool_count
            if cancelled.is_set():
                return
            etype = getattr(event, "type", None)
            if etype == "text_delta":
                collected_text.append(getattr(event, "text", ""))
                await _fire(
                    self._spec.emit_event,
                    {
                        "type": "text_delta",
                        "sessionId": record.id,
                        "text": getattr(event, "text", ""),
                    },
                )
            elif etype == "thinking_delta":
                await _fire(
                    self._spec.emit_event,
                    {
                        "type": "thinking_delta",
                        "sessionId": record.id,
                        "thinking": getattr(event, "thinking", ""),
                    },
                )
            elif etype == "tool_use_start":
                tool_count += 1
                tid = getattr(event, "id", "")
                current_tool_id["id"] = tid
                await _fire(
                    self._spec.emit_event,
                    {
                        "type": "tool_use_start",
                        "sessionId": record.id,
                        "id": tid,
                        "name": getattr(event, "name", ""),
                    },
                )
            elif etype == "tool_input_delta":
                await _fire(
                    self._spec.emit_event,
                    {
                        "type": "tool_input_delta",
                        "sessionId": record.id,
                        "id": current_tool_id["id"],
                        "partialJson": getattr(event, "partial_json", ""),
                    },
                )

        async def on_system_event(event: Any) -> None:
            await _fire(
                self._spec.emit_event,
                {
                    "type": "system_event",
                    "sessionId": record.id,
                    "subtype": getattr(event, "type", "unknown"),
                    "message": getattr(event, "message", ""),
                    "details": getattr(event, "details", {}) or {},
                },
            )

        # Build thinking config for the child runner
        from engine.types import ThinkingConfig
        child_thinking = ThinkingConfig()
        effort = agent_type.thinking_effort
        if effort not in ("off", "none", ""):
            if effort == "auto":
                # Import the auto-resolver from the bridge
                from bridge.freyja_bridge import _default_thinking_for_model
                child_thinking = _default_thinking_for_model(child_model)
            else:
                child_thinking = ThinkingConfig(enabled=True, effort=effort)

        # Telemetry callbacks tagged with this subagent's profile. Mirror
        # the parent's `_on_llm_call` / `_on_tool_metric` shape so the
        # dashboard sees uniform rows whether emitted by a root session
        # or a subagent.
        sub_id_local = record.id
        agent_type_name = agent_type.name
        parent_session_id_local = self._spec.parent_session_id
        turn_counter = {"n": 0}

        def _on_sub_llm_call(payload: dict[str, Any]) -> None:
            try:
                from bridge.compaction_telemetry import append_telemetry
                from engine.providers import compute_cost

                if payload.get("error"):
                    return
                turn_counter["n"] += 1
                model = payload.get("model") or child_model
                in_tok = int(payload.get("input_tokens", 0) or 0)
                out_tok = int(payload.get("output_tokens", 0) or 0)
                cr_tok = int(payload.get("cache_read_tokens", 0) or 0)
                cw_tok = int(payload.get("cache_write_tokens", 0) or 0)
                cost = compute_cost(
                    model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cr_tok,
                    cache_write_tokens=cw_tok,
                )
                append_telemetry({
                    "type": "llm_call_metric",
                    "session_id": sub_id_local,
                    "turn_id": f"{sub_id_local}-t{turn_counter['n']}",
                    "agent_type": agent_type_name,
                    "parent_session_id": parent_session_id_local,
                    "model": model,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cache_read_tokens": cr_tok,
                    "cache_write_tokens": cw_tok,
                    "cost_usd": float(cost) if cost is not None else None,
                    "duration_ms": int(payload.get("duration_ms", 0) or 0),
                })
            except Exception:
                pass

        def _on_sub_tool_metric(payload: dict[str, Any]) -> None:
            try:
                from bridge.compaction_telemetry import append_telemetry
                append_telemetry({
                    "type": "tool_call_metric",
                    "session_id": sub_id_local,
                    "turn_id": f"{sub_id_local}-t{turn_counter['n']}",
                    "agent_type": agent_type_name,
                    "parent_session_id": parent_session_id_local,
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_name": payload.get("tool_name") or "unknown",
                    "duration_ms": int(payload.get("duration_ms", 0) or 0),
                    "ok": bool(payload.get("ok", True)),
                    "result_bytes": int(payload.get("result_bytes", 0) or 0),
                })
            except Exception:
                pass

        # Subagent tool isolation (Gap J). Two tools the child registry
        # inherited from the parent are stateful in ways that DON'T
        # transfer correctly:
        #
        #   * SummarizeContextTool closes over the parent's session /
        #     provider / runner / pressure-pct getter. If the subagent
        #     called it, it would compact the parent's transcript, not
        #     its own.
        #   * SessionMemoryTool captured the parent's session_id in its
        #     constructor → writes go to the parent's memory.md file.
        #
        # We rebuild both per-subagent here, scoped to the child
        # session/runner. The runner doesn't exist yet at this point,
        # so SummarizeContextTool uses a small holder dict that gets
        # populated below.
        from bridge.tools.session_memory_tool import SessionMemoryTool
        from bridge.tools.summarize_context_tool import SummarizeContextTool

        sub_runner_holder: dict[str, Any] = {"runner": None}

        def _sub_summarize_pressure_pct() -> float | None:
            r = sub_runner_holder.get("runner")
            if r is None:
                return None
            try:
                provider_local = r.provider
                config_local = r.config
                usage = r.usage
                window = int(getattr(provider_local, "context_window", 0) or 0)
                if window <= 0:
                    return None
                reserved = int(getattr(config_local, "max_tokens_per_turn", 0) or 0)
                effective = max(1, window - reserved)
                used = int(usage.effective_context_tokens())
                return (used / effective) * 100
            except Exception:
                return None

        def _on_sub_summarize_call(payload: dict[str, Any]) -> None:
            try:
                from bridge.compaction_telemetry import append_telemetry
                append_telemetry({
                    "type": "summarize_context_call",
                    "session_id": sub_id_local,
                    "turn_id": f"{sub_id_local}-t{turn_counter['n']}",
                    "agent_type": agent_type_name,
                    "parent_session_id": parent_session_id_local,
                    "scope": payload.get("scope"),
                    "level_requested": payload.get("level_requested"),
                    "level_used": payload.get("level_used"),
                    "preserve_facts_count": int(payload.get("preserve_facts_count", 0) or 0),
                    "preserve_facts_missing": payload.get("preserve_facts_missing") or [],
                    "pinned_ordinals": payload.get("pinned_ordinals") or [],
                    "reason": payload.get("reason"),
                    "pressure_pct_at_call": payload.get("pressure_pct_at_call"),
                    "tokens_before": int(payload.get("tokens_before", 0) or 0),
                    "tokens_after": int(payload.get("tokens_after", 0) or 0),
                    "resumed_from_previous": bool(payload.get("resumed_from_previous", False)),
                    "entries_removed": int(payload.get("entries_removed", 0) or 0),
                    "success": bool(payload.get("success", False)),
                    "error": payload.get("error"),
                    "elapsed_ms": int(payload.get("elapsed_ms", 0) or 0),
                    "model": child_model,
                })
            except Exception:
                pass

        sub_summarize_tool = SummarizeContextTool(
            get_session=lambda: session,
            get_provider=lambda: provider,
            get_compactor=lambda: (
                sub_runner_holder["runner"].compaction
                if sub_runner_holder.get("runner") is not None
                else None
            ),
            on_summarize_call=_on_sub_summarize_call,
            get_current_pressure_pct=_sub_summarize_pressure_pct,
        )

        # Register the subagent-scoped instances IN PLACE so existing
        # references to child_registry (and the system_prompt that
        # already listed the tools by name) stay valid. Mutating the
        # private map keeps the registration order from registry build.
        if "summarize_context" in child_registry._tools:  # noqa: SLF001
            child_registry._tools["summarize_context"] = sub_summarize_tool  # noqa: SLF001
        if "session_memory" in child_registry._tools:  # noqa: SLF001
            child_registry._tools["session_memory"] = SessionMemoryTool(
                session_id=record.id,
            )  # noqa: SLF001

        # Pre-iteration hook: drain this sub-agent's inbox and prepend
        # incoming messages as attributed user turns before each LLM
        # call. Mirrors the main session's _drain_inbox_into_session.
        sub_inbox_ref = record.inbox

        async def _drain_subagent_inbox(sub_session: Any, iteration: int) -> None:
            if sub_inbox_ref is None or not sub_inbox_ref.has_unread():
                return
            msgs = sub_inbox_ref.drain()
            for m in msgs:
                try:
                    sub_session.add_user_message(m.as_user_block())
                except Exception:
                    continue

        runner = AsyncAgentRunner(
            provider=provider,
            compaction_strategy=SummaryCompaction(),
            tool_registry=child_registry,
            on_stream=on_stream,
            on_system_event=on_system_event,
            on_llm_call=_on_sub_llm_call,
            on_tool_metric=_on_sub_tool_metric,
            on_pre_iteration=_drain_subagent_inbox,
            thinking=child_thinking,
        )
        sub_runner_holder["runner"] = runner

        # Register the asyncio cancel token on the record so the
        # bridge's force-cancel path can wake us directly, and also
        # poll the threading.Event as a fallback path.
        asyncio_cancel = asyncio.Event()
        record.asyncio_cancel = asyncio_cancel
        record.loop = asyncio.get_running_loop()

        async def watchdog() -> None:
            # Wait on either the asyncio event (fast) or the threading
            # event (via 100ms poll) — whichever fires first.
            while not asyncio_cancel.is_set():
                if cancelled.is_set():
                    asyncio_cancel.set()
                    return
                try:
                    await asyncio.wait_for(asyncio_cancel.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

        from engine.runner import StopCondition
        # max_iterations_override (set by spawn_programmatically) wins
        # over the agent_type cap. The deep judge uses a high internal
        # safety-net via _DEEP_JUDGE_SAFETY_NET_ITERATIONS in
        # freyja_bridge.py — the actual verdict shape is guaranteed by
        # a separate structured-output synthesis pass after this run
        # returns, so this cap exists only as a brake on pathological
        # tool-loops.
        effective_max_iter = (
            getattr(record, "max_iterations_override", None)
            or agent_type.max_iterations
        )
        stop = StopCondition(max_iterations=effective_max_iter)

        run_task = asyncio.create_task(
            runner.run(
                session,
                # On resume, the saved transcript already carries the
                # original task + prior turns. We feed a thin "you
                # are being re-engaged; check your inbox" prompt
                # instead so the pre-iteration drain inserts the real
                # wake message before the LLM call.
                "[RESUME] You are being re-engaged after a pause. New messages have arrived in your inbox — read them and continue."
                if getattr(record, "resume_mode", False)
                else record.task,
                stream=True,
                stop_condition=stop,
            ),
            name=f"sub-run-{record.id}",
        )
        watch_task = asyncio.create_task(
            watchdog(), name=f"sub-watch-{record.id}"
        )

        cancelled_by_watchdog = False
        result = None
        run_exception: BaseException | None = None
        try:
            done, pending = await asyncio.wait(
                {run_task, watch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if watch_task in done and run_task not in done:
                cancelled_by_watchdog = True
                run_task.cancel()
                try:
                    await run_task
                except BaseException:  # noqa: BLE001
                    pass
                # Force-message compliance iteration: if the watchdog
                # fired because a force=true inbox message arrived (vs.
                # an external cancel), run ONE more bounded iteration
                # so the agent can acknowledge / comply with the force
                # message rather than dying mid-stream. The inbox
                # pre-iteration hook drains the force message as the
                # first thing the new iteration sees.
                if (
                    record.inbox is not None
                    and record.inbox.has_force_unread()
                ):
                    try:
                        # Reset cancel flags for the compliance pass.
                        cancelled.clear()
                        try:
                            asyncio_cancel.clear()
                        except Exception:
                            pass
                        compliance_result = await runner.run(
                            session,
                            "[INTERRUPT] Operator/parent force-stopped you. Read the message just delivered, summarize what you have, and exit.",
                            stream=True,
                            stop_condition=StopCondition(max_iterations=1),
                        )
                        result = compliance_result
                    except BaseException as exc:  # noqa: BLE001
                        run_exception = exc
            else:
                watch_task.cancel()
                try:
                    await watch_task
                except BaseException:  # noqa: BLE001
                    pass
                try:
                    result = run_task.result()
                except asyncio.CancelledError:
                    cancelled_by_watchdog = True
                except BaseException as exc:  # noqa: BLE001
                    run_exception = exc
        except asyncio.CancelledError:
            # Outer task cancelled (parent pending_task.cancel()). We
            # MUST cancel both inner tasks explicitly — the naked
            # asyncio.wait does NOT auto-cancel pending tasks when its
            # awaiting coroutine is cancelled, so without this they'd
            # keep running in the background.
            run_task.cancel()
            watch_task.cancel()
            for t in (run_task, watch_task):
                try:
                    await t
                except BaseException:  # noqa: BLE001
                    pass
            record.iterations = int(getattr(runner, "current_iteration", 0) or 0)
            self._spec.registry.mark_done(
                record.id, "Cancelled", SubAgentState.CANCELLED,
                iterations=record.iterations,
            )
            await self._mark_kanban_terminal(record, "cancelled", "Sub-agent cancelled")
            await self._mark_task_terminal(record, "cancelled", "Sub-agent cancelled")
            self._persist_child_transcript(
                record,
                session,
                child_model=child_model,
                agent_type=agent_type,
                state="cancelled",
            )
            await self._emit_terminal_events(record, success=False)
            await _emit_update(self._spec, record)
            raise

        if cancelled_by_watchdog:
            record.iterations = int(getattr(runner, "current_iteration", 0) or 0)
            self._spec.registry.mark_done(
                record.id, "Cancelled", SubAgentState.CANCELLED,
                iterations=record.iterations,
            )
            await self._mark_kanban_terminal(record, "cancelled", "Sub-agent cancelled")
            await self._mark_task_terminal(record, "cancelled", "Sub-agent cancelled")
            self._persist_child_transcript(
                record,
                session,
                child_model=child_model,
                agent_type=agent_type,
                state="cancelled",
            )
            await self._emit_terminal_events(record, success=False)
            await _emit_update(self._spec, record)
            return "(sub-agent cancelled)"

        if run_exception is not None:
            # Mark as failed and emit terminal events so the UI
            # clears the "running" spinner. Without this, an
            # exception inside the sub-agent runner left the UI
            # session row spinning forever even though the
            # underlying task was dead.
            record.iterations = int(getattr(runner, "current_iteration", 0) or 0)
            self._spec.registry.mark_done(
                record.id,
                f"Error: {run_exception}",
                SubAgentState.FAILED,
                iterations=record.iterations,
            )
            await self._mark_kanban_terminal(record, "blocked", f"Error: {run_exception}")
            await self._mark_task_terminal(record, "blocked", f"Error: {run_exception}")
            self._persist_child_transcript(
                record,
                session,
                child_model=child_model,
                agent_type=agent_type,
                state="failed",
            )
            await self._emit_terminal_events(record, success=False)
            await _emit_update(self._spec, record)
            raise run_exception

        # Extract stats
        usage = runner.usage
        record.input_tokens = int(getattr(usage, "input", 0) or 0)
        record.output_tokens = int(getattr(usage, "output", 0) or 0)
        try:
            record.context_tokens = int(usage.effective_context_tokens())
        except Exception:  # noqa: BLE001
            record.context_tokens = record.input_tokens
        record.tools_called = tool_count
        record.iterations = getattr(result, "iterations", 0) or 0

        text = "".join(collected_text).strip() or "(no output)"

        # Stash the final transcript snapshot on the record so callers
        # that need to chain a follow-up LLM call against the SAME
        # conversational context (deep judge synthesis pass, etc.) can
        # do so without re-running the investigation. Captured here on
        # the success path only — cancel/error paths return early and
        # leave the default empty list, signalling "no usable transcript".
        try:
            record.final_messages = session.get_messages()
            record.final_system_prompt = system_prompt
            record.final_model_id = child_model
        except Exception:  # noqa: BLE001
            # Failing to snapshot the transcript must not break the
            # subagent return — the downstream synthesis pass has its
            # own fallback when final_messages is empty.
            pass

        # Proactively persist the full result to an artifact file so it
        # survives truncation and compaction. The parent agent gets a
        # file path it can read_file on instead of losing the data.
        produced_before_final: list[str] = []
        if self._spec.artifact_store is not None:
            try:
                produced_before_final = self._spec.artifact_store.paths_for_creator(record.id)
            except Exception:  # noqa: BLE001
                produced_before_final = []
        try:
            artifact_dir = project_output_dir(self._spec.parent_session_id) / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact_file = artifact_dir / f"{record.id}.md"
            resolution = getattr(record, "model_resolution", None)
            model_policy = resolution.policy if resolution is not None else "n/a"
            produced_section = (
                "\n".join(f"- `{path}`" for path in produced_before_final)
                if produced_before_final
                else "(no verified files recorded before final summary)"
            )
            artifact_file.write_text(
                f"# {record.label}\n\n"
                f"**Agent type:** {agent_type.name}\n"
                f"**Task:** {record.task}\n"
                f"**Model:** {child_model}\n"
                f"**Model policy:** {model_policy}\n"
                f"**Tokens:** {record.input_tokens} in / {record.output_tokens} out\n"
                f"**Tools called:** {record.tools_called}\n\n"
                f"## Produced files\n\n"
                f"{produced_section}\n\n"
                f"---\n\n"
                f"{text}",
                encoding="utf-8",
            )
            record.artifact_path = str(artifact_file)
            if self._spec.artifact_store is not None:
                self._spec.artifact_store.record_file(
                    artifact_file,
                    creator_id=record.id,
                    creator_label=record.label,
                    operation="subagent_artifact",
                    source="subagent",
                    metadata={
                        "agentType": agent_type.name,
                        "model": child_model,
                    },
                )
            logger.info("Wrote artifact for %s → %s", record.id, artifact_file)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to write artifact for %s", record.id, exc_info=True)

        if self._spec.artifact_store is not None:
            try:
                record.created_files = self._spec.artifact_store.paths_for_creator(record.id)
            except Exception:  # noqa: BLE001
                record.created_files = [record.artifact_path] if record.artifact_path else []
        elif record.artifact_path:
            record.created_files = [record.artifact_path]

        self._persist_child_transcript(
            record,
            session,
            child_model=child_model,
            agent_type=agent_type,
            state="done",
        )

        self._spec.registry.mark_done(
            record.id,
            text,
            SubAgentState.DONE,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            iterations=record.iterations,
            tools_called=record.tools_called,
        )
        await self._mark_kanban_terminal(record, "done", text)
        await self._mark_task_terminal(record, "done", text)
        await self._emit_terminal_events(record, success=True, usage=usage)
        await _emit_update(self._spec, record)
        return text

    def _persist_child_transcript(
        self,
        record: SubAgentRecord,
        session: Any,
        *,
        child_model: str,
        agent_type: AgentType,
        state: str,
    ) -> None:
        """Persist a sub-agent's real engine transcript for later follow-up.

        The renderer can replay streamed child events, but that UI transcript
        is not enough for a future LLM turn. Saving the engine transcript here
        lets `switch_session` / `send_message` restore the child conversation
        and continue it without changing the parent-visible terminal state.
        """
        try:
            try:
                from bridge.freyja_bridge import _backfill_orphan_tool_results

                _backfill_orphan_tool_results(session)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "failed to backfill child transcript before save",
                    exc_info=True,
                )

            from bridge.transcript_persistence import save_transcript

            data = session.serialize_transcript()
            metadata = data.setdefault("metadata", {})
            metadata.update(
                {
                    "model_id": child_model,
                    "reasoning_level": agent_type.thinking_effort,
                    "parent_session_id": self._spec.parent_session_id,
                    "project_session_id": self._spec.parent_session_id,
                    "subagent_id": record.id,
                    "subagent_label": record.label,
                    "agent_type": agent_type.name,
                    "coordination_strategy": self._spec.coordination_strategy,
                    "subagent_state": state,
                    "artifact_path": record.artifact_path,
                    "created_files": list(record.created_files),
                }
            )
            data["session_id"] = record.id
            save_transcript(record.id, data)
            logger.info("Saved transcript for sub-agent %s", record.id)

            # Additionally write a SUBAGENT sidecar carrying every piece
            # of spawn config a future re-wake would need (talk() to an
            # archived sub-agent). Lets a follow-up message bring the
            # agent back to life with full context from its last run.
            try:
                from bridge.transcript_persistence import save_subagent_state
                save_subagent_state(record.id, {
                    "sessionId": record.id,
                    "parentSessionId": self._spec.parent_session_id,
                    "agentType": agent_type.name,
                    "model": child_model,
                    "reasoningLevel": agent_type.thinking_effort,
                    "task": record.task,
                    "label": record.label,
                    "coordinationStrategy": self._spec.coordination_strategy,
                    "transcript": data,
                    "savedAt": int(time.time() * 1000),
                })
            except Exception:  # noqa: BLE001
                logger.debug("failed to save subagent sidecar", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to save transcript for sub-agent %s",
                record.id,
                exc_info=True,
            )

    def _coordination_guidance(self, record: SubAgentRecord) -> str:
        from bridge.tools.coordination import STRATEGY_GOAL

        strategy = self._spec.coordination_strategy
        if strategy == STRATEGY_ISOLATED:
            task_id = getattr(record, "task_id", "")
            assignment = f"`{task_id}`" if task_id else "your assigned task"
            return (
                "\nCoordination mode: task-first solo.\n"
                f"Your task-led assignment is {assignment}. Call `tasks` with action=`show` first "
                "when a task id is available. Use `heartbeat` during long work, `complete` with "
                "verified artifacts/results when done, or `block` with the exact blocker. You do not have "
                "sibling communication tools; the task ledger is the durable handoff surface.\n"
            )
        if strategy == STRATEGY_KANBAN:
            task_id = getattr(record, "kanban_task_id", "")
            assignment = f"`{task_id}`" if task_id else "the card named in your task"
            return (
                "\nCoordination mode: kanban board.\n"
                f"Your board assignment is {assignment}. Call `kanban` with action=`show` first "
                "when a task id is available. Use `heartbeat` or `comment` during long work, "
                "and call `complete` with verified artifact paths and a concise handoff when done. If blocked, call `block` "
                "with the exact blocker instead of guessing.\n"
                "Verification routing: if your card's `requiresVerification` field is true, "
                "calling `complete` sends the card to the verifier (status flips to "
                "`done_unverified`) which then either signs off or bounces it back to you "
                "with feedback. If the flag is false (the default), `complete` seals the "
                "card directly to `done`. Either way you don't write the status yourself — "
                "just call `complete` and the board routes correctly.\n"
            )
        if strategy == STRATEGY_GOAL:
            # Goal-mode workers have no coordination surface at all — no
            # message bus (would let them sidestep the judge), no kanban,
            # no tasks tool. Their job is focused work-and-return; the
            # parent is the one running the goal loop and stitching
            # results back into milestones.
            return (
                "\nCoordination mode: goal loop (parent-driven).\n"
                "Work independently on the task you've been given. You don't have sibling "
                "messaging or shared boards in this mode — return a tight structured summary "
                "and the parent will integrate it into its goal-loop synthesis.\n"
            )
        return (
            "\nCoordination mode: message bus.\n"
            "When you discover something useful, call `publish_finding` so sibling agents can "
            "see it. Call `read_findings` to check what siblings have found when topics overlap.\n"
        )

    async def _mark_kanban_running(self, record: SubAgentRecord) -> None:
        task_id = getattr(record, "kanban_task_id", "")
        if (
            self._spec.coordination_strategy != STRATEGY_KANBAN
            or self._spec.kanban_board is None
            or not task_id
        ):
            return
        try:
            task = await self._spec.kanban_board.update(
                task_id,
                actor=f"{record.label} ({record.id})",
                status="running",
                assignee=record.label,
                comment="Sub-agent started",
            )
            await self._emit_kanban_state_event("update", task)
        except Exception:  # noqa: BLE001
            logger.debug("failed to mark kanban card running", exc_info=True)

    async def _mark_kanban_terminal(
        self,
        record: SubAgentRecord,
        status: str,
        summary: str,
    ) -> None:
        task_id = getattr(record, "kanban_task_id", "")
        if (
            self._spec.coordination_strategy != STRATEGY_KANBAN
            or self._spec.kanban_board is None
            or not task_id
        ):
            return
        try:
            # Opt-in verification: a worker that finished successfully on a
            # card with `requires_verification=True` should hand off to the
            # verifier rather than seal the card directly. Mirrors what the
            # worker's `complete` action does — this branch covers runs that
            # exit cleanly without an explicit complete call (most of them).
            target_status = status
            if status == "done":
                current = await self._spec.kanban_board.get(task_id)
                if (
                    current is not None
                    and getattr(current, "requires_verification", False)
                    and current.status != "done"
                ):
                    target_status = "done_unverified"
            task = await self._spec.kanban_board.update(
                task_id,
                actor=f"{record.label} ({record.id})",
                status=target_status,
                summary=summary[:4000],
                artifacts=list(record.created_files),
            )
            await self._emit_kanban_state_event(
                "complete" if target_status == "done" else "update",
                task,
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to mark kanban card terminal", exc_info=True)

    async def _mark_task_running(self, record: SubAgentRecord) -> None:
        # No coordination-strategy gate any more — task lifecycle
        # updates fire in any mode as long as the spawn carried a
        # `task_id`. The parent's tasks tool is universal; the worker
        # being attached to a specific task is the signal.
        task_id = getattr(record, "task_id", "")
        if self._spec.task_board is None or not task_id:
            return
        try:
            task = await self._spec.task_board.update(
                task_id,
                actor=f"{record.label} ({record.id})",
                status="active",
                assignee=record.label,
                progress=10,
                note="Sub-agent started",
            )
            await self._emit_task_state_event("update", task)
        except Exception:  # noqa: BLE001
            logger.debug("failed to mark task running", exc_info=True)

    async def _mark_task_terminal(
        self,
        record: SubAgentRecord,
        status: str,
        summary: str,
    ) -> None:
        task_id = getattr(record, "task_id", "")
        if self._spec.task_board is None or not task_id:
            return
        try:
            task = await self._spec.task_board.update(
                task_id,
                actor=f"{record.label} ({record.id})",
                status=status,
                progress=100 if status == "done" else None,
                summary=summary[:4000],
                result=summary[:4000] if status == "done" else "",
                artifacts=list(record.created_files),
            )
            await self._emit_task_state_event(
                "complete" if status == "done" else "update",
                task,
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to mark task terminal", exc_info=True)

    async def _emit_task_state_event(self, action: str, task: Any | None) -> None:
        if task is None:
            return
        await _fire(
            self._spec.emit_event,
            {
                "type": "system_event",
                "sessionId": self._spec.parent_session_id,
                "subtype": f"task_{action}",
                "message": f"Task {action}: {task.id} {task.title}",
                "details": {
                    "action": action,
                    "task": task.to_dict(),
                    "source": "sub_agent_state",
                },
            },
        )

    async def _emit_kanban_state_event(self, action: str, task: Any | None) -> None:
        if task is None:
            return
        await _fire(
            self._spec.emit_event,
            {
                "type": "system_event",
                "sessionId": self._spec.parent_session_id,
                "subtype": f"kanban_{action}",
                "message": f"Kanban {action}: {task.id} {task.title}",
                "details": {
                    "action": action,
                    "task": task.to_dict(),
                    "source": "sub_agent_state",
                },
            },
        )

    async def _emit_terminal_events(
        self,
        record: SubAgentRecord,
        *,
        success: bool,
        usage: Any = None,
    ) -> None:
        """Emit the full UI-clearing sequence for a finished sub-agent.

        Every terminal path (DONE / CANCELLED / FAILED) must emit:

          1. `usage` — so the sidebar row and activity panel show
             final token/cost numbers.
          2. `turn_complete` — flips `isStreaming=false` in the
             child's archived slice.
          3. `session_completed` — flips `completed=true` on the
             session row so the sidebar stops spinning and the
             "swarm" panel renders the child with a green/red dot
             instead of an animated progress ring.

        Previously the two cancelled paths (outer CancelledError and
        watchdog cancel) skipped steps 2 and 3, so a sub-agent that
        was killed mid-run would stay "running" in the UI forever —
        even though the Python bridge had long since released it.
        That's exactly the "stuck session" symptom the user saw.
        """
        cache_read = (
            int(getattr(usage, "cache_read", 0) or 0) if usage is not None else 0
        )
        cache_write = (
            int(getattr(usage, "cache_write", 0) or 0) if usage is not None else 0
        )
        # Cost: use the engine's per-model pricing table so the displayed
        # spend tracks the actual rate (the old hard-coded $3/$15-per-M
        # formula assumed Sonnet pricing for every model and ignored
        # cache reads + writes). Falls back to 0 when the model isn't
        # priced.
        try:
            from engine.providers import compute_cost as _compute_cost

            sub_model = (
                getattr(record, "child_model", None)
                or self._spec.parent_model
                or ""
            )
            cost_estimate = _compute_cost(
                sub_model,
                input_tokens=int(record.input_tokens or 0),
                output_tokens=int(record.output_tokens or 0),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            sub_cost = float(cost_estimate) if cost_estimate is not None else 0.0
        except Exception:  # noqa: BLE001
            sub_cost = 0.0
        await _fire(
            self._spec.emit_event,
            {
                "type": "usage",
                "sessionId": record.id,
                "contextTokens": record.context_tokens,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "cacheReadTokens": cache_read,
                "cacheWriteTokens": cache_write,
                "cost": sub_cost,
            },
        )
        await _fire(
            self._spec.emit_event,
            {
                "type": "turn_complete",
                "sessionId": record.id,
                "turnId": "turn-1",
                "success": success,
            },
        )
        await _fire(
            self._spec.emit_event,
            {
                "type": "session_completed",
                "sessionId": record.id,
                "success": success,
                "elapsedMs": int(record.elapsed * 1000),
                "contextTokens": record.context_tokens,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "toolsCalled": record.tools_called,
                "artifactPath": record.artifact_path,
                "createdFiles": list(record.created_files),
            },
        )

        # Persist a profile_completion JSONL row so the Profiles view can
        # show outcome mix (success / error / cancelled) and iterations
        # used vs the agent type's budget.
        #
        # Outcome classification: trust the ``success`` parameter directly
        # (every terminal call site sets it explicitly) and use the record
        # state only to distinguish CANCELLED from FAILED in the !success
        # case. The earlier ``record.state.name.lower() == "done"`` check
        # was correct in principle but fragile to any future change that
        # tweaks the state-set ordering — using the explicit boolean keeps
        # the dashboard's success-rate honest no matter how the record
        # transitioned.
        try:
            from bridge.compaction_telemetry import append_telemetry

            if success:
                outcome = "success"
            elif record.state == SubAgentState.CANCELLED:
                outcome = "cancelled"
            else:
                outcome = "error"
            append_telemetry({
                "type": "profile_completion",
                "session_id": record.id,
                "parent_session_id": self._spec.parent_session_id,
                "agent_type": getattr(record, "agent_type_name", "general"),
                "iterations_used": int(getattr(record, "iterations", 0) or 0),
                "final_outcome": outcome,
                "duration_ms": int(record.elapsed * 1000),
            })
        except Exception:
            pass


# ─── Helpers ──────────────────────────────────────────────────────────────


def _record_to_dict(record: SubAgentRecord) -> dict[str, Any]:
    resolution = getattr(record, "model_resolution", None)
    return {
        "id": record.id,
        "label": record.label,
        "mode": record.mode,
        "state": record.state.name.lower(),
        "task": record.task,
        "agentType": record.agent_type_name,
        "coordinationStrategy": getattr(record, "coordination_strategy", None),
        "kanbanTaskId": getattr(record, "kanban_task_id", None),
        "taskId": getattr(record, "task_id", None),
        "model": getattr(record, "child_model", None),
        "modelPolicy": resolution.policy if resolution is not None else None,
        "modelFallbackUsed": (
            resolution.fallback_used if resolution is not None else False
        ),
        "artifactPath": record.artifact_path,
        "createdFiles": list(record.created_files),
        "startedAt": int(record.start_time * 1000),
        "elapsedMs": int(record.elapsed * 1000),
        "tokensIn": record.input_tokens,
        "tokensOut": record.output_tokens,
        "toolsCalled": record.tools_called,
    }


async def _emit_update(spec: SubAgentSpec, record: SubAgentRecord) -> None:
    if record.state == SubAgentState.DONE:
        await _fire(
            spec.emit_event,
            {
                "type": "subagent_done",
                "id": record.id,
                "result": str(record.result or ""),
                "elapsedMs": int(record.elapsed * 1000),
            },
        )
    await _fire(
        spec.emit_event,
        {
            "type": "subagent_update",
            "id": record.id,
            "patch": _record_to_dict(record),
        },
    )


async def _fire(cb: SubAgentEventCb, event: dict[str, Any]) -> None:
    try:
        result = cb(event)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("sub-agent event callback failed")
