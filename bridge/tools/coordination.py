"""Session coordination strategy definitions for Freyja."""

from __future__ import annotations

from dataclasses import dataclass


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
            "- Use the `tasks` tool as a lightweight visible task ledger for meaningful units of work.\n"
            "- Claim or update tasks as you work; heartbeat during long-running work and complete/block with clear handoffs.\n"
            "- Use sub-agents as independent workers only when useful. Pass `task_id` when spawning a worker for an existing task.\n"
            "- Workers do not get message-bus tools; the parent owns synthesis and the task ledger is the durable coordination surface.\n"
        )
    if strategy == STRATEGY_KANBAN:
        return (
            "Coordination strategy: KANBAN BOARD.\n"
            "- Use the `kanban` tool as the shared coordination surface before launching broad work.\n"
            "- Create cards for meaningful units of work, link dependency gates, and assign profiles explicitly.\n"
            "- When spawning a sub-agent for a card, pass `kanban_task_id` and include the card id in the task prompt.\n"
            "- Workers should inspect their card first, heartbeat/comment during long work, and complete or block it with a useful handoff.\n"
            "- Prefer board comments and card status over ad-hoc chat for cross-agent handoffs.\n"
        )
    if strategy == STRATEGY_GOAL:
        return (
            "Coordination strategy: GOAL LOOP.\n"
            "- Treat the user's first request as an active objective, not a one-turn prompt.\n"
            "- Work normally inside this same session; use tools and sub-agents when they materially help.\n"
            "- After each response, Freyja will judge whether the active goal is complete and may continue automatically.\n"
            "- Finish with a clear completion note when the objective is done or explicitly blocked by missing user input.\n"
        )
    return (
        "Coordination strategy: MESSAGE BUS.\n"
        "- Use sub-agent profiles for parallel work and ask workers to publish findings when discoveries help siblings.\n"
        "- Use `read_findings` during overlapping research or review so agents can build on each other.\n"
        "- The parent should still synthesize the final answer and resolve conflicts.\n"
    )
