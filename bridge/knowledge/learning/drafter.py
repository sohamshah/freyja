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
from typing import Any

from bridge.knowledge.learning import events, skills_guard
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


# ── Model selection ──────────────────────────────────────────────────


_DEFAULT_DRAFTER_MODEL = "claude-opus-4-8"
_DRAFTER_MODEL_ENV = "FREYJA_DRAFTER_MODEL"
_logged_env_override = False


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
    override = os.environ.get(_DRAFTER_MODEL_ENV)
    if override and override != _DEFAULT_DRAFTER_MODEL:
        if not _logged_env_override:
            logger.info(
                "drafter: %s=%r overrides default %r",
                _DRAFTER_MODEL_ENV, override, _DEFAULT_DRAFTER_MODEL,
            )
            _logged_env_override = True
        return override
    return _DEFAULT_DRAFTER_MODEL


# ── User message assembly ────────────────────────────────────────────


_MAX_LISTED_SKILLS = 50


def build_user_message(
    *,
    conversation_excerpt: str,
    loaded_skill_names: list[str],
    all_skill_names: list[str],
    negative_library_excerpt: str,
) -> str:
    """Assemble the per-turn user message.

    Four fixed sections so prompt-cache hits stay clean across drafter
    calls — only the conversation excerpt should change between back-to-
    back invocations on the same session, and the [CONVERSATION] block
    is at the end so the cached prefix (system + skills + negative
    library) reuses cleanly when the negative library is stable.

    Sections:

      · [CURRENT SKILLS] — every skill name the operator has on disk.
        Capped at ``_MAX_LISTED_SKILLS`` so a power-user library
        doesn't blow the input context; the drafter sees enough to
        avoid duplicates without paying for the long tail.
      · [LOADED THIS SESSION] — the subset of the above that was
        actually loaded into this session's context. Drives the
        "PATCH a loaded skill first" preference order.
      · [NEGATIVE LIBRARY] — recently-rejected candidates so the
        drafter doesn't re-propose the same shape twice.
      · [CONVERSATION] — the verbatim excerpt the cadence engine
        decided is worth reviewing.
    """
    listed_all = all_skill_names[:_MAX_LISTED_SKILLS]
    all_block = (
        "\n".join(f"  - {name}" for name in listed_all)
        if listed_all
        else "  (none yet)"
    )
    if len(all_skill_names) > _MAX_LISTED_SKILLS:
        all_block += (
            f"\n  … {len(all_skill_names) - _MAX_LISTED_SKILLS} more not shown"
        )

    loaded_block = (
        "\n".join(f"  - {name}" for name in loaded_skill_names)
        if loaded_skill_names
        else "  (none — agent did not load any skill this session)"
    )

    neg_block = negative_library_excerpt.strip() or "(no recent rejections)"

    excerpt = conversation_excerpt.strip() or "(empty)"

    return (
        "[CURRENT SKILLS]\n"
        f"{all_block}\n\n"
        "[LOADED THIS SESSION]\n"
        f"{loaded_block}\n\n"
        "[NEGATIVE LIBRARY]\n"
        f"{neg_block}\n\n"
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
) -> str | None:
    """Run the drafter for one cadence-qualifying turn.

    Returns the ``candidate_id`` of the persisted candidate (either
    pending or rejected) on save. Returns ``None`` when the drafter
    decided to skip, when the provider returned no parsed output, or
    when any step in the pipeline failed.

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
                    "decidedAt": int(time.time() * 1000),
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

    # Key names MUST match ``candidates.Candidate`` dataclass field
    # names so the upstream C1 fix (which constructs a Candidate from
    # this payload) can do ``Candidate(**candidate_payload)`` cleanly.
    # In particular: ``source_session_id`` not ``session_id``,
    # ``source_turn_id`` not ``turn_id``, ``drafter_model`` not
    # ``model``. Drift here surfaces as TypeError("unexpected keyword
    # argument") which the outer try/except swallows silently.
    #
    # The ``guard_summary`` field is NOT on the Candidate dataclass —
    # it's threaded separately to the emit() payload for the renderer.
    # We strip it before constructing the dataclass (see write_pending
    # path below).
    candidate_payload = {
        "source_session_id": session_id,
        "source_turn_id": turn_id or "",
        "name": name,
        "description": description,
        "skill_type": skill_type,
        "triggers": triggers,
        "tags": tags,
        "body": body,
        "rationale": rationale,
        "drafter_model": _drafter_model(),
        "guard_verdict": scan.verdict,
        "guard_summary": scan.summary,
        "guard_findings": [f.to_dict() for f in scan.findings],
    }

    candidate_id: str | None = None

    if scan.is_dangerous():
        # Hard-block: never persist a dangerous candidate as pending.
        # Operator never sees a Promote button for content that tripped
        # critical patterns. Write to rejected/ so we have an audit
        # trail + the negative library picks it up.
        try:
            candidate_id = candidates.write_rejected(
                candidate_payload,
                reason="skills_guard_dangerous",
            )
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
        candidate_id = candidates.write_pending(candidate_payload)
    except Exception:
        logger.exception(
            "drafter: write_pending failed (session=%s name=%s)",
            session_id, name,
        )
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
            }
        )
    except Exception:
        logger.debug("drafter: emit(skill_candidate) failed", exc_info=True)

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
