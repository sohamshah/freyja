"""Session-local goal loop state, judge prompts, and operator brief.

The judge runs once per turn against the agent's recent work. The contract
(both the prompt and the structured response) is deliberately heavy: the goal
is to extract dense signal from the judge rather than a one-line verdict.

What's new vs. the original loop:
  · The judge is skeptical by default. "done: true" requires all enforced
    criteria to be explicitly met, no open questions remaining, and high
    confidence — it must refuse to rubber-stamp.
  · The verdict is structured: alongside the prose reason, the judge emits
    a list of criteria (with stable IDs and status: met/partial/missing)
    and a list of open questions naming what's still preventing done.
  · The judge sees the operator's brief — a voice paragraph, a rigor score
    (1–10), explicit must/should/may criteria, a never-do list, and any
    extra when-to-stop logic.
  · The judge sees several recent assistant turns, not just the last
    snippet, so it can spot contradictions and uncovered ground.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


# ─── JUDGE PROMPTS ────────────────────────────────────────────────────────────

GOAL_JUDGE_SYSTEM_PROMPT = """You are the judge for an autonomous agent loop.
You decide whether the agent's work satisfies a stated goal, and — when it
doesn't — you name precisely what is missing so the loop can continue.

You are SKEPTICAL BY DEFAULT. The default verdict is "not done". You only
mark a goal as done when every enforced criterion is explicitly demonstrated
as satisfied by the work itself (not implied, not promised, not gestured at),
when no open questions remain, and when your overall confidence is at or
above 0.85. When in doubt, hold the line and explain what would change your
mind. Rubber-stamping is a failure mode; resist it.

You will be given:
  · the standing goal, verbatim;
  · the operator's BRIEF — voice instructions, a rigor level (1–4 with
    explicit semantics, see below), explicit must/should/may criteria
    with stable IDs, a never-do list, and when-to-stop logic;
  · the criteria from the previous turn so you can update statuses across
    turns instead of starting fresh;
  · the agent's recent work — several assistant turns, not just the latest
    sentence, so you can spot contradictions and follow what was tried;
  · a compact history of recent verdicts so you can see trajectory.

You will return STRICT JSON in this exact shape, with no surrounding prose
and no markdown fence:

