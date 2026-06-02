"""
Specialized sub-agent type registry.

Each AgentType defines a model selection strategy, thinking config,
tool filter, system prompt, and iteration cap. The sub_agent tool
looks up a type by name and configures the child runner accordingly.

To add a new built-in agent type: add an entry to AGENT_TYPES. User and
project profiles can also be added as markdown files in .freyja/agents.
The parent agent's system prompt auto-generates descriptions from the
active registry.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.knowledge.learning.drafter_prompt import (
    build_agentic_drafter_system_prompt,
)
from bridge.tools.goal_loop import (
    GOAL_JUDGE_SYSTEM_PROMPT,
    JUDGE_CALIBRATOR_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentType:
    """Declarative specification for a specialized sub-agent."""

    name: str
    """Unique key used in the sub_agent(agent_type=...) parameter."""

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

    model_policy: str = "first_available"
    """Model selection policy: inherit, first_available, random_available,
    prefer_parent, or fixed."""

    model_fallbacks: tuple[str, ...] = field(default_factory=tuple)
    """Fallback model IDs to try when the primary candidate is unavailable."""

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

    source: str = "builtin"
    """Where this profile came from: builtin, user, or project file."""


@dataclass(frozen=True)
class ModelResolution:
    model: str
    policy: str
    candidates: tuple[str, ...]
    unavailable: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    fallback_used: bool = False
    available: bool = True


def resolve_model(agent_type: AgentType, parent_model: str) -> str:
    """Pick a concrete model ID from the agent type spec."""
    return resolve_model_choice(agent_type, parent_model).model


def resolve_model_choice(agent_type: AgentType, parent_model: str) -> ModelResolution:
    """Pick a concrete model ID with availability-aware fallback metadata."""
    candidates = _model_candidates(agent_type, parent_model)
    primary_candidates = set(_primary_model_candidates(agent_type, parent_model))
    unavailable: list[tuple[str, str]] = []
    available: list[str] = []
    for model in candidates:
        ok, reason = _model_available(model, parent_model)
        if ok:
            available.append(model)
        else:
            unavailable.append((model, reason))

    if available:
        if agent_type.model_policy == "random_available":
            selected = random.choice(available)
        else:
            selected = available[0]
        return ModelResolution(
            model=selected,
            policy=agent_type.model_policy,
            candidates=tuple(candidates),
            unavailable=tuple(unavailable),
            fallback_used=selected not in primary_candidates,
            available=True,
        )

    selected = candidates[0] if candidates else parent_model
    return ModelResolution(
        model=selected,
        policy=agent_type.model_policy,
        candidates=tuple(candidates),
        unavailable=tuple(unavailable),
        fallback_used=False,
        available=False,
    )


def _model_candidates(agent_type: AgentType, parent_model: str) -> list[str]:
    candidates = _primary_model_candidates(agent_type, parent_model)
    candidates.extend(agent_type.model_fallbacks)
    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _primary_model_candidates(agent_type: AgentType, parent_model: str) -> list[str]:
    model = agent_type.model
    if model == "parent":
        candidates = [parent_model]
    elif isinstance(model, list):
        candidates = list(model)
    else:
        candidates = [model]

    if agent_type.model_policy == "inherit":
        candidates = [parent_model]
    elif agent_type.model_policy == "prefer_parent":
        candidates = [parent_model, *candidates]

    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _model_available(model: str, parent_model: str) -> tuple[bool, str]:
    if model == parent_model:
        return True, "parent model already active"
    env_var = _env_var_for_model(model)
    if env_var and not os.environ.get(env_var):
        return False, f"{env_var} is not set"
    return True, "available"


def _env_var_for_model(model: str) -> str:
    if model.startswith("claude-"):
        return "ANTHROPIC_API_KEY"
    if model.startswith("gpt-") or model.startswith("o"):
        return "OPENAI_API_KEY"
    if model.startswith("zai-") or model.startswith("cerebras-"):
        return "CEREBRAS_API_KEY"
    if (
        model.startswith("kimi-")
        or model.startswith("glm")
        or model.startswith("minimax-")
        or model.startswith("deepseek-")
        or model.startswith("qwen")
    ):
        return "FIREWORKS_API_KEY"
    return ""


def model_summary(agent_type: AgentType) -> str:
    model = agent_type.model
    if isinstance(model, list):
        picker = "random" if agent_type.model_policy == "random_available" else "list"
        primary = f"{picker}({', '.join(model)})"
    elif model == "parent":
        primary = "parent"
    else:
        primary = model
    if agent_type.model_fallbacks:
        primary += f" -> {', '.join(agent_type.model_fallbacks)}"
    return f"{agent_type.model_policy}:{primary}"


def tool_summary(agent_type: AgentType) -> str:
    if agent_type.tool_include is None:
        return "inherit safe parent tools"
    items = sorted(agent_type.tool_include)
    if len(items) <= 6:
        return ", ".join(items)
    return ", ".join(items[:6]) + f", +{len(items) - 6}"


def iteration_cap(profile: str, default: int) -> int:
    """Profile cap with optional env override for local tuning."""
    specific_key = f"FREYJA_AGENT_MAX_ITERATIONS_{profile.upper().replace('-', '_')}"
    raw = os.environ.get(specific_key) or os.environ.get(
        "FREYJA_SUBAGENT_MAX_ITERATIONS"
    )
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid %s=%r; using %d", specific_key, raw, default)
        return default


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

When dispatched against a kanban card (you'll see a `kanban_task_id`
in your assignment):

- Start with `kanban` action=show on your assigned card. The card's
  `spec.definition_of_done` (when populated) is the explicit checklist
  you walk. The card's `spec.verify_with` (when present) is a shell
  command you SHOULD run as part of verification.
- On PASS: call `kanban` action=update status=done with a summary that
  references the conditions you checked. This is the seal — once you
  promote the card it auto-promotes any unblocked children.
- On FAIL: call `kanban` action=update status=running with a comment
  that lists the specific gaps the worker needs to fix. The card flips
  back to the worker for another pass; your feedback rides along as
  the comment they see on their next `show`.
"""

