"""Morning briefing — the briefer job + the data contract the view reads.

The briefer is a regular scheduled job (no special scheduler machinery):
it fires daily before the user's morning, reads the working-memory
substrate the offline backfill + live triggers keep fresh, and writes
one briefing per day under ``~/.freyja/briefing/{YYYY-MM-DD}/``:

  briefing.json   — structured contract rendered by the Morning Room view
  briefing.md     — the same content as a readable narrative (Slack/chat
                    fallback, archive, debuggability)

``ensure_briefer_job`` is called at bridge boot and creates the job once
(idempotent via the ``briefer`` tag). The job id is mirrored to
``~/.freyja/briefing/briefer.json`` so the renderer can "rebrief now"
via scheduler.run_job_now without searching the job list.

Design notes
------------
· Execution is a fresh session per fire — continuity lives in the job's
  working notes (MemorySpec) and yesterday's briefing, both of which the
  prompt tells the agent to read. A persistent session would grow
  unboundedly for no benefit.
· Sinks are noop — the Morning Room IS the delivery surface; the
  briefing files are the artifact. (A Slack mirror can be added as a
  second sink later without touching this module.)
· The briefer only ever STAGES work (decisions + today-plan carry
  intents the view dispatches on user commit). It must never execute
  project work itself — that boundary is what makes an ambitious
  briefing safe.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("freyja.briefing")

BRIEFER_TAG = "briefer"
BRIEFER_NAME = "Morning briefing"


def briefing_root() -> Path:
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    return Path(base) / "briefing"


def briefer_pointer_path() -> Path:
    return briefing_root() / "briefer.json"


def local_iana_timezone() -> str:
    """Best-effort IANA timezone for the cron schedule. macOS keeps
    /etc/localtime as a symlink into the zoneinfo tree."""
    try:
        target = os.readlink("/etc/localtime")
        # .../zoneinfo/America/Los_Angeles → America/Los_Angeles
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


# ─── The contract ──────────────────────────────────────────────────────
# Documented here as the single source of truth; the prompt embeds it
# and the Morning Room view types mirror it (src/shared/events.ts
# BriefingDoc). Bump "version" on breaking changes.

BRIEFING_SCHEMA_DOC = """{
  "version": 1,
  "date": "YYYY-MM-DD",
  "generated_at_iso": "ISO-8601 with offset",
  "since_label": "HH:MM yesterday" or "your last visit",
  "hero": {
    "projects_in_motion": <int>,
    "events_since": <int>
  },
  "projects": [
    {
      "name": "short project name",
      "state": "ready" | "in_motion" | "blocked" | "quiet",
      "attention": <bool — true when this needs the user's eyes>,
      "summary": "ONE sentence, letterpress voice — what moved / where it stands",
      "session_id": "most relevant session id, or null"
    }
  ],
  "decisions": [
    {
      "verb": "approve" | "resolve" | "confirm" | "unblock" | "review",
      "project": "project name",
      "ref": "short ref to the thing (file, thread, run)",
      "meta": "status line — where it is, time pressure",
      "body": "2-3 sentence evidence paragraph; state a default recommendation",
      "actions": [
        {
          "label": "verb-led action label",
          "kind": "primary" | "secondary",
          "intent": {
            "kind": "open_session" | "fire_job" | "prompt",
            "session_id": "<for open_session>",
            "job_id": "<for fire_job>",
            "prompt": "<for prompt — a self-contained prompt to run in a NEW session>"
          }
        }
      ]
    }
  ],
  "today": [
    {
      "time": "HH:MM",
      "project": "project name",
      "what": "one sentence describing the staged run",
      "duration": "~30m",
      "intent": { same shape as decision action intents }
    }
  ],
  "weekly_review": null,
  "colophon": "one-line sign-off (sessions read · jobs checked · generated in Ns)"
}"""


def _home_str() -> str:
    """The FREYJA_HOME the prompt's paths must reference — keeps the
    briefer working when the bridge runs with a non-default home."""
    return os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")


def briefer_prompt() -> str:
    """Render the briefer's prompt with the live FREYJA_HOME. Called at
    job-creation time (the prompt is snapshotted onto the JobRecord)."""
    home = _home_str()
    return f"""You are Freyja's briefing agent. Produce today's morning briefing by
