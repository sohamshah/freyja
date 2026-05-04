"""
Memory tools for the Freyja bridge.

Provides a tool for persisting user preferences:

1. record_user_preference - Personal user preferences (style, tone, workflow)
   Stored in MEMORY.md, loaded into future sessions.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from bridge.knowledge.memory_store import MemoryStore
from bridge.knowledge.models import MemoryItem
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)


class RecordUserPreferenceTool:
    """
    Persist user preferences to MEMORY.md.

    This tool is exclusively for storing personal user preferences that
    should be remembered across sessions - communication style, tone,
    formatting preferences, workflow preferences, etc.
    """

    def __init__(
        self,
        workspace: Path | str | None = None,
        memory_store: MemoryStore | None = None,
        on_memory_updated: Callable[[MemoryItem, str], Awaitable[None] | None] | None = None,
    ):
        """
        Initialize the remember tool.

        Args:
            workspace: The workspace directory where MEMORY.md will be stored.
                      Defaults to current working directory.
        """
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.memory_store = memory_store or MemoryStore(self.workspace)
        self.on_memory_updated = on_memory_updated

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="record_user_preference",
            summary="Record user preferences for future sessions",
            tier=ToolTier.WARM,
            description="""Record a user preference for future sessions.

PURPOSE: Store personal user preferences that should persist across sessions. This is
exclusively for understanding HOW the user wants to work, not WHAT was learned technically.

WHAT TO REMEMBER:
- Communication style: "User prefers concise responses without excessive explanation"
- Tone preferences: "User likes direct feedback, no sugarcoating"
- Formatting preferences: "User prefers bullet points over paragraphs"
- Code style: "User prefers functional style over OOP", "Always use type hints"
- Workflow preferences: "User likes to see the plan before execution"
- Review preferences: "User wants PR descriptions to be detailed"
- Domain context: "User is a senior engineer, skip basic explanations"
- Tool preferences: "User prefers vim keybindings", "Use pytest not unittest"

SIGNALS TO WATCH FOR:
- Explicit: "I prefer...", "Always...", "Don't...", "I like when you..."
- Implicit: User consistently edits your responses a certain way
- Corrections: "That's too verbose", "Be more specific", "Skip the intro"
- Frustration: "You keep doing X, please stop"

EXAMPLES:
- "User prefers short commit messages (50 char max)"
- "User wants code examples before explanations"
- "User dislikes emoji in responses"
- "User prefers snake_case for Python, camelCase for TypeScript"
- "User is color-blind, avoid red/green distinctions in output"
- "User works in Pacific timezone"
- "User prefers aggressive refactoring over incremental changes" """,
            parameters={
                "type": "object",
                "properties": {
                    "preference": {
                        "type": "string",
                        "description": "The user preference to remember. Be specific and actionable. Start with 'User prefers...', 'User likes...', 'User dislikes...', or similar.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "style",
                            "tone",
                            "formatting",
                            "workflow",
                            "code",
                            "communication",
                            "other",
                        ],
                        "description": "Category of preference: 'style' (communication style), 'tone' (formality, directness), 'formatting' (output format preferences), 'workflow' (how they like to work), 'code' (coding preferences), 'communication' (how to interact), 'other' (anything else).",
                    },
                },
                "required": ["preference"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute memory write asynchronously."""
        loop = asyncio.get_running_loop()
        result, item = await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )
        if item is not None and self.on_memory_updated is not None:
            emitted = self.on_memory_updated(item, "record_user_preference")
            if inspect.isawaitable(emitted):
                await emitted
        return result

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> tuple[ToolResult, MemoryItem | None]:
        """Sync implementation for executor."""
        preference = arguments.get("preference", "").strip()
        category = arguments.get("category", "").strip() or "other"

        if not preference:
            return (
                ToolResult(
                    call_id=call_id,
                    content="Error: preference is required",
                    is_error=True,
                ),
                None,
            )

        try:
            item = self.memory_store.record_preference(preference, category)
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Remembered: {preference[:100]}{'...' if len(preference) > 100 else ''}",
                ),
                item,
            )

        except PermissionError:
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Error: Permission denied writing memory files in {self.workspace}",
                    is_error=True,
                ),
                None,
            )
        except Exception as e:
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Error writing memory: {e}",
                    is_error=True,
                ),
                None,
            )
