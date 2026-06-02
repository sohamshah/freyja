"""Skill candidate drafter.

The drafter is the LLM-driven step at the back of the skill-learning
loop. When the cadence engine decides "this turn warrants a review",
the bridge hands us a conversation snapshot and the current skill
landscape; we produce at most one candidate (or refuse) via a single
structured-output Opus call.

Lifecycle
─────────
  1. Build the user message (skill landscape + negative library +
     conversation excerpt).
  2. One structured-output call to Opus 4.8 — schema-enforced output
     so the model physically cannot return a malformed candidate.
  3. Parse the response. ``decision == "skip"`` short-circuits.
  4. Run Skills Guard over (name + description + body) to catch
     injection, exfiltration, destructive patterns embedded in the
     drafted body. Dangerous → discarded outright; safe / caution
     → forwarded to the candidate flow.
  5. Persist via ``candidates.write_pending`` (safe/caution) or
     ``candidates.write_rejected`` (dangerous), log the matching
     events, and emit a bridge event so the desktop UI / Slack
     gateway can surface a toast.

Why an LLM at the end of the pipeline at all
────────────────────────────────────────────
The cadence engine deciding "now" is cheap deterministic; what to
SAVE requires reading the full conversation and matching it against
the Hermes review-prompt rules (negative-claim avoidance, class-level
naming, declarative voice). Those rules don't compress into a regex.
We pay one Opus call per qualifying turn — typical $0.05-$0.15 —
because that's the cost of getting durable skill quality.

Failure mode
────────────
Drafter MUST NOT raise into the bridge's turn loop. The entire
async body is wrapped in a try/except that logs and returns None.
The cadence engine just sees "no candidate this turn" and moves on;
the next qualifying turn will retry.

References
──────────
  · Hermes background_review:
    ``docs/skill-learning-reference/artifacts/fork_construction.txt``
    (we mirror the stdout/stderr redirect pattern around the provider
    call so chatter never leaks into the user's chat surface).
  · Drafter prompt assembly: ``drafter_prompt.py``.
  · Skills Guard verdict policy: ``skills_guard.py`` module docstring.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from typing import Any

from bridge.knowledge.learning import events, skills_guard
from bridge.knowledge.learning.constants import (
    DRAFTER_DEFAULT_MODEL,
    DRAFTER_MAX_LISTED_SKILLS,
    DRAFTER_MODEL_ENV_VAR,
    OVERWRITE_DESTRUCTIVE_LINES,
    OVERWRITE_DESTRUCTIVE_RATIO,
)
from bridge.knowledge.learning.drafter_prompt import build_drafter_system_prompt

logger = logging.getLogger(__name__)


# ── Schema (enforced provider-side) ──────────────────────────────────


def _emit_candidate_schema() -> dict[str, Any]:
    """JSON schema for the drafter's single structured-output call.

    Anthropic's tool-call schema only honors a subset of JSON Schema —
    ``oneOf`` / ``if`` / ``then`` / ``else`` keywords are silently
    ignored, so we can't branch ``required`` fields on the value of
    ``decision`` the way pure JSON Schema would. Instead, we require
    EVERY save-path field at the top level and instruct the model (via
    each property's description) to emit empty strings + empty arrays
    when ``decision='skip'``. The drafter's parse path treats empty
    save-fields with ``decision='skip'`` as a normal skip (no extra
    cost) and rejects empty save-fields with ``decision='save'`` (the
    existing name/body emptiness check below catches this before the
    Skills Guard call so we never persist a half-built candidate).

    The enum on ``decision``, the enum on ``skill_type`` (with ``""``
    allowed for the skip path), and the maxLength caps on every string
    field are the model's hard guardrails — the provider refuses to
    emit output that violates them.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        # Every save-path field is required at the top level. The
        # model emits empty strings / empty arrays on the skip branch
        # rather than omitting the keys. Without this, decision='save'
        # with no name/body silently burned a full Opus call.
        "required": [
            "decision",
            "rationale",
            "name",
            "description",
            "skill_type",
            "triggers",
            "tags",
            "body",
        ],
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["save", "skip"],
                "description": (
                    "Whether to emit a candidate. On 'skip' the "
                    "save-path fields (name, description, skill_type, "
                    "triggers, tags, body) must still be present as "
                    "empty string / empty array."
                ),
            },
            "rationale": {
                "type": "string",
                "maxLength": 400,
                "description": (
                    "On 'skip' — one-line refusal reason (operator "
                    "reviews refusals to tune cadence). On 'save' — "
                    "short note on WHY this skill is worth saving."
                ),
            },
            # The prompt instructs the drafter to keep names 3-40 chars
            # (kebab-case, no path separators). The schema is the
            # provider-side guardrail for the same rule — keep them in
            # lockstep so we never see a 41-char name reach the writer.
            "name": {
                "type": "string",
                "maxLength": 40,
                "description": (
                    "On 'save' — kebab-case class-level skill name "
                    "(3-40 chars). On 'skip' — empty string."
                ),
            },
            "description": {
                "type": "string",
                "maxLength": 200,
                "description": (
                    "On 'save' — one-line description of when the "
                    "skill applies. On 'skip' — empty string."
                ),
            },
            "skill_type": {
                "type": "string",
                "enum": ["build", "guard", "reference", "workflow", ""],
                "description": (
                    "On 'save' — one of build / guard / reference / "
                    "workflow. On 'skip' — empty string."
                ),
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": (
                    "On 'save' — up to 8 trigger phrases. On 'skip' "
                    "— empty array."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": (
                    "On 'save' — up to 8 tags. On 'skip' — empty "
                    "array."
                ),
            },
            "body": {
                "type": "string",
                "maxLength": 30000,
                "description": (
                    "On 'save' — full SKILL.md body (markdown). On "
                    "'skip' — empty string."
                ),
            },
        },
    }


