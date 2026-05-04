"""
Orchestration tool for managing background sub-agents from within a turn.

Provides `action=list|wait|wait_all|kill` against the SubAgentRegistry.
The desktop UI also exposes its own sidebar that mirrors the registry state,
so this tool is primarily for the model to introspect its own spawned agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier
from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

logger = logging.getLogger(__name__)


class SubAgentsTool:
    """Manage background sub-agents: list, wait, wait_all, kill."""

    def __init__(self, registry: SubAgentRegistry) -> None:
        self._registry = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="subagents",
            summary="Manage background sub-agents",
            tier=ToolTier.HOT,
            description="""Manage sub-agents you've spawned with `sub_agent`.

Actions:
- list: returns JSON of all sub-agents with state, mode, stats
- wait: blocks until a specific sub-agent (by id) reaches a terminal
  state (done / failed / cancelled). There is NO timeout — the wait
  lasts as long as the sub-agent takes. Sub-agents are expensive to
  restart, so never "give up" on one: either wait for it, or
  explicitly kill it.
- wait_all: blocks until every running background sub-agent reaches
  a terminal state. Returns the full final output of EVERY completed
  subagent (not just metadata). Same no-timeout contract as `wait`.
- kill: cancels a running sub-agent by id

IMPORTANT: `wait` and `wait_all` WILL block your entire turn for as
long as the sub-agent keeps running. Do NOT fall back to doing the
same work yourself "in case it's taking too long" — you are
guaranteed to receive the final state (the user can panic-stop the
whole session if truly needed).""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "wait", "wait_all", "kill"],
                        "description": "What to do",
                    },
                    "id": {
                        "type": "string",
                        "description": "Sub-agent id (required for wait/kill)",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = arguments.get("action", "list")
        sub_id = arguments.get("id")

        if action == "list":
            rows = []
            for r in self._registry.list_all():
                row: dict[str, Any] = {
                    "id": r.id,
                    "label": r.label,
                    "mode": r.mode,
                    "state": r.state.name.lower(),
                    "agent_type": r.agent_type_name,
                    "elapsed_s": round(r.elapsed, 2),
                    "tokens_in": r.input_tokens,
                    "tokens_out": r.output_tokens,
                    "tools_called": r.tools_called,
                    "task": r.task[:200],
                    "artifact_path": r.artifact_path,
                }
                if r.result:
                    preview = str(r.result)[:300]
                    row["summary"] = preview + ("..." if len(str(r.result)) > 300 else "")
                    row["full_length"] = len(str(r.result))
                rows.append(row)
            return ToolResult(
                call_id=call_id,
                content=json.dumps({"subagents": rows}, indent=2),
                is_error=False,
            )

        if action == "kill":
            if not sub_id:
                return ToolResult(
                    call_id=call_id,
                    content="Error: `id` is required for kill",
                    is_error=True,
                )
            killed = self._registry.kill(sub_id)
            return ToolResult(
                call_id=call_id,
                content=f"Kill signal {'sent' if killed else 'ignored (not running)'} for {sub_id}",
                is_error=not killed,
            )

        if action == "wait":
            if not sub_id:
                return ToolResult(
                    call_id=call_id,
                    content="Error: `id` is required for wait",
                    is_error=True,
                )
            record = self._registry.get(sub_id)
            if record is None:
                return ToolResult(
                    call_id=call_id,
                    content=f"Unknown sub-agent: {sub_id}",
                    is_error=True,
                )
            # Poll the done_event with a short asyncio sleep. This
            # never returns "timeout" — the only way out is a terminal
            # state on the record or an asyncio.CancelledError from
            # the parent session's emergency stop propagating through.
            # The user explicitly asked for no-timeout: the parent
            # agent kept bailing on slow sub-agents and redoing the
            # work in the main loop, so we now guarantee that wait
            # either delivers a result or the user kills it.
            while not record.done_event.is_set():
                await asyncio.sleep(0.25)
            self._registry.mark_delivered(sub_id)
            result_text = str(record.result or "")
            response = {
                "id": record.id,
                "label": record.label,
                "state": record.state.name.lower(),
                "agent_type": record.agent_type_name,
                "artifact_path": record.artifact_path,
                "summary": result_text[:2000] + ("..." if len(result_text) > 2000 else ""),
                "full_length": len(result_text),
            }
            if record.artifact_path:
                response["_hint"] = f"Use read_file on {record.artifact_path} to see full results"
            return ToolResult(
                call_id=call_id,
                content=json.dumps(response, indent=2),
                is_error=record.state != SubAgentState.DONE,
            )

        if action == "wait_all":
            # Snapshot the currently-running background agents once,
            # then await them all one-by-one.
            pending = [
                r
                for r in self._registry.list_all()
                if r.mode == "background" and r.is_running
            ]
            for r in pending:
                while not r.done_event.is_set():
                    await asyncio.sleep(0.25)

            # Return a structured JSON index with per-agent summaries
            # and artifact file paths. The parent can read_file on any
            # artifact_path to get the full output without the truncator
            # destroying it.
            entries = []
            for r in pending:
                result_text = str(r.result or "")
                entry: dict[str, Any] = {
                    "id": r.id,
                    "label": r.label,
                    "state": r.state.name.lower(),
                    "agent_type": r.agent_type_name,
                    "tokens_in": r.input_tokens,
                    "tokens_out": r.output_tokens,
                    "tools_called": r.tools_called,
                    "elapsed_s": round(r.elapsed, 2),
                    "artifact_path": r.artifact_path,
                    "summary": result_text[:500] + ("..." if len(result_text) > 500 else ""),
                    "full_length": len(result_text),
                }
                entries.append(entry)
                self._registry.mark_delivered(r.id)

            body = json.dumps(
                {
                    "completed": entries,
                    "count": len(entries),
                    "_hint": "Use read_file on artifact_path to see full results",
                },
                indent=2,
            ) if entries else "(no background sub-agents)"
            return ToolResult(
                call_id=call_id,
                content=body,
                is_error=False,
            )

        return ToolResult(
            call_id=call_id,
            content=f"Unknown action: {action}",
            is_error=True,
        )
