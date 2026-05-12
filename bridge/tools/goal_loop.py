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
  · the operator's BRIEF — voice instructions, a rigor score (1 lenient ↔
    10 demanding), explicit must/should/may criteria with stable IDs,
    a never-do list, and when-to-stop logic;
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
  · Apply the operator's voice instructions and rigor score. Higher rigor
    means demanding harder evidence and resisting "good enough" more often.
    Always honor the never-do list as hard constraints.

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
become the main activity."""


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
JUDGE_PROFILES = ("quick", "standard", "deep")


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
class JudgeRules:
    """Operator-authored rules the judge applies on every turn. Persists
    per session.

    `voice` is a detailed freeform paragraph; `rigor_score` is 1 (lenient)
    to 10 (demanding); `judge_profile` is one of "quick" / "standard" /
    "deep" — picks the model + thinking budget for the judge call.
    `judge_tools` is an optional allowlist that overrides the profile
    default (empty → use the default for the active profile).
    `judge_max_iterations` caps how many turns the judge may take when
    running as a subagent (deep profile).

    The operator should write `voice` like a memo to a colleague —
    multiple sentences, specific guidance — not a one-liner.
    """

    voice: str = ""
    rigor_score: int = 6
    judge_profile: str = "standard"
    criteria: list[RuleCriterion] = field(default_factory=list)
    never_do: list[str] = field(default_factory=list)
    when_to_stop: str = ""
    # Empty list = use the profile default. Otherwise = explicit allowlist
    # for this brief, overriding the profile's defaults.
    judge_tools: list[str] = field(default_factory=list)
    # Bounded [1, 10]. Only consulted for the `deep` profile (subagent
    # path); quick / standard are always single-call.
    judge_max_iterations: int = 3
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice": self.voice,
            "rigorScore": self.rigor_score,
            "judgeProfile": self.judge_profile,
            "criteria": [c.to_dict() for c in self.criteria],
            "neverDo": list(self.never_do),
            "whenToStop": self.when_to_stop,
            "judgeTools": list(self.judge_tools),
            "judgeMaxIterations": self.judge_max_iterations,
            "updatedAt": int(self.updated_at * 1000),
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
            rigor = int(payload.get("rigorScore", 6))
        except Exception:
            rigor = 6
        try:
            max_iter = int(payload.get("judgeMaxIterations", 3))
        except Exception:
            max_iter = 3
        return cls(
            voice=str(payload.get("voice") or ""),
            rigor_score=max(1, min(rigor, 10)),
            judge_profile=_clamp_judge_profile(payload.get("judgeProfile")),
            criteria=crits,
            never_do=[str(x) for x in (payload.get("neverDo") or []) if str(x).strip()],
            when_to_stop=str(payload.get("whenToStop") or ""),
            judge_tools=[str(t).strip() for t in (payload.get("judgeTools") or []) if str(t).strip()],
            judge_max_iterations=max(1, min(max_iter, 10)),
            updated_at=time.time(),
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
        rigor_label = (
            "1 (lenient)" if self.rigor_score <= 2 else
            "10 (demanding)" if self.rigor_score >= 9 else
            f"{self.rigor_score} (moderate-strict)" if self.rigor_score >= 6 else
            f"{self.rigor_score} (moderate-lenient)"
        )
        out.append(f"Rigor: {self.rigor_score}/10 — {rigor_label}")
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
        if not (self.voice or self.criteria or self.never_do or self.when_to_stop):
            return "(no operator brief — apply default judgment)"
        return "\n".join(out)


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
            "maxTurns": self.max_turns,
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


def parse_goal_verdict(text: str) -> GoalVerdict:
    """Parse a goal judge response.

    Handles the new structured shape (criteria, open_questions) AND the
    legacy {done, reason, confidence} shape — graceful fallback when the
    judge regresses or an old model is used.
    """
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
    # rigorScore default is 6; treat anything other than 6 as content
    try:
        if int(payload.get("rigorScore") or 6) != 6:
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


def build_recent_work_block(messages: list[dict[str, Any]], limit: int = 5, per_msg_chars: int = 4000) -> str:
    """Serialize the last `limit` assistant turns into the judge prompt.

    `messages` is a list of `{role, content}` dicts (or anything with those
    keys). We pick the last `limit` assistant turns, in reverse chronological
    order (newest first), and truncate each to `per_msg_chars`.
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
        assistant_turns.append(text[:per_msg_chars])
        if len(assistant_turns) >= limit:
            break
    if not assistant_turns:
        return "(no assistant turns yet)"
    blocks = []
    for i, turn in enumerate(assistant_turns, start=1):
        blocks.append(f"--- TURN -{i} (most recent first) ---\n{turn}")
    return "\n\n".join(blocks)