# ── Module-level state ──────────────────────────────────────────────
# Constants moved to `constants.py`; only mutable state stays here.


_logged_env_override = False


def _compute_existing_skill_diff_stats(
    skill_name: str,
    new_body: str,
) -> dict[str, Any]:
    """Compare a candidate body against the on-disk SKILL.md, if any.

    Returns a dict the renderer attaches to the candidate row. When the
    skill doesn't exist on disk, returns ``{"exists": False}`` and the
    renderer treats the candidate as net-new. When it does exist,
    returns line-level stats so the toast can render "↻ overwrites
    existing: +47 / -287 lines" plus an ``isDestructive`` flag.

    The actual unified diff is NOT included here — it can be large
    (10–100 KB for a long skill). The diff modal fetches it on demand
    via the ``skill:candidateDiff`` IPC handler.

    Best-effort: any failure (missing file, read error, encoding
    weirdness) returns ``{"exists": False}`` so the candidate emit
    path is never blocked by a stats computation problem.
    """
    try:
        from bridge.knowledge.learning.paths import skills_root, safe_skill_filename
        import difflib
        # Mirror the dir-naming rule that confirmation.promote uses so
        # the existence check matches the actual on-disk path.
        safe = safe_skill_filename(skill_name)
        if not safe:
            return {"exists": False}
        skill_path = skills_root() / safe / "SKILL.md"
        if not skill_path.exists() or not skill_path.is_file():
            return {"exists": False}
        existing_body = skill_path.read_text(encoding="utf-8", errors="replace")
        old_lines = existing_body.splitlines()
        new_lines = (new_body or "").splitlines()
        added = 0
        removed = 0
        for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        existing_line_count = len(old_lines)
        is_destructive = (
            removed >= OVERWRITE_DESTRUCTIVE_LINES
            or (
                existing_line_count > 0
                and removed / existing_line_count >= OVERWRITE_DESTRUCTIVE_RATIO
            )
        )
        return {
            "exists": True,
            "linesAdded": added,
            "linesRemoved": removed,
            "linesExisting": existing_line_count,
            "linesNew": len(new_lines),
            "bytesExisting": len(existing_body),
            "bytesNew": len(new_body or ""),
            "isDestructive": is_destructive,
            "skillPath": str(skill_path),
        }
    except Exception:  # noqa: BLE001
        logger.debug("drafter: diff stats failed for %s", skill_name, exc_info=True)
        return {"exists": False}


