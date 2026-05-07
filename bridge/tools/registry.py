"""
Desktop tool registry assembly.

Owns the single function that wires all standalone tools into a
ToolRegistry for the Freyja bridge. Keep this file boring: it's the one
place where tools are listed, and it should be easy to add/remove.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from bridge.tools.base import ToolRegistry
from bridge.tools.bash_tool import BashTool
from bridge.tools.browser_tools import BrowserExecuteJsTool, BrowserScreenshotTool
from bridge.tools.file_tools import (
    EditFileTool,
    EditJsonTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from bridge.tools.memory_tools import RecordUserPreferenceTool
from bridge.tools.search_tools import GlobTool, GrepTool
from bridge.tools.skill_tools import ListSkillsTool, LoadSkillTool, SearchSkillsTool
from bridge.tools.sub_agent_registry import SubAgentRegistry
from bridge.tools.sub_agent_tool import SubAgentSpec, SubAgentTool
from bridge.tools.subagents_tool import SubAgentsTool
from bridge.tools.tool_search_tool import ToolSearchTool

# Computer-use tools are imported lazily below — they pull in
# `freyja_native` which may not be built in every environment.

logger = logging.getLogger(__name__)


def build_desktop_registry(
    *,
    workspace: Path | str,
    include_bash: bool = True,
    include_web: bool = True,
    include_file_write: bool = True,
    include_subagents: bool = True,
    include_computer: bool = False,
    computer_session_id: str = "",
    computer_cancel_event: Any | None = None,
    permission_handler: Any | None = None,
    excluded: frozenset[str] = frozenset(),
    subagent_registry: SubAgentRegistry | None = None,
    subagent_provider_factory: Any | None = None,
    subagent_model: str = "claude-sonnet-4-6",
    subagent_reasoning_level: str = "auto",
    subagent_emit: Any | None = None,
    subagent_parent_session_id: str = "",
    subagent_wrap_registry: Any | None = None,
    message_bus: Any | None = None,
    memory_store: Any | None = None,
    skill_store: Any | None = None,
    on_memory_updated: Any | None = None,
    on_skill_event: Any | None = None,
) -> ToolRegistry:
    """Construct a ToolRegistry containing all desktop tools.

    Parameters
    ----------
    workspace:
        Working directory for file operations (read_file, write_file, glob,
        grep, bash default cwd, record_user_preference MEMORY.md location).
    include_bash:
        Register BashTool. Off-by-default for fully read-only sessions.
    include_web:
        Register WebSearchTool / WebFetchTool if the `parallel` SDK is
        importable and PARALLEL_API_KEY is set.
    include_file_write:
        Register WriteFileTool, EditFileTool, EditJsonTool. If False, only
        read-only file operations are available.
    permission_handler:
        Optional HumanInteractionHandler subclass. If None, no prompting
        happens — all tools run in permissive mode.
    excluded:
        Tool names to skip after the default set has been assembled.
    """
    workspace_path = Path(workspace).expanduser().resolve()

    registry = ToolRegistry(permission_handler=permission_handler)

    tools: list[Any] = []

    # Read-only file + search tools — always safe
    tools.append(ReadFileTool())
    tools.append(ListDirectoryTool())
    tools.append(GlobTool())
    tools.append(GrepTool())

    # Mutating file tools — behind a flag
    if include_file_write:
        tools.append(WriteFileTool())
        tools.append(EditFileTool())
        tools.append(EditJsonTool())

    # Memory (writes to MEMORY.md + structured JSONL)
    tools.append(
        RecordUserPreferenceTool(
            workspace=workspace_path,
            memory_store=memory_store,
            on_memory_updated=on_memory_updated,
        )
    )

    # Skills (file-backed progressive disclosure)
    if skill_store is not None:
        tools.append(SearchSkillsTool(skill_store, on_skill_event=on_skill_event))
        tools.append(LoadSkillTool(skill_store, on_skill_event=on_skill_event))
        tools.append(ListSkillsTool(skill_store, on_skill_event=on_skill_event))

    # Bash — permission-gated
    if include_bash:
        tools.append(BashTool(working_dir=str(workspace_path)))

    # Browser CDP tools — require browser launched with --remote-debugging-port
    tools.append(BrowserExecuteJsTool())
    tools.append(BrowserScreenshotTool())

    # Web tools — only if SDK + key available
    if include_web:
        try:
            from bridge.tools.web_tools import (
                WebFetchTool,
                WebSearchTool,
                WebTaskTool,
            )

            if os.environ.get("PARALLEL_API_KEY"):
                tools.append(WebSearchTool())
                tools.append(WebFetchTool())
                tools.append(WebTaskTool())
            else:
                logger.info(
                    "web tools skipped: PARALLEL_API_KEY not set",
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("web tools skipped: %s", exc)

    # Meta tool — lets the model load WARM-tier schemas on demand
    tools.append(ToolSearchTool(registry))

    # Sub-agents (must be wired last so they see the full parent tool set)
    sub_registry = subagent_registry or SubAgentRegistry()
    sub_spec: SubAgentSpec | None = None
    if include_subagents and subagent_provider_factory is not None:
        async def _noop(_: dict[str, Any]) -> None:
            return None

        sub_spec = SubAgentSpec(
            parent_workspace=str(workspace_path),
            parent_model=subagent_model,
            build_provider=subagent_provider_factory,
            parent_registry=registry,
            registry=sub_registry,
            emit_event=subagent_emit or _noop,
            parent_reasoning_level=subagent_reasoning_level,
            parent_session_id=subagent_parent_session_id,
            wrap_registry=subagent_wrap_registry,
            message_bus=message_bus,
        )
        tools.append(SubAgentTool(sub_spec))
        tools.append(SubAgentsTool(sub_registry))

    # Computer-use tools: atomic primitives for the parent + a `computer_use`
    # sub-agent tool that runs the whole observe→act loop in a child session.
    # All gated on `include_computer` (settings toggle) AND the native Rust
    # extension being importable.
    if include_computer:
        try:
            import asyncio

            from bridge.tools.computer_tools import (
                ComputerToolSpec,
                build_computer_tools,
            )
            from bridge.tools.computer_use_tool import ComputerUseTool
            from bridge.tools.provider_computer_tool import OpenAIComputerToolAdapter

            parent_cancel = computer_cancel_event or asyncio.Event()
            parent_spec = ComputerToolSpec(
                session_id=computer_session_id or subagent_parent_session_id,
                emit_event=subagent_emit or (lambda _evt: None),
                cancel_event=parent_cancel,
                enabled=True,
                require_approval=False,
                owner="parent",
            )
            atomic_computer_tools = build_computer_tools(parent_spec)
            tools.extend(atomic_computer_tools)
            tools.append(
                OpenAIComputerToolAdapter(
                    {
                        tool.definition.name: tool
                        for tool in atomic_computer_tools
                    }
                )
            )

            if sub_spec is not None:
                tools.append(
                    ComputerUseTool(sub_spec=sub_spec, enabled=True)
                )
        except ImportError as exc:
            logger.warning(
                "computer-use tools unavailable: %s (install freyja_native)",
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("computer-use tools init failed: %s", exc)

    registered: list[str] = []
    for tool in tools:
        name = tool.definition.name
        if name in excluded:
            continue
        try:
            registry.register(tool)
            registered.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to register %s: %s", name, exc)

    logger.info(
        "desktop tool registry ready: %d tools — %s",
        len(registered),
        ", ".join(sorted(registered)),
    )
    # Attach the sub-agent registry for callers that need it
    registry.subagent_registry = sub_registry  # type: ignore[attr-defined]
    return registry
