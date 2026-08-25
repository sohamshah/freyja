"""Parse the forked drafter's decision out of its final message.

Why a fenced block instead of a tool call
─────────────────────────────────────────
The forked drafter runs on the parent session's exact request prefix — same
tools, same system prompt, same transcript — because that byte-identical prefix
is what makes the parent's prompt cache hit. Anthropic's cache is prefix-
ordered (tools → system → messages), so adding even one tool definition to the
array invalidates everything behind it and the fork would pay full input rate
on the entire conversation it was created to reuse.

``propose_skill`` is registered in the parent's pool at WARM tier, which means
its schema is NOT in the request until something promotes it. Promoting it
would change the tools array. So the primary output path is a fenced block in
the drafter's final text, which costs nothing.

``propose_skill`` still works as a fallback if the model promotes it through
``tool_search`` — that path publishes on its own and this parser simply finds
no block, which the caller treats as "already handled or skipped".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: The fence label the drafter is told to use. Four+ backticks are requested so
#: a body containing ordinary ``` code fences doesn't terminate the block early.
FENCE_LABEL = "skill-candidate"

# ``\r?$`` rather than ``$``: with re.MULTILINE, ``$`` matches before the
# ``\n`` but AFTER the ``\r`` of a CRLF line ending, so a model that emits
# Windows line endings would produce no match at all and its decision block
# would be silently discarded as "no block emitted".
_FENCE_OPEN = re.compile(
    r"^[ \t]*(`{3,})[ \t]*" + FENCE_LABEL + r"[ \t]*\r?$", re.MULTILINE
)

VALID_SKILL_TYPES = ("build", "guard", "reference", "workflow")

#: Mirrors the provider-side cap the single-call drafter used, and the
#: 3-60 char rule ``confirmation._SAFE_NAME_RE`` actually enforces at the write
#: boundary. Kept here so a bad name is a parse failure with a reason rather
#: than a silent refusal three layers down.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,59}$")

MAX_BODY_CHARS = 60_000


@dataclass
class ForkDecision:
    """What the drafter decided, once parsed and validated."""

    decision: str  # "save" | "skip"
    rationale: str = ""
    name: str = ""
    description: str = ""
    skill_type: str = "build"
    triggers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    body: str = ""
    #: Set when a block was found but could not be used. The caller reports
    #: this rather than silently treating a malformed save as a skip.
    error: str = ""

    @property
    def is_save(self) -> bool:
        return self.decision == "save" and not self.error


def extract_block(text: str) -> str | None:
    """Return the JSON payload of the LAST ``skill-candidate`` fence.

    Follows the CommonMark fence rule: an opening run of N backticks is closed
    by a line of at least N backticks. That is what lets the drafter wrap a
    SKILL.md body that itself contains ``` fences — it opens with four.

    The LAST block wins so a drafter that shows a draft, reconsiders, and emits
    a final version is read as intending the final one.
    """
    if not text:
        return None
    found: list[str] = []
    for match in _FENCE_OPEN.finditer(text):
        fence = match.group(1)
        rest = text[match.end():]
        close = re.search(
            r"^[ \t]*`{" + str(len(fence)) + r",}[ \t]*\r?$", rest, re.MULTILINE
        )
        payload = rest[: close.start()] if close else rest
        payload = payload.strip()
        if payload:
            found.append(payload)
    return found[-1] if found else None


def _clean_list(raw: Any, limit: int = 8) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def parse_fork_decision(text: str) -> ForkDecision | None:
    """Parse the drafter's final message.

    ``None`` means no block was present at all — the drafter either published
    through ``propose_skill`` or wandered off. A ``ForkDecision`` with
    ``error`` set means a block WAS present but unusable, which is worth
    surfacing: a silently-dropped candidate looks identical to a rational skip.
    """
    payload = extract_block(text)
    if payload is None:
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return ForkDecision(decision="skip", error=f"candidate block is not valid JSON: {exc}")

    if not isinstance(data, dict):
        return ForkDecision(decision="skip", error="candidate block is not a JSON object")

    decision = str(data.get("decision") or "").strip().lower()
    rationale = str(data.get("rationale") or "").strip()

    if decision == "skip":
        return ForkDecision(decision="skip", rationale=rationale)
    if decision != "save":
        return ForkDecision(
            decision="skip",
            rationale=rationale,
            error=f"unknown decision {decision!r}",
        )

    name = str(data.get("name") or "").strip().lower()
    description = str(data.get("description") or "").strip()
    body = str(data.get("body") or "")
    skill_type = str(data.get("skill_type") or "build").strip().lower()

    if not name:
        return ForkDecision(decision="skip", rationale=rationale, error="save with no name")
    if not _NAME_RE.match(name):
        return ForkDecision(
            decision="skip",
            rationale=rationale,
            error=(
                f"name {name!r} is not a valid skill name "
                "(lowercase letters, digits, . _ -; 3-60 chars)"
            ),
        )
    if not body.strip():
        return ForkDecision(decision="skip", rationale=rationale, error="save with no body")
    if len(body) > MAX_BODY_CHARS:
        return ForkDecision(
            decision="skip",
            rationale=rationale,
            error=f"body is {len(body)} chars, over the {MAX_BODY_CHARS} cap",
        )
    if not description:
        return ForkDecision(
            decision="skip",
            rationale=rationale,
            error="save with no description (the description IS the trigger)",
        )
    if skill_type not in VALID_SKILL_TYPES:
        skill_type = "build"

    return ForkDecision(
        decision="save",
        rationale=rationale,
        name=name,
        description=description,
        skill_type=skill_type,
        triggers=_clean_list(data.get("triggers")),
        tags=_clean_list(data.get("tags")),
        body=body,
    )
