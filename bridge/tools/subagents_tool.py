"""
Orchestration tool for the sub-agents a session has spawned.

Sub-agents always run in the background: `sub_agent` returns as soon as the
child is launched, and when the child finishes a memo lands in the parent's
inbox (waking the parent if it is idle). Nothing here blocks — the parent
never sits on a child, so the operator can keep talking to it while the
swarm works.

Actions: `list` / `status` (snapshot), `result` (a finished child's full
output), `kill`. The old blocking `wait` / `wait_all` actions are gone; a call
to either (restored transcripts still contain them) returns the current
status immediately with a pointer to the memo mechanism.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier
from bridge.tools.sub_agent_registry import SubAgentRecord, SubAgentRegistry

logger = logging.getLogger(__name__)

# `result` returns this much of a finished child's final text inline; the
# rest is in the artifact file the child wrote.
RESULT_INLINE_CHARS = 20_000

_NO_WAIT_NOTE = (
    "Sub-agents run in the background and there is no blocking wait. You "
    "will get a memo in your inbox when each one finishes (it wakes you if "
    "you are idle), so don't poll: keep working on something else, or end "
    "your turn and tell the operator what is in flight."
)


def _row(r: SubAgentRecord, *, preview_chars: int = 300) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": r.id,
        "label": r.label,
        "state": r.state.name.lower(),
        "agent_type": r.agent_type_name,
        "elapsed_s": round(r.elapsed, 1),
        "tokens_in": r.input_tokens,
        "tokens_out": r.output_tokens,
        "tools_called": r.tools_called,
        "task": r.task[:200],
        "artifact_path": r.artifact_path,
        "created_files": list(r.created_files),
    }
    if r.result:
        text = str(r.result)
        row["summary"] = text[:preview_chars] + ("..." if len(text) > preview_chars else "")
        row["full_length"] = len(text)
    return row


class SubAgentsTool:
    """Inspect and manage background sub-agents: list, status, result, kill."""

    def __init__(self, registry: SubAgentRegistry) -> None:
        self._registry = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="subagents",
            summary="Inspect or stop your background sub-agents",
            tier=ToolTier.HOT,
            description="""Inspect or stop the sub-agents you spawned with `sub_agent`.

Sub-agents always run in the background. When one finishes you receive a
memo in your inbox — "[sub-agent memo …]" with its summary and artifact
path — and an idle session is woken to handle it. You never need to wait
or poll: keep working, answer the operator, or end your turn.

Actions:
- list: every sub-agent with state, elapsed time, and stats
- status: one sub-agent (pass `id`), or omit `id` for the ones still running
- result: the full final output of a finished sub-agent (pass `id`) — use
  it to re-read a memo's summary in full
- kill: stop a running sub-agent (pass `id`)""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status", "result", "kill"],
                        "description": "What to do",
                    },
                    "id": {
                        "type": "string",
                        "description": "Sub-agent id (required for result/kill)",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = arguments.get("action", "list")
        sub_id = (arguments.get("id") or "").strip() or None

        if action == "list":
            rows = [_row(r) for r in self._registry.list_all()]
            return ToolResult(
                call_id=call_id,
                content=json.dumps({"subagents": rows}, indent=2),
                is_error=False,
            )

        if action in ("status", "wait", "wait_all"):
            return self._status(call_id, sub_id, legacy=action != "status")

        if action == "result":
            if not sub_id:
                return ToolResult(
                    call_id=call_id,
                    content="Error: `id` is required for result",
                    is_error=True,
                )
            record = self._registry.get(sub_id)
            if record is None:
                return ToolResult(
                    call_id=call_id,
                    content=f"Unknown sub-agent: {sub_id}",
                    is_error=True,
                )
            if record.is_running:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Sub-agent {sub_id} is still running "
                        f"({round(record.elapsed)}s so far). {_NO_WAIT_NOTE}"
                    ),
                    is_error=False,
                )
            text = str(record.result or "")
            body: dict[str, Any] = {
                "id": record.id,
                "label": record.label,
                "state": record.state.name.lower(),
                "artifact_path": record.artifact_path,
                "created_files": list(record.created_files),
                "result": text[:RESULT_INLINE_CHARS],
            }
            if len(text) > RESULT_INLINE_CHARS:
                body["truncated"] = True
                body["full_length"] = len(text)
                if record.artifact_path:
                    body["_hint"] = f"read_file {record.artifact_path} for the rest"
            return ToolResult(
                call_id=call_id,
                content=json.dumps(body, indent=2),
                is_error=False,
            )

        if action == "kill":
            if not sub_id:
                return ToolResult(
                    call_id=call_id,
                    content="Error: `id` is required for kill",
                    is_error=True,
                )
            # Distinguish three outcomes so callers can fence
            # idempotently without is_error noise:
            #   · unknown id → genuine error (typo / wrong session)
            #   · already terminal → no-op success (goal already met)
            #   · running → kill sent, success
            # Previously a kill against an already-finished agent
            # returned is_error=True, which both surprised operators
            # ("I just wanted to make sure it was stopped") and
            # propagated into agent traces as a failure to retry —
            # see session-mq67ogk0 where the parent killed a judge
            # that had already finished and the result confused the
            # next turn's reasoning.
            record = self._registry.get(sub_id)
            if record is None:
                return ToolResult(
                    call_id=call_id,
                    content=f"Unknown sub-agent: {sub_id}",
                    is_error=True,
                )
            if not record.is_running:
                terminal = record.state.name.lower() if record.state else "terminal"
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Sub-agent {sub_id} already in terminal state "
                        f"({terminal}) — kill is a no-op"
                    ),
                    is_error=False,
                )
            # The caller knows it stopped this one; a memo telling it so
            # would only wake it for nothing.
            record.cancel_origin = "parent"
            self._registry.kill(sub_id)
            return ToolResult(
                call_id=call_id,
                content=f"Kill signal sent to {sub_id}",
                is_error=False,
            )

        return ToolResult(
            call_id=call_id,
            content=f"Unknown action: {action}",
            is_error=True,
        )

    def _status(self, call_id: str, sub_id: str | None, *, legacy: bool) -> ToolResult:
        if sub_id:
            record = self._registry.get(sub_id)
            if record is None:
                return ToolResult(
                    call_id=call_id,
                    content=f"Unknown sub-agent: {sub_id}",
                    is_error=True,
                )
            body: dict[str, Any] = _row(record, preview_chars=2000)
            if record.artifact_path and not record.is_running:
                body["_hint"] = (
                    f"subagents result id={record.id} (or read_file "
                    f"{record.artifact_path}) for the full output"
                )
        else:
            running = [_row(r) for r in self._registry.list_all() if r.is_running]
            body = {"running": running, "count": len(running)}
        if legacy or (not sub_id) or (sub_id and body.get("state") == "running"):
            body["_note"] = _NO_WAIT_NOTE
        return ToolResult(
            call_id=call_id,
            content=json.dumps(body, indent=2),
            is_error=False,
        )
