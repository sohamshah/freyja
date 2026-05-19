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
from bridge.tools.artifacts_tool import ArtifactsTool
from bridge.tools.bash_tool import BashTool
from bridge.tools.browser_tools import BrowserExecuteJsTool, BrowserScreenshotTool
from bridge.tools.file_tools import (
    EditFileTool,
    EditJsonTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from bridge.tools.image_generation_tool import GenerateImageTool
from bridge.tools.kanban_board import KanbanTool
from bridge.tools.task_board import TaskBoardTool
from bridge.tools.video_analysis_tool import AnalyzeVideoTool
from bridge.tools.memory_tools import MemoryTool, RecordUserPreferenceTool
from bridge.tools.search_tools import GlobTool, GrepTool
from bridge.tools.skill_tools import ListSkillsTool, LoadSkillTool, SearchSkillsTool
from bridge.tools.session_memory_tool import SessionMemoryTool
from bridge.tools.sub_agent_registry import SubAgentRegistry
from bridge.tools.sub_agent_tool import SubAgentSpec, SubAgentTool
from bridge.tools.subagents_tool import SubAgentsTool
from bridge.tools.summarize_context_tool import SummarizeContextTool
from bridge.tools.talk_tool import (
    ListAgentSessionsTool,
    TalkRouter,
    TalkRouterContext,
    TalkTool,
)
from bridge.tools.tool_search_tool import ToolSearchTool
from bridge.tools.widget_tool import ReadWidgetSpecTool, ShowWidgetTool

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
    coordination_strategy: str = "bus",
    kanban_board: Any | None = None,
    task_board: Any | None = None,
    memory_store: Any | None = None,
    skill_store: Any | None = None,
    image_store: Any | None = None,
    project_output_dir: Path | str | None = None,
    artifact_store: Any | None = None,
    on_memory_updated: Any | None = None,
    on_skill_event: Any | None = None,
    summarize_context_session_getter: Any | None = None,
    summarize_context_provider_getter: Any | None = None,
    summarize_context_compactor_getter: Any | None = None,
    summarize_context_pressure_getter: Any | None = None,
    summarize_context_telemetry: Any | None = None,
    summarize_context_on_system_event: Any | None = None,
    summarize_context_on_pin_changed: Any | None = None,
    summarize_context_on_summarizer_llm_call: Any | None = None,
    # Reader for the session-wide tool-call counter. The `tasks` tool
    # uses it to stamp each task's `last_touched_tool_index` after an
    # action lands, which the stale-task reminder reads to decide if
    # the agent has done other work since the last task touch. None
    # in test fixtures / standalone uses — the reminder simply won't
    # fire in that case.
    task_tool_call_index_getter: Any | None = None,
    talk_router: TalkRouter | None = None,
    talk_caller_session_id: str = "",
    talk_caller_label: str = "",
    talk_caller_role: str = "agent",
    talk_parent_session_id: str | None = None,
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

    # Memory (writes to MEMORY.md + structured JSONL). Two tool entries:
    # `record_user_preference` keeps existing transcripts working;
    # `memory` is the new action-based curation surface (list / show /
    # record / update / delete / restore / merge). Both share the same
    # MemoryStore and both pass the calling session id + a "parent"
    # actor label into the per-item audit trail.
    _mem_actor = "parent"
    _mem_session = subagent_parent_session_id or ""
    tools.append(
        RecordUserPreferenceTool(
            workspace=workspace_path,
            memory_store=memory_store,
            on_memory_updated=on_memory_updated,
            session_id=_mem_session,
            actor=_mem_actor,
        )
    )
    if _mem_session:
        tools.append(SessionMemoryTool(session_id=_mem_session))
    tools.append(
        MemoryTool(
            workspace=workspace_path,
            memory_store=memory_store,
            on_memory_updated=on_memory_updated,
            session_id=_mem_session,
            actor=_mem_actor,
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

    # Creative media generation — WARM tier so it is discoverable, but the
    # model must explicitly load the schema before spending on generation.
    tools.append(
        GenerateImageTool(
            image_store=image_store,
            default_output_dir=(
                Path(project_output_dir).expanduser().resolve() / "images"
                if project_output_dir is not None
                else None
            ),
        )
    )

    # Video understanding via Gemini. Local files go through the Files API,
    # YouTube URLs go through ``Part.from_uri`` directly. WARM so the schema
    # only loads when the agent asks for it.
    tools.append(AnalyzeVideoTool())

    if artifact_store is not None:
        tools.append(ArtifactsTool(artifact_store))

    # Session-local board coordination. This is intentionally only present
    # when a session starts in kanban mode, so the strategy is visible in the
    # tool surface instead of becoming another always-on abstraction.
    if kanban_board is not None:
        tools.append(
            KanbanTool(
                kanban_board,
                actor_id=subagent_parent_session_id or "parent",
                actor_label="parent",
                emit_event=subagent_emit,
                parent_session_id=subagent_parent_session_id,
            )
        )

    if task_board is not None:
        tools.append(
            TaskBoardTool(
                task_board,
                actor_id=subagent_parent_session_id or "parent",
                actor_label="parent",
                emit_event=subagent_emit,
                parent_session_id=subagent_parent_session_id,
                get_tool_call_index=task_tool_call_index_getter,
            )
        )

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

    # Generative-UI widget tools. `widget_spec` (WARM) returns the
    # design-system markdown; `show_widget` (HOT) mounts an inline
    # iframe. Both are session-scoped so emitted widget_render events
    # carry the right sessionId for the renderer to route on.
    tools.append(ReadWidgetSpecTool())
    tools.append(
        ShowWidgetTool(
            session_id=subagent_parent_session_id or "",
            emit_event=subagent_emit,
        )
    )

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
            coordination_strategy=coordination_strategy,
            kanban_board=kanban_board,
            task_board=task_board,
            task_tool_call_index_getter=task_tool_call_index_getter,
            artifact_store=artifact_store,
            talk_router=talk_router,
        )
        tools.append(SubAgentTool(sub_spec))
        tools.append(SubAgentsTool(sub_registry))

    # Inter-agent messaging tools. Need a TalkRouter (bridge-side
    # dispatcher with global session lookup + re-wake) plus a per-caller
    # context (so 'parent' / 'siblings' aliases resolve correctly).
    if talk_router is not None and talk_caller_session_id:
        talk_ctx = TalkRouterContext(
            caller_session_id=talk_caller_session_id,
            caller_label=talk_caller_label or talk_caller_session_id,
            caller_role=talk_caller_role,
            parent_session_id=talk_parent_session_id,
        )
        tools.append(TalkTool(router=talk_router, ctx=talk_ctx))
        tools.append(ListAgentSessionsTool(router=talk_router, ctx=talk_ctx))

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

    # Cooperative compaction tool. Lazy getters because session/runner
    # don't exist when the registry is being built — they're populated
    # later in _BridgeSession.initialize. We just need the getters to
    # return the right object by the time the agent actually invokes
    # the tool.
    if (
        summarize_context_session_getter is not None
        and summarize_context_provider_getter is not None
        and summarize_context_compactor_getter is not None
    ):
        tools.append(
            SummarizeContextTool(
                get_session=summarize_context_session_getter,
                get_provider=summarize_context_provider_getter,
                get_compactor=summarize_context_compactor_getter,
                on_summarize_call=summarize_context_telemetry,
                get_current_pressure_pct=summarize_context_pressure_getter,
                on_system_event=summarize_context_on_system_event,
                on_pin_changed=summarize_context_on_pin_changed,
                on_summarizer_llm_call=summarize_context_on_summarizer_llm_call,
            )
        )

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
