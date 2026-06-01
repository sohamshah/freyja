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