{
  "done": false,
  "confidence": 0.0,
  "reason": "<a thorough, detailed paragraph — multiple sentences. Walk
    through what the work covers, what's missing, where evidence is thin,
    and what would change your verdict. Quote the work where useful. A
    two-sentence verdict means you did not think hard enough; expand.>",
  "criteria": [
    {"id": "<stable id; reuse IDs from the previous turn>",
     "text": "<short criterion>",
     "priority": "must" | "should" | "may",
     "status": "met" | "partial" | "missing",
     "note": "<one sentence on the current status — what's been done and
       what's still owed>"}
  ],
  "open_questions": [
    "<a specific question whose unresolved status is preventing 'done'.
     Concrete and answerable, not vague.>"
  ]
}

Hard rules for the verdict:
  · `done` may only be `true` when EVERY criterion with priority "must"
    has status "met", AND `open_questions` is empty, AND your `confidence`
    is at or above 0.85.
  · `confidence` is how certain you are of your overall judgment, not how
    close the agent is to completion.
  · `criteria` MUST preserve IDs from the previous turn and update their
    statuses. You may add new criteria when the work surfaces considerations
    the operator did not anticipate; give those a fresh ID and note that you
    surfaced them.
  · `open_questions` should be concrete questions whose answers would
    materially change your verdict. If you can't name any open questions
    when marking `done: false`, you have not thought carefully enough —
    name at least one specific gap.
  · Apply the operator's voice instructions and rigor level. The rigor
    scale has four discrete levels with explicit semantics — apply them
    literally, not as "higher number = stricter":
        1 LENIENT — accept the work if it broadly addresses the goal.
            Partial criteria count as met. Open questions may be left
            unresolved if the substance is there. Use for brainstorm,
            ideation, drafts where breadth matters.
        2 STANDARD (default) — accept if all `must` criteria are at
            least partial AND no never-do is violated AND the
            substance addresses the goal. Open questions are
            tolerated if they're follow-ups, not gaps.
        3 STRICT — every `must` criterion must be status `met` (not
            partial). Open questions disqualify. Confidence must be
            ≥ 0.85. Use when the deliverable will be acted on.
        4 ADVERSARIAL — strictest. Default to `done: false`. Demand
            verifiable evidence for every factual claim. Treat
            unsourced assertions, paraphrased quotes, and round
            numbers as failures. Use for audits, research with
            citations, anything high-stakes.
    Always honor the never-do list as hard constraints, regardless
    of rigor.

If the agent's recent work contains a contradiction, a regression from a
previous turn's progress, or an unsupported claim, surface it in `reason`
explicitly. Be specific. The operator is relying on you to detect the
failure modes they will miss.

═══════════════════════════════════════════════════════════════════════
TOOL USE (when tools are available — `deep` profile only)
═══════════════════════════════════════════════════════════════════════
When tools are enabled you may use them to verify the agent's claims
rather than taking them on faith. Use this power deliberately and stay
within the read-only contract.

  · `read_file` / `grep` / `glob` — fine, use freely to inspect the
    workspace, check that files the agent claimed exist actually do, and
    pull supporting passages into your `reason`.
  · `fetch_url` — fine, use to verify cited URLs or fetch a reference
    the agent mentioned. Do not crawl widely; one fetch per cited link
    is enough.
  · `bash` — READ ONLY. Use it only for compound exploration commands
    such as `cat`, `head`, `tail`, `awk '...'`, `grep -r ...`, `find . -type f`,
    `wc -l`, `ls`, `du`, `stat`, piping any of these together. You may
    NOT use bash for: writes (`>`, `>>`, `tee`, `cp`, `mv`, `rm`,
    `mkdir`, `touch`, `chmod`, `chown`), git mutations (`git add`,
    `commit`, `push`, `reset`, `checkout`, `branch -D`, anything that
    changes state), package installs (`pip`, `npm`, `brew`, `apt`),
    process control (`kill`, `pkill`), or anything that touches the
    network beyond what `fetch_url` would do. If the command would
    change state, do not run it — that is not your job and not safe.

Use the minimum number of tool calls needed to verify the agent's
claims. The operator is paying per call. After you have what you need,
return the structured JSON verdict and stop. Do not let tool exploration
become the main activity.

═══════════════════════════════════════════════════════════════════════
OUTPUT FORMAT — READ THIS LAST, OBEY EXACTLY
═══════════════════════════════════════════════════════════════════════
Your final response MUST be a single JSON object and nothing else.

  · NO preamble. Do not write "Here is my verdict:" or "Based on the
    work…". Start with `{`.
  · NO markdown fences. Do not wrap the JSON in ``` or ```json.
  · NO postamble. Do not append "Let me know if you need clarification."
    The closing `}` is the end of your response.
  · NO trailing commas. NO comments. NO single quotes around keys or
    string values.
  · ALL keys exactly as shown below (camelCase). Use `open_questions`
    OR `openQuestions` — both are accepted by the parser, but pick one
    and stick with it within a response.

A valid response looks exactly like this (template — substitute your
own values; preserve the field order and types):

{"done": false, "confidence": 0.42, "reason": "The handbook addresses three of four required topics but the verdict-flow section is missing. The 'tools' section quotes `bridge/tools/goal_loop.py:254` but the actual content at that line is the DEFAULT_DEEP_JUDGE_TOOLS declaration, not what the handbook claims it says. Fix that quote, add the verdict-flow section, and resubmit.", "criteria": [{"id": "crit_a1", "text": "Four required sections present", "priority": "must", "status": "partial", "note": "Three of four present; verdict flow missing."}, {"id": "crit_a2", "text": "Every verbatim quote matches the cited file at the cited line", "priority": "must", "status": "missing", "note": "Spot-checked 2 of 5 quotes; one mismatch at goal_loop.py:254."}], "open_questions": ["Where is the verdict-flow section?", "Will the operator accept paraphrased quotes if marked as paraphrase?"]}

If you emit anything other than a single JSON object in that exact
shape, the loop will treat it as a parse failure and discard your
verdict. Do not let that happen."""


GOAL_JUDGE_USER_TEMPLATE = """STANDING GOAL
{goal}

JUDGE RULES
{rules_block}

PREVIOUS TURN CRITERIA STATUS
{previous_criteria_block}

RECENT VERDICT HISTORY
{verdict_history_block}

AGENT'S RECENT WORK (most recent first)
{recent_work_block}

Return your verdict as strict JSON per the schema in the system prompt. Be
detailed in `reason`. Name specific gaps in `open_questions`. Default to
`done: false` unless every must criterion is explicitly met."""


JUDGE_CALIBRATOR_SYSTEM_PROMPT = """You are the JUDGE CALIBRATOR — a one-shot configuration agent.

You read a freshly-armed goal and propose the optimal judge configuration
for that specific goal. The judge is what evaluates whether the agent's
work satisfies the goal on every loop turn; bad judge config means
either the agent gets rubber-stamped at "good enough" or the loop runs
forever without ever satisfying invented criteria. Your job is to thread
that needle.

You have one shot. No tools. No follow-ups. Read the goal, infer what
*kind* of work this is and what its dominant failure modes are, and
return a structured JSON configuration.

The judge has four profiles, ordered by intensity:
  · `skip`     — no judge runs at all. The loop fires the agent turn
                 and stops. Use ONLY when the goal has nothing
                 verifiable to evaluate: greetings, simple factual
                 lookups ("what time is it"), trivial conversational
                 turns, or "goals" that are really just one-shot
                 questions. If there is any real work to do — research,
                 code, analysis, anything multi-turn — pick at least
                 `quick`. When in doubt, do NOT pick skip.
  · `quick`    — Haiku 4.5, no thinking, single-call. Use when the goal is
                 small, mechanical, or genuinely easy to evaluate (e.g.
                 "rename this function", "format this file"). Cheap.
  · `standard` — same model as the agent, no thinking, single-call. The
                 default. Use for routine work where the verdict
                 doesn't need deep verification.
  · `deep`     — same model as the agent with HIGH thinking, runs as a
                 subagent with read-only tools (read_file, list_directory,
                 grep, glob, bash, fetch_url). Use when the goal asks for
                 something *checkable* — claims that can be verified by
                 reading files or fetching URLs, citations, line numbers,
                 quantitative assertions. The deep judge will actually
                 grep the codebase to catch fabrication.

Rigor is a 4-level scale with literal semantics — pick the one whose
description fits the goal, NOT a number on a sliding scale:
    1 LENIENT — accept work that broadly addresses the goal; partial
        criteria and open follow-ups are tolerated. Use for
        brainstorm, ideation, drafts where breadth matters.
    2 STANDARD (default) — accept if all `must` criteria are at
        least partial and no never-do is violated. Open questions
        tolerated as follow-ups, not gaps. Routine deliverables.
    3 STRICT — every `must` must be status `met` (not partial);
        any open question disqualifies; confidence ≥ 0.85. For
        deliverables that will be acted on.
    4 ADVERSARIAL — strictest. Default to not-done. Demand
        verifiable evidence for every factual claim. For audits,
        citation-heavy work, anything high-stakes.

Heuristics for picking profile + rigor:
  · The goal is a greeting, a single factual lookup, or a trivial
    conversational message ("hi", "what time is it", "thanks") →
    `skip`. Other fields still need to be present but won't be
    consulted.
  · The goal demands verifiable claims (file refs, citations, code
    that must compile/run, exact quotes) → `deep`, rigor 4.
  · The goal is open-ended research / exploration with no clear
    "done" → `deep`, rigor 3, with explicit when-to-stop logic.
  · The goal is a routine writing or analysis deliverable that will
    be acted on (memo, summary, plan) → `standard`, rigor 3.
  · The goal is a pure-text deliverable with no checkable claims
    (haiku, brainstorm, persona writing) → `standard`, rigor 2.
  · The goal is a tiny mechanical task (rename, format, reword) →
    `quick`, rigor 1 or 2.

Voice: write a short paragraph (3-6 sentences) telling the judge what
to be skeptical of FOR THIS SPECIFIC GOAL. Reference the dominant
failure modes you anticipate from the goal type. Don't write generic
"be skeptical" — name the specific traps (e.g. "agents writing
documentation tend to paraphrase quotes; demand exact match" or "for
research deliverables, watch for confident assertions without
attribution").

Criteria: 3-6 items total. At least 2 should be `must`. Each must be
checkable — not aspirational. Phrase as a condition the judge can mark
met / partial / missing. Avoid restating the goal verbatim. Stable IDs
prefixed with `cal_` so the operator can tell at a glance which were
calibrator-authored.

Never-do: 1-3 items naming specific behaviors that should disqualify
the work even if criteria look met. Common entries: "fabricate file
paths", "paraphrase a quote and call it verbatim", "ship without
running the cited command". Only include never-dos that are plausible
failure modes for THIS goal type.

When-to-stop: optional. Use only if the goal's natural termination
isn't already implied by criteria. Examples: "stop after the first
passing test if the user-facing behavior is verified" or "stop on the
third regression — additional turns are likely to make things worse".

Tools (deep profile only): omit unless you want to narrow the default
allowlist. Default is read_file/list_directory/grep/glob/bash/fetch_url.
Narrow only if a tool is clearly wrong for the goal (e.g. drop
fetch_url for a goal that has no URLs; drop bash for a goal where
file reads alone suffice).

There is no iteration cap to set — the deep judge investigates until
it's satisfied (under a generous internal safety net) and then a
separate structured-output synthesis pass renders the verdict. Your
job is to define the rigor / criteria / voice, not the runtime.

You will return STRICT JSON in this exact shape, with no surrounding
prose and no markdown fence:

{
  "judgeProfile": "skip" | "quick" | "standard" | "deep",
  "rigorScore": 1 | 2 | 3 | 4,
  "voice": "<3-6 sentence paragraph>",
  "criteria": [
    {"id": "cal_xx", "text": "...", "priority": "must" | "should" | "may"}
  ],
  "neverDo": ["..."],
  "whenToStop": "<optional, may be empty string>",
  "judgeTools": ["..."],
  "rationaleOverall": "<one paragraph naming the goal type, the dominant failure modes you inferred, and why you picked this profile + rigor>",
  "rationaleByField": {
    "judgeProfile": "<one sentence>",
    "rigorScore": "<one sentence>",
    "voice": "<one sentence>",
    "criteria": "<one sentence>",
    "neverDo": "<one sentence>",
    "whenToStop": "<one sentence; may be empty if the field is empty>",
    "judgeTools": "<one sentence; may say 'using profile defaults'>"
  },
  "confidence": 0.0..1.0
}

Hard rules:
  · Output a SINGLE JSON object — no preamble, no fences, no postamble.
    Start with `{`, end with `}`.
  · Every field listed above must be present. Use empty strings or
    empty arrays for fields you intentionally leave blank, but the
    KEY must exist.
  · `criteria` must have between 3 and 6 entries.
  · `rationaleByField` must have one entry per JudgeRules field even
    if the field is blank ("using default for this goal type" is fine).
  · IDs in `criteria` must be unique within your response and prefixed
    with `cal_`.
  · `confidence` is your overall confidence in the configuration. If
    the goal is too ambiguous to calibrate well, return 0.4-0.6 and say
    so in `rationaleOverall`.

This is your only chance to shape the judge for this goal. Make it
count."""


JUDGE_CALIBRATOR_USER_TEMPLATE = """A new goal has just been armed. Calibrate the judge.

STANDING GOAL
{goal}

ADDITIONAL CONTEXT (recent operator messages, may be empty)
{context_block}

Return the structured JSON configuration. Be specific to THIS goal —
generic configurations are useless. Name the dominant failure modes
you anticipate by goal type in your rationale."""


GOAL_CONTINUATION_TEMPLATE = """[Continuing toward the active Freyja goal]

Goal: {goal}

The judge has not yet marked this goal complete. Most recent assessment from
the judge:

> {reason}

Open questions still preventing 'done':
{open_questions_block}

Continue from the current transcript. Use tools as needed. Address the open
questions above directly, then either make concrete progress or finish with
a clear completion note that names which criteria are now met."""


# ─── DATA MODEL ───────────────────────────────────────────────────────────────


@dataclass
class VerdictCriterion:
    """One criterion in a judge verdict — a checkable item the judge tracks
    across turns. IDs are stable; statuses change."""

    id: str
    text: str
    priority: str = "should"  # "must" | "should" | "may"
    status: str = "missing"   # "met" | "partial" | "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class GoalVerdict:
    done: bool
    reason: str
    confidence: float = 0.0
    criteria: list[VerdictCriterion] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    raw: str = ""
    # Phase 3 (deep judge as subagent) populates this with the judge's
    # child session id so the renderer can link the "Judge inspected"
    # row in the verdict card to the judge's full child session view —
    # tool calls, intermediate thinking, the works. None for quick /
    # standard profiles which still run inline without a child session.
    judge_session_id: str | None = None
    # When the deep subagent path fails and we fall back to the inline
    # standard call (Q5 fallback), this carries the originally-requested
    # profile so the UI surfaces "this verdict came from a fallback"
    # rather than silently degrading.
    fallback_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "reason": self.reason,
            "confidence": self.confidence,
            "criteria": [c.to_dict() for c in self.criteria],
            "openQuestions": list(self.open_questions),
            "judgeSessionId": self.judge_session_id,
            "fallbackFrom": self.fallback_from,
            "raw": self.raw,
        }


# JSON schema for the judge's structured response. Used by providers that
# support constrained decoding (OpenAI Responses API json_schema strict
# mode, Anthropic tool_use shape). The shape mirrors the parser's
# expectations — every field the parser reads is declared here, and the
# parser's lenient cleanups handle the cases where a model doesn't
# honor the schema.
GOAL_VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["done", "confidence", "reason", "criteria", "open_questions"],
    "properties": {
        "done": {
            "type": "boolean",
            "description": (
                "True ONLY when every must-criterion is met, there are "
                "no open questions, and your confidence is >= 0.85."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "How certain you are of your overall judgment, 0.0-1.0.",
        },
        "reason": {
            "type": "string",
            "description": (
                "A thorough multi-sentence verdict. Walk through what the "
                "work covers, what's missing, where evidence is thin, and "
                "what would change your verdict. Quote the work where useful."
            ),
        },
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "text", "priority", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "priority": {"type": "string", "enum": ["must", "should", "may"]},
                    "status": {"type": "string", "enum": ["met", "partial", "missing"]},
                    "note": {"type": "string"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete questions whose answers would materially change "
                "your verdict. If empty AND done=false, you have not thought "
                "carefully enough."
            ),
        },
    },
}


@dataclass
class RuleCriterion:
    """An operator-defined criterion that gets injected into every judge call."""

    id: str
    text: str
    priority: str = "should"  # "must" | "should" | "may"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "priority": self.priority}


# Profile values determine model, thinking budget, and (later) tool
# allowlist for the judge call. Kept on the brief so the operator picks
# explicitly per goal. Defaults to "standard" — current behavior.
JUDGE_PROFILES = ("skip", "quick", "standard", "deep")


# Rigor: 4 discrete levels with literal semantics. See
# GOAL_JUDGE_SYSTEM_PROMPT for the exact rules each level applies.
RIGOR_LEVELS = (1, 2, 3, 4)
RIGOR_LABELS: dict[int, str] = {
    1: "lenient",
    2: "standard",
    3: "strict",
    4: "adversarial",
}
RIGOR_DESCRIPTIONS: dict[int, str] = {
    1: (
        "Accept the work if it broadly addresses the goal. Partial criteria "
        "count as met. Open follow-ups are tolerated. Default to 'done' "
        "when the substance is there."
    ),
    2: (
        "Accept if all `must` criteria are at least partial AND no never-do "
        "is violated. Open questions are tolerated as follow-ups, not gaps. "
        "Default rigor for routine deliverables."
    ),
    3: (
        "Every `must` criterion must be status `met` (not partial). Any open "
        "question disqualifies. Confidence must be at or above 0.85. For "
        "deliverables that will be acted on."
    ),
    4: (
        "Strictest. Default to `done: false`. Demand verifiable evidence for "
        "every factual claim. Treat unsourced assertions, paraphrased quotes, "
        "and round numbers as failures. For audits, citation-heavy work, "
        "anything high-stakes."
    ),
}


def _migrate_rigor(value: int) -> int:
    """Map legacy 1–10 rigor scores onto the new 1–4 scale.

    Old briefs persisted to the sidecar before the scale was tightened
    use 1–10. This compresses them so re-loaded sessions don't reset
    or look broken: 1-2→1 lenient, 3-5→2 standard, 6-8→3 strict, 9-10→4
    adversarial. Already-clamped 1–4 values pass through untouched.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 2
    if v <= 0:
        return 2
    if v <= 4:
        return v
    if v <= 5:
        return 2
    if v <= 8:
        return 3
    return 4


def _clamp_judge_profile(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in JUDGE_PROFILES else "standard"


# Default tool allowlist for the `deep` profile when the operator hasn't
# overridden it on the JudgeRules. Read-only — judge can inspect file
# contents, grep/glob across the workspace, fetch URLs to verify
# citations, and shell out to read-only bash for compound queries. The
# system prompt enforces read-only bash discipline.
DEFAULT_DEEP_JUDGE_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_directory",
    "grep",
    "glob",
    "bash",
    "fetch_url",
)


@dataclass
class CalibratorMeta:
    """Provenance + rationale for the auto-calibrated judge rules.

    When the judge-calibrator subagent runs on goal-set, it returns a
    proposed JudgeRules plus per-field reasoning. We attach that
    reasoning here so the JudgeBrief modal can show the operator *why*
    each value was chosen, and the editor can mark which fields the
    operator has since hand-edited.
    """

    model: str = ""
    ran_at: float = 0.0
    version: int = 1
    confidence: float = 0.0
    rationale_overall: str = ""
    rationale_by_field: dict[str, str] = field(default_factory=dict)
    # Names of JudgeRules fields the calibrator set. Lets the UI tag
    # specific fields as "set by calibrator" so the operator can tell at
    # a glance what's machine-suggested vs. their own edits.
    calibrator_set_fields: list[str] = field(default_factory=list)
    # Source session id of the calibrator child run (so the UI can link
    # to the calibrator's transcript and thinking).
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ranAt": int(self.ran_at * 1000) if self.ran_at else 0,
            "version": self.version,
            "confidence": self.confidence,
            "rationaleOverall": self.rationale_overall,
            "rationaleByField": dict(self.rationale_by_field),
            "calibratorSetFields": list(self.calibrator_set_fields),
            "sessionId": self.session_id,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CalibratorMeta | None":
        if not isinstance(payload, dict):
            return None
        try:
            ran_at_ms = int(payload.get("ranAt") or 0)
        except Exception:
            ran_at_ms = 0
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        rbf_raw = payload.get("rationaleByField")
        rbf = (
            {str(k): str(v) for k, v in rbf_raw.items() if str(v).strip()}
            if isinstance(rbf_raw, dict) else {}
        )
        return cls(
            model=str(payload.get("model") or ""),
            ran_at=ran_at_ms / 1000.0 if ran_at_ms else 0.0,
            version=int(payload.get("version") or 1),
            confidence=max(0.0, min(confidence, 1.0)),
            rationale_overall=str(payload.get("rationaleOverall") or ""),
            rationale_by_field=rbf,
            calibrator_set_fields=[
                str(f).strip() for f in (payload.get("calibratorSetFields") or [])
                if str(f).strip()
            ],
            session_id=(
                str(payload.get("sessionId")).strip()
                if isinstance(payload.get("sessionId"), str) and payload.get("sessionId").strip()
                else None
            ),
        )


# Names of every JudgeRules field the calibrator may set. The calibrator
# is required to return a value for each in its structured response, but
# we record which ones it actually set on the rules so the UI can tag
# them in the editor.
CALIBRATOR_FIELDS: tuple[str, ...] = (
    "voice",
    "rigorScore",
    "judgeProfile",
    "criteria",
    "neverDo",
    "whenToStop",
    "judgeTools",
)


@dataclass
class JudgeRules:
    """Operator-authored rules the judge applies on every turn. Persists
    per session.

    `voice` is a detailed freeform paragraph; `rigor_score` is a discrete
    1–4 level (1 lenient · 2 standard · 3 strict · 4 adversarial — see
    GOAL_JUDGE_SYSTEM_PROMPT for exact semantics); `judge_profile` is one of "quick" / "standard" /
    "deep" — picks the model + thinking budget for the judge call.
    `judge_tools` is an optional allowlist that overrides the profile
    default (empty → use the default for the active profile).

    There is no operator-tunable iteration cap. The deep judge runs as
    a real subagent under a generous internal safety net (see
    `_DEEP_JUDGE_SAFETY_NET_ITERATIONS` in freyja_bridge.py); the
    verdict shape is guaranteed by a separate structured-output
    synthesis pass that always runs after investigation completes.

    `calibrator_meta` is set by the auto-calibrator that fires on goal
    set; it carries the model + per-field rationale so the editor can
    surface why each value was picked. Operator hand-edits do NOT clear
    the meta — instead the UI compares current values against the
    calibrator's snapshot to decide which fields are still "calibrator
    authored" vs. operator-touched.

    The operator should write `voice` like a memo to a colleague —
    multiple sentences, specific guidance — not a one-liner.
    """

    voice: str = ""
    rigor_score: int = 2
    judge_profile: str = "standard"
    criteria: list[RuleCriterion] = field(default_factory=list)
    never_do: list[str] = field(default_factory=list)
    when_to_stop: str = ""
    # Empty list = use the profile default. Otherwise = explicit allowlist
    # for this brief, overriding the profile's defaults.
    judge_tools: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    calibrator_meta: CalibratorMeta | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice": self.voice,
            "rigorScore": self.rigor_score,
            "judgeProfile": self.judge_profile,
            "criteria": [c.to_dict() for c in self.criteria],
            "neverDo": list(self.never_do),
            "whenToStop": self.when_to_stop,
            "judgeTools": list(self.judge_tools),
            "updatedAt": int(self.updated_at * 1000),
            "calibratorMeta": self.calibrator_meta.to_dict() if self.calibrator_meta else None,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "JudgeRules":
        if not isinstance(payload, dict):
            return cls()
        crits = []
        for raw in payload.get("criteria", []) or []:
            if not isinstance(raw, dict):
                continue
            crits.append(
                RuleCriterion(
                    id=str(raw.get("id") or _new_id("rule")),
                    text=str(raw.get("text") or "").strip(),
                    priority=_clamp_priority(raw.get("priority")),
                )
            )
        try:
            rigor = int(payload.get("rigorScore", 2))
        except Exception:
            rigor = 2
        rigor = _migrate_rigor(rigor)
        return cls(
            voice=str(payload.get("voice") or ""),
            rigor_score=max(1, min(rigor, 4)),
            judge_profile=_clamp_judge_profile(payload.get("judgeProfile")),
            criteria=crits,
            never_do=[str(x) for x in (payload.get("neverDo") or []) if str(x).strip()],
            when_to_stop=str(payload.get("whenToStop") or ""),
            judge_tools=[str(t).strip() for t in (payload.get("judgeTools") or []) if str(t).strip()],
            updated_at=time.time(),
            calibrator_meta=CalibratorMeta.from_dict(payload.get("calibratorMeta")),
        )

    def effective_tools(self) -> tuple[str, ...]:
        """Return the active tool allowlist for the deep judge call.

        Brief override wins when set; otherwise falls back to the profile
        default. Only meaningful for the deep profile — quick/standard
        are single-shot and ignore this.
        """
        if self.judge_tools:
            return tuple(self.judge_tools)
        return DEFAULT_DEEP_JUDGE_TOOLS

    def render_for_prompt(self) -> str:
        """Serialize the brief for inclusion in the judge user template."""
        out: list[str] = []
        level = max(1, min(int(self.rigor_score or 2), 4))
        label = RIGOR_LABELS.get(level, "standard")
        desc = RIGOR_DESCRIPTIONS.get(level, "")
        out.append(f"Rigor: {level}/4 — {label}")
        if desc:
            out.append(f"  ({desc})")
        if self.voice.strip():
            out.append("")
            out.append("Voice (apply throughout):")
            out.append(self.voice.strip())
        if self.criteria:
            out.append("")
            out.append("Operator-defined criteria (track these by ID across turns):")
            for c in self.criteria:
                out.append(f"  · [{c.id}] ({c.priority}) {c.text}")
        if self.never_do:
            out.append("")
            out.append("Never do (hard constraints):")
            for item in self.never_do:
                out.append(f"  · {item}")
        if self.when_to_stop.strip():
            out.append("")
            out.append("Additional when-to-stop conditions:")
            out.append(self.when_to_stop.strip())
        non_default_rigor = max(1, min(int(self.rigor_score or 2), 4)) != 2
        if not (
            self.voice
            or self.criteria
            or self.never_do
            or self.when_to_stop
            or non_default_rigor
        ):
            return "(no operator brief — apply default judgment)"
        return "\n".join(out)


@dataclass
class GoalState:
    goal: str
    status: str = "active"
    turns_used: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_verdict: GoalVerdict | None = None
    pause_reason: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    def continuation_prompt(self) -> str:
        reason = (self.last_verdict.reason if self.last_verdict else "").strip()
        if not reason:
            reason = "The previous turn did not complete the goal."
        questions = (
            list(self.last_verdict.open_questions)
            if self.last_verdict and self.last_verdict.open_questions
            else []
        )
        questions_block = (
            "\n".join(f"  · {q}" for q in questions) if questions else "  (none named)"
        )
        return GOAL_CONTINUATION_TEMPLATE.format(
            goal=self.goal,
            reason=reason,
            open_questions_block=questions_block,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "turnsUsed": self.turns_used,
            "createdAt": int(self.created_at * 1000),
            "updatedAt": int(self.updated_at * 1000),
            "lastVerdict": self.last_verdict.to_dict() if self.last_verdict else None,
            "pauseReason": self.pause_reason,
        }


# ─── PARSING ──────────────────────────────────────────────────────────────────


def _clamp_priority(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("must", "should", "may"):
        return v
    return "should"


def _clamp_status(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("met", "partial", "missing"):
        return v
    return "missing"


def _new_id(prefix: str) -> str:
    seed = f"{prefix}-{time.time_ns()}"
    return f"{prefix}_{hashlib.md5(seed.encode()).hexdigest()[:8]}"


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of arbitrary model text.

    Real-world judge outputs include any of: bare JSON, fenced JSON
    (```json ... ``` or ``` ... ```), preamble ("Here is the verdict:"),
    postamble ("Let me know if you need …"), trailing commas, smart
    quotes. This walks the string with a balanced-brace scanner that
    respects string literals + escape sequences, then attempts a
    sequence of increasingly lenient cleanups before giving up.

    Returns the parsed dict, or None if no parseable object is found.
    """
    if not text:
        return None
    candidate = text.strip()

    # 1. Strip a single outer markdown code fence if present. We handle
    #    ```json … ```, ``` … ```, and bare leading triple-backticks on
    #    their own line. Inner fences are left alone — they'll be ignored
    #    by the balanced-brace scanner since they're not braces.
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    # 2. Try a straight parse first — fastest path when the model
    #    actually obeyed.
    parsed = _try_parse_lenient(candidate)
    if parsed is not None:
        return parsed

    # 3. Balanced-brace scan. Walk every '{' as a potential JSON start
    #    and find its matching '}', then attempt to parse that slice.
    #    Respects string literals + escape sequences so braces inside
    #    quoted text don't throw off the count.
    for start in (i for i, c in enumerate(candidate) if c == "{"):
        depth = 0
        in_string = False
        escape = False
        for end in range(start, len(candidate)):
            ch = candidate[end]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    slice_ = candidate[start : end + 1]
                    parsed = _try_parse_lenient(slice_)
                    if parsed is not None:
                        return parsed
                    break  # try the next outer '{' instead

    return None


def _try_parse_lenient(s: str) -> dict[str, Any] | None:
    """Attempt to JSON-parse a string, with two fallback cleanups."""
    s = s.strip()
    if not s:
        return None
    # Direct parse — happy path.
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except Exception:
        pass
    # Cleanup pass 1: smart quotes → ASCII; non-breaking space → space.
    cleaned = (
        s.replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
        .replace(" ", " ")
    )
    try:
        out = json.loads(cleaned)
        return out if isinstance(out, dict) else None
    except Exception:
        pass
    # Cleanup pass 2: strip trailing commas before } or ]. The most common
    # GPT mistake. Two-character lookbehind plus a single-pass regex.
    no_trailing = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    try:
        out = json.loads(no_trailing)
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def parse_goal_verdict(text: str) -> GoalVerdict:
    """Parse a goal judge response.

    Handles the new structured shape (criteria, open_questions) AND the
    legacy {done, reason, confidence} shape — graceful fallback when the
    judge regresses or an old model is used.

    The parser is intentionally lenient: it accepts preamble + postamble,
    markdown code fences, smart quotes, and trailing commas. The judge is
    instructed to emit strict JSON only, but real models drift; the
    parser absorbs the drift so the loop doesn't stall on cosmetic noise.
    """
    raw = (text or "").strip()
    payload = _extract_first_json_object(raw)

    if not isinstance(payload, dict):
        return GoalVerdict(
            done=False,
            reason="Judge response was not valid JSON; continuing conservatively.",
            confidence=0.0,
            criteria=[],
            open_questions=["Judge response failed to parse — verify model output."],
            raw=raw,
        )

    done = bool(payload.get("done"))
    reason = str(payload.get("reason") or "").strip()

    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(float(confidence_raw), 1.0))
    except Exception:
        confidence = 0.0

    criteria: list[VerdictCriterion] = []
    for raw_crit in payload.get("criteria", []) or []:
        if not isinstance(raw_crit, dict):
            continue
        cid = str(raw_crit.get("id") or _new_id("crit")).strip() or _new_id("crit")
        ctext = str(raw_crit.get("text") or "").strip()
        if not ctext:
            continue
        criteria.append(
            VerdictCriterion(
                id=cid,
                text=ctext,
                priority=_clamp_priority(raw_crit.get("priority")),
                status=_clamp_status(raw_crit.get("status")),
                note=str(raw_crit.get("note") or "").strip(),
            )
        )

    open_questions_raw = (
        payload.get("open_questions")
        or payload.get("openQuestions")
        or []
    )
    open_questions = [
        str(q).strip() for q in open_questions_raw if str(q).strip()
    ] if isinstance(open_questions_raw, list) else []

    # Tighten the verdict: if the judge said done=true but criteria/conditions
    # don't actually justify it, override to false. Belt-and-suspenders against
    # the rubber-stamping failure mode.
    if done:
        must_unmet = any(
            c.priority == "must" and c.status != "met" for c in criteria
        )
        if must_unmet or open_questions or confidence < 0.85:
            done = False
            reason = (
                (reason + "\n\n" if reason else "")
                + "[loop guard] Judge marked done despite unmet must-criteria, "
                "open questions, or confidence below 0.85. Verdict flipped to "
                "continue."
            )

    return GoalVerdict(
        done=done,
        reason=reason or (
            "Goal satisfied." if done else "Goal still needs work — judge "
            "returned no explanation."
        ),
        confidence=confidence,
        criteria=criteria,
        open_questions=open_questions,
        raw=raw,
    )


# ─── PROMPT-BUILDING HELPERS ──────────────────────────────────────────────────


def build_previous_criteria_block(criteria: list[VerdictCriterion]) -> str:
    if not criteria:
        return "(no criteria from previous turn — this may be the first verdict)"
    lines: list[str] = []
    for c in criteria:
        lines.append(
            f"  · [{c.id}] ({c.priority}/{c.status}) {c.text}"
            + (f" — {c.note}" if c.note else "")
        )
    return "\n".join(lines)


def build_verdict_history_block(history: list[GoalVerdict], limit: int = 6) -> str:
    if not history:
        return "(no prior verdicts in this goal)"
    rows: list[str] = []
    for i, v in enumerate(history[-limit:], start=1):
        status = "DONE" if v.done else "continue"
        short = v.reason.replace("\n", " ").strip()[:240]
        rows.append(f"  turn {i}: [{status}] conf {v.confidence:.2f} — {short}")
    return "\n".join(rows)


def verdict_from_dict(payload: Any) -> GoalVerdict | None:
    """Rehydrate a GoalVerdict from its to_dict() shape. Returns None for junk."""
    if not isinstance(payload, dict):
        return None
    crits: list[VerdictCriterion] = []
    for raw in payload.get("criteria", []) or []:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not cid or not text:
            continue
        crits.append(
            VerdictCriterion(
                id=cid,
                text=text,
                priority=_clamp_priority(raw.get("priority")),
                status=_clamp_status(raw.get("status")),
                note=str(raw.get("note") or "").strip(),
            )
        )
    js_id = payload.get("judgeSessionId") or payload.get("judge_session_id")
    fb = payload.get("fallbackFrom") or payload.get("fallback_from")
    return GoalVerdict(
        done=bool(payload.get("done")),
        reason=str(payload.get("reason") or "").strip(),
        confidence=max(0.0, min(float(payload.get("confidence") or 0.0), 1.0)),
        criteria=crits,
        open_questions=[
            str(q).strip()
            for q in (payload.get("openQuestions") or payload.get("open_questions") or [])
            if str(q).strip()
        ],
        raw=str(payload.get("raw") or ""),
        judge_session_id=str(js_id).strip() if isinstance(js_id, str) and js_id.strip() else None,
        fallback_from=str(fb).strip() if isinstance(fb, str) and fb.strip() else None,
    )


def parse_calibrator_response(text: str, *, session_id: str | None = None, model: str = "") -> tuple[JudgeRules | None, CalibratorMeta | None]:
    """Parse the calibrator subagent's structured response into a JudgeRules
    + CalibratorMeta pair.

    Returns (rules, meta) on success; (None, None) if the response is
    unparseable or missing required fields. The bridge falls back to
    leaving the operator's existing rules alone in that case.
    """
    if not text:
        return None, None
    payload = _extract_first_json_object(text)
    if not isinstance(payload, dict):
        return None, None

    # Per-field rationale — an empty dict is fine; we render the editor
    # without per-field tooltips in that case.
    rbf_raw = payload.get("rationaleByField")
    rationale_by_field: dict[str, str] = {}
    if isinstance(rbf_raw, dict):
        for k, v in rbf_raw.items():
            ks = str(k).strip()
            vs = str(v or "").strip()
            if ks and vs:
                rationale_by_field[ks] = vs

    # Build the rules. Track which fields the calibrator actually populated
    # (vs left blank) so the UI can label them as calibrator-set.
    crits: list[RuleCriterion] = []
    set_fields: list[str] = []

    profile = _clamp_judge_profile(payload.get("judgeProfile"))
    if str(payload.get("judgeProfile") or "").strip().lower() in JUDGE_PROFILES:
        set_fields.append("judgeProfile")

    try:
        rigor = int(payload.get("rigorScore") or 2)
    except Exception:
        rigor = 2
    rigor = max(1, min(_migrate_rigor(rigor), 4))
    if "rigorScore" in payload:
        set_fields.append("rigorScore")

    voice = str(payload.get("voice") or "").strip()
    if voice:
        set_fields.append("voice")

    raw_crits = payload.get("criteria") or []
    if isinstance(raw_crits, list):
        for raw in raw_crits:
            if not isinstance(raw, dict):
                continue
            text_val = str(raw.get("text") or "").strip()
            if not text_val:
                continue
            cid = str(raw.get("id") or "").strip() or _new_id("cal")
            crits.append(
                RuleCriterion(
                    id=cid,
                    text=text_val,
                    priority=_clamp_priority(raw.get("priority")),
                )
            )
    if crits:
        set_fields.append("criteria")

    never_do = [
        str(x).strip()
        for x in (payload.get("neverDo") or [])
        if str(x).strip()
    ]
    if never_do:
        set_fields.append("neverDo")

    when_to_stop = str(payload.get("whenToStop") or "").strip()
    if when_to_stop:
        set_fields.append("whenToStop")

    judge_tools = [
        str(t).strip()
        for t in (payload.get("judgeTools") or [])
        if str(t).strip()
    ]
    if judge_tools:
        set_fields.append("judgeTools")

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    meta = CalibratorMeta(
        model=model,
        ran_at=time.time(),
        version=1,
        confidence=confidence,
        rationale_overall=str(payload.get("rationaleOverall") or "").strip(),
        rationale_by_field=rationale_by_field,
        calibrator_set_fields=set_fields,
        session_id=session_id,
    )

    rules = JudgeRules(
        voice=voice,
        rigor_score=rigor,
        judge_profile=profile,
        criteria=crits,
        never_do=never_do,
        when_to_stop=when_to_stop,
        judge_tools=judge_tools,
        updated_at=time.time(),
        calibrator_meta=meta,
    )
    return rules, meta


def rules_has_content(payload: Any) -> bool:
    """Return True if a brief dict has any operator-authored content worth persisting."""
    if not isinstance(payload, dict):
        return False
    if str(payload.get("voice") or "").strip():
        return True
    if str(payload.get("whenToStop") or "").strip():
        return True
    if [c for c in (payload.get("criteria") or []) if isinstance(c, dict) and str(c.get("text") or "").strip()]:
        return True
    if [n for n in (payload.get("neverDo") or []) if str(n).strip()]:
        return True
    # rigorScore default is 2 (standard); anything else counts as
    # operator-authored content.
    try:
        if _migrate_rigor(int(payload.get("rigorScore") or 2)) != 2:
            return True
    except Exception:
        pass
    # judgeProfile default is "standard"; non-default counts as content
    if str(payload.get("judgeProfile") or "standard").strip().lower() not in ("standard", ""):
        return True
    return False


def merge_rule_criteria_into_verdict(brief: "JudgeRules | None", verdict: GoalVerdict) -> GoalVerdict:
    """Make sure every operator-authored criterion appears in the verdict.

    The judge sometimes drops brief criteria from its output (especially on
    early turns or when the model is being terse). We backfill them with
    `status="missing"` so the operator never sees their must-criteria
    disappear silently. Criteria with matching IDs in the verdict take
    precedence — judge updates win.
    """
    if not brief or not brief.criteria:
        return verdict
    seen_ids = {c.id for c in verdict.criteria}
    backfilled: list[VerdictCriterion] = []
    for bc in brief.criteria:
        if bc.id in seen_ids:
            continue
        backfilled.append(
            VerdictCriterion(
                id=bc.id,
                text=bc.text,
                priority=bc.priority,
                status="missing",
                note="(operator-defined; judge did not address this turn)",
            )
        )
    if backfilled:
        verdict.criteria = list(verdict.criteria) + backfilled
    return verdict


def build_recent_work_block(
    messages: list[dict[str, Any]],
    limit: int = 5,
    per_msg_chars: int = 4000,
    *,
    latest_per_msg_chars: int = 80_000,
) -> str:
    """Serialize the last `limit` assistant turns into the judge prompt.

    `messages` is a list of `{role, content}` dicts (or anything with those
    keys). We pick the last `limit` assistant turns, in reverse chronological
    order (newest first).

    Truncation rules:
      · The MOST RECENT turn (the one being judged) gets `latest_per_msg_chars`
        — generously sized so the judge sees the full work it's adjudicating.
        Default 80k characters covers a ~12k-token critique, well above what
        any single assistant turn realistically produces.
      · Older turns (kept for trajectory context) get `per_msg_chars`
        truncation since they're already represented in the verdict history.
      · When we DO truncate, the slice is annotated with an explicit marker
        showing the original length and that the cut wasn't agent-side.
        Without this, the judge can't tell apart "agent stopped mid-sentence"
        from "prompt slice landed mid-sentence" — a real failure mode.
    """
    if not messages:
        return "(no agent work yet — first turn)"
    assistant_turns: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "assistant":
            continue
        body = msg.get("content")
        if isinstance(body, list):
            parts = []
            for chunk in body:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    parts.append(str(chunk.get("text") or ""))
                elif isinstance(chunk, str):
                    parts.append(chunk)
            body = "\n".join(parts)
        text = str(body or "").strip()
        if not text:
            continue
        is_latest = len(assistant_turns) == 0
        cap = latest_per_msg_chars if is_latest else per_msg_chars
        original_len = len(text)
        if original_len > cap:
            # Keep the head — that's where the agent's structure is. Append
            # an explicit marker so the judge knows this is prompt-side
            # truncation, not agent stoppage.
            text = (
                text[:cap]
                + f"\n\n[...PROMPT-SIDE TRUNCATION: showing first {cap:,} of "
                f"{original_len:,} chars. The agent's full message was longer; "
                f"the rest was dropped to fit the judge prompt budget — "
                f"this is NOT agent stoppage.]"
            )
        assistant_turns.append(text)
        if len(assistant_turns) >= limit:
            break
    if not assistant_turns:
        return "(no assistant turns yet)"
    blocks = []
    for i, turn in enumerate(assistant_turns, start=1):
        blocks.append(f"--- TURN -{i} (most recent first) ---\n{turn}")
    return "\n\n".join(blocks)
