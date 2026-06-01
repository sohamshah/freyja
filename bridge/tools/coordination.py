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
            "The `kanban` board is the ONLY planning surface in this mode. The\n"
            "`tasks` tool is intentionally NOT registered — it competed with the\n"
            "board for the agent's attention and pulled work off the swarm.\n"
            "Everything you'd reach for tasks to do (multi-step planning,\n"
            "synthesis checklists, decomposition) belongs on the board.\n"
            "\n"
            "One card = one shippable deliverable. Something a fresh agent could\n"
            "pick up cold, work on, and hand to a judge for pass/fail. Every card\n"
            "a worker finishes flows through the REVIEW pipeline automatically: a\n"
            "judge-deep subagent reads the artifacts and renders a verdict. Pass\n"
            "→ done. Fail → the worker is rewoken (same session) with the judge's\n"
            "critique to rework, up to 5 cycles before the card hits `blocked`.\n"
            "The board is the unit of dispatch, review, handoff, and parallel\n"
            "execution.\n"
            "\n"
            "DECISION RULE — when something is a piece of work, ask:\n"
            "  Does it need (a) another agent's eyes, (b) judge verification\n"
            "  before it can be considered done, (c) parallel execution, or\n"
            "  (d) a clean handoff between phases?\n"
            "    → YES to any: kanban card. Always.\n"
            "    → NO to all:  just do it without recording — most one-step\n"
            "      internal moves don't deserve any tracking at all.\n"
            "\n"
            "OPERATING LOOP:\n"
            "  1. New work arrives → decompose into kanban cards FIRST. Each card\n"
            "     is one deliverable with a checkable definition of done. Use the\n"
            "     `parents` field to gate dependencies between cards.\n"
            "  2. Assign workers via the `assignee` field. Leave it blank and the\n"
            "     dispatcher will pick the `general` profile; otherwise name a\n"
            "     specific agent_type. When you spawn a worker via the `sub_agent`\n"
            "     tool, pass `kanban_task_id=<card_id>` so the card binds to the\n"
            "     worker's session.\n"
            "  3. The worker does the work and calls `kanban` action=`complete`\n"
            "     when finished.\n"
            "  4. Card auto-routes to REVIEW. Judge runs. You don't drive this —\n"
            "     the dispatcher does. Verdict either lands the card in `done` or\n"
            "     wakes the worker for rework.\n"
            "  5. You can split a card MID-WORK by creating CHILD kanban cards\n"
            "     with `parents=[<parent_id>]`. Use this when a piece you're\n"
            "     working on turns out to need its own agent / its own judge /\n"
            "     its own parallel path.\n"
            "\n"
            "CREATING A CARD DOES NOT AUTHORIZE YOU TO DO ITS WORK YOURSELF.\n"
            "  The board exists so workers can pick cards up. If you create a\n"
            "  card and then immediately start executing its work in your own\n"
            "  session, you are acting as both decomposer and worker — defeating\n"
            "  the point of the board. If autopilot is off and you need a card\n"
            "  executed now, tell the operator that the board is queued and\n"
            "  autopilot is disabled. Do not silently become the worker.\n"
            "\n"
            "THE BOARD IS A DURABLE RECORD OF WHAT SHIPPED — not just a dispatch\n"
            "surface. Anything that is a shipped deliverable — code changes,\n"
            "files written, configuration changed — belongs on a card even when\n"
            "only one agent is doing it. Future operators and future agents\n"
            "reconstruct what happened by reading the board, not by reading chat\n"
            "history. If you finish a piece of real work without creating a card\n"
            "for it, that work effectively didn't happen as far as the board is\n"
            "concerned.\n"
            "\n"
            "THINGS TO AVOID:\n"
            "  · Don't bypass the board for cross-agent handoffs. Use card\n"
            "    comments + status changes, not ad-hoc chat.\n"
            "  · Don't act as the worker for cards you yourself created. Either\n"
            "    let a dispatched worker pick them up, or explicitly `claim` the\n"
            "    card before doing work so the ownership is recorded.\n"
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
