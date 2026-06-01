"""Outcome taxonomy for the skill-learning loop.

Two orthogonal axes per loaded-skill observation:

  · **Utilization** — how was the skill used?
  · **Utility**     — was it good that we loaded it?

Cross-product collapses to 12 named categories that cover the space
densely enough for the value ranker (``value_score.py``) and the
eventual decay model (Phase 4) without fragmenting the data.

The weights here are the only place the loop assigns "this is good /
this is bad". Every other module reads them through ``weight_for``,
so a single edit here re-tunes ranking + decay everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ── Polarity buckets ─────────────────────────────────────────────────


POSITIVE = "positive"
NEUTRAL = "neutral"
NEGATIVE = "negative"
DECAY = "decay"


# ── The 12 categories ────────────────────────────────────────────────


@dataclass(frozen=True)
class CategoryDef:
    """One outcome label. ``weight`` is what gets summed in the V-score
    rollup (``value_score.compute_rollup``). ``polarity`` is for
    surface display + future decay-model bucket gates."""

    name: str
    polarity: str
    weight: float
    one_line: str


# Order here is the order shown in operator UIs. Polarity groupings are
# visually obvious so the operator can read the table at a glance.
ALL: tuple[CategoryDef, ...] = (
    # Positive — skill helped, often visibly
    CategoryDef(
        name="user_endorsed",
        polarity=POSITIVE,
        weight=+2.0,
        one_line="User explicitly affirmed the response that the skill influenced.",
    ),
    CategoryDef(
        name="cited",
        polarity=POSITIVE,
        weight=+1.5,
        one_line="Agent's response text explicitly referenced the skill (e.g. 'Per X, …').",
    ),
    CategoryDef(
        name="compounded",
        polarity=POSITIVE,
        weight=+1.2,
        one_line="Skill enabled or unblocked another skill or downstream workflow.",
    ),
    # Mixed — skill partially worked, no negative signal
    CategoryDef(
        name="partial",
        polarity=NEUTRAL,
        weight=+0.3,
        one_line="Some of the skill's guidance was followed, some was not, no correction.",
    ),
    # Default neutral — the most common outcome for a healthy skill
    CategoryDef(
        name="clean",
        polarity=NEUTRAL,
        weight=0.0,
        one_line="Task completed; no skill-attributable positive or negative signal.",
    ),
    # Mis-use — skill wasn't worth loading on this task
    CategoryDef(
        name="ignored",
        polarity=NEUTRAL,
        weight=-0.2,
        one_line="Skill loaded but the agent's subsequent behavior shows no influence.",
    ),
    CategoryDef(
        name="redundant",
        polarity=NEUTRAL,
        weight=-0.3,
        one_line="Another skill loaded this turn covered the same ground; this one added nothing.",
    ),
    CategoryDef(
        name="false_trigger",
        polarity=NEGATIVE,
        weight=-0.6,
        one_line="Triggers matched (lexical) but the skill's content was semantically irrelevant.",
    ),
    # Active harm
    CategoryDef(
        name="correction",
        polarity=NEGATIVE,
        weight=-1.0,
        one_line="User explicitly corrected behavior the skill was supposed to govern.",
    ),
    CategoryDef(
        name="superseded",
        polarity=NEGATIVE,
        weight=-1.2,
        one_line="Agent abandoned the skill's approach mid-task for an alternative that worked.",
    ),
    CategoryDef(
        name="error_loop",
        polarity=NEGATIVE,
        weight=-1.5,
        one_line="≥3 repeated tool errors of the same family that the skill should have prevented.",
    ),
    # Decay — skill's content is going stale relative to current env
    CategoryDef(
        name="outdated",
        polarity=DECAY,
        weight=-1.5,
        one_line="Skill's advice was followed and produced an environment-attributable failure.",
    ),
)


_BY_NAME = {c.name: c for c in ALL}
NAMES: tuple[str, ...] = tuple(c.name for c in ALL)


def get(name: str) -> CategoryDef | None:
    """Look up a category def by name. Returns None for unknown labels —
    callers can treat that as 'classifier produced garbage, fall back to
    clean' rather than raising."""
    return _BY_NAME.get(name)


def weight_for(name: str) -> float:
    """Score weight for an outcome name. Unknown → 0.0 (treats as neutral
    so a malformed classifier output doesn't poison the rollup)."""
    c = _BY_NAME.get(name)
    return c.weight if c is not None else 0.0


def is_negative(name: str) -> bool:
    """True for categories that count against the skill in ranking."""
    c = _BY_NAME.get(name)
    return c is not None and c.polarity in (NEGATIVE, DECAY)


def is_positive(name: str) -> bool:
    c = _BY_NAME.get(name)
    return c is not None and c.polarity == POSITIVE


def schema_enum() -> list[str]:
    """The list to feed into the classifier's JSON schema as an enum
    constraint. Order doesn't matter for the schema; we keep it stable so
    repeated builds produce byte-identical schemas (matters for prompt
    cache hits)."""
    return list(NAMES)


def render_table(occurrences: Iterable[tuple[str, int]]) -> str:
    """Compact per-skill rendering used in the system-prompt skill listing
    + operator UIs. Shows non-zero counts only.

    ``occurrences`` is ``[(category_name, count)]``.
    """
    parts: list[str] = []
    for name in NAMES:  # render in canonical order
        count = next((n for cat, n in occurrences if cat == name), 0)
        if count > 0:
            parts.append(f"{count} {name}")
    return ", ".join(parts) or "no observations yet"
