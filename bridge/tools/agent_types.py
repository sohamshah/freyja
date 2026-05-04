"""
Specialized sub-agent type registry.

Each AgentType defines a model selection strategy, thinking config,
tool filter, system prompt, and iteration cap. The sub_agent tool
looks up a type by name and configures the child runner accordingly.

To add a new agent type: add an entry to AGENT_TYPES. The parent
agent's system prompt auto-generates descriptions from this registry.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentType:
    """Declarative specification for a specialized sub-agent."""

    name: str
    """Unique key used in the sub_agent(type=...) parameter."""

    description: str
    """One-line description shown to the parent agent so it knows when to
    use this type. Keep this tight — it goes into the system prompt."""

    usage_hint: str
    """When / how the parent should use this type. Injected into system prompt."""

    model: str | list[str]
    """Fixed model ID, or a list to pick randomly from (for load balancing
    or diversity). Use 'parent' to inherit the parent's model."""

    thinking_effort: str
    """Thinking effort level: 'off', 'low', 'medium', 'high', 'max'.
    Passed through to ThinkingConfig. Ignored for models that don't
    support thinking."""

    tool_include: frozenset[str] | None = None
    """Whitelist of tool names. None = inherit all parent tools (minus
    the standard exclusions). When set, ONLY these tools are available."""

    tool_exclude: frozenset[str] | None = None
    """Additional tools to exclude on top of DEFAULT_EXCLUDED_TOOLS.
    Applied after tool_include filtering."""

    system_prompt: str = ""
    """Specialized system prompt. If empty, uses the default sub-agent prompt."""

    max_iterations: int = 25
    """Max runner iterations for this agent type."""


def resolve_model(agent_type: AgentType, parent_model: str) -> str:
    """Pick a concrete model ID from the agent type spec."""
    model = agent_type.model
    if model == "parent":
        return parent_model
    if isinstance(model, list):
        return random.choice(model)
    return model


# ─── System prompt templates ─────────────────────────────────────────────


_EXPLORE_PROMPT = """\
You are an EXPLORE sub-agent — a deep research specialist.

Your job is to thoroughly research the task you've been given using web
search, web fetching, and file system tools. You have a 1M token context
window — use it. Don't skim; go deep.

Strategy:
1. Start with broad web searches to identify the best sources.
2. Fetch and read the most promising pages in full.
3. When you find something important, call `publish_finding` immediately
   so siblings working in parallel can benefit from your discovery.
4. If you find references to papers, repos, or docs — fetch those too.
5. Download files when instructed (use bash for curl/wget).
6. Midway through your work, call `read_findings` to check if siblings
   have found anything relevant to your task — but only if their topics
   overlap with yours (you'll see their objectives below).
7. Synthesize your findings into a structured summary.

Return a well-organized report with:
- Key findings (most important first)
- Sources (URLs) for each finding
- Any files you downloaded and where they are
- Open questions or areas that need further investigation
"""

_EXPLORE_FAST_PROMPT = """\
You are a FAST EXPLORE sub-agent — quick lookup specialist.

Your job is to quickly find specific information via web search and
return a concise answer. Don't go deep — breadth over depth. You're
optimized for speed, not exhaustiveness.

When you find something, call `publish_finding` so siblings see it too.
If siblings are researching related topics, call `read_findings` midway.

Return a tight summary (under 200 words) with the key facts and URLs.
"""

_CODE_PROMPT = """\
You are a CODE sub-agent — focused code implementer.

Your job is to make specific code changes in isolation. You have file
read/write/edit tools and bash for running tests. Work methodically:

1. Read the relevant files to understand the current state.
2. Make the requested changes.
3. Run any applicable tests to verify your changes work.
4. Report what you changed and the test results.

Do NOT make changes beyond what was asked. Do NOT refactor surrounding
code. Do NOT add comments or docstrings to code you didn't change.
"""