def _drafter_model() -> str:
    """The model used by the drafter.

    Defaults to ``claude-opus-4-8`` (top quality tier — verified present
    in ``freyja_bridge.AVAILABLE_MODELS``). Overridable via
    ``FREYJA_DRAFTER_MODEL`` so a cost-sensitive deployment can drop to
    Sonnet without code changes.

    When the env override is in effect, we log it ONCE per process so
    the operator can spot a forgotten override that's silently routing
    Opus calls to a cheaper / different model. Repeated drafter calls
    don't re-log to keep the harness output quiet.
    """
    global _logged_env_override
    override = os.environ.get(DRAFTER_MODEL_ENV_VAR)
    if override and override != DRAFTER_DEFAULT_MODEL:
        if not _logged_env_override:
            logger.info(
                "drafter: %s=%r overrides default %r",
                DRAFTER_MODEL_ENV_VAR, override, DRAFTER_DEFAULT_MODEL,
            )
            _logged_env_override = True
        return override
    return DRAFTER_DEFAULT_MODEL


# ── User message assembly ────────────────────────────────────────────


def build_user_message(
    *,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    negative_library_excerpt: str,
    operator_guidance: str = "",
) -> str:
    """Assemble the per-turn user message.

    Four fixed sections so prompt-cache hits stay clean across drafter
    calls — only the conversation excerpt should change between back-to-
    back invocations on the same session, and the [CONVERSATION] block
    is at the end so the cached prefix (system + skills + negative
    library) reuses cleanly when the negative library is stable.

    Sections:

      · [CURRENT SKILLS] — every skill name the operator has on disk.
        Capped at ``DRAFTER_MAX_LISTED_SKILLS`` so a power-user library
        doesn't blow the input context; the drafter sees enough to
        avoid duplicates without paying for the long tail.
      · [LOADED THIS SESSION] — the subset of the above that was
        actually loaded into this session's context. Drives the
        "PATCH a loaded skill first" preference order.
      · [NEGATIVE LIBRARY] — recently-rejected candidates so the
        drafter doesn't re-propose the same shape twice.
      · [OPERATOR GUIDANCE] — optional. When the operator typed
        ``/learn-this <free text>``, the free text is piped here so
        the drafter knows what to focus on, what to generalize, or
        which part of the conversation to extract. When empty the
        section is omitted entirely (don't pollute the cache prefix
        with empty slots).
      · [CONVERSATION] — the verbatim excerpt the cadence engine
        decided is worth reviewing.
    """
    listed_all = all_skill_names[:DRAFTER_MAX_LISTED_SKILLS]
    all_block = (
        "\n".join(f"  - {name}" for name in listed_all)
        if listed_all
        else "  (none yet)"
    )
    if len(all_skill_names) > DRAFTER_MAX_LISTED_SKILLS:
        all_block += (
            f"\n  … {len(all_skill_names) - DRAFTER_MAX_LISTED_SKILLS} more not shown"
        )

    loaded_block = (
        "\n".join(f"  - {name}" for name in loaded_skill_names)
        if loaded_skill_names
        else "  (none — agent did not load any skill this session)"
    )

    neg_block = negative_library_excerpt.strip() or "(no recent rejections)"

    excerpt = conversation_excerpt.strip() or "(empty)"

    guidance = (operator_guidance or "").strip()
    guidance_block = (
        f"[OPERATOR GUIDANCE]\nThe operator invoked /learn-this with this hint — take it as a steer, "
        f"not as a hard constraint, and still skip if the conversation doesn't support a real skill:\n"
        f"{guidance}\n\n"
        if guidance
        else ""
    )

    return (
        "[CURRENT SKILLS]\n"
        f"{all_block}\n\n"
        "[LOADED THIS SESSION]\n"
        f"{loaded_block}\n\n"
        "[NEGATIVE LIBRARY]\n"
        f"{neg_block}\n\n"
        f"{guidance_block}"
        "[CONVERSATION]\n"
        f"{excerpt}\n\n"
        "[TASK]\n"
        "Review the conversation per the rules above and emit one "
        "candidate or skip."
    )


# ── Public API ───────────────────────────────────────────────────────


