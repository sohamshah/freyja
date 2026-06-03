"""Candidate publishing — shared by the propose_skill tool.

The drafter sub-agent (registered as AgentType ``skill-drafter`` in
``bridge.tools.agent_types``) calls ``propose_skill`` as its final
action. That tool delegates to :func:`publish_candidate` here, which
owns the cross-cutting work:

  · Validate the supplied fields (name, description, body)
  · Scan the candidate content via Skills Guard
  · Construct the ``Candidate`` dataclass
  · Persist to ``.candidates/<id>.yaml`` (safe / caution) OR
    ``.rejected/<id>.yaml`` (dangerous)
  · Emit telemetry events (``EVENT_DRAFTED`` / ``EVENT_GUARD_VERDICT`` /
    ``EVENT_DISCARDED``) so the audit log records the decision
  · Emit the bridge-level ``skill_candidate`` event so the desktop
    SkillToast + SkillCandidatesPanel surface the candidate to the
    operator for approve / edit / discard
  · Compute and attach the +/- diff stats against the existing on-disk
    SKILL.md so the toast can render the overwrite badge

Returns a tuple ``(candidate_id, verdict, error)`` — the tool's
human-readable result is built from this.

Why centralize here: previously this logic lived inline in
``drafter._run_drafter_inner``, where it was tangled with the
single-LLM-call drafter shape. The agentic drafter calls it from a
tool, but the single-call drafter (kept around for tests + as a
fallback) can also call it. One canonical publish path = no drift
between the two entry points.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from bridge.knowledge.learning import candidates, events, skills_guard

logger = logging.getLogger(__name__)


# Re-export for convenience so callers don't need a separate import.
VERDICT_SAFE = skills_guard.VERDICT_SAFE
VERDICT_CAUTION = skills_guard.VERDICT_CAUTION
VERDICT_DANGEROUS = skills_guard.VERDICT_DANGEROUS


def publish_candidate(
    *,
    name: str,
    description: str,
    body: str,
    skill_type: str = "build",
    triggers: list[str] | None = None,
    tags: list[str] | None = None,
    rationale: str = "",
    source_session_id: str = "",
    source_turn_id: str = "",
    drafter_model: str = "",
    emit_fn: Any = None,
) -> tuple[str | None, str, str | None]:
    """Persist a skill candidate and announce it to the operator.

    Returns ``(candidate_id, verdict, error)``:

      · On success (safe / caution): ``(candidate_id, "safe"|"caution",
        None)`` — candidate written to ``.candidates/``; operator sees
        a SkillToast.
      · On guard-dangerous: ``("", "dangerous", reason_string)`` —
        candidate written to ``.rejected/`` so it lands in the negative
        library; no operator toast (the drafter should NOT have
        proposed this; we report it as an error to the calling tool).
      · On validation failure: ``("", "", reason_string)`` — nothing
        written; the calling tool reports the validation message.

    ``emit_fn`` is the bridge's ``emit`` (so we can stay decoupled
    from importing freyja_bridge from a low-level module). If ``None``,
    the renderer skill_candidate event isn't fired; the candidate still
    lands on disk where the IPC poller picks it up.
    """
    # ── Validate inputs ──
    name = (name or "").strip()
    description = (description or "").strip()
    body = body or ""
    skill_type = (skill_type or "build").strip() or "build"
    triggers_clean = [
        str(t).strip() for t in (triggers or []) if isinstance(t, str) and t.strip()
    ]
    tags_clean = [
        str(t).strip() for t in (tags or []) if isinstance(t, str) and t.strip()
    ]
    if not name:
        return "", "", "name is required"
    if not body:
        return "", "", "body is required"
    if not description:
        return "", "", "description is required (it's the skill's primary trigger surface)"

    # ── Skills Guard scan ──
    # Includes name + description so a malicious value in any
    # user-facing surface trips patterns, not just body bytes.
    scan_content = f"{name}\n{description}\n{body}"
    scan = skills_guard.scan_text(scan_content)

    new_candidate_id = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    candidate = candidates.Candidate(
        candidate_id=new_candidate_id,
        drafted_at=now_ms,
        source_session_id=source_session_id,
        source_turn_id=source_turn_id,
        drafter_model=drafter_model,
        decision="save",
        rationale=rationale or "",
        guard_verdict=scan.verdict,
        guard_findings=[f.to_dict() for f in scan.findings],
        name=name,
        description=description,
        triggers=triggers_clean,
        tags=tags_clean,
        body=body,
        skill_type=skill_type,
    )

    # ── Dangerous path: write to rejected/, log, return error ──
    if scan.is_dangerous():
        try:
            candidates.write_rejected(
                candidate,
                reason="skills_guard_dangerous",
                actor="guard",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "publish_candidate: write_rejected failed (name=%s)", name
            )
            return "", "dangerous", "guard verdict dangerous; write_rejected failed"
        _safe_event_append({
            "event": events.EVENT_DISCARDED,
            "skill": name,
            "session_id": source_session_id,
            "candidate_id": new_candidate_id,
            "reason": "skills_guard_dangerous",
        })
        _safe_event_append({
            "event": events.EVENT_GUARD_VERDICT,
            "skill": name,
            "session_id": source_session_id,
            "candidate_id": new_candidate_id,
            "verdict": scan.verdict,
            "summary": scan.summary,
            "finding_count": len(scan.findings),
        })
        return "", "dangerous", (
            "Skills Guard flagged the proposed content as dangerous and refused "
            "to publish. Findings: " + (scan.brief_summary() or "(none)")
        )

    # ── Safe / caution path: write to candidates/, log, emit ──
    try:
        candidates.write_pending(candidate)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_candidate: write_pending failed (name=%s)", name)
        return "", scan.verdict, f"write_pending failed: {type(exc).__name__}: {exc}"

    _safe_event_append({
        "event": events.EVENT_DRAFTED,
        "skill": name,
        "session_id": source_session_id,
        "candidate_id": new_candidate_id,
        "skill_type": skill_type,
        "guard_verdict": scan.verdict,
    })
    _safe_event_append({
        "event": events.EVENT_GUARD_VERDICT,
        "skill": name,
        "session_id": source_session_id,
        "candidate_id": new_candidate_id,
        "verdict": scan.verdict,
        "summary": scan.summary,
        "finding_count": len(scan.findings),
    })

    # Existing-skill diff stats (computed lazily here — drafter.py owns
    # the helper because that's where _compute_existing_skill_diff_stats
    # was originally written; the import is local to avoid a circular
    # dependency at module load).
    try:
        from bridge.knowledge.learning.drafter import _compute_existing_skill_diff_stats
        # Pass the Candidate so the diff renders the full SKILL.md
        # (assembled frontmatter + body) — matches what promote writes.
        existing_stats = _compute_existing_skill_diff_stats(candidate)
    except Exception:  # noqa: BLE001
        existing_stats = {"exists": False}

    if emit_fn is not None:
        try:
            body_preview = (
                body if len(body) <= 600 else body[:600] + "\n…[truncated, see candidate file]…"
            )
            emit_fn({
                "type": "skill_candidate",
                "sessionId": source_session_id,
                "sourceTurnId": source_turn_id or "",
                "candidateId": new_candidate_id,
                "name": name,
                "description": description,
                "skillType": skill_type,
                "bodyPreview": body_preview,
                "triggers": triggers_clean,
                "tags": tags_clean,
                "guardVerdict": scan.verdict,
                "guardSummary": scan.summary,
                "draftedAt": now_ms,
                "existingSkill": existing_stats,
            })
        except Exception:  # noqa: BLE001
            logger.debug("publish_candidate: emit(skill_candidate) failed", exc_info=True)

    logger.info(
        "publish_candidate: written name=%s id=%s verdict=%s",
        name, new_candidate_id, scan.verdict,
    )
    return new_candidate_id, scan.verdict, None


def _safe_event_append(payload: dict[str, Any]) -> None:
    """Append to the event log; swallow any failure. Telemetry must never
    break a candidate publish — the candidate is the operator-facing
    output, the event log is a cross-cutting audit trail."""
    try:
        events.append(payload)
    except Exception:  # noqa: BLE001
        logger.debug("publish_candidate: event append failed", exc_info=True)
