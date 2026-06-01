"""Outcome classifier for the skill-learning loop.

After a skill loads, we want to know what happened next. Was it
helpful? Did the user correct it? Did the agent abandon the skill's
advice halfway? The classifier reads a window of post-load turns and
emits a single best-fit category from ``categories.NAMES``.

LLM-driven, schema-enforced. The model can only return one of the 12
known labels (enforced via provider-side structured output), so a
hallucinated outcome can't poison the event log.

Cost
────
One Opus call per loaded-skill per session, typical input ~3-10k
tokens of conversation excerpt + skill body. Roughly $0.05-$0.15
per call. Heavy day: ~$1 in classification calls. Defensible —
this is the only place we LEARN whether a skill is working.

Model choice
────────────
Defaults to ``claude-opus-4-8`` (the operator's chosen quality tier).
Overridable via ``FREYJA_OUTCOME_CLASSIFIER_MODEL`` env var so a
cost-sensitive deployment can drop to Sonnet without code changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from bridge.knowledge.learning import categories
from engine.types import Message, ThinkingConfig

logger = logging.getLogger(__name__)


# ── Schema (enforced provider-side) ──────────────────────────────────


def classifier_schema() -> dict[str, Any]:
    """JSON schema for the structured-output call.

    ``category`` is constrained to the 12-enum so the classifier physically
    cannot emit a label the rollup logic doesn't know about. ``secondary``
    is optional and unconstrained polarity, used to capture mixed signal
    (e.g. primary=`cited` secondary=`partial`)."""
    enum = categories.schema_enum()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "evidence"],
        "properties": {
            "category": {
                "type": "string",
                "enum": enum,
                "description": (
                    "Best-fit single label for how this skill load played "
                    "out. See instructions for definitions."
                ),
            },
            "secondary": {
                "type": ["string", "null"],
                "enum": enum + [None],
                "description": (
                    "Optional second label when signal is genuinely mixed."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "One sentence pointing to the specific user message, "
                    "tool call, or agent statement that drove this label. "
                    "Quote a fragment when possible — the operator reviews "
                    "this in the value timeline."
                ),
                "maxLength": 600,
            },
        },
    }


# ── Prompt ───────────────────────────────────────────────────────────


def _build_classifier_prompt() -> str:
    """The system prompt for the classifier.

    Two design decisions:

    1. We expose the full category-definition table verbatim so the
       classifier doesn't guess from label names alone. The one-liners
       in ``categories.ALL`` are explicit about what each label means.

    2. We push hard on "pick ONE label" because Opus' default behavior
       on a mixed-signal turn is to hedge with multiple. We DO let
       it set ``secondary`` for genuine mixed cases but make it clear
       that the primary is the one that drives ranking.
    """
    rows = []
    for c in categories.ALL:
        rows.append(f"  · `{c.name}` ({c.polarity}): {c.one_line}")
    table = "\n".join(rows)
    return (
        "You are Freyja's outcome classifier. A skill was loaded into "
        "an agent's context in a recent session. Read the post-load "
        "window of conversation + tool activity and decide what "
        "happened.\n\n"
        "Pick EXACTLY ONE best-fit label from this table:\n\n"
        f"{table}\n\n"
        "Decision rules:\n"
        "  1. Read the skill body so you know what behavior the skill was "
        "trying to govern. The classification is about whether the skill's "
        "specific guidance played out — not whether the task succeeded.\n"
        "  2. If the user directly contradicted the skill, `correction` "
        "wins regardless of other signal.\n"
        "  3. If the agent's response text explicitly quotes/cites the "
        "skill AND the user accepted it, `cited` (or `user_endorsed` if "
        "the user explicitly affirmed).\n"
        "  4. If the post-load window is < 1 substantive turn — the "
        "session ended or the operator asked something orthogonal — pick "
        "`ignored` or `clean` rather than guessing.\n"
        "  5. If the agent abandoned the skill's approach mid-task and a "
        "different approach succeeded, `superseded`.\n"
        "  6. Use `outdated` ONLY when there's evidence of an "
        "environment-level failure attributable to the skill's advice "
        "(missing path, dead API, renamed function). Otherwise prefer "
        "`correction` / `superseded` even if the skill seems wrong.\n"
        "  7. `false_trigger` is for skills whose triggers MATCHED but "
        "whose content was off-topic for the actual task. Different from "
        "`ignored` (skill was relevant but the agent didn't act on it).\n\n"
        "Output a JSON object via the classifier tool. The `evidence` "
        "field must point at a specific message or tool call — quote a "
        "fragment when possible so the operator can spot-check your "
        "verdict."
    )


# ── Public API ───────────────────────────────────────────────────────


@dataclass
class OutcomeClassification:
    """Result returned by the classifier."""

    skill_name: str
    category: str
    secondary: str | None
    evidence: str


def _classifier_model() -> str:
    return os.environ.get("FREYJA_OUTCOME_CLASSIFIER_MODEL", "claude-opus-4-8")


async def classify(
    *,
    skill_name: str,
    skill_body: str,
    post_load_window: str,
    load_context: str = "",
) -> OutcomeClassification | None:
    """Classify a single skill-load outcome.

    ``post_load_window`` is the rendered conversation excerpt covering
    the turn the skill was loaded in plus the next ~3 turns (or
    end-of-session, whichever first). The watcher in ``outcome_watcher``
    is responsible for building this string.

    ``load_context`` is optional metadata describing why the skill was
    loaded (operator-issued ``/skill X``, agent-decided ``load_skill``
    call, etc.). The classifier uses it to disambiguate `ignored`
    (skill loaded by operator but agent didn't act on it) from
    `false_trigger` (skill auto-loaded but didn't apply).

    Returns None on provider error — caller decides whether to log
    a `clean` default or skip the event. Never raises.
    """
    # Local import: bridge.freyja_bridge.build_provider is the canonical
    # entry-point for any LLM client construction in Freyja, and lives in
    # a module that imports a lot of heavy stuff. We pay the import cost
    # only when a classification is actually requested, not at module
    # load.
    from bridge.freyja_bridge import build_provider

    try:
        provider = build_provider(_classifier_model(), thinking_level="off")
    except Exception:
        logger.exception("classifier: failed to build provider")
        return None

    user_message = (
        f"Skill name: {skill_name}\n\n"
        f"Skill body (the guidance that was in the agent's context):\n"
        f"---\n{skill_body.strip()}\n---\n\n"
        f"Load context: {load_context or 'auto-loaded by agent'}\n\n"
        f"Post-load conversation window:\n"
        f"---\n{post_load_window.strip()}\n---\n\n"
        f"Classify the outcome."
    )

    messages = [Message(role="user", content=user_message)]

    try:
        result = await provider.complete_structured(
            messages=messages,
            schema=classifier_schema(),
            schema_name="emit_outcome",
            schema_description="Emit the outcome classification.",
            system_prompt=_build_classifier_prompt(),
            max_tokens=600,
            thinking=ThinkingConfig(enabled=False),
        )
    except Exception:
        logger.exception("classifier: provider call failed for %s", skill_name)
        return None

    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        logger.warning(
            "classifier: provider returned no parsed dict for %s", skill_name,
        )
        return None

    category = str(parsed.get("category") or "")
    if categories.get(category) is None:
        # The structured-output enum constraint should make this
        # impossible, but if a provider quirk produces an unknown label
        # we fail safe by treating it as clean and logging.
        logger.warning(
            "classifier: unknown category %r for %s — defaulting to clean",
            category, skill_name,
        )
        category = "clean"

    secondary_raw = parsed.get("secondary")
    secondary = str(secondary_raw) if isinstance(secondary_raw, str) and secondary_raw else None
    if secondary and categories.get(secondary) is None:
        secondary = None

    evidence = str(parsed.get("evidence") or "").strip()

    return OutcomeClassification(
        skill_name=skill_name,
        category=category,
        secondary=secondary,
        evidence=evidence,
    )