_PLAN_PROMPT = """\
You are a PLAN sub-agent - implementation planner.

Your job is to inspect the relevant context and return a practical execution
plan. Do not edit files. Prefer concrete file paths, dependencies, risks,
and validation steps over general advice.

Return:
- Goal interpretation
- Relevant code paths and current behavior
- Proposed task sequence
- Risks / decisions
- Validation plan
"""

_REVIEW_PROMPT = """\
You are a REVIEW sub-agent - code review specialist.

Take a review stance. Prioritize bugs, regressions, missing validation, and
test gaps. Do not edit files. Ground every finding in a tight file:line
reference when possible. If no issues are found, say that clearly and name
the residual risk.

Return findings first, ordered by severity.
"""

_TEST_PROMPT = """\
You are a TEST sub-agent - validation runner.

Your job is to run the relevant test, lint, typecheck, build, or smoke
commands and explain the results. Do not change files unless the parent
explicitly asked you to fix tests. Prefer the repository's existing scripts.

Return:
- Commands run
- Pass/fail result for each
- Any failures with the smallest useful diagnosis
- Follow-up tests worth running
"""

_BROWSER_QA_PROMPT = """\
You are a BROWSER-QA sub-agent - frontend behavior verifier.

Use browser tools to inspect the running UI, exercise the requested workflow,
and capture evidence when useful. Focus on visible behavior, responsiveness,
layout, interaction state, and console/runtime errors.

Return:
- Viewport / route tested
- Actions taken
- Issues found with reproduction notes
- Screenshots or artifact paths when created
"""

