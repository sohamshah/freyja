"""
Tool framework for the engine.

Provides:
- ToolDefinition: Schema for tool parameters
- Tool protocol: Interface for executable tools (async-native)
- ToolRegistry: Manages available tools with async/parallel execution
- ToolResultTruncator: Handles oversized tool results
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import tempfile
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from engine.types import AgentConfig, ContentBlock, ToolCall, ToolResult
from engine.tokenizer import count_tokens

if TYPE_CHECKING:
    from engine.permissions import HumanInteractionHandler

logger = logging.getLogger(__name__)


# ============================================================================
# Tool Definition
# ============================================================================

class ToolTier(str, Enum):
    """Visibility tier for a registered tool."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass
class ToolDefinition:
    """
    Definition of a tool's interface.

    Attributes:
        name: Unique tool name (must match ^[a-zA-Z0-9_-]+$ for Cerebras compatibility)
        description: Human-readable description for the model (detailed, for API schema)
        summary: Brief one-line description for system prompt listing
        parameters: JSON Schema for tool parameters
        tier: Tool tier (HOT/WARM/COLD) for progressive disclosure
        strict: Enable constrained decoding on providers that support it.
            Cerebras (zai-glm-4.7) enforces the schema via constrained decoding.
            OpenAI (gpt-4o+) enforces via strict JSON Schema mode.
            Fireworks and Anthropic ignore this flag.
            When strict=True, the parameters schema must comply with JSON Schema
            subset: additionalProperties=false on every object, all fields in
            `required`, no `pattern`/`minItems`/`maxItems`, no recursive refs.
    """

    name: str
    description: str
    summary: str
    parameters: dict[str, Any] = field(default_factory=dict)
    tier: ToolTier = field(default=ToolTier.HOT)
    strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass
class ToolCatalogEntry:
    """Registry metadata for a single tool."""

    tool: "Tool"
    tier: ToolTier = ToolTier.HOT
    summary_visible: bool = True
    schema_visible: bool = True


# ============================================================================
# Tool Protocol
# ============================================================================

@runtime_checkable
class Tool(Protocol):
    """Interface for executable tools (async-native)."""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Get the tool's definition."""
        ...

    @abstractmethod
    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """
        Execute the tool asynchronously.

        Args:
            call_id: Unique identifier for this tool call
            arguments: Tool arguments from the model

        Returns:
            ToolResult with the execution output
        """
        ...


# ============================================================================
# Tool Registry
# ============================================================================

