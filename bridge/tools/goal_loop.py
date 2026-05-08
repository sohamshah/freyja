"""Session-local goal loop state and judging prompts."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


GOAL_JUDGE_SYSTEM_PROMPT = """You judge whether an autonomous agent has satisfied a standing user goal.

Return strict JSON only:
{"done": true|false, "reason": "short reason", "confidence": 0.0-1.0}

Mark done only when the latest assistant response clearly reports that the requested deliverable is complete, the objective is impossible/blocked and needs real user input, or continuing would be pointless. Otherwise mark done false.
"""

GOAL_JUDGE_USER_TEMPLATE = """Standing goal:
{goal}

Latest assistant response:
{response}

Has the standing goal been satisfied? Return strict JSON only."""

GOAL_CONTINUATION_TEMPLATE = """[Continuing toward the active Freyja goal]

Goal: {goal}

The previous turn did not complete the goal. Continue from the current transcript, use tools as needed, and either make concrete progress or finish with a clear completion note."""


@dataclass
class GoalVerdict:
    done: bool
    reason: str
    confidence: float = 0.0
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "reason": self.reason,
            "confidence": self.confidence,
            "raw": self.raw,
        }


@dataclass
class GoalState:
    goal: str
    status: str = "active"
    turns_used: int = 0
    max_turns: int = 20
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_verdict: GoalVerdict | None = None
    pause_reason: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    def continuation_prompt(self) -> str:
        return GOAL_CONTINUATION_TEMPLATE.format(goal=self.goal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "turnsUsed": self.turns_used,
            "maxTurns": self.max_turns,
            "createdAt": int(self.created_at * 1000),
            "updatedAt": int(self.updated_at * 1000),
            "lastVerdict": self.last_verdict.to_dict() if self.last_verdict else None,
            "pauseReason": self.pause_reason,
        }


def parse_goal_verdict(text: str) -> GoalVerdict:
    """Parse a goal judge response, falling back to continue on malformed JSON."""
    raw = (text or "").strip()
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except Exception:
                payload = None

    if not isinstance(payload, dict):
        return GoalVerdict(
            done=False,
            reason="Judge response was not valid JSON; continuing conservatively.",
            confidence=0.0,
            raw=raw,
        )

    done = bool(payload.get("done"))
    reason = str(payload.get("reason") or "").strip()
    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(float(confidence_raw), 1.0))
    except Exception:
        confidence = 0.0
    return GoalVerdict(
        done=done,
        reason=reason or ("Goal satisfied." if done else "Goal still needs work."),
        confidence=confidence,
        raw=raw,
    )
