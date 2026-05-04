"""Tool discovery helper for loading tool schemas on demand."""

from __future__ import annotations

from difflib import get_close_matches
from typing import Any

from bridge.tools.base import ToolDefinition, ToolRegistry, ToolResult, ToolTier


class ToolSearchTool:
    """Load the full schema for a tool that is not yet fully loaded."""

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
                "When the tool is found, its schema is loaded into the next model turn "
                "so you can call it directly."
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

        already_loaded = entry.schema_visible
        definition = self._registry.promote_tool(tool_name)
        if definition is None:
            return ToolResult(
                call_id=call_id,
                content=f"Unable to load schema for '{tool_name}'.",
                is_error=True,
            )

        status = "already loaded" if already_loaded else "loaded and ready on the next turn"
        return ToolResult(
            call_id=call_id,
            content=(
                f"Tool '{tool_name}' {status}.\n"
                "Call the tool by its exact name on your next turn."
            ),
        )
