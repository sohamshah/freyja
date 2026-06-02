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


class ProposeSkillTool:
    """The skill-drafter sub-agent calls this to publish a candidate
    awaiting operator approval.

    Persists the candidate to ``~/.freyja/skills/.candidates/<id>.yaml``
    and emits the bridge-level ``skill_candidate`` event so the desktop
    shows a SkillToast / Slack delivers a Block Kit card. The operator
    decides via the existing approve / edit / discard flow.

    Each call creates a NEW candidate. The drafter can be re-engaged
    (it's a normal sub-agent session, the operator can chat with it) and
    call propose_skill again to publish a refined draft — the operator
    then sees both in the queue and picks the one they want.

    Skills Guard runs server-side; a ``dangerous`` verdict refuses to
    publish (the candidate lands in ``.rejected/`` for the negative
    library, but the operator sees no toast and the tool reports an
    error to the drafter so it can revise).
    """

    def __init__(self, *, session_id: str, source_turn_id: str = "", drafter_model: str = "") -> None:
        # The session id we record on the candidate is the PARENT
        # session's id (the one the drafter was spawned to review),
        # not the drafter sub-agent's id — that way the candidate
        # provenance points back at the conversation the drafter was
        # reviewing, which is what the operator cares about for
        # promote/discard decisions.
        self._session_id = session_id
        self._source_turn_id = source_turn_id
        self._drafter_model = drafter_model

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="propose_skill",
            summary="Publish a skill candidate to the operator for approval",
            tier=ToolTier.WARM,
            description=(
                "Publish a skill candidate awaiting operator approval. The "
                "operator sees a SkillToast (desktop) / Block Kit card (Slack) "
                "with promote / edit / discard buttons. Each call creates a "
                "new candidate. Use after you have reviewed the conversation, "
                "called load_skill for any same-named existing skill, and "
                "decided what to propose. Skills Guard runs on the content; "
                "if it flags the body as dangerous the publish refuses."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "lowercase-hyphenated skill name (letters, digits, hyphens). "
                            "If overwriting an existing skill, use the existing name verbatim. "
                            "Short, verb-led, ideally namespaced when it improves triggering "
                            "(e.g. gh-address-comments, ema-release-ops)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "The skill's primary triggering surface — ALL 'when to use' "
                            "guidance goes here, not in the body. Mention both what the skill "
                            "does AND the specific contexts/triggers/error strings/tool names "
                            "that should activate it. The body is only loaded after a trigger; "
                            "'When to Use This Skill' sections in the body are useless."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Markdown body of the skill. Imperative voice. Concise — assume "
                            "the consuming agent is smart and only add what it doesn't already "
                            "know. Under 500 lines. Match the level of specificity to the "
                            "task's fragility: text for many-valid-approaches, pseudocode for "
                            "preferred-pattern, exact scripts for fragile/error-prone."
                        ),
                    },
                    "skill_type": {
                        "type": "string",
                        "enum": ["build", "guard", "reference", "workflow"],
                        "description": "Classification of the skill. Defaults to 'build'.",
                    },
                    "triggers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short trigger phrases — error messages, tool names, file patterns, "
                            "etc. — that should activate this skill. Complement the description, "
                            "don't duplicate it."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for grouping (e.g. release, deploy, ema).",
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "1-3 sentences explaining what you learned from the conversation "
                            "that justifies this candidate. Surfaced to the operator on the "
                            "SkillToast detail view so they can sanity-check the framing."
                        ),
                    },
                },
                "required": ["name", "description", "body"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        # Lazy import — publish.py imports from drafter.py via the
        # diff-stats helper; importing it at module load could pull
        # too much before the registry is ready.
        from bridge.knowledge.learning.publish import publish_candidate
        try:
            from bridge.freyja_bridge import emit as _emit
        except Exception:  # noqa: BLE001
            _emit = None
        candidate_id, verdict, error = publish_candidate(
            name=str(arguments.get("name") or "").strip(),
            description=str(arguments.get("description") or "").strip(),
            body=str(arguments.get("body") or ""),
            skill_type=str(arguments.get("skill_type") or "build").strip() or "build",
            triggers=list(arguments.get("triggers") or []),
            tags=list(arguments.get("tags") or []),
            rationale=str(arguments.get("rationale") or "").strip(),
            source_session_id=self._session_id,
            source_turn_id=self._source_turn_id,
            drafter_model=self._drafter_model,
            emit_fn=_emit,
        )
        if error and not candidate_id:
            return ToolResult(call_id=call_id, content=f"Refused: {error}", is_error=True)
        # Even on guard-caution we return success — the operator gets
        # the toast and can decide. The verdict is reported so the
        # drafter knows the guard flagged something it should perhaps
        # explain in its rationale.
        msg = (
            f"Published candidate {candidate_id} (guard={verdict}). "
            "Operator will see a SkillToast for approve / edit / discard."
        )
        return ToolResult(call_id=call_id, content=msg)
