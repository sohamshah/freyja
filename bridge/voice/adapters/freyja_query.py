"""freyja.* verbs — Freyja querying itself (contract §12.4).

The voice agent can drive the Mac; these verbs let the operator ask
Freyja about its OWN ongoing work: "what are my agents working on",
"where's the such-and-such project", "ask a research agent about X".
All read-only over the session index + per-session working memory +
today's briefing (``freyja.ask`` is the one write path — it spawns a real
mission — and it lives in service.py because it needs bridge session
access; only ``freyja.sessions`` / ``freyja.project_status`` register
here).

Data sources (all under FREYJA_HOME, default ``~/.freyja``):
  · sessions/_index.json — {version, updatedAt, sessions:[{id, title,
    updatedAt, agentType, parentSessionId, …}]}. Newest by updatedAt.
  · projects/<id>/working_memory.json — {overview:{summary}, …}, the
    cheap one-line preview.
  · briefing/<date>/briefing.json — the briefer's clustered projects
    (via bridge.briefing.read_briefing, which honors FREYJA_HOME).

Nothing here shells out or reaches the network — it is pure disk reads,
so tests point FREYJA_HOME at a tmp tree and never touch the real home.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_SESSIONS_CAP = 8
_PREVIEW_CHARS = 140
# Sub-agent / compaction / scheduler-internal sessions aren't "what the
# operator's agents are working on" — same family the briefer skips.
_SKIP_PREFIXES = ("sub_", "comp_", "scheduler")


def _home() -> Path:
    """FREYJA_HOME (honored like the briefer) so tests can redirect it."""
    return Path(os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja"))


def _relative_time(updated_ms: float, *, now_ms: Optional[float] = None) -> str:
    """A terse human age: 'just now', '5m ago', '3h ago', '2d ago'."""
    now = now_ms if now_ms is not None else time.time() * 1000.0
    delta_s = max(0.0, (now - updated_ms) / 1000.0)
    if delta_s < 45:
        return "just now"
    if delta_s < 3600:
        return f"{int(round(delta_s / 60))}m ago"
    if delta_s < 86400:
        return f"{int(delta_s // 3600)}h ago"
    return f"{int(delta_s // 86400)}d ago"


def _load_index() -> list[dict[str, Any]]:
    """The sessions list from _index.json, or [] on any read/shape error."""
    path = _home() / "sessions" / "_index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt index is empty, not fatal
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict)]


def _updated_ms(s: dict[str, Any]) -> float:
    v = s.get("updatedAt") or s.get("updated_at") or s.get("lastActivity") or 0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _session_id(s: dict[str, Any]) -> str:
    return str(s.get("id") or s.get("session_id") or s.get("sessionId") or "")


def _wm_preview(session_id: str) -> str:
    """A short working-memory summary for the session, or "" — cheap: one
    file read of overview.summary, no thread walking."""
    if not session_id:
        return ""
    path = _home() / "projects" / session_id / "working_memory.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no working memory yet is fine
        return ""
    if not isinstance(d, dict):
        return ""
    ov = d.get("overview")
    summary = ov.get("summary") if isinstance(ov, dict) else None
    if not isinstance(summary, str):
        return ""
    flat = " ".join(summary.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[: _PREVIEW_CHARS - 1] + "…"


async def _sessions(args: dict[str, Any]) -> VerbResult:
    """Recent Freyja agent sessions, newest first, capped — 'what are my
    agents working on'. Subagent/compaction/scheduler sessions are
    filtered (same family the briefer skips)."""
    try:
        limit = int(args.get("limit"))
    except (TypeError, ValueError):
        limit = _SESSIONS_CAP
    limit = max(1, min(_SESSIONS_CAP, limit))

    rows: list[tuple[float, dict[str, Any]]] = []
    for s in _load_index():
        sid = _session_id(s)
        if not sid or sid.startswith(_SKIP_PREFIXES):
            continue
        rows.append((_updated_ms(s), s))
    rows.sort(key=lambda r: r[0], reverse=True)

    now_ms = time.time() * 1000.0
    listing: list[dict[str, Any]] = []
    for updated_ms, s in rows[:limit]:
        sid = _session_id(s)
        entry: dict[str, Any] = {
            "sessionId": sid,
            "title": str(s.get("title") or "").strip() or "(untitled)",
            "updatedAt": _relative_time(updated_ms, now_ms=now_ms) if updated_ms else "",
        }
        preview = _wm_preview(sid)
        if preview:
            entry["preview"] = preview
        listing.append(entry)

    if not listing:
        return VerbResult(
            ok=True, summary="no recent sessions", data={"sessions": []}
        )
    return VerbResult(
        ok=True,
        summary=f"{len(listing)} recent session{'s' if len(listing) != 1 else ''}",
        data={"sessions": listing},
    )


def _score_project(query: str, name: str) -> int:
    """Fuzzy match score of a briefing project name against the spoken
    query. Higher is better; 0 means no match. Case-insensitive, token
    aware — exact > substring > shared-word — so 'the release project'
    finds 'Release ops' without an exact string."""
    q = query.strip().lower()
    n = (name or "").strip().lower()
    if not q or not n:
        return 0
    if q == n:
        return 100
    if q in n or n in q:
        return 70
    q_words = {w for w in q.split() if len(w) > 2}
    n_words = {w for w in n.split() if len(w) > 2}
    shared = q_words & n_words
    if shared:
        return 40 + len(shared)
    # A prefix overlap ("kanban" vs "kanban board redesign") on any word.
    for qw in q_words:
        for nw in n_words:
            if qw.startswith(nw) or nw.startswith(qw):
                return 20
    return 0


async def _project_status(args: dict[str, Any]) -> VerbResult:
    """Match a spoken project name against today's (or the latest)
    briefing's clustered projects and report its state + summary. The
    briefer already does the clustering; we just fuzzy-resolve the name
    and read it back."""
    name = str(args.get("name") or "").strip()
    if not name:
        return VerbResult(
            ok=False, summary="which project?", error="missing_name"
        )
    try:
        from bridge.briefing import read_briefing

        briefing = read_briefing()
    except Exception as exc:  # noqa: BLE001 — briefing read is best-effort
        return VerbResult(
            ok=False,
            summary="couldn't read the briefing",
            error=str(exc).splitlines()[0][:120] if str(exc) else "briefing_unavailable",
        )
    doc = briefing.get("json") if isinstance(briefing, dict) else None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, list) or not projects:
        return VerbResult(
            ok=False,
            summary="no briefing yet — nothing to match against",
            data={"setup": "briefing"},
            error="no_briefing",
        )

    best: Optional[dict[str, Any]] = None
    best_score = 0
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        score = _score_project(name, str(proj.get("name") or ""))
        if score > best_score:
            best, best_score = proj, score
    if best is None or best_score == 0:
        known = [str(p.get("name") or "") for p in projects if isinstance(p, dict)]
        known = [k for k in known if k][:6]
        return VerbResult(
            ok=False,
            summary=f"no project matching {name!r}",
            data={"projects": known},
            error="no_match — the briefing knows: " + ", ".join(known) if known else "no_match",
        )

    proj_name = str(best.get("name") or name)
    state = str(best.get("state") or "unknown")
    summary_line = str(best.get("summary") or "").strip()
    session_id = best.get("session_id")
    data: dict[str, Any] = {
        "name": proj_name,
        "state": state,
        "summary": summary_line,
        "attention": bool(best.get("attention")),
        "date": briefing.get("date") if isinstance(briefing, dict) else None,
    }
    if isinstance(session_id, str) and session_id:
        data["sessionId"] = session_id
    spoken = f"{proj_name} — {state}"
    if summary_line:
        spoken = f"{spoken}: {summary_line}"
    return VerbResult(ok=True, summary=spoken[:120], data=data, say=summary_line or None)


def register(registry: VerbRegistry) -> None:
    """freyja.sessions + freyja.project_status. freyja.ask registers in
    service.py (it needs bridge session access to spawn a mission)."""
    registry.register(
        Verb(
            name="freyja.sessions",
            description=(
                "What Freyja's agents are working on: recent agent sessions, "
                "newest first, with a one-line preview"
            ),
            params={
                "limit": {"type": "integer", "description": "how many, cap 8"},
            },
            required=[],
            tier="auto",
            run=_sessions,
        )
    )
    registry.register(
        Verb(
            name="freyja.project_status",
            description=(
                "Where a project stands: its state + one-line summary from "
                "today's briefing (fuzzy-matches the name)"
            ),
            params={
                "name": {"type": "string", "description": "the project, as you'd say it"},
            },
            required=["name"],
            tier="auto",
            run=_project_status,
        )
    )
