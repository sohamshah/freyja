"""Session coordination strategy definitions for Freyja."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def current_datetime_block() -> str:
    """Return a one-line "current date and time" block for inclusion in
    every agent system prompt.

    Models behave very differently when they don't know the current
    date — they fall back to their training cutoff, refuse temporal
    questions, or hallucinate. Includes both ISO-8601 UTC (machine-
    readable) and a local-tz human format (the local time the operator
    sees on their machine).
    """
    now_local = datetime.now().astimezone()
    iso_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    human = now_local.strftime("%A %B %d, %Y at %H:%M %Z")
    return (
        f"The current date and time is: {human} "
        f"(ISO-8601 UTC: {iso_utc})."
    )


STRATEGY_BUS = "bus"
STRATEGY_ISOLATED = "isolated"
STRATEGY_KANBAN = "kanban"
STRATEGY_GOAL = "goal"


@dataclass(frozen=True)
class CoordinationStrategy:
    id: str
    label: str
    summary: str
    uses_message_bus: bool = False
    uses_kanban: bool = False


COORDINATION_STRATEGIES: dict[str, CoordinationStrategy] = {
    STRATEGY_BUS: CoordinationStrategy(
        id=STRATEGY_BUS,
        label="Message bus",
        summary="Current Freyja mode: profile-driven agents publish/read findings on a shared bus.",
        uses_message_bus=True,
    ),
    STRATEGY_ISOLATED: CoordinationStrategy(
        id=STRATEGY_ISOLATED,
        label="Tasks",
        summary="Solo/task mode: the parent keeps an explicit task ledger and allocates work as needed.",
    ),
    STRATEGY_KANBAN: CoordinationStrategy(
        id=STRATEGY_KANBAN,
        label="Kanban",
        summary="Board-driven mode: plan cards, link dependencies, assign agents, and report progress through a shared board.",
        uses_kanban=True,
    ),
    STRATEGY_GOAL: CoordinationStrategy(
        id=STRATEGY_GOAL,
        label="Goal loop",
        summary="Same-session autonomous continuation: a judge checks the goal after every turn and keeps the session moving until done.",
    ),
}


def normalize_coordination_strategy(value: str | None) -> str:
    key = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "default": STRATEGY_BUS,
        "message-bus": STRATEGY_BUS,
        "messages": STRATEGY_BUS,
        "delegate": STRATEGY_ISOLATED,
        "delegation": STRATEGY_ISOLATED,
        "solo": STRATEGY_ISOLATED,
        "board": STRATEGY_KANBAN,
        "tasks": STRATEGY_ISOLATED,
        "task": STRATEGY_ISOLATED,
        "goal-loop": STRATEGY_GOAL,
        "goals": STRATEGY_GOAL,
        "ralph": STRATEGY_GOAL,
    }
    key = aliases.get(key, key)
    if key not in COORDINATION_STRATEGIES:
        return STRATEGY_BUS
    return key


def get_coordination_strategy(value: str | None) -> CoordinationStrategy:
    return COORDINATION_STRATEGIES[normalize_coordination_strategy(value)]


def strategy_uses_message_bus(value: str | None) -> bool:
    return get_coordination_strategy(value).uses_message_bus


def strategy_uses_kanban(value: str | None) -> bool:
    return get_coordination_strategy(value).uses_kanban


def coordination_prompt(value: str | None) -> str:
    strategy = normalize_coordination_strategy(value)
    if strategy == STRATEGY_ISOLATED:
        return (
            "Coordination strategy: TASK-FIRST SOLO.\n"
            "- The `tasks` tool is BOTH your personal planning surface and the coordination ledger for sub-agents in this mode.\n"
            "- Create tasks for meaningful units of work, claim active work, heartbeat during long-running work, and complete/block with clear handoffs.\n"
            "- Use sub-agents as independent workers only when useful. Pass `task_id` when spawning a worker for an existing task — the worker inherits the tasks tool scoped to that task and updates it as it works.\n"
            "- Workers do not get message-bus tools here; the parent owns synthesis and the task ledger is the durable coordination surface.\n"
        )
    if strategy == STRATEGY_KANBAN:
        return (
            "Coordination strategy: KANBAN BOARD.\n"
            "- TWO PLANNING SURFACES — use them for different things:\n"
            "  - `kanban` cards = the swarm's shared work board. Anything that should be picked up by a worker, dispatched to a profile, or gated by dependencies between agents.\n"
            "  - `tasks` = your own short-term planning that doesn't need to be visible to workers (synthesis steps, ordering decisions, drafting passes). Operator sees them; workers don't.\n"
            "- For multi-agent work: kanban cards. For your own multi-step thinking: tasks. They're not interchangeable.\n"
            "- Create kanban cards for meaningful units of work, link dependency gates, and assign profiles explicitly.\n"
            "- When spawning a sub-agent for a card, pass `kanban_task_id` and include the card id in the task prompt.\n"
            "- Workers should inspect their card first, heartbeat/comment during long work, and complete or block it with a useful handoff.\n"
            "- Prefer board comments and card status over ad-hoc chat for cross-agent handoffs.\n"
        )
    if strategy == STRATEGY_GOAL:
        return (
            "Coordination strategy: GOAL LOOP.\n"
            "- Treat the user's first request as an active objective, not a one-turn prompt.\n"
            "- Use the `tasks` tool to break the goal into milestones the judge can verify against. Complete tasks only when their acceptance criteria are met — the judge sees what you've claimed done and may overrule.\n"
            "- Work normally inside this same session; use tools and sub-agents when they materially help.\n"
            "- After each response, Freyja will judge whether the active goal is complete and may continue automatically.\n"
            "- Finish with a clear completion note when the objective is done or explicitly blocked by missing user input.\n"
        )
    return (
        "Coordination strategy: MESSAGE BUS.\n"
        "- Use `tasks` for your own multi-step planning — especially for synthesis-heavy work (reading multiple findings, writing a structured deliverable, comparing options). Tasks are private to you; the operator sees them.\n"
        "- Use sub-agent profiles for parallel work and ask workers to publish findings when discoveries help siblings.\n"
        "- Use `read_findings` during overlapping research or review so agents can build on each other.\n"
        "- The parent should still synthesize the final answer and resolve conflicts — your task list is how the operator follows your synthesis path.\n"
    )