class ToolRegistry:
    """
    Manages available tools with async-native execution.

    Provides tool registration, lookup, and async execution.
    """

    def __init__(
        self,
        permission_handler: "HumanInteractionHandler | None" = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._catalog: dict[str, ToolCatalogEntry] = {}
        self._permission_handler = permission_handler
        self._permission_lock = asyncio.Lock()

    def register(
        self,
        tool: Tool,
    ) -> None:
        """Register a tool."""
        name = tool.definition.name

        # Both permission members must be present together or absent together
        declares_permission_required = getattr(tool, "requires_permission", False)
        implements_permission_prompt = callable(getattr(tool, "permission_prompt", None))
        if declares_permission_required and not implements_permission_prompt:
            raise ValueError(
                f"Tool '{name}' sets requires_permission=True but does not "
                f"implement permission_prompt(). Both are required."
            )
        if implements_permission_prompt and not declares_permission_required:
            raise ValueError(
                f"Tool '{name}' implements permission_prompt() but does not "
                f"set requires_permission=True. Both are required."
            )

        if name in self._tools:
            logger.warning(f"Overwriting existing tool: {name}")
        resolved_tier = ToolTier(tool.definition.tier)
        summary_visible = resolved_tier != ToolTier.COLD
        schema_visible = resolved_tier == ToolTier.HOT
        self._tools[name] = tool
        self._catalog[name] = ToolCatalogEntry(
            tool=tool,
            tier=resolved_tier,
            summary_visible=summary_visible,
            schema_visible=schema_visible,
        )
        logger.debug("Registered tool: %s (tier=%s)", name, resolved_tier.value)

    def unregister(self, name: str) -> bool:
        """Unregister a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            self._catalog.pop(name, None)
            logger.debug(f"Unregistered tool: {name}")
            return True
        return False

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_catalog_entry(self, name: str) -> ToolCatalogEntry | None:
        """Get visibility metadata for a tool."""
        return self._catalog.get(name)

    def promote_tool(self, name: str) -> ToolDefinition | None:
        """Promote a deferred tool so its full schema becomes visible."""
        entry = self._catalog.get(name)
        if entry is None:
            return None
        entry.summary_visible = True
        entry.schema_visible = True
        return entry.tool.definition

    def list_definitions(self) -> list[ToolDefinition]:
        """Get definitions for tools whose full schema is currently visible."""
        return [
            entry.tool.definition
            for entry in self._catalog.values()
            if entry.schema_visible
        ]

    def list_all_definitions(self) -> list[ToolDefinition]:
        """Get definitions for ALL registered tools regardless of tier."""
        return [entry.tool.definition for entry in self._catalog.values()]

    def list_summaries(self) -> dict[str, str]:
        """Get prompt-visible tool summaries, including deferred tools."""
        return {
            entry.tool.definition.name: entry.tool.definition.summary
            for entry in self._catalog.values()
            if entry.summary_visible
        }

    def list_summary_tiers(self) -> dict[str, str]:
        """Get prompt-visible tools mapped to their current access tier."""
        tiers: dict[str, str] = {}
        for entry in self._catalog.values():
            if not entry.summary_visible:
                continue
            current_tier = ToolTier.HOT if entry.schema_visible else ToolTier.WARM
            tiers[entry.tool.definition.name] = current_tier.value
        return tiers

    def hidden_tool_count(self) -> int:
        """Count tools currently hidden from the base prompt."""
        return sum(1 for entry in self._catalog.values() if not entry.summary_visible)

    def list_names(self) -> list[str]:
        """Get names of all registered tools."""
        return list(self._tools.keys())

    # -----------------------------------------------------------------
    # PRIMARY: Async execution
    # -----------------------------------------------------------------

    async def execute(
        self,
        call: ToolCall,
        *,
        timeout: float | None = None,
        use_cache: bool = False,
    ) -> ToolResult:
        """
        Execute a tool call asynchronously.

        Args:
            call: The tool call to execute
            timeout: Optional timeout in seconds
            use_cache: Reserved for future use

        Returns:
            ToolResult with execution output
        """
        tool = self._tools.get(call.name)
        if tool is None:
            logger.error(f"Unknown tool: {call.name}")
            return ToolResult(
                call_id=call.id,
                content=f"Error: Unknown tool '{call.name}'",
                is_error=True,
            )

        # Permission check
        permission_prompt_fn = getattr(tool, "permission_prompt", None)
        if callable(permission_prompt_fn) and self._permission_handler is not None:
            permission_request = await permission_prompt_fn(call.arguments or {})
            if permission_request is not None:
                async with self._permission_lock:
                    permission_result = self._permission_handler.request_permission(
                        action=permission_request.prompt,
                        level=permission_request.level,
                        details=permission_request.details,
                    )
                    if asyncio.iscoroutine(permission_result):
                        user_response = await permission_result
                    else:
                        user_response = permission_result
                if not user_response.approved:
                    return ToolResult(
                        call_id=call.id,
                        content=json.dumps({
                            "denied": True,
                            "reason": f"User rejected: {permission_request.prompt}",
                        }),
                        is_error=False,
                    )

        try:
            if timeout:
                result = await asyncio.wait_for(
                    tool.execute(call.id, call.arguments),
                    timeout=timeout,
                )
            else:
                result = await tool.execute(call.id, call.arguments)

            logger.debug(f"Tool {call.name} executed successfully")
            return result

        except asyncio.TimeoutError:
            timeout_text = f"{timeout}s" if timeout else "(inner timeout)"
            logger.error(f"Tool {call.name} timed out after {timeout_text}")
            return ToolResult(
                call_id=call.id,
                content=f"Error: Timed out after {timeout_text}",
                is_error=True,
            )
        except Exception as e:
            logger.error(f"Tool {call.name} failed: {e}")
            return ToolResult(
                call_id=call.id,
                content=f"Error executing tool: {e}",
                is_error=True,
            )

    async def execute_parallel(
        self,
        calls: list[ToolCall],
        *,
        max_concurrent: int = 10,
        timeout: float | None = None,
        use_cache: bool = False,
    ) -> list[ToolResult]:
        """
        Execute multiple tool calls concurrently.

        Args:
            calls: List of tool calls to execute
            max_concurrent: Maximum concurrent executions (default 10)
            timeout: Optional timeout per tool in seconds
            use_cache: Reserved for future use

        Returns:
            List of ToolResults in the same order as input calls
        """
        if not calls:
            return []

        semaphore = asyncio.Semaphore(max_concurrent)

        async def with_semaphore(call: ToolCall) -> ToolResult:
            async with semaphore:
                return await self.execute(call, timeout=timeout, use_cache=use_cache)

        return await asyncio.gather(*[with_semaphore(c) for c in calls])

    # -----------------------------------------------------------------
    # COMPATIBILITY: Sync wrapper for legacy code
    # -----------------------------------------------------------------

    def execute_sync(
        self,
        call: ToolCall,
        *,
        timeout: float | None = None,
        use_cache: bool = False,
    ) -> ToolResult:
        """
        Synchronous execution wrapper for backwards compatibility.

        Use this only for sync code paths. Prefer execute() in async contexts.
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "execute_sync() called from async context. "
                "Use 'await execute()' instead."
            )
        except RuntimeError as e:
            if "no running event loop" not in str(e):
                raise
            return asyncio.run(
                self.execute(call, timeout=timeout, use_cache=use_cache)
            )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ============================================================================