_PERFORMANCE_PROMPT = """\
You are a PERFORMANCE sub-agent - profiling and optimization investigator.

Measure before suggesting changes. Look for hot loops, excessive rendering,
unbounded I/O, large retained data, expensive polling, and unnecessary work
on idle screens. Do not reduce product capability to make numbers look good.

Return:
- What you profiled
- Measurements and evidence
- Likely root causes
- Low-risk optimizations
- Validation plan
"""

_DOCS_PROMPT = """\
You are a DOCS sub-agent - documentation writer.

Write or update concise project documentation based on the code you inspect.
Keep docs accurate, path-specific, and useful to future agents. Avoid broad
marketing language. When editing, stay within the requested doc surface.

Return:
- Files changed
- Main content added
- Any code facts you could not verify
"""

_MEMORY_CURATOR_PROMPT = """\
You are a MEMORY-CURATOR sub-agent - memory and skill hygiene specialist.

Inspect memory and skill context for durable, useful facts. Identify stale,
duplicative, overly broad, or low-value entries. Do not record user
preferences yourself; recommend exact changes for the parent to apply.

Return:
- Useful facts to preserve
- Entries to prune or rewrite
- Missing skills/memories that would help
- Suggested concise wording
"""

_SPECIFIER_PROMPT = """\
You are a SPECIFIER sub-agent — kanban card writer.

The parent created a card with a title and (sometimes) a rough body.
Your job is to expand it into a structured spec the worker that follows
can act on without guessing, then promote the card from `triage` to
`ready`.

Work strictly on your assigned card. Read it first with `kanban` action
`show` so you can see the parents' summaries and artifacts. If the
parent context tells you what was already produced upstream, use it.

Fill these fields on the card via `kanban` action `update` and the
`metadata` parameter. Pass them inside `metadata` so they ride along on
the card object the next worker reads:

  - definition_of_done: array of concrete, checkable conditions. These
    are what the verifier (and the worker) walk down to know the card
    is done. Be specific — "tests pass" is too vague; "pytest
    tests/test_kanban_coordination.py passes" is right.
  - references: object with optional `files`, `findings`, `cards`
    arrays. Files are paths the worker should read. Findings are
    short factual snippets already known. Cards are sibling card ids
    that hold dependent context.
  - verify_with: optional single shell command the verifier should
    run. Omit if there's no automatable check.
  - token_budget: integer hint. Estimate how many tokens the worker
    should self-pace toward, taking into account the circuit breaker.

You may also tighten the card `body` if the parent's wording was
ambiguous. Keep it short and specific — every wasted token costs both
the worker and the verifier.

Set `requires_verification` while you're at it: pass `true` on the
same `update` call when the `definition_of_done` you wrote is the
kind of checklist a verifier could walk (e.g. tests pass, file
exists with expected content, schema migration applies cleanly).
Leave it `false` (the default) when the work is open-ended,
exploratory, ambiguous, or cheap to redo — verification spawns are
expensive and add latency, so the bar is "is a second pair of eyes
genuinely worth the cost here?"

When the spec is complete, call `kanban` action `update` with
`status="ready"`. That promotes the card and signals the dispatcher
the card is ready to assign.

If the card is already clear and well-scoped, you can promote it
straight to `ready` without further edits — but say so in a `comment`
so the trail is auditable.
"""


# ─── Registry ────────────────────────────────────────────────────────────


