"""Read-only mirror of a session's tool registry, for forked reviewers.

A forked session (today: the skill drafter) runs on its parent's exact request
prefix so the parent's prompt cache hits. Anthropic caches by prefix in the
order tools → system → messages, so changing the tools array by even one entry
invalidates everything behind it — the whole conversation the fork exists to
reuse. The tool DEFINITIONS therefore have to be byte-identical to the
parent's, in the same order, with the same schema visibility (a session that
promoted a deferred tool via ``tool_search`` must stay promoted in the fork).

But a fork with the parent's full toolset can write files, spawn sub-agents,
message other sessions, and edit memory — none of which a reviewer should do,
and all of which would be attributed to work the operator never asked for.

This module resolves that: same definitions, different behaviour. Denied tools
keep their exact ``definition`` and refuse at execution time with an error the
model can read and route around.

Note what this replaces. The old sub-agent drafter restricted itself with an
``tool_include`` whitelist and a system prompt asserting "everything else is
read-only" — but ``bash`` was on the whitelist and its read-only restriction
was prose, not code. This is the first version of that promise that is
actually enforced.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.tools import ToolCatalogEntry, ToolDefinition, ToolRegistry
from engine.types import ToolResult

logger = logging.getLogger(__name__)


#: Tools a read-only fork may actually run. An ALLOW-list, not a deny-list, so
#: a tool added to the registry later defaults to refused rather than silently
#: handing a reviewer new powers.
#:
#: Deliberately excluded and why:
#:   · ``bash`` — no way to distinguish a read-only invocation from a mutating
#:     one, and the fork already has the entire transcript, so it needs far
#:     less shelling than an excerpt-based reviewer did.
#:   · ``web_search`` / ``web_fetch`` / ``web_research`` — read-only, but
#:     network-bound and billable, and a skill review has no business
#:     researching the open internet.
#:   · ``working_memory`` / ``session_memory`` / ``memory`` — all have write
#:     actions, and a reviewer writing to the parent's memory is exactly the
#:     cross-contamination a fork is supposed to avoid.
#:   · ``sub_agent`` / ``subagents`` — recursion. A drafter that spawns a
#:     drafter is the failure mode the sub-agent path guarded against with
#:     DEFAULT_EXCLUDED_TOOLS; a fork inheriting the parent registry would
#:     otherwise walk straight past that guard.
#:   · ``talk`` — a reviewer messaging live sessions is noise the operator
#:     never asked for.
#:   · ``summarize_context`` — compacts a transcript. The fork's transcript is
#:     a copy, but the tool instance is parent-bound.
READ_ONLY_TOOLS = frozenset(
    {
        # Filesystem reads
        "read_file",
        "list_directory",
        "glob",
        "grep",
        # Artifact + transcript reads
        "artifacts",
        "recall",
        "view_image",
        # Skill library
        "list_skills",
        "search_skills",
        "load_skill",
        # Deferred-schema loader. Rebound to the mirror below — the parent's
        # instance would promote tools on the PARENT's catalog.
        "tool_search",
    }
)


_REFUSAL = (
    "`{name}` is not available in this review fork. This fork is read-only: "
    "it exists to review the conversation above, not to change anything. "
    "Available: read_file, list_directory, glob, grep, artifacts, recall, "
    "list_skills, search_skills, load_skill. Emit your decision block when "
    "you have enough to decide."
)


class RefusedTool:
    """Stands in for a denied tool, keeping its definition byte-identical.

    The definition is what goes on the wire, so it must be the original object
    — that is the entire reason this class exists rather than simply dropping
    the tool from the registry.
    """

    def __init__(self, tool: Any, *, extra_tools: frozenset[str] = frozenset()) -> None:
        self._tool = tool
        self._extra = extra_tools
        # A refused call must not trigger the operator's permission prompt on
        # its way to being refused.
        self.requires_permission = False

    @property
    def definition(self) -> ToolDefinition:
        return self._tool.definition

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        name = self._tool.definition.name
        logger.info("fork registry: refused %s", name)
        return ToolResult(
            call_id=call_id,
            content=_REFUSAL.format(name=name),
            is_error=True,
        )


def build_read_only_fork_registry(
    parent: ToolRegistry,
    *,
    also_allow: frozenset[str] = frozenset(),
) -> ToolRegistry:
    """Mirror ``parent`` with mutating tools stubbed out.

    Preserves registration order and each tool's CURRENT schema visibility,
    both of which are part of the cached prefix. Building this by calling
    ``register()`` in a loop would reset visibility to the tool's static tier
    and re-sort nothing — a session that had promoted ``propose_skill`` via
    ``tool_search`` would silently lose the promotion, changing the tools array
    and costing the cache hit this whole design is for.

    ``also_allow`` lets a caller permit one extra tool it has a specific reason
    to trust — the drafter passes ``propose_skill``, which is a legitimate
    second output path.
    """
    allowed = READ_ONLY_TOOLS | also_allow
    mirror = ToolRegistry()
    for name, entry in parent._catalog.items():  # noqa: SLF001
        tool = entry.tool if name in allowed else RefusedTool(entry.tool)
        mirror._tools[name] = tool  # noqa: SLF001
        mirror._catalog[name] = ToolCatalogEntry(
            tool=tool,
            tier=entry.tier,
            summary_visible=entry.summary_visible,
            schema_visible=entry.schema_visible,
        )

    # ``tool_search`` closes over the registry it promotes into. The parent's
    # instance would flip schema_visible on the PARENT's catalog — changing the
    # parent's tools array from inside a fork, which busts the very cache the
    # fork exists to reuse, and does not even make the tool callable here since
    # dispatch reads the mirror. Rebind it to the mirror.
    #
    # Safe for prefix identity: ToolSearchTool's definition is a static literal
    # with no dependency on registry contents, so the schema on the wire is
    # byte-identical to the parent's.
    if "tool_search" in mirror._tools:  # noqa: SLF001
        try:
            from bridge.tools.tool_search_tool import ToolSearchTool

            rebound = ToolSearchTool(mirror)
            mirror._tools["tool_search"] = rebound  # noqa: SLF001
            mirror._catalog["tool_search"].tool = rebound
        except Exception:  # noqa: BLE001
            # Better to refuse the tool than to let it mutate the parent.
            stub = RefusedTool(parent._tools["tool_search"])  # noqa: SLF001
            mirror._tools["tool_search"] = stub  # noqa: SLF001
            mirror._catalog["tool_search"].tool = stub
            logger.warning("fork registry: could not rebind tool_search; refusing it")

    return mirror
