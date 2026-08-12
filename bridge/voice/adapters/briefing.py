"""Briefing read-aloud verb: briefing.read — the "radio edition".

The daily briefer (bridge/briefing.py) writes a structured edition to
``~/.freyja/briefing/<YYYY-MM-DD>/briefing.json`` each morning; the Morning
Room renders it. This verb lets the voice brain READ it aloud — "read me
my briefing", "what does my day look like" — by returning a compact spoken
script plus the structured data, so the model delivers it in Freyja's
terse voice instead of reciting markdown.

No app, no AppleScript, no TCC — just the briefing file on disk.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_MAX_PROJECTS = 8
_MAX_TODAY = 6


def _briefing_root() -> Path:
    # Mirror bridge.briefing.briefing_root without importing it (keeps the
    # adapter import graph free of the briefer's heavier deps).
    import os

    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    return Path(base) / "briefing"


def _load_edition(date: str) -> Optional[dict[str, Any]]:
    path = _briefing_root() / date / "briefing.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _latest_date() -> Optional[str]:
    """Newest edition dir name (YYYY-MM-DD), or None."""
    try:
        dates = sorted(
            p.name
            for p in _briefing_root().iterdir()
            if p.is_dir() and len(p.name) == 10 and p.name[4] == "-"
        )
    except OSError:
        return None
    return dates[-1] if dates else None


def _spoken_script(edition: dict[str, Any]) -> str:
    """A compact radio edition the model reads aloud — hero line, the
    decisions that need the operator, then today's first few blocks."""
    hero = edition.get("hero") or {}
    projects_in_motion = hero.get("projects_in_motion")
    events_since = hero.get("events_since")
    since = str(edition.get("since_label") or "").strip()
    parts: list[str] = []

    lead = ""
    if isinstance(projects_in_motion, int):
        lead = f"{projects_in_motion} project{'s' if projects_in_motion != 1 else ''} in motion"
    if isinstance(events_since, int):
        tail = f"{events_since} event{'s' if events_since != 1 else ''}"
        tail += f" since {since}" if since else ""
        lead = f"{lead}, {tail}" if lead else tail.capitalize()
    if lead:
        parts.append(lead + ".")

    decisions = edition.get("decisions") or []
    if decisions:
        needs = []
        for d in decisions[:4]:
            verb = str(d.get("verb") or "").strip()
            project = str(d.get("project") or "").strip()
            ref = str(d.get("ref") or "").strip()
            line = " ".join(x for x in (verb, project) if x)
            if ref:
                line += f" ({ref})"
            if line:
                needs.append(line)
        if needs:
            parts.append("Needs you: " + "; ".join(needs) + ".")

    today = edition.get("today") or []
    if today:
        blocks = []
        for t in today[:_MAX_TODAY]:
            tm = str(t.get("time") or "").strip()
            what = str(t.get("what") or t.get("project") or "").strip()
            if what:
                blocks.append(f"{tm} {what}".strip())
        if blocks:
            parts.append("Today: " + "; ".join(blocks) + ".")

    return " ".join(parts).strip()


def _structured(edition: dict[str, Any]) -> dict[str, Any]:
    """The trimmed data payload the model can reference for follow-ups."""
    projects = []
    for p in (edition.get("projects") or [])[:_MAX_PROJECTS]:
        projects.append(
            {
                "name": p.get("name"),
                "state": p.get("state"),
                "attention": bool(p.get("attention")),
                "summary": p.get("summary"),
            }
        )
    decisions = []
    for d in (edition.get("decisions") or [])[:5]:
        decisions.append(
            {
                "verb": d.get("verb"),
                "project": d.get("project"),
                "body": d.get("body"),
            }
        )
    today = []
    for t in (edition.get("today") or [])[:_MAX_TODAY]:
        today.append(
            {"time": t.get("time"), "project": t.get("project"), "what": t.get("what")}
        )
    return {"date": edition.get("date"), "projects": projects, "decisions": decisions, "today": today}


async def _read(args: dict[str, Any]) -> VerbResult:
    date = str(args.get("date") or "").strip() or time.strftime("%Y-%m-%d", time.localtime())
    edition = _load_edition(date)
    stale = False
    if edition is None:
        # Today's isn't there yet (the 6am fire may have slept) — offer the
        # most recent instead of nothing, and say it's not today's.
        latest = _latest_date()
        if latest and latest != date:
            edition = _load_edition(latest)
            stale = edition is not None
    if edition is None:
        return VerbResult(
            ok=False,
            summary="no briefing yet — none has been generated",
            error="no_briefing",
        )
    script = _spoken_script(edition)
    ed_date = str(edition.get("date") or date)
    prefix = f"(from {ed_date}, not today) " if stale else ""
    return VerbResult(
        ok=True,
        summary=f"briefing for {ed_date}",
        say=(prefix + script) if script else None,
        data={"stale": stale, **_structured(edition)},
    )


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="briefing.read",
            description=(
                "Read the daily briefing aloud — the morning radio edition: "
                "projects in motion, decisions that need the operator, and "
                "today's plan. Use for 'read me my briefing', 'what's my day "
                "look like', 'what needs me today'."
            ),
            params={
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD; omit for today's edition",
                }
            },
            required=[],
            tier="auto",
            run=_read,
        )
    )
