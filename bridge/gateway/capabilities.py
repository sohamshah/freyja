"""Capability sets for gateway-routed sessions.

When a session is created by an inbound platform message (vs the
desktop renderer), it runs unattended — the operator can't approve a
per-tool permission prompt, and the session may run anywhere the
gateway daemon is hosted. With ``slack.enable_tool_filter`` on (it is
off by default) the tool surface is restricted to the list below: file
reads, writes in the session's project output dir, web research,
sub-agents, skills, memory and schedules, with no shell, computer use
or browser.

The default set lives here as code. v2 will read it from
``~/.freyja/gateway.yaml`` with per-workflow overrides.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Tools the gateway-routed agent is allowed to call. Everything else
# is stripped from the registry at session-init time (see
# ``_BridgeSession.initialize`` in ``bridge/freyja_bridge.py``).
#
# Four buckets:
#
#   1. READ — safe reads of the local workspace. The agent can
#      reference code, docs, and previously-produced artifacts.
#   2. RESEARCH — internet access for context. No persistent
#      side effects on the user's machine.
#   3. WRITE — write_file, edit_file and edit_json, which write only
#      inside the session's project output dir (confine_writes). The
#      source tree and ~/.freyja stay out of reach.
#   4. SYNTHESIS — sub-agent spawn so the agent can fan out to
#      explore-fast / verify / code profiles. A sub-agent of a
#      filtered session gets only tools on this list too
#      (SubAgentSpec.tool_allowlist_getter). A job the session or
#      its sub-agents schedule keeps the filter (JobRecord.tool_filter).
#
# Notably ABSENT:
#   · bash — too broad; even read-only intent gets misused
#   · computer / computer_use / click / move_mouse / type_text
#     / press_key / screenshot — no operator at keyboard to veto
#   · browser_execute_js / browser_screenshot — same reason
#   · generate_image / analyze_video — fine in principle but not
#     part of the demo surface; reintroduce after operator opts in
#   · memory mutators (record_user_preference) — agent shouldn't
#     mutate the operator's memory store unattended. ``memory``
#     below only lists and shows (read_only_memory)
#   · session_memory — same reason
#
# Future: this becomes per-platform / per-workspace / per-workflow.
SLACK_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # READ
    "read_file",
    "list_directory",
    "glob",
    "grep",
    "artifacts",
    # RESEARCH
    "web_search",
    "web_fetch",
    "web_research",
    "twitter_search",
    # WRITE (scoped)
    "write_file",
    "edit_file",
    "edit_json",
    # SYNTHESIS / coordination
    "sub_agent",
    "subagents",
    "summarize_context",
    "tool_search",
    # Runs tools tool_search loaded. It dispatches through the registry, so
    # a tool this list strips stays unreachable.
    "call_tool",
    # SCHEDULING — durable jobs created from Slack are core UX.
    "schedule",
    # KNOWLEDGE (read-only)
    "list_skills",
    "search_skills",
    "load_skill",
    "memory",
    # GATEWAY meta tools (added in a later phase — e.g. /goal
    # implementation may want goal_control). Listed here so future
    # additions are explicit.
})


# Tools the gateway adds back on top of the allowlist for specific
# platforms. Keeps the default allowlist small but allows per-platform
# customization.
_PLATFORM_EXTRAS: dict[str, frozenset[str]] = {
    # Slack-only tools: native widgets in the reply and Slack Code
    # channels. Neither touches the operator's machine.
    "slack": frozenset({"send_attachment", "slack_widget", "code_channel"}),
}


def tools_allowed_for_gateway(platform: Any) -> set[str]:
    """Return the set of tool names a gateway-routed session may call,
    for the given platform. Unknown platform → the slack defaults
    (the most-restrictive baseline)."""
    base = set(SLACK_TOOL_ALLOWLIST)
    if platform is not None:
        platform_name = getattr(platform, "value", None) or str(platform)
        extras = _PLATFORM_EXTRAS.get(platform_name)
        if extras:
            base |= set(extras)
    return base


class _ProjectDirWriteTool:
    """A write tool that writes only inside one session's project output
    dir. Any other path, absolute or climbing out with ``..``, is refused."""

    def __init__(self, tool: Any, resolver: Any, platform: str) -> None:
        self._tool = tool
        self._resolver = resolver
        self._platform = platform

    @property
    def definition(self) -> Any:
        return self._tool.definition

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> Any:
        from bridge.tools.base import ToolResult

        name = self._tool.definition.name
        project_dir = self._resolver.project_dir
        try:
            path = self._resolver.resolve(
                str(arguments.get("path") or ""),
                tool_name=name,
                base=str(arguments.get("base") or arguments.get("location") or "auto"),
            )
        except ValueError as exc:
            return ToolResult(call_id=call_id, content=f"Error: {exc}", is_error=True)
        if not path.is_relative_to(project_dir):
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: with the {self._platform.title()} tool filter on, {name} "
                    f"writes only inside this session's project output directory, "
                    f"{project_dir}. {path} is outside it. Pass a path inside it, "
                    f'or a relative path with base="project".'
                ),
                is_error=True,
            )
        return await self._tool.execute(call_id, {**arguments, "path": str(path)})


def confine_writes(registry: Any, resolver: Any, platform: str) -> None:
    """Make write_file, edit_file and edit_json in ``registry`` write only
    inside ``resolver.project_dir``, the session's project output dir.

    Called on a filtered session's registry. A sub-agent copies these tool
    objects, so it is confined to the same directory."""
    from bridge.artifact_store import MUTATING_FILE_TOOL_NAMES

    for name in sorted(MUTATING_FILE_TOOL_NAMES):
        tool = registry.get(name)
        if tool is not None:
            registry.unregister(name)
            registry.register(_ProjectDirWriteTool(tool, resolver, platform))


class _ReadOnlyMemoryTool:
    """The ``memory`` tool with only its list and show actions. Every later
    session reads the memory store, desktop sessions with every tool
    included, so a filtered session may not record, change or delete in it."""

    _READS = frozenset({"list", "show"})

    def __init__(self, tool: Any, platform: str) -> None:
        self._tool = tool
        self._platform = platform

    @property
    def definition(self) -> Any:
        return self._tool.definition

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> Any:
        from bridge.tools.base import ToolResult

        action = str(arguments.get("action") or "").strip().lower()
        if action not in self._READS:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: with the {self._platform.title()} tool filter on, "
                    f"memory only lists and shows memories, so {action or 'this'!r} "
                    f"is refused. Ask the operator to save it from the desktop app."
                ),
                is_error=True,
            )
        return await self._tool.execute(call_id, arguments)


def read_only_memory(registry: Any, platform: str) -> None:
    """Make ``memory`` in a filtered session's ``registry`` list and show
    only. A sub-agent copies the tool object, so it is read-only too."""
    tool = registry.get("memory")
    if tool is not None:
        registry.unregister("memory")
        registry.register(_ReadOnlyMemoryTool(tool, platform))


def tools_denied_for_gateway(platform: Any, all_tools: set[str]) -> set[str]:
    """For audit / display: list of tools that would be stripped from
    ``all_tools`` if we ran them through ``tools_allowed_for_gateway``."""
    allowed = tools_allowed_for_gateway(platform)
    return {t for t in all_tools if t not in allowed}


def gateway_filter_enabled(platform: Any) -> bool:
    """Return True if the gateway should restrict the tool surface for
    sessions on this platform.

    Defaults to False — the operator is presumed to be the only user
    of their own install, so the agent gets the same tool surface
    over Slack that it has on the desktop (bash, computer-use,
    browser, image generation, memory mutations, etc.). Flip on per
    workspace via ``slack.enable_tool_filter: true`` in
    ``~/.freyja/gateway.yaml`` when a shared workspace has other
    users who can DM the bot.

    Read once per session init (no long-running cache). If the
    operator edits gateway.yaml, the change takes effect at the
    next session creation. A gateway.yaml that cannot be read or
    parsed turns the filter on, and the log says so.
    """
    if platform is None:
        return False
    from bridge.gateway.config import GatewayConfig, config_path
    try:
        cfg = GatewayConfig.load(strict=True)
    except Exception as exc:  # noqa: BLE001
        # Fail closed. With the filter on the operator still gets replies
        # and loses some tools until the file is fixed; off, everyone on
        # the allowlist would get the shell.
        logger.error(
            "%s could not be read (%s), so the Slack tool filter is on "
            "until it is fixed",
            config_path(), exc,
        )
        return True
    platform_name = getattr(platform, "value", None) or str(platform)
    if platform_name == "slack":
        return cfg.slack.enable_tool_filter
    # Unknown platform: no filter by default. Add per-platform
    # toggles to GatewayConfig as new platforms ship.
    return False