async def run_drafter(
    *,
    session_id: str,
    turn_id: str | None,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    operator_guidance: str = "",
    run_id: str = "",
) -> str | None:
    """Run the drafter for one cadence-qualifying turn.

    Returns the ``candidate_id`` of the persisted candidate (either
    pending or rejected) on save. Returns ``None`` when the drafter
    decided to skip, when the provider returned no parsed output, or
    when any step in the pipeline failed.

    ``operator_guidance`` is the free-text hint accompanying a
    ``/learn-this`` invocation (e.g. "focus on the deployment workflow"
    or "generalize the cherry-pick pattern"). Empty for automatic
    cadence trips. Threaded into the user message so the LLM knows the
    operator's framing without changing the system prompt — preserving
    prompt-cache hits for the system block.

    Never raises — the entire body is wrapped in a try/except so a
    failure here cannot break the bridge's turn loop. Telemetry-side
    errors (event-log write failures, candidate write failures) are
    logged and swallowed.
    """
    try:
        return await _run_drafter_inner(
            session_id=session_id,
            turn_id=turn_id,
            conversation_excerpt=conversation_excerpt,
            loaded_skill_names=loaded_skill_names,
            all_skill_names=all_skill_names,
            operator_guidance=operator_guidance,
            run_id=run_id,
        )
    except Exception:
        # Last-resort guard: nothing in the drafter is allowed to
        # bubble into the bridge's turn loop. Log with traceback so we
        # can debug failures post-hoc, then swallow.
        logger.exception(
            "drafter: unexpected failure (session=%s turn=%s) — swallowed",
            session_id, turn_id,
        )
        return None