# Simple Tool Implementation
# ============================================================================

class SimpleTool:
    """
    A simple tool implementation using a callable.

    Supports both sync and async handlers - automatically detects and wraps as needed.
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable,
        summary: str | None = None,
    ):
        self._definition = ToolDefinition(
            name=name,
            description=description,
            summary=summary or description.split("\n")[0][:500],
            parameters=parameters,
        )
        self._handler = handler
        self._is_async = asyncio.iscoroutinefunction(handler)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute the tool asynchronously."""
        try:
            if self._is_async:
                result = await self._handler(**arguments)
            else:
                # Run sync handler in thread pool to avoid blocking
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    functools.partial(self._handler, **arguments),
                )
            content = result if isinstance(result, str) else str(result)
            return ToolResult(call_id=call_id, content=content)
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: {e}",
                is_error=True,
            )


# ============================================================================
# Tool Result Truncation
# ============================================================================

class ToolResultTruncator:
    """
    Truncates oversized tool results using token-based limits.

    Uses tiktoken to count actual tokens and proportionally truncates
    content that exceeds the configured max_tool_result_tokens.

    Supports dynamic budgeting: callers can pass a context-aware max_tokens
    computed from the model's context window minus existing transcript and
    tool definition overhead (see compute_dynamic_budget).

    When truncation occurs, the full content is automatically written to a
    file so the agent can access it if needed.
    """

    # Fallback when file writing fails
    TRUNCATION_SUFFIX_NO_FILE = (
        "\n\n[Content truncated. "
        "Use filters or parameters to narrow results.]"
    )

    # Fraction of remaining context budget a single tool result may use.
    BUDGET_FRACTION = 0.5

    # Absolute minimum budget
    MIN_BUDGET_TOKENS = 4_000

    # Safety margin subtracted from context window
    SAFETY_MARGIN_TOKENS = 15_000

    # Provider serialization overhead multiplier
    TOOL_TOKEN_MULTIPLIER = 2.5

    def __init__(self, config: AgentConfig):
        self.max_tokens = config.max_tool_result_tokens
        self._output_dir = self._setup_output_dir()
        self._tool_tokens_cache: tuple[int, int] | None = None

    def _setup_output_dir(self) -> Path:
        """Set up directory for storing truncated tool outputs."""
        home_dir = Path.home() / ".freyja" / "truncated"
        try:
            home_dir.mkdir(parents=True, exist_ok=True)
            return home_dir
        except (OSError, PermissionError):
            return Path(tempfile.gettempdir()) / "freyja-truncated"

    def estimate_tool_definition_tokens(
        self,
        tool_definitions: list[ToolDefinition],
    ) -> int:
        """
        Estimate total tokens used by tool definitions in the API payload.

        Applies a multiplier to account for provider serialization overhead
        and tokenizer differences. Caches per tool count.
        """
        tool_count = len(tool_definitions)
        if self._tool_tokens_cache and self._tool_tokens_cache[0] == tool_count:
            return self._tool_tokens_cache[1]

        import json as _json

        raw_total = 0
        for defn in tool_definitions:
            text = defn.name + " " + defn.description + " " + _json.dumps(defn.parameters)
            raw_total += count_tokens(text) + 10
        total = int(raw_total * self.TOOL_TOKEN_MULTIPLIER)
        self._tool_tokens_cache = (tool_count, total)
        return total

    def compute_dynamic_budget(
        self,
        context_window: int,
        session_tokens: int,
    ) -> int:
        """
        Compute the maximum tokens a single tool result should use.

        Based on how much context is left after accounting for everything
        already in the session plus a safety margin.
        """
        remaining = (
            context_window
            - session_tokens
            - self.SAFETY_MARGIN_TOKENS
        )
        budget = int(remaining * self.BUDGET_FRACTION)
        budget = max(budget, self.MIN_BUDGET_TOKENS)
        budget = min(budget, self.max_tokens)
        return budget

    def _write_full_content(
        self,
        content: str,
        tool_name: str | None = None,
    ) -> str | None:
        """Write full content to a file and return the file path."""
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            tool_prefix = tool_name.replace("/", "_").replace(" ", "_") if tool_name else "tool"
            unique_suffix = datetime.now().strftime("%f")[:6]
            filename = f"{tool_prefix}_{timestamp}_{unique_suffix}.txt"
            file_path = self._output_dir / filename
            file_path.write_text(content, encoding="utf-8")
            logger.debug(f"Wrote truncated content to {file_path}")
            return str(file_path)
        except (OSError, PermissionError) as e:
            logger.warning(f"Failed to write truncated content to file: {e}")
            return None

    @staticmethod
    def _detect_json_structure(content: str) -> str | None:
        """Attempt to parse content as JSON and return a structural summary."""
        import json as _json

        stripped = content.lstrip()
        if not stripped or stripped[0] not in ("{", "["):
            return None
        if len(content) > 5_000_000:
            return None

        try:
            data = _json.loads(content)
        except (_json.JSONDecodeError, ValueError):
            return None

        if isinstance(data, list):
            length = len(data)
            if length > 0 and isinstance(data[0], dict):
                keys = sorted(data[0].keys())
                return f"JSON array with {length:,} items. Keys per item: {', '.join(keys)}"
            return f"JSON array with {length:,} items"

        if isinstance(data, dict):
            keys = sorted(data.keys())
            if len(keys) > 15:
                shown = ", ".join(keys[:15])
                return f"JSON object with {len(keys)} keys: {shown}, ..."
            return f"JSON object with keys: {', '.join(keys)}"

        return None

    def _build_truncation_suffix(
        self,
        content: str,
        tokens: int,
        limit: int,
        file_path: str | None,
    ) -> str:
        """Build a context-rich truncation suffix."""
        parts: list[str] = ["\n\n"]

        size_info = (
            f"[Content truncated -- showing ~{limit:,} of {tokens:,} tokens "
            f"({len(content):,} chars)."
        )
        if file_path:
            size_info += f" Full output saved to: {file_path}]"
        else:
            size_info += "]"
        parts.append(size_info)

        structure = self._detect_json_structure(content)
        if structure:
            parts.append(f"[Structure: {structure}]")

        if file_path:
            if structure and "JSON array" in structure:
                parts.append(
                    f"[Tip: use bash with jq to extract specific fields, e.g.: "
                    f"jq '.[] | {{id, name}}' {file_path}]"
                )
            elif structure and "JSON object" in structure:
                parts.append(
                    f"[Tip: use bash with jq to extract specific keys, e.g.: "
                    f"jq '.keyName' {file_path}]"
                )
            else:
                parts.append(
                    f"[Tip: use grep/head/tail to extract sections from the full file, e.g.: "
                    f"grep -i 'keyword' {file_path}]"
                )

        return "\n".join(parts)

    def truncate_if_needed(
        self,
        content: str,
        max_tokens: int | None = None,
        tool_name: str | None = None,
    ) -> tuple[str, bool]:
        """
        Truncate content if it exceeds the token limit.

        Uses proportional cutting: if content is 2x over budget,
        keep ~50% of characters (with safety margin).

        When truncation occurs, the full content is written to a file
        and the file path is included in the truncation message.

        Returns:
            Tuple of (content, was_truncated)
        """
        limit = max_tokens if max_tokens is not None else self.max_tokens
        tokens = count_tokens(content)

        if tokens <= limit:
            return content, False

        file_path = self._write_full_content(content, tool_name)

        ratio = limit / tokens
        cut_point = int(len(content) * ratio * 0.9)

        last_newline = content.rfind("\n", 0, cut_point)
        if last_newline > cut_point * 0.8:
            cut_point = last_newline

        if file_path:
            suffix = self._build_truncation_suffix(content, tokens, limit, file_path)
        else:
            suffix = self.TRUNCATION_SUFFIX_NO_FILE

        truncated = content[:cut_point] + suffix

        logger.debug(
            f"Truncated tool result from {tokens} to ~{count_tokens(truncated)} tokens"
        )
        return truncated, True

    def truncate_tool_result(
        self,
        result: ToolResult,
        max_tokens: int | None = None,
        tool_name: str | None = None,
    ) -> tuple[ToolResult, bool]:
        """Truncate a tool result if oversized."""
        if isinstance(result.content, str):
            content, was_truncated = self.truncate_if_needed(
                result.content, max_tokens, tool_name
            )
            if was_truncated:
                return ToolResult(
                    call_id=result.call_id,
                    content=content,
                    is_error=result.is_error,
                ), True
            return result, False

        if isinstance(result.content, list):
            new_content: list[ContentBlock] = []
            any_truncated = False

            for block in result.content:
                if hasattr(block, "text"):
                    text, was_truncated = self.truncate_if_needed(
                        block.text, max_tokens, tool_name
                    )
                    if was_truncated:
                        any_truncated = True
                        new_block = type(block)(**{**block.__dict__, "text": text})
                        new_content.append(new_block)
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)

            if any_truncated:
                return ToolResult(
                    call_id=result.call_id,
                    content=new_content,
                    is_error=result.is_error,
                ), True

        return result, False
