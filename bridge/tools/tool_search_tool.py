"""Tool discovery: load a deferred tool's schema, then call it.

A deferred tool (listed to the model by name and summary only) is never added
to the request's tool list mid-session. The tool list sits at the front of the
cached prompt, so changing it makes the provider re-read the whole
conversation: about $8 at 130k tokens on GPT-6 Astra Ultrafast. Instead,
``tool_search`` returns the schema as a tool result, which is the newest part
of the conversation, and ``call_tool`` runs the tool by name.
"""

from __future__ import annotations

import json
from difflib import get_close_matches
from typing import Any

from bridge.tools.base import ToolCall, ToolDefinition, ToolRegistry, ToolResult, ToolTier

CALL_TOOL_NAME = "call_tool"


class ToolSearchTool:
    """Return the full schema for a tool that is not in the tool list."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="tool_search",
            summary="Load full schema for a tool",
            tier=ToolTier.HOT,
            description=(
                "Load the full schema for a tool by exact name. "
                "Use this before calling any tool that is listed by name and summary only, "
                "or any tool revealed by a skill or workflow. "
                f"It returns the tool's parameters; then run the tool with `{CALL_TOOL_NAME}`. "
                "Tools that are already in your tool list are called directly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Exact tool name to load",
                    },
                },
                "required": ["tool_name"],
            },
        )

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Normalize a tool name by lowercasing and stripping non-alphanumeric characters."""
        return "".join(ch for ch in name.lower() if ch.isalnum())

    def _find_close_matches(self, tool_name: str) -> list[str]:
        """Return up to 5 tool names that closely match *tool_name*.

        Matches are ranked by: exact-prefix first, then substring, then
        fuzzy (difflib) similarity. All comparisons use normalized names.
        """
        visible = self._registry.list_summaries()
        query = self._normalize_tool_name(tool_name)
        if not query:
            return []

        normalized_to_names: dict[str, list[str]] = {}
        for name in visible:
            normalized = self._normalize_tool_name(name)
            normalized_to_names.setdefault(normalized, []).append(name)

        ranked: list[str] = []
        seen: set[str] = set()

        def add_matches(normalized_names: list[str]) -> None:
            for normalized_name in normalized_names:
                for original_name in sorted(normalized_to_names.get(normalized_name, [])):
                    if original_name not in seen:
                        ranked.append(original_name)
                        seen.add(original_name)

        add_matches(
            sorted(
                normalized_name
                for normalized_name in normalized_to_names
                if normalized_name.startswith(query)
            )
        )
        add_matches(
            sorted(
                normalized_name
                for normalized_name in normalized_to_names
                if query in normalized_name and not normalized_name.startswith(query)
            )
        )
        add_matches(get_close_matches(query, list(normalized_to_names), n=5, cutoff=0.6))
        return ranked[:5]

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Look up a tool by exact name and promote its full schema into the session."""
        tool_name = str(arguments.get("tool_name", "")).strip()
        if not tool_name:
            return ToolResult(
                call_id=call_id,
                content="Error: tool_name is required",
                is_error=True,
            )

        entry = self._registry.get_catalog_entry(tool_name)
        if entry is None:
            matches = self._find_close_matches(tool_name)
            if matches:
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"No exact tool named '{tool_name}'. Close matches: "
                        + ", ".join(matches)
                    ),
                    is_error=True,
                )
            return ToolResult(
                call_id=call_id,
                content=f"No visible tool named '{tool_name}'.",
                is_error=True,
            )

        if entry.schema_visible:
            return ToolResult(
                call_id=call_id,
                content=f"Tool '{tool_name}' is already in your tool list. Call it directly.",
            )
        definition = entry.tool.definition
        schema = json.dumps(definition.parameters, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(
            call_id=call_id,
            content=(
                f"Tool '{tool_name}' is ready. It stays out of your tool list; run it with "
                f'`{CALL_TOOL_NAME}(tool_name="{tool_name}", arguments={{...}})`, '
                "where arguments match this schema.\n"
                f"Description: {definition.description}\n"
                f"Parameters (JSON Schema): {schema}"
            ),
        )


class CallToolTool:
    """Run a tool that ``tool_search`` loaded, by name.

    Dispatch goes through ``registry.execute``, the same path as a direct call,
    so the tool's permission prompt still applies, and a tool the registry
    does not run (stripped for a gateway session, refused in a read-only fork)
    is still refused.
    """

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=CALL_TOOL_NAME,
            summary="Run a tool loaded with tool_search",
            tier=ToolTier.HOT,
            description=(
                "Run a tool whose schema tool_search returned. Pass the tool's exact name "
                "and its arguments. The tool runs exactly as if called directly, including "
                "any permission prompt, and its result is returned as this call's result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Exact tool name, as tool_search returned it",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "The tool's arguments, matching the schema tool_search returned",
                    },
                },
                "required": ["tool_name", "arguments"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        tool_name = str(arguments.get("tool_name", "")).strip()
        if not tool_name:
            return ToolResult(call_id=call_id, content="Error: tool_name is required", is_error=True)
        if tool_name in (CALL_TOOL_NAME, "tool_search"):
            return ToolResult(
                call_id=call_id,
                content=f"Error: call {tool_name} directly, not through {CALL_TOOL_NAME}.",
                is_error=True,
            )
        tool_args = arguments.get("arguments")
        if tool_args is None:
            tool_args = {}
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args) if tool_args.strip() else {}
            except json.JSONDecodeError as exc:
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: arguments is not valid JSON: {exc}",
                    is_error=True,
                )
        if not isinstance(tool_args, dict):
            return ToolResult(
                call_id=call_id,
                content="Error: arguments must be an object",
                is_error=True,
            )
        if self._registry.get(tool_name) is None:
            matches = ToolSearchTool(self._registry)._find_close_matches(tool_name)  # noqa: SLF001
            hint = f" Close matches: {', '.join(matches)}" if matches else ""
            return ToolResult(
                call_id=call_id,
                content=f"Error: no tool named '{tool_name}'.{hint}",
                is_error=True,
            )
        return await self._registry.execute(
            ToolCall(id=call_id, name=tool_name, arguments=tool_args)
        )
