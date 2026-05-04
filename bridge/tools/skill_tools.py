"""Skill discovery and loading tools."""

from __future__ import annotations

import inspect
from difflib import get_close_matches
from typing import Any, Awaitable, Callable

from bridge.knowledge.models import SkillRecord
from bridge.knowledge.skill_store import SkillStore
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

SkillEventCallback = Callable[[SkillRecord, str], Awaitable[None] | None]


class ListSkillsTool:
    def __init__(self, store: SkillStore, on_skill_event: SkillEventCallback | None = None):
        self._store = store
        self._on_skill_event = on_skill_event

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_skills",
            summary="List available skills",
            tier=ToolTier.WARM,
            description=(
                "List the available skills by metadata. Use this when you need to "
                "inspect what procedural knowledge is available before deciding "
                "whether to call load_skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of skills to list. Defaults to 20.",
                    }
                },
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        limit = int(arguments.get("limit") or 20)
        skills = self._store.list_skills()[: max(1, min(limit, 100))]
        if not skills:
            return ToolResult(call_id=call_id, content="No skills found.")
        for skill in skills:
            await _maybe_emit(self._on_skill_event, skill, "listed")
        lines = [f"Found {len(skills)} skill(s):"]
        for skill in skills:
            lines.append(
                f"- {skill.name} [{skill.skill_type}/{skill.confidence}]: {skill.description}"
            )
        return ToolResult(call_id=call_id, content="\n".join(lines))


class SearchSkillsTool:
    def __init__(self, store: SkillStore, on_skill_event: SkillEventCallback | None = None):
        self._store = store
        self._on_skill_event = on_skill_event

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_skills",
            summary="Search available skills by topic, trigger, tag, or error text",
            tier=ToolTier.HOT,
            description=(
                "Search Freyja's file-backed skill library. Use this when the "
                "available skill index does not make the right skill obvious. "
                "Returns metadata only; call load_skill(name) for full instructions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic, task, tool, file type, or error text to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches. Defaults to 8.",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(call_id=call_id, content="Error: query is required", is_error=True)
        limit = int(arguments.get("limit") or 8)
        matches = self._store.search(query, limit=max(1, min(limit, 20)))
        if not matches:
            return ToolResult(call_id=call_id, content=f"No skills found for: {query}")
        for skill, _score, reason in matches:
            await _maybe_emit(self._on_skill_event, skill, f"search match: {reason}")
        lines = [f"Found {len(matches)} skill match(es) for: {query}"]
        for skill, score, reason in matches:
            suffix = f" via {reason}" if reason else ""
            lines.append(
                f"- {skill.name} [{skill.skill_type}/{skill.confidence}, score={score}]{suffix}: {skill.description}"
            )
        lines.append("")
        lines.append("Call load_skill(name) to load full instructions.")
        return ToolResult(call_id=call_id, content="\n".join(lines))


class LoadSkillTool:
    def __init__(self, store: SkillStore, on_skill_event: SkillEventCallback | None = None):
        self._store = store
        self._on_skill_event = on_skill_event

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="load_skill",
            summary="Load full instructions for a skill by exact name",
            tier=ToolTier.HOT,
            description=(
                "Load the full markdown instructions for a skill by exact name. "
                "Use this after identifying a relevant skill from the prompt index "
                "or search_skills results. Loaded skills may later be pruned from "
                "context and can be reloaded by calling this again."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name exactly as shown in the available skills index.",
                    },
                },
                "required": ["name"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return ToolResult(call_id=call_id, content="Error: name is required", is_error=True)
        skill, content = self._store.load(name)
        if skill is None:
            names = [s.name for s in self._store.list_skills()]
            close = get_close_matches(name, names, n=5, cutoff=0.5)
            hint = f" Close matches: {', '.join(close)}." if close else ""
            return ToolResult(call_id=call_id, content=f"Skill '{name}' not found.{hint}", is_error=True)
        updated = self._store.record_load(skill)
        await _maybe_emit(self._on_skill_event, updated, "loaded")
        return ToolResult(call_id=call_id, content=content)


async def _maybe_emit(
    callback: SkillEventCallback | None,
    skill: SkillRecord,
    reason: str,
) -> None:
    if callback is None:
        return
    result = callback(skill, reason)
    if inspect.isawaitable(result):
        await result
