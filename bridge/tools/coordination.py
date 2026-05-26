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
            "\n"
            "Two planning surfaces exist in this mode. They are NOT interchangeable —\n"
            "they have different semantics and different costs. Pick the right one for\n"
            "each piece of work.\n"
            "\n"
            "  `kanban` cards  ← the shared, durable work board.\n"
            "    One card = one shippable deliverable unit. Something a fresh agent\n"
            "    could pick up cold, work on, and hand to a judge for pass/fail.\n"
            "    Every card that a worker finishes flows through the REVIEW pipeline\n"
            "    automatically: a judge-deep subagent reads the artifacts and renders\n"
            "    a verdict. Pass → done. Fail → the worker is rewoken (same session)\n"
            "    with the judge's critique to rework, up to 5 cycles before the card\n"
            "    hits `blocked`. The board is the unit of dispatch, review, handoff,\n"
            "    and parallel execution.\n"
            "\n"
            "  `tasks`         ← YOUR own private scratch checklist.\n"
            "    Sequential todos you want to keep straight while working on one\n"
            "    deliverable. Drafting passes, ordering decisions, synthesis steps.\n"
            "    Tasks are NEVER dispatched, NEVER judged, NEVER gate anything. They\n"
            "    are scratch paper. The operator sees them; workers do not.\n"
            "\n"
            "DECISION RULE — when something is a piece of work, ask:\n"
            "  Does it need (a) another agent's eyes, (b) judge verification before\n"
            "  it can be considered done, (c) parallel execution, or (d) a clean\n"
            "  handoff between phases?\n"
            "    → YES to any: kanban card. Always.\n"
            "    → NO to all:  task. (Or: just do it without recording — most one-\n"
            "      step internal moves don't deserve any tracking at all.)\n"
            "\n"
            "OPERATING LOOP:\n"
            "  1. New work arrives → decompose into kanban cards FIRST. Each card is\n"
            "     one deliverable with a checkable definition of done. Use the\n"
            "     `parents` field to gate dependencies between cards.\n"
            "  2. Assign workers via the `assignee` field. Leave it blank and the\n"
            "     dispatcher will pick the `general` profile; otherwise name a\n"
            "     specific agent_type. When you spawn a worker via the `sub_agent`\n"
            "     tool, pass `kanban_task_id=<card_id>` so the card binds to the\n"
            "     worker's session.\n"
            "  3. The worker does the work, optionally using `tasks` as its own\n"
            "     private checklist for the ticket's internal steps, and calls\n"
            "     `kanban` action=`complete` when finished.\n"
            "  4. Card auto-routes to REVIEW. Judge runs. You don't drive this — the\n"
            "     dispatcher does. Verdict either lands the card in `done` or wakes\n"
            "     the worker for rework.\n"
            "  5. You can split a card MID-WORK by creating CHILD kanban cards with\n"
            "     `parents=[<parent_id>]`. Use this when a piece you're working on\n"
            "     turns out to need its own agent / its own judge / its own parallel\n"
            "     path.\n"
            "\n"
            "THINGS TO AVOID:\n"
            "  · Do not use `tasks` as your decomposition surface. Tasks are flat,\n"
            "    untracked, unjudged. Decomposition belongs on the board.\n"
            "  · Don't make cards too small. Each card costs a judge review cycle.\n"
            "    `card_001: read file A` is wasteful. A card should be a meaningful\n"
            "    deliverable, not a keystroke.\n"
            "  · Don't make cards too large either. If the judge couldn't reasonably\n"
            "    verify the whole deliverable in one pass, split it.\n"
            "  · Don't bypass the board for cross-agent handoffs. Use card comments\n"
            "    + status changes, not ad-hoc chat.\n"
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
