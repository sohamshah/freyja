"""IPC helper invoked by the desktop main process.

Spawned as a one-shot subprocess (`python -m bridge.knowledge.learning._ipc_helper <command>`)
to answer read-only queries that need the learning-loop modules:

  rollup <skill_name>          → JSON ValueRollup with last 10 outcome events
  list-candidates              → JSON array of pending candidates
  list-rejected [limit]        → JSON array of rejected candidates

Results are written to stdout as a single JSON object on the final
line; logs / warnings go to stderr. The main process parses the last
line of stdout. Errors are returned as ``{"ok": false, "error": "..."}``
with exit code 0 — the Node side surfaces the error string in the
renderer rather than crashing the IPC.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

from bridge.knowledge.learning import candidates, events, value_score


def _findings_to_summary(findings: list[dict[str, Any]] | None) -> str:
    if not findings:
        return ""
    parts: list[str] = []
    for f in findings[:4]:
        sev = str(f.get("severity") or "").strip()
        reason = str(f.get("reason") or f.get("pattern") or "").strip()
        line = str(f.get("line") or "")
        if reason:
            parts.append(f"[{sev or 'low'}{':L'+line if line else ''}] {reason}")
    return "; ".join(parts)


def _candidate_to_record(c: candidates.Candidate) -> dict[str, Any]:
    body = c.body or ""
    preview = body[:600] + ("…" if len(body) > 600 else "")
    # Compute overwrite stats so the candidates panel row shows the
    # +X/-Y badge inline — same shape as the live skill_candidate
    # event. Lazy import to avoid pulling drafter into the IPC helper
    # at module load time (it imports freyja_bridge, which is heavy).
    try:
        from bridge.knowledge.learning.drafter import _compute_existing_skill_diff_stats
        existing_stats = _compute_existing_skill_diff_stats(c)
    except Exception:  # noqa: BLE001
        existing_stats = {"exists": False}
    return {
        "candidateId": c.candidate_id,
        "name": c.name,
        "description": c.description,
        "skillType": c.skill_type or "build",
        "bodyPreview": preview,
        "body": body,
        "triggers": list(c.triggers or []),
        "tags": list(c.tags or []),
        "guardVerdict": c.guard_verdict or "safe",
        "guardSummary": _findings_to_summary(c.guard_findings),
        "decision": c.decision,
        "draftedAt": int(c.drafted_at or 0),
        "sourceSessionId": c.source_session_id or None,
        "sourceTurnId": c.source_turn_id or None,
        "existingSkill": existing_stats,
    }


def _rollup(skill_name: str) -> dict[str, Any]:
    rollup = value_score.compute_rollup(skill_name)
    # Pull the most recent outcome events (up to 10) so the operator
    # can see evidence text alongside the score.
    recent: list[dict[str, Any]] = []
    try:
        for ev in events.iter_events(skill_name=skill_name):
            if ev.get("event") == events.EVENT_OUTCOME:
                recent.append(ev)
    except Exception:  # noqa: BLE001
        recent = []
    recent.sort(key=lambda e: int(e.get("ts") or 0), reverse=True)
    recent = recent[:10]
    return {"ok": True, "rollup": rollup.to_json(recent_outcomes=recent)}


def _list_candidates() -> dict[str, Any]:
    try:
        items = candidates.list_pending()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"list_pending: {e!r}"}
    return {
        "ok": True,
        "candidates": [_candidate_to_record(c) for c in items],
    }


def _list_rejected(limit: int = 50) -> dict[str, Any]:
    # Read rejected candidates by walking the rejected_dir; each YAML
    # carries the same Candidate shape + a ``rejected_at`` / ``reason``
    # / ``actor`` sidecar block on disk.
    from pathlib import Path

    import yaml

    from bridge.knowledge.learning.paths import rejected_dir

    out: list[dict[str, Any]] = []
    try:
        d = rejected_dir()
        if d.exists():
            files = sorted(
                d.glob("*.yaml"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
            for path in files:
                try:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(raw, dict):
                    continue
                candidate_block = raw.get("candidate") or {}
                if not isinstance(candidate_block, dict):
                    candidate_block = {}
                body = str(candidate_block.get("body") or "")
                rec = {
                    "candidateId": str(raw.get("candidate_id") or ""),
                    "name": str(candidate_block.get("name") or ""),
                    "description": str(candidate_block.get("description") or ""),
                    "skillType": str(candidate_block.get("skill_type") or "build"),
                    "bodyPreview": body[:600] + ("…" if len(body) > 600 else ""),
                    "body": body,
                    "triggers": list(candidate_block.get("triggers") or []),
                    "tags": list(candidate_block.get("tags") or []),
                    "guardVerdict": str(raw.get("guard_verdict") or "safe"),
                    "guardSummary": (
                        str(raw.get("guard_summary") or "")
                        or _findings_to_summary(list(raw.get("guard_findings") or []))
                    ),
                    "decision": str(raw.get("decision") or "save"),
                    "draftedAt": int(raw.get("drafted_at") or 0),
                    "sourceSessionId": str(raw.get("source_session_id") or "") or None,
                    "sourceTurnId": str(raw.get("source_turn_id") or "") or None,
                    "rejectedAt": int(raw.get("rejected_at") or 0),
                    "reason": str(raw.get("rejected_reason") or raw.get("reason") or ""),
                    "actor": str(raw.get("rejected_by") or raw.get("actor") or ""),
                }
                out.append(rec)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"list_rejected: {e!r}"}
    return {"ok": True, "rejected": out}


def _candidate_diff(candidate_id: str) -> dict[str, Any]:
    """Return a unified diff between the candidate's rendered SKILL.md
    and the on-disk SKILL.md for the same skill name.

    We render the candidate via ``confirmation.render_skill_md`` — i.e.
    we diff the EXACT bytes that will land on disk after promote
    (frontmatter assembled from name/description/triggers/tags + body
    appended). Previously this compared ``candidate.body`` directly to
    the on-disk SKILL.md, which made it look like the description was
    being deleted on every overwrite — because the body field never
    contains frontmatter, but the on-disk file does. The user's promote
    re-emits the frontmatter; the diff was just misleading.

    Used by the renderer's SkillDiffModal when the operator clicks
    "view diff" on an overwriting candidate. Lazy: the skill_candidate
    event only carries +/- stats; this call produces the full diff
    text on demand.
    """
    import difflib
    from bridge.knowledge.learning.confirmation import render_skill_md
    from bridge.knowledge.learning.paths import skills_root, safe_skill_filename

    c = candidates.get_pending(candidate_id)
    if c is None:
        return {"ok": False, "error": "candidate not found"}
    safe = safe_skill_filename(c.name or "")
    if not safe:
        return {"ok": False, "error": "invalid skill name"}
    skill_path = skills_root() / safe / "SKILL.md"
    new_text = render_skill_md(c)
    if not skill_path.exists():
        # No existing skill — the "diff" is the whole new SKILL.md, but
        # render as a single +-block.
        return {
            "ok": True,
            "exists": False,
            "candidateBody": new_text,
            "existingBody": "",
            "unifiedDiff": "",
        }
    try:
        existing_text = skill_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"read existing failed: {exc!r}"}
    diff_lines = list(
        difflib.unified_diff(
            existing_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"{c.name} (existing)",
            tofile=f"{c.name} (after promote)",
            lineterm="",
            n=3,
        )
    )
    return {
        "ok": True,
        "exists": True,
        "skillPath": str(skill_path),
        "existingBody": existing_text,
        "candidateBody": new_text,
        "unifiedDiff": "\n".join(diff_lines),
    }


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "rollup":
            skill = argv[2] if len(argv) > 2 else ""
            if not skill:
                result = {"ok": False, "error": "rollup requires skill name"}
            else:
                result = _rollup(skill)
        elif cmd == "list-candidates":
            result = _list_candidates()
        elif cmd == "list-rejected":
            limit = int(argv[2]) if len(argv) > 2 else 50
            result = _list_rejected(limit=limit)
        elif cmd == "candidate-diff":
            candidate_id = argv[2] if len(argv) > 2 else ""
            if not candidate_id:
                result = {"ok": False, "error": "candidate-diff requires candidate id"}
            else:
                result = _candidate_diff(candidate_id)
        else:
            result = {"ok": False, "error": f"unknown command: {cmd!r}"}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"helper: {e!r}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