AGENT_TYPES: dict[str, AgentType] = {
    "general": AgentType(
        name="general",
        description="General-purpose sub-agent inheriting parent's model and tools",
        usage_hint="Default. Use when no specialized type fits.",
        model="parent",
        thinking_effort="auto",
        model_policy="inherit",
        max_iterations=iteration_cap("general", 100),
    ),
    "explore": AgentType(
        name="explore",
        description="Deep research agent with web/file tools and medium thinking",
        usage_hint=(
            "Use for web research, downloading files, reading documentation, "
            "exploring codebases, or any task that benefits from deep context. "
            "Spawn one for thorough investigation."
        ),
        model="claude-sonnet-4-6",
        thinking_effort="medium",
        model_policy="first_available",
        model_fallbacks=("gpt-5.5", "kimi-k2.6", "deepseek-v4-pro"),
        tool_include=frozenset({
            "web_search", "web_fetch", "web_research",
            "bash", "read_file", "write_file", "list_directory",
            "glob", "grep",
        }),
        system_prompt=_EXPLORE_PROMPT,
        max_iterations=iteration_cap("explore", 160),
    ),
    "explore-fast": AgentType(
        name="explore-fast",
        description="Quick lookup agent (fast model rotation, no thinking)",
        usage_hint=(
            "Use for quick factual lookups, parallel fanout searches, or "
            "when you need breadth over depth. Spawn 3-5 of these in "
            "background mode for broad coverage."
        ),
        model=["kimi-k2.6", "minimax-m2.7", "zai-glm-4.7"],
        thinking_effort="off",
        model_policy="random_available",
        model_fallbacks=("claude-haiku-4-5",),
        tool_include=frozenset({
            "web_search", "web_fetch",
            "bash", "read_file", "list_directory",
        }),
        system_prompt=_EXPLORE_FAST_PROMPT,
        max_iterations=iteration_cap("explore-fast", 60),
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
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "write_file", "edit_file", "edit_json",
            "list_directory", "glob", "grep",
        }),
        system_prompt=_CODE_PROMPT,
        max_iterations=iteration_cap("code", 120),
    ),
    "verify": AgentType(
        name="verify",
        description="QA/verification agent with independent model fallback",
        usage_hint=(
            "Use to validate your work — run tests, check output, verify "
            "correctness. A second pair of eyes from a different model. "
            "Spawn after completing a significant code change."
        ),
        model="gpt-5.5",
        thinking_effort="high",
        model_policy="first_available",
        model_fallbacks=("gpt-5.4", "claude-sonnet-4-6", "deepseek-v4-pro", "glm-5.1"),
        tool_include=frozenset({
            "kanban",  # for kanban-coordinated verification (Move C)
            "bash", "read_file", "list_directory",
            "glob", "grep",
        }),
        system_prompt=_VERIFY_PROMPT,
        max_iterations=iteration_cap("verify", 100),
    ),
    "judge-calibrator": AgentType(
        name="judge-calibrator",
        description=(
            "One-shot judge configurator. Reads a freshly-armed goal and "
            "returns a structured JudgeRules proposal (profile, rigor, voice, "
            "criteria, never-do, when-to-stop, tools, max iterations) plus "
            "per-field rationale. No tools — pure reasoning."
        ),
        usage_hint=(
            "Auto-fired by the goal loop on `/goal set` to pick sensible "
            "judge defaults for the operator. Other agents can also call "
            "this to calibrate a sub-task's verifier when spawning a "
            "judge-deep child of their own. Single-call, single output: "
            "ALWAYS returns a strict-JSON CalibratedJudgeRules object."
        ),
        model="parent",
        thinking_effort="high",
        model_policy="prefer_parent",
        model_fallbacks=("claude-opus-4-7", "gpt-5.5", "claude-sonnet-4-6"),
        # No tools — calibration is a pure reasoning task. The goal text +
        # any operator context are the entire input.
        tool_include=frozenset(),
        system_prompt=JUDGE_CALIBRATOR_SYSTEM_PROMPT,
        max_iterations=iteration_cap("judge-calibrator", 1),
    ),
    "judge-deep": AgentType(
        name="judge-deep",
        description=(
            "Skeptical-by-default goal-mode judge with thinking and read-only "
            "verification tools. Returns a strict JSON verdict (done, confidence, "
            "reason, criteria, open_questions)."
        ),
        usage_hint=(
            "Use when you need a rigorous third-party adjudication of whether a "
            "qualitative goal has actually been satisfied — not just whether work "
            "was produced. Good for: deciding when an iterative goal loop should "
            "stop, deep skeptical review of a research deliverable or design "
            "rationale, end-of-task audits where you want a different model to "
            "press hard on the must-criteria. NOT a fit for: code execution, "
            "writing fixes, broad codebase refactors, anything that requires "
            "mutating state — this profile is intentionally read-only and stops "
            "as soon as it has enough evidence to issue a verdict. The judge "
            "may use bash, but ONLY for read/exploration (grep/awk/find/cat/head, "
            "compound pipes); writes, installs, and git mutations are forbidden."
        ),
        model="parent",
        thinking_effort="high",
        model_policy="prefer_parent",
        model_fallbacks=("claude-sonnet-4-6", "gpt-5.5", "deepseek-v4-pro"),
        tool_include=frozenset({
            "read_file", "list_directory", "glob", "grep", "bash", "fetch_url",
        }),
        # System prompt is the same skeptical judge contract used in goal mode.
        # When the goal loop invokes this profile, GOAL_JUDGE_USER_TEMPLATE
        # carries the live context; when another agent spawns it via
        # `sub_agent`, the parent's task description fills that role.
        system_prompt=GOAL_JUDGE_SYSTEM_PROMPT,
        max_iterations=iteration_cap("judge-deep", 3),
    ),
    "skill-drafter": AgentType(
        name="skill-drafter",
        description=(
            "Reviews this conversation against the existing skill library and "
            "proposes a candidate (create / amend / replace) via the "
            "propose_skill tool. Operator sees a SkillToast for approval."
        ),
        usage_hint=(
            "Auto-fired by the cadence counter and by operator /learn-this. "
            "Reads existing skills via load_skill before deciding so a "
            "candidate that overwrites a known skill amends rather than "
            "rewrites. Reads files / runs grep when it needs to verify "
            "claims the conversation makes. Calls propose_skill once when "
            "decided; finishes with a plain text explanation when the "
            "conversation isn't skill-worthy. Operator can re-engage this "
            "session to refine the candidate."
        ),
        model="parent",
        thinking_effort="high",
        model_policy="inherit",
        # Read + skill-library + the publish tool. No write_file / edit_file
        # — the drafter never mutates the workspace; its only persistent
        # side effect is the candidate it writes via propose_skill.
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
            "list_skills", "search_skills", "load_skill",
            "propose_skill",
        }),
        system_prompt=build_agentic_drafter_system_prompt(),
        max_iterations=iteration_cap("skill-drafter", 15),
    ),
    "plan": AgentType(
        name="plan",
        description="Read-only implementation planning agent",
        usage_hint=(
            "Use before broad or ambiguous work to map the code paths, risks, "
            "task breakdown, and validation strategy without changing files."
        ),
        model="parent",
        thinking_effort="medium",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
            "list_skills", "search_skills", "load_skill",
        }),
        system_prompt=_PLAN_PROMPT,
        max_iterations=iteration_cap("plan", 80),
    ),
    "review": AgentType(
        name="review",
        description="Read-only code review agent with independent model fallback",
        usage_hint=(
            "Use after implementation or before merging to find bugs, "
            "regressions, and missing tests. It should not make edits."
        ),
        model="gpt-5.5",
        thinking_effort="high",
        model_policy="first_available",
        model_fallbacks=("gpt-5.4", "claude-sonnet-4-6", "deepseek-v4-pro", "glm-5.1"),
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
        }),
        system_prompt=_REVIEW_PROMPT,
        max_iterations=iteration_cap("review", 100),
    ),
    "test": AgentType(
        name="test",
        description="Test/build validation agent",
        usage_hint=(
            "Use to run focused validation commands, diagnose failures, and "
            "report exactly what passed or failed."
        ),
        model="parent",
        thinking_effort="medium",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
        }),
        system_prompt=_TEST_PROMPT,
        max_iterations=iteration_cap("test", 100),
    ),
    "browser-qa": AgentType(
        name="browser-qa",
        description="Frontend/browser behavior verification agent",
        usage_hint=(
            "Use to inspect a running UI, exercise workflows, check layout "
            "responsiveness, and capture browser-side evidence."
        ),
        model="parent",
        thinking_effort="medium",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
            "browser_execute_js", "browser_screenshot",
        }),
        system_prompt=_BROWSER_QA_PROMPT,
        max_iterations=iteration_cap("browser-qa", 100),
    ),
    "performance": AgentType(
        name="performance",
        description="Performance profiling and optimization investigator",
        usage_hint=(
            "Use when the app feels hot, laggy, memory-heavy, or slow. It "
            "should measure, identify low-risk optimizations, and avoid "
            "reducing feature limits or capability."
        ),
        model="parent",
        thinking_effort="high",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
            "browser_execute_js", "browser_screenshot",
        }),
        system_prompt=_PERFORMANCE_PROMPT,
        max_iterations=iteration_cap("performance", 140),
    ),
    "docs": AgentType(
        name="docs",
        description="Documentation writing agent",
        usage_hint=(
            "Use for writing design docs, implementation notes, and codebase "
            "guides once the relevant source has been inspected."
        ),
        model="parent",
        thinking_effort="medium",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "write_file", "edit_file",
            "list_directory", "glob", "grep",
        }),
        system_prompt=_DOCS_PROMPT,
        max_iterations=iteration_cap("docs", 100),
    ),
    "memory-curator": AgentType(
        name="memory-curator",
        description="Memory and skill pruning/planning agent",
        usage_hint=(
            "Use to inspect durable memory and available skills, identify "
            "stale or missing context, and recommend exact changes."
        ),
        model="parent",
        thinking_effort="medium",
        model_policy="inherit",
        tool_include=frozenset({
            "bash", "read_file", "list_directory", "glob", "grep",
            "list_skills", "search_skills", "load_skill",
        }),
        system_prompt=_MEMORY_CURATOR_PROMPT,
        max_iterations=iteration_cap("memory-curator", 80),
    ),
    "specifier": AgentType(
        name="specifier",
        description="Kanban card specifier (triage -> ready)",
        usage_hint=(
            "Use under kanban coordination to expand a triage card into "
            "a structured spec (definition_of_done, references, "
            "verify_with, token_budget) and promote it to ready."
        ),
        model="parent",
        thinking_effort="low",
        model_policy="inherit",
        tool_include=frozenset({
            "kanban",
            "read_file", "list_directory", "glob", "grep",
        }),
        system_prompt=_SPECIFIER_PROMPT,
        max_iterations=iteration_cap("specifier", 30),
    ),
}