reading the state of every project, session, and scheduled job, then
writing two files. You STAGE work for the user to commit — you never
execute project work yourself.

# Read (in this order)

1. Your working notes (injected above) — calibration you've learned
   about how this user wants their briefing.
2. Yesterday's briefing, if it exists: the newest directory under
   {home}/briefing/ (format YYYY-MM-DD) — read its briefing.json so
   today's edition reflects what changed rather than restating.
3. Session summaries: every {home}/projects/*/working_memory.json.
   Each has an `overview` ({{summary, actionsCompleted}}) and an entity
   graph (workstreams / decisions / findings / open_threads /
   artifact_notes). Skip docs that are empty. open_threads with
   status "open" are your primary decision candidates.
4. Recent activity: for sessions whose working memory mentions ongoing
   work, {home}/projects/<same-dir>/action_ledger.jsonl tail (last
   ~30 lines) tells you what concretely happened and when (createdAt is
   epoch ms).
5. Session index: {home}/sessions/_index.json — titles + updatedAt
   for mapping session ids to human names and recency.
6. Scheduled jobs: {home}/schedules/jobs/*.json and each job's
   newest run under {home}/schedules/runs/<job_id>/. Failed or
   partial_failure runs are decision candidates; enabled jobs with
   upcoming fires belong in today's plan context.

Use bash (ls, cat, jq if available, python3) — read efficiently: list
first, then open only what matters. Most working_memory.json files are
small; do read all the non-empty ones.

# Synthesize

· Cluster sessions into PROJECTS by intent: matching/related workstream
  titles, shared artifact paths, session-title similarity. A project is
  the durable thing the user is trying to accomplish; sessions are
  visits to it. 3-8 projects is typical — if you find 20, you're
  splitting too fine.
· Project state: "blocked" if an unresolved open_thread or failed run
  gates progress; "ready" if a deliverable awaits the user; "in_motion"
  if work progressed in the last ~48h; else "quiet". attention=true
  for blocked/ready states.
· DECISIONS: only calls the USER must make — unresolved open_threads
  that gate work, failed scheduled runs needing a judgment, conflicting
  definitions, "should I continue X". 0-5 of them, ordered by urgency ×
  time-pressure. Each gets a default recommendation in the body (state
  it plainly: "Default: ...") and 1-2 actions with concrete intents.
· TODAY: stage 1 run per active project (max ~5). Each entry's intent
  should be a `prompt` intent with a fully self-contained prompt that
  an agent could execute without asking questions, OR `fire_job` when
  an existing scheduled job covers it, OR `open_session` when the right
  move is the user resuming a conversation.
· VOICE: letterpress-terse. One-sentence summaries. No filler, no
  "exciting progress!", no hedging. Numbers are load-bearing. Write
  like the call sheet of someone whose time matters.

# Write

mkdir -p {home}/briefing/$(date +%F)/ then write BOTH:

1. {home}/briefing/$(date +%F)/briefing.json — EXACTLY this schema:

{BRIEFING_SCHEMA_DOC}

   Valid JSON, no trailing commas, no comments. session_id/job_id must
   be REAL ids you saw in the data — never invent them. Omit an intent
   field (use null) rather than fabricating a target.

2. {home}/briefing/$(date +%F)/briefing.md — the same content as a
   readable memo: hero line, ## Projects, ## Needs you, ## Today,
   colophon. This is the Slack/archive rendering.

# Learn

Before finishing, append to your working notes anything that will make
tomorrow's briefing better: clustering choices you made (so tomorrow's
edition keeps project names STABLE), sources that were empty/noisy,
calibration about what the user engaged with. Keep project names
consistent day to day — renaming projects daily destroys the user's
mental model.
"""


# ─── Job creation ──────────────────────────────────────────────────────


async def ensure_briefer_job(state: Any) -> str | None:
    """Create the briefer scheduled job if it doesn't exist. Idempotent
    via the ``briefer`` tag. Returns the job id (existing or new), or
    None when the scheduler isn't available. Called at bridge boot.

    Cross-process safe: the desktop bridge and the headless daemon both
    boot through here, often within the same second after a rebuild —
    a flock around the check+create closes the TOCTOU window that would
    otherwise mint two briefer jobs (= two daily fires, double spend)."""
    scheduler = getattr(state, "scheduler", None)
    if scheduler is None:
        return None

    from bridge.scheduler.persistence import FileLock, locks_dir

    lock = FileLock(locks_dir() / "briefer-ensure.lock")
    if not lock.acquire():
        # A peer is mid-create. Don't race it; the pointer file will be
        # there on the next boot (and the tag check makes a later retry
        # a no-op anyway).
        return None
    try:
        try:
            jobs = await scheduler.list_jobs(None)
            for j in jobs:
                if BRIEFER_TAG in (getattr(j, "tags", None) or []):
                    _write_pointer(j.id)
                    return j.id
        except Exception:  # noqa: BLE001
            logger.debug("briefer job lookup failed", exc_info=True)
            return None
        return await _create_briefer_job(state, scheduler)
    finally:
        lock.release()


async def _create_briefer_job(state: Any, scheduler: Any) -> str | None:

    try:
        from bridge.scheduler.models import (
            CronSchedule,
            JobRecord,
            CreatorRef,
            MemorySpec,
            NewSession,
            NoopSinkSpec,
        )

        tz = local_iana_timezone()
        spec = JobRecord(
            id="",
            name=BRIEFER_NAME,
            description=(
                "Daily synthesis of all sessions, projects, and scheduled "
                "jobs into the Morning Room briefing. Auto-created at boot; "
                "safe to edit the schedule or pause."
            ),
            creator=CreatorRef(surface="api", session_id="briefing:boot"),
            schedule=CronSchedule(expression="0 6 * * *", timezone=tz),
            prompt=briefer_prompt(),
            execution=NewSession(),
            # Deliberately yolo, not the boot-time state tier: at boot the
            # tier still holds its 'low' default (settings land later via
            # IPC), and a scheduled fire can't answer permission prompts —
            # each gated tool call would stall to the auto-deny timeout
            # and the briefing would take an hour or fail. The briefer is
            # read-heavy and writes only under FREYJA_HOME/briefing.
            permission_snapshot="yolo",
            memory=MemorySpec(enabled=True),
            artifact=str(briefing_root()),
            sinks=[NoopSinkSpec()],
            tags=[BRIEFER_TAG, "system"],
        )
        job = await scheduler.create_job(spec)
        _write_pointer(job.id)
        logger.info("briefer job created: %s (cron 06:00 %s)", job.id, tz)
        return job.id
    except Exception:  # noqa: BLE001
        logger.exception("briefer job creation failed")
        return None


def _write_pointer(job_id: str) -> None:
    """Mirror the briefer's job id where the renderer can find it."""
    try:
        briefing_root().mkdir(parents=True, exist_ok=True)
        p = briefer_pointer_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"job_id": job_id}, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        logger.debug("briefer pointer write failed", exc_info=True)


# ─── Read side (used by the IPC handler indirectly via main process) ───


def list_briefing_dates() -> list[str]:
    """Available briefing dates, newest first."""
    root = briefing_root()
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[7] == "-":
            out.append(p.name)
    return sorted(out, reverse=True)


def read_briefing(date: str | None = None) -> dict[str, Any]:
    """Load one day's briefing (newest when ``date`` is None). Returns
    ``{dates, date, json, markdown, briefer_job_id}`` — json/markdown
    are None when absent or unparseable."""
    dates = list_briefing_dates()
    chosen = date if (date and date in dates) else (dates[0] if dates else None)
    doc: dict[str, Any] | None = None
    md: str | None = None
    if chosen:
        day_dir = briefing_root() / chosen
        jp = day_dir / "briefing.json"
        mp = day_dir / "briefing.md"
        try:
            if jp.exists():
                parsed = json.loads(jp.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    doc = parsed
        except Exception:  # noqa: BLE001
            logger.debug("briefing.json parse failed for %s", chosen, exc_info=True)
        try:
            if mp.exists():
                md = mp.read_text(encoding="utf-8")
        except OSError:
            pass
    job_id: str | None = None
    try:
        ptr = briefer_pointer_path()
        if ptr.exists():
            job_id = (json.loads(ptr.read_text(encoding="utf-8")) or {}).get("job_id")
    except Exception:  # noqa: BLE001
        pass
    return {
        "dates": dates,
        "date": chosen,
        "json": doc,
        "markdown": md,
        "briefer_job_id": job_id,
    }
