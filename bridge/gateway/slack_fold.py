"""Fold the step cards of a sealed Slack message into one plan block.

While a turn runs, the stream consumer shows the thinking and every
tool call as a ``task_card`` in the order they happen, so the operator
can follow along. Once the message is sealed those cards are history,
and a long turn leaves a thread that is mostly card rows: one row per
step, dozens of rows between two paragraphs of prose.

Slack's ``plan`` block holds up to 50 task cards collapsed under one
title, and ``chat.update`` accepts it on a sealed streamed message
(measured 2026-10-22: a 30k plan went through ``chat.update`` where
``chat.postMessage`` answered ``msg_blocks_too_long``). A message may
hold ONE plan block ("Cannot have more than 1 plan block"), so every
card of the message goes into one plan placed where the first card was,
and the prose keeps its order around it. That is also how Slack itself
lays out a stream in ``plan`` display mode.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Slack: "Cannot have more than 50 tasks".
PLAN_TASK_LIMIT = 50
# Tool names spelled out in the plan title before "more".
_TITLE_NAMES = 3
# Titles listed inside the overflow task.
_OVERFLOW_LINES = 40
_TASK_STATUSES = {"pending", "in_progress", "complete", "error"}


def fold_task_cards(
    blocks: list[dict[str, Any]],
    *,
    keep_output: bool = True,
    max_tasks: int = PLAN_TASK_LIMIT,
) -> list[dict[str, Any]] | None:
    """Return the message's blocks with its ``task_card`` blocks folded
    into one ``plan`` block, or None when there is nothing to fold.

    The plan takes the place of the first card; the other blocks keep
    their order. ``block_id`` values are dropped because Slack wants
    fresh ones on an update. Past ``max_tasks`` cards, the last task of
    the plan stands for the rest and lists their titles. With
    ``keep_output`` off the tasks carry titles and sources only, for a
    message whose outputs make the update too long.
    """
    cards = [b for b in blocks if isinstance(b, dict) and b.get("type") == "task_card"]
    if not cards:
        return None
    tasks = [_task(card, i, keep_output=keep_output) for i, card in enumerate(cards)]
    if len(tasks) > max_tasks:
        head, rest = tasks[: max_tasks - 1], tasks[max_tasks - 1 :]
        head.append(_overflow_task(rest))
        tasks = head
    plan = {"type": "plan", "title": fold_title(cards), "tasks": tasks}
    out: list[dict[str, Any]] = []
    placed = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "task_card":
            if not placed:
                out.append(plan)
                placed = True
            continue
        out.append(_without_block_id(block))
    return out


def fold_title(cards: list[dict[str, Any]]) -> str:
    """``"14 steps, 1 failed (bash ×9, read_file ×3, thinking)"``."""
    n = len(cards)
    failed = sum(1 for c in cards if c.get("status") == "error")
    head = f"{n} step" if n == 1 else f"{n} steps"
    if failed:
        head += f", {failed} failed"
    names = Counter(step_name(str(c.get("title") or "")) for c in cards)
    parts = [f"{name} ×{k}" if k > 1 else name for name, k in names.most_common(_TITLE_NAMES)]
    if len(names) > _TITLE_NAMES:
        parts.append("more")
    return f"{head} ({', '.join(parts)})" if parts else head


def step_name(title: str) -> str:
    """The tool a card stands for: the part before the first colon
    (``bash: Run the tests`` → ``bash``), else the first word."""
    title = title.strip()
    head, sep, _ = title.partition(":")
    if sep and head and " " not in head and len(head) <= 40:
        return head
    words = title.split()
    return words[0] if words else "step"


def _task(card: dict[str, Any], index: int, *, keep_output: bool) -> dict[str, Any]:
    status = str(card.get("status") or "complete")
    if status not in _TASK_STATUSES:
        status = "complete"
    task: dict[str, Any] = {
        "task_id": str(card.get("task_id") or f"step-{index}"),
        "title": str(card.get("title") or "step"),
        "status": status,
    }
    if keep_output:
        for key in ("details", "output"):
            value = card.get(key)
            if isinstance(value, dict):
                task[key] = _without_block_id(value)
    sources = card.get("sources")
    if isinstance(sources, list) and sources:
        task["sources"] = sources
    return task


def _overflow_task(rest: list[dict[str, Any]]) -> dict[str, Any]:
    failed = sum(1 for t in rest if t.get("status") == "error")
    title = f"{len(rest)} more steps"
    if failed:
        title += f", {failed} failed"
    lines = [str(t.get("title") or "step") for t in rest[:_OVERFLOW_LINES]]
    if len(rest) > _OVERFLOW_LINES:
        lines.append(f"and {len(rest) - _OVERFLOW_LINES} more")
    return {
        "task_id": "fold-overflow",
        "title": title,
        "status": "error" if failed else "complete",
        "output": {
            "type": "rich_text",
            "elements": [{
                "type": "rich_text_list",
                "style": "bullet",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": line}]}
                    for line in lines
                ],
            }],
        },
    }


def _without_block_id(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if k != "block_id"}