async def _run_drafter_inner(
    *,
    session_id: str,
    turn_id: str | None,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    operator_guidance: str = "",
    run_id: str = "",
) -> str | None:
    # Local imports for two reasons:
    #   1. ``bridge.freyja_bridge`` is a heavy module; we don't want to
    #      pay its import cost at module load time, only when a drafter
    #      invocation is actually requested.
    #   2. ``candidates`` is a sibling in the same MVP track and may
    #      not exist in every checkout — local import keeps this
    #      module importable on its own (the harness's import check
    #      passes even when ``candidates`` is half-built).
    from bridge.freyja_bridge import build_provider, emit
    from bridge.knowledge.learning import candidates
    from engine.types import Message, ThinkingConfig

    # Negative library excerpt is best-effort. If the candidates module
    # can't materialize one (fresh install, no rejections file yet) we
    # still want to run the drafter — the negative library is a quality
    # nudge, not a hard requirement.
    try:
        negative_excerpt = candidates.negative_library_excerpt()
    except Exception:
        logger.debug("drafter: negative_library_excerpt failed", exc_info=True)
        negative_excerpt = ""

    user_message = build_user_message(
        conversation_excerpt=conversation_excerpt,
        loaded_skill_names=loaded_skill_names,
        all_skill_names=all_skill_names,
        negative_library_excerpt=negative_excerpt,
        operator_guidance=operator_guidance,
    )

    # Build provider. Failures here mean Opus is unreachable in this
    # process — log + skip; the cadence engine retries on the next
    # qualifying turn.
    try:
        provider = build_provider(_drafter_model(), thinking_level="off")
    except Exception:
        logger.exception("drafter: failed to build provider")
        return None

    messages = [Message(role="user", content=user_message)]
    # System prompt = Hermes review block + Freyja port preamble + format
    # contract. ~3-4KB of stable text reused across every drafter
    # invocation. The Anthropic provider wraps any non-empty
    # ``system_prompt`` in a block-format payload with
    # ``cache_control: ephemeral`` attached (see
    # engine/anthropic_provider.py:_build_request lines 713-722), so the
    # second + later drafter calls within the cache window only pay
    # output tokens — confirmed cache reuse so long as
    # ``build_drafter_system_prompt()`` returns the same string.
    system_prompt = build_drafter_system_prompt()

    # Wrap the provider call in stdout/stderr redirect to devnull so any
    # SDK-side chatter (rate-limit retries, deprecation warnings, etc.)
    # never leaks into the user's chat surface. Matches Hermes'
    # background_review pattern (see fork_construction.txt).
    parsed: dict[str, Any] = {}
    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull, \
             contextlib.redirect_stdout(devnull), \
             contextlib.redirect_stderr(devnull):
            result = await provider.complete_structured(
                messages=messages,
                schema=_emit_candidate_schema(),
                schema_name="emit_candidate",
                schema_description=(
                    "Emit at most one skill candidate, or skip with a "
                    "rationale."
                ),
                system_prompt=system_prompt,
                max_tokens=4000,
                thinking=ThinkingConfig(enabled=False),
            )
    except Exception:
        logger.exception("drafter: provider call failed (session=%s)", session_id)
        return None

    # ``StructuredResponse`` exposes the parsed dict on ``.data``; we
    # also check ``.parsed`` for forward-compatibility with provider
    # backends that adopt the alternate name. Either way, an empty /
    # non-dict payload is the "no structured output" signal.
    parsed_field = getattr(result, "parsed", None)
    data_field = getattr(result, "data", None)
    if isinstance(parsed_field, dict) and parsed_field:
        parsed = parsed_field
    elif isinstance(data_field, dict) and data_field:
        parsed = data_field
    else:
        logger.warning(
            "drafter: provider returned no parsed dict (session=%s)",
            session_id,
        )
        return None

    decision = str(parsed.get("decision") or "").strip().lower()
    rationale = str(parsed.get("rationale") or "").strip()

    if decision == "skip":
        logger.info(
            "drafter: skip (session=%s rationale=%.200s)",
            session_id, rationale or "(none)",
        )
        # Telemetry: persist a drafter_skip event so the operator (and
        # any downstream dashboards) can distinguish "cadence never
        # tripped" from "tripped but skipped". Without this, the
        # "drafter ran but produced no candidate" path is invisible —
        # the only signal is the absence of a candidate file, which is
        # also what happens when no cadence trip fires.
        _safe_event_append(
            {
                "event": events.EVENT_DRAFTER_SKIP,
                "session_id": session_id,
                "turn_id": turn_id or "",
                "rationale": rationale or "",
                "model": _drafter_model(),
            }
        )
        # Also write the unified EVENT_DRAFTER_DECISION so a single
        # query against .events.jsonl can answer "what did the drafter
        # decide on its last run." Pairs with the trip event written
        # by review_worker.
        try:
            events.append_drafter_decision(
                session_id,
                turn_id=turn_id,
                result="skip",
                rationale=rationale or "",
            )
        except Exception:  # noqa: BLE001
            pass
        # Low-noise bridge event so the renderer's drafter-activity
        # strip can show "last decision: skip (rationale)" without
        # polling the events.jsonl file. NOT a toast — this is meant
        # for an always-visible activity surface, never a popup.
        try:
            emit(
                {
                    "type": "skill_drafter_pass",
                    "sessionId": session_id,
                    "decision": "skip",
                    "rationale": rationale or "",
                    "sourceTurnId": turn_id or "",
                    "model": _drafter_model(),
                    "ranAt": int(time.time() * 1000),
                    "runId": run_id,
                }
            )
        except Exception:
            logger.debug("drafter: emit(skill_drafter_pass) failed", exc_info=True)
        return None

    if decision != "save":
        # The schema enum constraint should make this impossible, but
        # if a provider quirk slipped a different value through we fail
        # safe by treating it as a skip.
        logger.warning(
            "drafter: unknown decision %r (session=%s) — treating as skip",
            decision, session_id,
        )
        return None

    # ── decision == "save" ──

    name = str(parsed.get("name") or "").strip()
    description = str(parsed.get("description") or "").strip()
    body = str(parsed.get("body") or "")
    skill_type = str(parsed.get("skill_type") or "build").strip() or "build"
    triggers_raw = parsed.get("triggers") or []
    tags_raw = parsed.get("tags") or []
    triggers = [str(t).strip() for t in triggers_raw if isinstance(t, str) and t.strip()]
    tags = [str(t).strip() for t in tags_raw if isinstance(t, str) and t.strip()]

    if not name or not body:
        logger.warning(
            "drafter: save decision missing name/body (session=%s) — skipping",
            session_id,
        )
        return None

    # Skills Guard scan. Concatenate the user-facing surfaces so a
    # malicious description or name (not just body) trips patterns —
    # name + description appear in the operator-facing confirmation
    # toast and could be a vector on their own.
    scan_content = f"{name}\n{description}\n{body}"
    scan = skills_guard.scan_text(scan_content)

    # Construct the Candidate dataclass directly. The bug we hit before
    # was constructing a dict here and passing it to write_pending which
    # expects a Candidate — the dict was also missing ``candidate_id``,
    # ``drafted_at``, and ``decision`` (all required dataclass fields),
    # and carried an extra ``guard_summary`` field the dataclass doesn't
    # accept. End-to-end candidate emission silently crashed at every
    # write_pending call until this fix.
    #
    # ``guard_summary`` is threaded SEPARATELY into the renderer emit()
    # payload below; it's intentionally not on the dataclass because the
    # YAML format keeps verdict+findings only (the summary text is
    # derivable from the findings list at render time).
    new_candidate_id = uuid.uuid4().hex
    candidate = candidates.Candidate(
        candidate_id=new_candidate_id,
        drafted_at=int(time.time() * 1000),
        source_session_id=session_id,
        source_turn_id=turn_id or "",
        drafter_model=_drafter_model(),
        decision="save",
        rationale=rationale,
        guard_verdict=scan.verdict,
        guard_findings=[f.to_dict() for f in scan.findings],
        name=name,
        description=description,
        triggers=triggers,
        tags=tags,
        body=body,
        skill_type=skill_type,
    )

    candidate_id: str | None = None

    if scan.is_dangerous():
        # Hard-block: never persist a dangerous candidate as pending.
        # Operator never sees a Promote button for content that tripped
        # critical patterns. Write to rejected/ so we have an audit
        # trail + the negative library picks it up.
        try:
            candidates.write_rejected(
                candidate,
                reason="skills_guard_dangerous",
                actor="guard",
            )
            candidate_id = new_candidate_id
        except Exception:
            logger.exception(
                "drafter: write_rejected failed (session=%s name=%s)",
                session_id, name,
            )
            return None

        # Telemetry: log a discarded + guard_verdict so the value
        # rollup + operator timeline both see the rejection.
        _safe_event_append(
            {
                "event": events.EVENT_DISCARDED,
                "skill": name,
                "session_id": session_id,
                "candidate_id": candidate_id,
                "reason": "skills_guard_dangerous",
            }
        )
        _safe_event_append(
            {
                "event": events.EVENT_GUARD_VERDICT,
                "skill": name,
                "session_id": session_id,
                "candidate_id": candidate_id,
                "verdict": scan.verdict,
                "summary": scan.summary,
                "finding_count": len(scan.findings),
            }
        )
        logger.info(
            "drafter: dangerous candidate rejected (session=%s name=%s candidate=%s)",
            session_id, name, candidate_id,
        )
        return None

    # ── safe / caution path ──
    try:
        candidates.write_pending(candidate)
        candidate_id = new_candidate_id
    except Exception as exc:
        logger.exception(
            "drafter: write_pending failed (session=%s name=%s)",
            session_id, name,
        )
        # Audit: distinguish "drafter rationally skipped" from "drafter
        # produced a candidate but write failed." Without this, the
        # outer review_worker logs `result=skip` for both — and the
        # operator can't tell the system is broken vs. quiet.
        error_rationale = f"write_pending failed: {type(exc).__name__}: {str(exc)[:200]}"
        try:
            events.append_drafter_decision(
                session_id,
                turn_id=turn_id,
                result="error",
                rationale=error_rationale,
            )
        except Exception:  # noqa: BLE001
            pass
        # Surface the failure to the DrafterActivityStrip so the user
        # actually sees it instead of "no candidate appeared, must have
        # skipped." This is the bridge event the renderer subscribes to.
        try:
            emit(
                {
                    "type": "skill_drafter_pass",
                    "sessionId": session_id,
                    "decision": "error",
                    "rationale": error_rationale,
                    "sourceTurnId": turn_id or "",
                    "model": _drafter_model(),
                    "ranAt": int(time.time() * 1000),
                    "runId": run_id,
                }
            )
        except Exception:
            logger.debug("drafter: emit(skill_drafter_pass error) failed", exc_info=True)
        return None

    _safe_event_append(
        {
            "event": events.EVENT_DRAFTED,
            "skill": name,
            "session_id": session_id,
            "candidate_id": candidate_id,
            "skill_type": skill_type,
            "guard_verdict": scan.verdict,
        }
    )
    _safe_event_append(
        {
            "event": events.EVENT_GUARD_VERDICT,
            "skill": name,
            "session_id": session_id,
            "candidate_id": candidate_id,
            "verdict": scan.verdict,
            "summary": scan.summary,
            "finding_count": len(scan.findings),
        }
    )

    # Surface a bridge-level event so the desktop UI / Slack gateway can
    # render a confirmation toast without polling the candidates dir.
    # Best-effort: the bridge emit hits stdout, and any failure here is
    # logged + swallowed (we have the on-disk candidate either way).
    #
    # Field names are CAMELCASE — the renderer's store reducer + the
    # SkillToast component read these exact names. A snake_case slip
    # here surfaces as "undefined" in every toast field, so don't drift
    # without also updating src/shared/events.ts.
    try:
        body_preview = body if len(body) <= 600 else body[:600] + "\n…[truncated, see candidate file]…"
        # Compute overwrite stats so the toast can show "↻ overwrites
        # existing: +X / -Y" and flag destructive promotes BEFORE the
        # operator clicks PROMOTE. The first ema-release-ops draft was
        # ~120 lines replacing a 404-line skill; without this badge the
        # operator had no way to spot the 65% content loss until after
        # promotion deleted it from disk.
        existing_stats = _compute_existing_skill_diff_stats(name, body)
        emit(
            {
                "type": "skill_candidate",
                "sessionId": session_id,
                "sourceTurnId": turn_id or "",
                "candidateId": candidate_id,
                "name": name,
                "description": description,
                "skillType": skill_type,
                "bodyPreview": body_preview,
                "triggers": triggers,
                "tags": tags,
                "guardVerdict": scan.verdict,
                "guardSummary": scan.summary,
                "draftedAt": int(time.time() * 1000),
                "existingSkill": existing_stats,
            }
        )
    except Exception:
        logger.debug("drafter: emit(skill_candidate) failed", exc_info=True)

    # Also surface the save decision to the DrafterActivityStrip — the
    # strip listens on `skill_drafter_pass` and previously only saw the
    # rational-skip path, so a successful candidate emission left the
    # strip stale (last_decision still showed the previous run). Now
    # the strip updates on every drafter exit: skip, candidate, error.
    try:
        emit(
            {
                "type": "skill_drafter_pass",
                "sessionId": session_id,
                "decision": "save",
                "rationale": f"saved candidate {name!r}",
                "sourceTurnId": turn_id or "",
                "model": _drafter_model(),
                "ranAt": int(time.time() * 1000),
                "candidateId": candidate_id,
                "name": name,
            }
        )
    except Exception:
        logger.debug("drafter: emit(skill_drafter_pass save) failed", exc_info=True)
    # Unified decision event for the .events.jsonl audit trail. Pairs
    # with EVENT_DRAFTER_TRIP (review_worker) and EVENT_DRAFTED above.
    try:
        events.append_drafter_decision(
            session_id,
            turn_id=turn_id,
            result="candidate",
            candidate_id=candidate_id,
            rationale=f"saved {name}",
        )
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "drafter: candidate written (session=%s name=%s candidate=%s verdict=%s)",
        session_id, name, candidate_id, scan.verdict,
    )
    return candidate_id


# ── Internal helpers ─────────────────────────────────────────────────


def _safe_event_append(payload: dict[str, Any]) -> None:
    """Wrap ``events.append`` so a telemetry failure can't bubble up.

    The events module itself swallows OSError, but a bug in event
    construction (e.g. a non-serializable field added by a future
    caller) would propagate as a TypeError. We catch broadly here
    because losing one event line is strictly better than failing the
    drafter run after the candidate has already been persisted."""
    try:
        events.append(payload)
    except Exception:
        logger.debug("drafter: event append failed", exc_info=True)
