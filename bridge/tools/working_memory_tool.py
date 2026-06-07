"""`working_memory` — the agent's structured, durable, semantic memory.

The authoring surface for Milestone 2 (``bridge/working_memory.py``). Distinct
from the sibling tools:
  • `session_memory` — a flat markdown scratchpad.
  • `artifacts` / the runtime write-ledger — *what files changed* (ground truth).
  • `working_memory` — *what the work means*, organized by entity (workstream /
    decision / finding / open thread / artifact note), so it survives compaction
    as queryable, self-authored state rather than lossy prose.

The agent supplies the high-level intent only it knows; file facts are seeded
from the ledger automatically, so this never has to restate what you wrote.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier
from bridge.working_memory import ENTITY_TYPES, WorkingMemory

logger = logging.getLogger(__name__)

_FIELD_KEYS = ("title", "text", "request", "rationale", "source", "status", "path", "note")


class WorkingMemoryTool:
    ACTIONS = ("read", "upsert", "resolve")

    def __init__(
        self,
        *,
        memory: WorkingMemory,
        get_ledger_effects: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._memory = memory
        self._get_ledger_effects = get_ledger_effects

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="working_memory",
            summary="Structured, durable memory of your work organized by entity",
            tier=ToolTier.WARM,
            description=(
                "Maintain a structured, compaction-proof record of what you're "
                "doing and why — organized by entity, not flat prose. Use it to "
                "keep your own high-level intent retrievable after the "
                "conversation is condensed.\n\n"
                "ACTIONS:\n"
                "  • read — render the current structured memory (your "
                "workstreams, decisions, findings, open threads, plus the files "
                "the runtime recorded you changing).\n"
                "  • upsert — create or update an entity. Pass `type` and "
                "`fields`; pass `id` to update an existing one.\n"
                "  • resolve — mark an open_thread resolved or a workstream done "
                "(pass `id`).\n\n"
                "ENTITY TYPES (`type`): workstream (the task you're on: "
                "title+request), decision (title+rationale), finding "
                "(text+source), open_thread (text), artifact_note (path+note — a "
                "semantic note over a file you changed). Link a child to its "
                "workstream by passing `fields.workstream_id`.\n\n"
                "WHEN TO USE: start a workstream when you take on a task; record "
                "a decision when you choose an approach; record a finding when "
                "you learn something worth keeping; add an open_thread for "
                "follow-ups. You do NOT need to restate files you wrote — those "
                "are already tracked. This is for the reasoning the runtime "
                "can't capture."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(self.ACTIONS)},
                    "type": {
                        "type": "string",
                        "enum": list(ENTITY_TYPES),
                        "description": "Entity type for upsert.",
                    },
                    "id": {
                        "type": "string",
                        "description": "Entity id for update/resolve (returned by a prior upsert).",
                    },
                    "fields": {
                        "type": "object",
                        "description": (
                            "Entity fields: title, text, request, rationale, "
                            "source, status, path, note, workstream_id."
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in self.ACTIONS:
            return _err(call_id, f"Unknown action `{action}`. Valid: {', '.join(self.ACTIONS)}")
        try:
            if action == "read":
                return self._read(call_id)
            if action == "upsert":
                return self._upsert(call_id, arguments)
            if action == "resolve":
                return self._resolve(call_id, arguments)
        except Exception as exc:  # noqa: BLE001
            logger.exception("working_memory failed")
            return _err(call_id, f"working_memory {action} failed: {exc}")
        return _err(call_id, "unreachable")

    def _read(self, call_id: str) -> ToolResult:
        effects = None
        if self._get_ledger_effects is not None:
            try:
                effects = self._get_ledger_effects()
            except Exception:  # noqa: BLE001
                effects = None
        rendered = self._memory.render(ledger_effects=effects)
        return _ok(call_id, {
            "action": "read",
            "rendered": rendered or "(empty)",
            "entities": self._memory.all(),
        })

    def _upsert(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        etype = str(arguments.get("type") or "").strip()
        if etype not in ENTITY_TYPES:
            return _err(call_id, f"upsert requires `type` in {', '.join(ENTITY_TYPES)}")
        raw_fields = arguments.get("fields")
        fields: dict[str, Any] = {}
        if isinstance(raw_fields, dict):
            for k in _FIELD_KEYS:
                if raw_fields.get(k) is not None:
                    fields[k] = raw_fields[k]
            # Map snake_case workstream link to the stored camelCase key.
            ws = raw_fields.get("workstream_id") or raw_fields.get("workstreamId")
            if ws:
                fields["workstreamId"] = ws
        entity_id = str(arguments.get("id") or "").strip() or None
        ent = self._memory.upsert(type=etype, fields=fields, entity_id=entity_id)
        if ent is None:
            return _err(call_id, "upsert failed (invalid type or fields)")
        return _ok(call_id, {"action": "upsert", "entity": ent})

    def _resolve(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        entity_id = str(arguments.get("id") or "").strip()
        if not entity_id:
            return _err(call_id, "resolve requires `id`")
        ent = self._memory.resolve(entity_id)
        if ent is None:
            return _err(call_id, f"no entity with id `{entity_id}`")
        return _ok(call_id, {"action": "resolve", "entity": ent})


def _err(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)


def _ok(call_id: str, payload: dict[str, Any]) -> ToolResult:
    return ToolResult(call_id=call_id, content=json.dumps(payload, indent=2))