def load_agent_types(workspace: Path | str | None = None) -> dict[str, AgentType]:
    """Load built-in plus user/project markdown-backed agent profiles."""
    agent_types = dict(AGENT_TYPES)
    for path in _agent_profile_paths(workspace):
        try:
            agent_type = _load_agent_profile(path)
        except Exception as exc:
            logger.warning("failed to load agent profile %s: %s", path, exc)
            continue
        agent_types[agent_type.name] = agent_type
    return agent_types


def _agent_profile_paths(workspace: Path | str | None) -> list[Path]:
    roots: list[Path] = [Path.home() / ".freyja" / "agents"]
    if workspace is not None:
        roots.append(Path(workspace).expanduser().resolve() / ".freyja" / "agents")

    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("**/*.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return paths


def _load_agent_profile(path: Path) -> AgentType:
    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(text)
    name = str(metadata.get("name") or path.stem).strip()
    description = str(
        metadata.get("description")
        or f"Custom agent profile from {path.name}"
    ).strip()
    usage_hint = str(
        metadata.get("usage_hint")
        or metadata.get("usage")
        or "Use when this profile's description matches the task."
    ).strip()

    model_value: Any
    if "models" in metadata:
        model_value = _as_list(metadata.get("models"))
    else:
        raw_model = metadata.get("model") or "parent"
        raw_list = _as_list(raw_model)
        model_value = raw_list if isinstance(raw_model, list) else raw_model
        if isinstance(raw_model, str) and raw_model.strip().startswith("["):
            model_value = raw_list

    model_fallbacks = tuple(
        _as_list(metadata.get("model_fallbacks") or metadata.get("fallbacks"))
    )
    tool_include = _as_frozenset(
        metadata.get("tools") or metadata.get("tool_include")
    )
    tool_exclude = _as_frozenset(metadata.get("tool_exclude"))

    max_iterations_raw = metadata.get("max_iterations") or metadata.get("max_steps")
    try:
        max_iterations = int(max_iterations_raw) if max_iterations_raw else 25
    except (TypeError, ValueError):
        max_iterations = 25

    return AgentType(
        name=name,
        description=description,
        usage_hint=usage_hint,
        model=model_value,
        thinking_effort=str(
            metadata.get("thinking_effort") or metadata.get("thinking") or "medium"
        ),
        model_policy=str(metadata.get("model_policy") or "first_available"),
        model_fallbacks=model_fallbacks,
        tool_include=tool_include,
        tool_exclude=tool_exclude,
        system_prompt=body.strip(),
        max_iterations=max_iterations,
        source=str(path),
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_index = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_index = idx
            break
    if end_index < 0:
        return {}, text

    metadata: dict[str, Any] = {}
    for raw_line in lines[1:end_index]:
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = _parse_frontmatter_value(value.strip())

    body = "\n".join(lines[end_index + 1:]).lstrip("\n")
    return metadata, body


def _parse_frontmatter_value(value: str) -> Any:
    if not value:
        return ""
    if value[0] in ("'", '"') and value[-1:] == value[0]:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [
            part.strip().strip("'\"")
            for part in inner.split(",")
            if part.strip()
        ]
    return value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        return _as_list(_parse_frontmatter_value(text))
    return [part.strip().strip("'\"") for part in text.split(",") if part.strip()]


def _as_frozenset(value: Any) -> frozenset[str] | None:
    items = _as_list(value)
    return frozenset(items) if items else None


def get_agent_type(name: str, workspace: Path | str | None = None) -> AgentType:
    """Look up an agent type by name, falling back to 'general'."""
    agent_types = load_agent_types(workspace)
    return agent_types.get(name, agent_types["general"])


def agent_types_for_prompt(
    workspace: Path | str | None = None,
    parent_model: str | None = None,
) -> str:
    """Generate the sub-agent types section for the parent system prompt.

    Auto-generated from the active registry so adding a new type is
    zero-touch on the prompt side.
    """
    agent_types = load_agent_types(workspace)
    lines: list[str] = ["## Sub-agent types\n"]
    lines.append(
        "When delegating work, choose the right agent type for the job. "
        "Pass `agent_type` to the `sub_agent` tool (defaults to `general`).\n"
    )
    lines.append(
        "Profile metadata below is authoritative: model policy, thinking, "
        "tool surface, max iterations, and source.\n"
    )
    for atype in agent_types.values():
        metadata = (
            f"model: {model_summary(atype)}; "
            f"thinking: {atype.thinking_effort}; "
            f"tools: {tool_summary(atype)}; "
            f"max iterations: {atype.max_iterations}"
        )
        if parent_model is not None:
            resolution = resolve_model_choice(atype, parent_model)
            if resolution.available:
                selected = f"; selected model now: {resolution.model}"
                if resolution.fallback_used:
                    selected += " (fallback)"
            else:
                reasons = ", ".join(
                    f"{model} unavailable ({reason})"
                    for model, reason in resolution.unavailable
                )
                selected = f"; currently unavailable: {reasons}"
            metadata += selected
        if atype.source != "builtin":
            metadata += f"; source: {atype.source}"
        lines.append(
            f"- **`{atype.name}`** — {atype.description}\n"
            f"  {atype.usage_hint}\n"
            f"  {metadata}\n"
        )
    return "\n".join(lines)