_VERIFY_PROMPT = """\
You are a VERIFY sub-agent — quality assurance specialist.

Your job is to thoroughly validate work that was done by another agent
or the user. You are a skeptic — assume nothing works until proven.

Strategy:
1. Read the code or artifacts to understand what was changed.
2. Run the COMPLETE test suite, not just a subset.
3. Check edge cases and error conditions.
4. Verify the changes actually address the stated goal.
5. Look for regressions in adjacent code.

Return a structured report:
- PASS/FAIL overall verdict
- What tests you ran and their results
- Any issues found (with file:line references)
- Suggestions for improvement (if any)

You MUST run tests before declaring PASS. Never mark as passing based
on code reading alone.
"""


# ─── Registry ────────────────────────────────────────────────────────────


AGENT_TYPES: dict[str, AgentType] = {
    "general": AgentType(
        name="general",
        description="General-purpose sub-agent inheriting parent's model and tools",
        usage_hint="Default. Use when no specialized type fits.",
        model="parent",
        thinking_effort="auto",
        max_iterations=25,
    ),
    "explore": AgentType(
        name="explore",
        description="Deep research agent (Sonnet 4.6, 1M context, medium thinking)",
        usage_hint=(
            "Use for web research, downloading files, reading documentation, "
            "exploring codebases, or any task that benefits from deep context. "
            "Spawn one for thorough investigation."
        ),
        model="claude-sonnet-4-6",
        thinking_effort="medium",
        tool_include=frozenset({
            "web_search", "web_fetch", "web_research",
            "bash", "read_file", "write_file", "list_directory",
            "glob", "grep",
        }),
        system_prompt=_EXPLORE_PROMPT,
        max_iterations=40,
    ),
    "explore-fast": AgentType(
        name="explore-fast",
        description="Quick lookup agent (fast model rotation, no thinking)",
        usage_hint=(
            "Use for quick factual lookups, parallel fanout searches, or "
            "when you need breadth over depth. Spawn 3-5 of these in "
            "background mode for broad coverage."
        ),
        model=["kimi-k2.5", "glm5", "zai-glm-4.7"],
        thinking_effort="off",
        tool_include=frozenset({
            "web_search", "web_fetch",
            "bash", "read_file", "list_directory",
        }),
        system_prompt=_EXPLORE_FAST_PROMPT,
        max_iterations=15,
    ),
    "code": AgentType(
        name="code",
        description="Focused code agent (parent model, high thinking, file tools only)",
        usage_hint=(
            "Use for isolated code changes that don't need your full context. "
            "Good for parallel refactors across separate files/modules."
        ),
        model="parent",
        thinking_effort="high",
        tool_include=frozenset({
            "bash", "read_file", "write_file", "edit_file", "edit_json",
            "list_directory", "glob", "grep",
        }),
        system_prompt=_CODE_PROMPT,
        max_iterations=30,
    ),
    "verify": AgentType(
        name="verify",
        description="QA/verification agent (GPT-5.5, reasoning on, read-only tools)",
        usage_hint=(
            "Use to validate your work — run tests, check output, verify "
            "correctness. A second pair of eyes from a different model. "
            "Spawn after completing a significant code change."
        ),
        model="gpt-5.5",
        thinking_effort="high",
        tool_include=frozenset({
            "bash", "read_file", "list_directory",
            "glob", "grep",
        }),
        system_prompt=_VERIFY_PROMPT,
        max_iterations=20,
    ),
}


def get_agent_type(name: str) -> AgentType:
    """Look up an agent type by name, falling back to 'general'."""
    return AGENT_TYPES.get(name, AGENT_TYPES["general"])


def agent_types_for_prompt() -> str:
    """Generate the sub-agent types section for the parent system prompt.

    Auto-generated from AGENT_TYPES so adding a new type is zero-touch
    on the prompt side.
    """
    lines: list[str] = ["## Sub-agent types\n"]
    lines.append(
        "When delegating work, choose the right agent type for the job. "
        "Pass `type` to the `sub_agent` tool (defaults to `general`).\n"
    )
    for atype in AGENT_TYPES.values():
        model_desc = atype.model
        if isinstance(model_desc, list):
            model_desc = f"random({', '.join(model_desc)})"
        elif model_desc == "parent":
            model_desc = "your model"
        lines.append(
            f"- **`{atype.name}`** — {atype.description}\n"
            f"  {atype.usage_hint}\n"
        )
    return "\n".join(lines)
