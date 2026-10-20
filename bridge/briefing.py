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

# Recency-shortlist tuning. The briefer's substrate is hundreds of
# project dirs (most months-stale); free-form triage over them is
# unreliable and skipped the most recent session in practice. So we
# compute the fresh set deterministically and inject it at fire time.
_SKIP_PREFIXES = ("sub_", "comp_", "scheduler")
_SHORTLIST_MIN = 3          # always surface at least the N most-recent
_SHORTLIST_MAX = 30         # bound the injected block
_DEFAULT_LOOKBACK_SECONDS = 72 * 3600  # fallback window if no prior edition
_OVERVIEW_PREVIEW_CHARS = 320
# A project is on the page only if one of its sessions had work this
# recently. Older work is a choice the user made not to act on.
ACTIVE_WINDOW_DAYS = 7
_ACTIVE_LIST_MAX = 40
# Files a session writes only when work happens: a model call appends to
# raw_messages.jsonl, a tool call to action_ledger.jsonl.
_WORK_FILES = ("raw_messages.jsonl", "action_ledger.jsonl")


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
  "version": 2,
  "date": "YYYY-MM-DD",
  "generated_at_iso": "ISO-8601 with offset",
  "since_label": "HH:MM yesterday" or "your last visit",
  "hero": {
    "projects_in_motion": <int — MUST equal the number of projects below>,
    "events_since": <int>
  },
  "projects": [
    {
      "name": "short project name, stable day to day",
      "state": "blocked" | "ready" | "in_motion",
      "attention": <bool — true for blocked and ready>,
      "last_active": "YYYY-MM-DD — newest work in any of its sessions, from the injected block",
      "summary": "where it stands now — 1-2 short sentences",
      "moved": ["what changed since the last edition — past tense, one fact each (0-3)"],
      "pending": ["open work — what is left and who owns it (0-4)"],
      "next": "the single next action, verb first, naming who does it — or null",
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


# ─── Recency shortlist (fire-time context) ─────────────────────────────
# The briefer can't reliably hand-triage 150+ working_memory.json files,
# and its snapshotted prompt can't carry data that changes daily. So we
# compute the set of sessions active since the last edition determinist-
# ically and inject it at fire time (runtime._compose_fire_prompt calls
# fire_context_block). Keyed on session updatedAt — NOT working-memory
# mtime — so a lagging backfill can never hide a freshly-active session.


def _sessions_index() -> list[dict]:
    p = Path(_home_str()) / "sessions" / "_index.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return [s for s in sessions if isinstance(s, dict)] if isinstance(sessions, list) else []


def _session_id(s: dict) -> str:
    return str(s.get("id") or s.get("session_id") or s.get("sessionId") or "")


def _updated_ms(s: dict) -> float:
    v = s.get("updatedAt") or s.get("updated_at") or s.get("lastActivity") or 0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _project_dir(session_id: str) -> Path:
    from bridge.project_paths import safe_session_id

    return Path(_home_str()) / "projects" / safe_session_id(session_id)


def _last_work_ms(s: dict) -> float:
    """When a session last did work, in epoch ms.

    The index's updatedAt is not that: opening a session or a backfill
    pass moves it with no new work. On 2026-10-19 a re-index moved six
    old sessions to that day, one last worked on Jul 1, and they would
    have read as fresh. The session's own logs change only when a model
    or tool call happens, so their mtime is the work signal. A session
    with neither file (a Jev run, an index-only row) falls back to
    updatedAt."""
    pdir = _project_dir(_session_id(s))
    newest = 0.0
    for name in _WORK_FILES:
        try:
            newest = max(newest, (pdir / name).stat().st_mtime * 1000.0)
        except OSError:
            continue
    return newest or _updated_ms(s)


def _work_rows() -> list[tuple[float, str, str]]:
    """Every non-subagent session as ``(last_work_ms, session_id,
    title)``, newest work first. A session with a parent (a Jev run) is
    left out: its parent's ledger records the run."""
    rows: list[tuple[float, str, str]] = []
    for s in _sessions_index():
        sid = _session_id(s)
        if not sid or sid.startswith(_SKIP_PREFIXES) or s.get("parentSessionId"):
            continue
        rows.append((_last_work_ms(s), sid, str(s.get("title") or "")[:80]))
    rows.sort(reverse=True)
    return rows


def recency_shortlist(
    *, now: float, since: float, rows: list[tuple[float, str, str]] | None = None
) -> list[tuple[float, str, str]]:
    """Non-subagent sessions with work since ``since`` (epoch s), newest
    first, as ``(last_work_ms, session_id, title)``. Always returns at
    least ``_SHORTLIST_MIN`` (the most recent, even if older than
    ``since``) so a quiet window still surfaces the latest activity, and
    never more than ``_SHORTLIST_MAX``."""
    rows = _work_rows() if rows is None else rows
    recent = [r for r in rows if r[0] / 1000.0 >= since]
    if len(recent) < _SHORTLIST_MIN:
        recent = rows[:_SHORTLIST_MIN]
    return recent[:_SHORTLIST_MAX]


def active_window(
    *, now: float, exclude: set[str], rows: list[tuple[float, str, str]] | None = None
) -> list[tuple[float, str, str]]:
    """Sessions with work in the last ``ACTIVE_WINDOW_DAYS`` that are not
    in ``exclude`` (the fresh set), newest first. With the fresh set,
    this is every session a project on the page may come from."""
    rows = _work_rows() if rows is None else rows
    floor = now - ACTIVE_WINDOW_DAYS * 86400
    return [
        r for r in rows if r[0] / 1000.0 >= floor and r[1] not in exclude
    ][:_ACTIVE_LIST_MAX]


def _wm_overview(session_id: str) -> dict | None:
    """Best-effort {summary, open_threads} for a session's working
    memory. None when the file is missing/unparseable (e.g. backfill
    hasn't summarized it yet) — the session still belongs on the list."""
    p = _project_dir(session_id) / "working_memory.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    ov = d.get("overview")
    summary = str(ov.get("summary") or "") if isinstance(ov, dict) else ""
    threads = d.get("open_threads")
    n_open = 0
    if isinstance(threads, list):
        n_open = sum(
            1 for t in threads
            if isinstance(t, dict) and str(t.get("status") or "").lower() == "open"
        )
    return {"summary": summary, "open_threads": n_open}


def _prior_briefing_ts(today_str: str) -> float | None:
    """generated_at epoch of the most recent edition strictly before
    ``today_str`` — the window a fresh edition (or a same-day rebrief)
    should measure 'new' against."""
    from datetime import datetime

    for date in list_briefing_dates():  # newest first
        if date >= today_str:
            continue
        jp = briefing_root() / date / "briefing.json"
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
            iso = (d or {}).get("generated_at_iso")
            if iso:
                return datetime.fromisoformat(iso).timestamp()
        except Exception:  # noqa: BLE001
            continue
    return None


def render_recency_block(*, now: float | None = None) -> str:
    """The fire-time block: every session active since the last edition,
    newest first, each with a working-memory preview. Empty string when
    there's nothing to show (no sessions / no index)."""
    import time

    now = now if now is not None else time.time()
    today_str = time.strftime("%Y-%m-%d", time.localtime(now))
    prior = _prior_briefing_ts(today_str)
    since = prior if prior is not None else (now - _DEFAULT_LOOKBACK_SECONDS)
    rows = _work_rows()
    entries = recency_shortlist(now=now, since=since, rows=rows)
    if not entries:
        return ""
    others = active_window(now=now, exclude={sid for _, sid, _ in entries}, rows=rows)

    home = _home_str()
    since_label = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(prior))
        if prior is not None else "the last ~72h"
    )

    def when(ms: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return time.strftime(fmt, time.localtime(ms / 1000.0)) if ms else "unknown"

    lines = [
        f"# Sessions with new work since your last briefing ({since_label}) — READ EVERY ONE",
        "",
        "This is the complete set of non-subagent sessions with new work since "
        "your last edition, newest first. It is computed from the session "
        "logs — do NOT rely on your own scan to find what's new. For EACH "
        "entry, read its full `working_memory.json` and the tail of its "
        f"`action_ledger.jsonl` under `{home}/projects/<dir>/`. Never skip an "
        "entry because it looks finished: a delivered artifact awaiting the "
        "user is a `ready` project, not a non-event. The previews are partial "
        "— open the files for the real content. This list OVERRIDES your "
        "working notes: if a note says to ignore or skip a project, but one of "
        "its sessions appears here, that note is STALE — the session is active "
        "NOW. Surface it.",
        "",
    ]
    for i, (ms, sid, title) in enumerate(entries, 1):
        ov = _wm_overview(sid)
        ttl = f' · "{title}"' if title else ""
        dir_note = ""
        if _project_dir(sid).name != sid:
            dir_note = f" · dir {_project_dir(sid).name}"
        if ov is None:
            lines.append(f"{i}. {sid}{ttl} · last work {when(ms)}{dir_note}")
            lines.append(
                "    overview: (working memory not yet summarized — read this "
                "session's action_ledger.jsonl tail directly)"
            )
        else:
            summary = " ".join((ov["summary"] or "").split())[:_OVERVIEW_PREVIEW_CHARS]
            threads = f" · {ov['open_threads']} open thread(s)" if ov["open_threads"] else ""
            lines.append(f"{i}. {sid}{ttl} · last work {when(ms)}{threads}{dir_note}")
            lines.append(f"    overview: {summary or '(empty)'}")

    lines += [
        "",
        f"# Also worked on in the last {ACTIVE_WINDOW_DAYS} days",
        "",
        "These sessions had work before your last edition. Open one only when "
        "a project needs its detail. A project is on today's page only if one "
        "of its sessions is in this list or the one above. Every session not "
        f"listed here had no work in the last {ACTIVE_WINDOW_DAYS} days, even "
        "when `_index.json` shows a recent updatedAt (opening a session or a "
        "re-index moves that time without new work).",
        "",
    ]
    if others:
        for ms, sid, title in others:
            if not title or sid.endswith(title):
                # Gateway threads are titled with their own id; the
                # start of the summary says what the thread is about.
                ov = _wm_overview(sid)
                title = " ".join(((ov or {}).get("summary") or "").split())[:80]
            ttl = f' · "{title}"' if title else ""
            lines.append(f"- {sid}{ttl} · last work {when(ms, '%Y-%m-%d')}")
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def fire_context_block(job: Any) -> str:
    """Hook called by the scheduler at fire time. Returns the recency
    block for the briefer job, '' for every other job. Guarded so a
    failure here can never break a scheduled fire."""
    try:
        if BRIEFER_TAG not in (getattr(job, "tags", None) or []):
            return ""
        return render_recency_block()
    except Exception:  # noqa: BLE001
        logger.debug("recency block render failed", exc_info=True)
        return ""


def briefer_prompt() -> str:
    """Render the briefer's prompt with the live FREYJA_HOME. Called at
    job-creation time (the prompt is snapshotted onto the JobRecord)."""
    home = _home_str()
    return f"""You are Freyja's briefing agent. Each morning you tell the user where
their active projects stand, what needs them, and what to do next. You
read their sessions and scheduled jobs, then write two files.
You STAGE work for the user to commit — you never execute project work
yourself.

# Read (in this order)

1. **The "Sessions with new work since your last briefing" block above
   is your authoritative reading list.** It is computed from the session
   logs and names EVERY non-subagent session with new work since the
   last edition, newest first. **It outranks your working notes**: if a
   note tells you to ignore or skip a project but one of its sessions is
   listed, the note is STALE — surface the session. For each listed
   session, read {home}/projects/<dir>/working_memory.json AND the tail
   of its action_ledger.jsonl (last ~30 lines; createdAt is epoch ms).
   <dir> is the session id with every character outside [A-Za-z0-9_.-]
   replaced by "-" (the block names the dir when it differs). Do not skip
   any: a delivered artifact awaiting the user is a `ready` project.
2. The "Also worked on in the last {ACTIVE_WINDOW_DAYS} days" list in the
   same block. Open one of those sessions only when a project needs its
   detail.
3. Your working notes (injected above): names, clustering, and what this
   user engages with.
4. The newest edition under {home}/briefing/ (YYYY-MM-DD dirs). Read its
   briefing.json so today says what changed instead of restating, and so
   project names stay stable.
5. Scheduled jobs: {home}/schedules/jobs/*.json and each job's newest run
   under {home}/schedules/runs/<job_id>/. A failed or partial_failure run
   is a decision candidate.
6. {home}/sessions/_index.json maps session ids to titles. Its updatedAt
   is NOT a work signal: opening a session or a re-index moves it with
   no new work. Use the dates in the injected block.

If the injected block is absent, scan {home}/projects/*/working_memory.json
and take the newer mtime of each dir's raw_messages.jsonl and
action_ledger.jsonl as that session's last work.

Use bash (ls, cat, jq if available, python3) and read efficiently. Each
working_memory.json has an `overview` ({{summary, actionsCompleted}}) and
an entity graph (workstreams / decisions / findings / open_threads /
artifact_notes). open_threads with status "open" are decision candidates.

# What goes on the page

A project is the durable thing the user is trying to get done; sessions
are visits to it. Cluster sessions into projects by intent: related
titles, shared repos or artifact paths. 3-8 projects is typical — if you
find 20, you're splitting too fine.

The page shows ACTIVE projects only:

· A project is on the page only if at least one of its sessions is in
  the injected block (new work since the last edition, or work in the
  last {ACTIVE_WINDOW_DAYS} days). Nothing else puts a project on the
  page: not your notes, not yesterday's edition, not an unread
  deliverable.
· Yesterday's edition and your notes keep names stable. They are not a
  roster. A project from yesterday with no session in the block drops
  off today. That is correct, not a loss.
· Do not count how long something has waited. An old deliverable the
  user has not opened, or an approved plan nobody started, means the
  user chose not to act. Leave it off; no "N days unread" lines.
· Signals you gather yourself (disk space, a failed scheduled run) are
  not projects. If one needs action, it goes in Needs you or Today.
· State: "blocked" when an unresolved question or a failed run stops
  progress. "ready" when a deliverable from the window waits for the
  user. "in_motion" when work moved and more remains. A project that is
  none of these has nothing to say: leave it off. attention=true for
  blocked and ready.
· Order: blocked, then ready, then in_motion. Within a state, newest
  work first.

# Project entries

Each entry answers four questions for a reader who missed yesterday:

· summary — Where does it stand now? 1-2 short sentences about the
  current state, not the history.
· moved — What changed since the last edition? 0-3 items, past tense,
  one fact each. Empty when nothing moved.
· pending — What work is still open, and who owns it? 0-4 items. Start
  an item with the owner when it is not the user ("Platform team: ...").
· next — The single most useful next action. Verb first. Say who does
  it: "You:" for the user, a name for someone else, "Freyja:" for work
  an agent can do (and stage that work in Today). null only when nothing
  is left to do.

Say each fact once. Don't repeat the summary in moved or pending.

This entry is too dense:

  "summary": "PR #812 is held because the staging config points the
  payments client at a placeholder host, and you withdrew your approval.
  Separately, 4 PRs (#820, #818, #805, #790) wait on review."

This one is right:

  "summary": "The payments migration (PR #812) is blocked. Its staging
  config sends traffic to a placeholder host.",
  "moved": ["You withdrew your approval of #812 on Saturday.",
            "PR #820 (retry fix) opened Sunday."],
  "pending": ["Platform team: point the staging config at the real host.",
              "4 PRs wait for reviewers; #790 (cache eviction) is the oldest."],
  "next": "You: request reviewers on #820 and #818."

# Needs you (decisions)

· Only calls the USER must make: an unresolved question that stops
  work, a failed scheduled run that needs a judgment, two definitions
  that conflict, "should I continue X". 0-5 of them, ordered by urgency
  × time pressure.
· Each body gives the evidence in 2-3 sentences and ends with a plain
  recommendation: "Default: ...". Give 1-2 actions with concrete
  intents.
· A decision may rest on a session older than {ACTIVE_WINDOW_DAYS} days
  only when waiting costs more each day (money spent daily, a booking
  that can sell out).
· Before you repeat an ask from the last edition, check it is still
  open (for a PR, `gh pr view`). Repeat an unanswered ask in at most 5
  editions. Word the 5th as the final ask ("skipping it keeps the
  default"), then drop it.

# Today

Stage 1 run per active project (max ~5). Each entry's intent is a
`prompt` intent with a fully self-contained prompt an agent could run
without asking questions, OR `fire_job` when an existing scheduled job
covers it, OR `open_session` when the right move is the user resuming a
conversation.

# Writing

Write for a reader who knows the projects but missed yesterday.

· Short sentences. One idea per sentence. Active voice. Plain words.
· Say what a thing is, not only its number: "PR #820 (retry fix)", not
  "#820". Never list bare numbers; count them and name the one that
  matters.
· Name the owner of every open item.
· Use a number only when it changes what the user does. No event
  counts, token counts, or day counters in project entries.
· No filler, no hedging, no praise ("great progress").
· No internal ids (session ids, run ids) in prose. PR numbers and commit
  hashes are fine when the user needs them to act.

# Write

ALWAYS write today's edition fresh, OVERWRITING any briefing.json /
briefing.md already present for today. There is exactly ONE edition per
day and every fire makes it reflect the current moment. NEVER skip
writing because today's file already exists or "looks current" — a fire
is often a user-triggered rebrief that expects a regenerated edition, and
a fire that writes nothing reads to the user as a broken button. If
nothing material changed since the existing edition, still rewrite it
(refreshed timestamp, same substance is fine).

mkdir -p {home}/briefing/$(date +%F)/ then write BOTH:

1. {home}/briefing/$(date +%F)/briefing.json — EXACTLY this schema:

{BRIEFING_SCHEMA_DOC}

   Valid JSON, no trailing commas, no comments. session_id/job_id must
   be REAL ids you saw in the data — never invent them. Omit an intent
   field (use null) rather than fabricating a target. generated_at_iso
   MUST be the real current time INCLUDING seconds (e.g. via
   `date +%Y-%m-%dT%H:%M:%S%z`) so two rebriefs are always distinguishable.
   Before writing, check every project: its last_active must fall inside
   the last {ACTIVE_WINDOW_DAYS} days or come from the block's first list.
   Drop any project that fails.

2. {home}/briefing/$(date +%F)/briefing.md — the same content as a
   readable memo: hero line; ## Projects, with "### <name> · <state>",
   the summary, then Moved / Pending / Next bullets for each project;
   ## Needs you; ## Today; colophon. This is the Slack/archive rendering.

# Learn

Before finishing, append to your working notes anything that will make
tomorrow's briefing better: clustering choices you made (so tomorrow's
edition keeps project names STABLE), sources that were empty or noisy,
calibration about what the user engaged with. Keep project names
consistent day to day — renaming projects daily destroys the user's
mental model. Keep the notes short; collapse old run logs into a few
durable facts.

Notes never put a project on the page. Don't keep a fixed roster of
projects, and don't keep day counters for work the user hasn't touched.
Older notes may contain both ("stable project clusters", "N days
unread", "undispatched since"). Those rules are retired: this prompt
replaces them.

NEVER write a permanent suppression rule into your notes — no "ignore
project X", "skip EMA/work sessions", "these are complete one-off
investigations, don't surface them", or similar. Such a rule is wrong
the moment that work resumes, and it silently buries a freshly-active
project the user cares about. Editorial judgment about whether something
is worth surfacing is made FRESH each fire against the recency block, not
frozen in notes. (If a class of session should be filtered permanently,
that belongs in code — `_SKIP_PREFIXES` in render_recency_block — not in
notes.) Notes are for STABILITY — names, clustering, voice — never for
vetoing what the recency block surfaces.
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
                    await _maybe_refresh_prompt(scheduler, j)
                    _write_pointer(j.id)
                    return j.id
        except Exception:  # noqa: BLE001
            logger.debug("briefer job lookup failed", exc_info=True)
            return None
        return await _create_briefer_job(state, scheduler)
    finally:
        lock.release()


async def _maybe_refresh_prompt(scheduler: Any, job: Any) -> None:
    """Keep the live briefer job's snapshotted prompt in sync with the
    code. The prompt is frozen onto the JobRecord at creation, so a code
    change to ``briefer_prompt()`` would otherwise never reach the
    already-existing job. Patch only when it actually differs (so this is
    a no-op on every boot after the upgrade lands)."""
    try:
        desired = briefer_prompt()
        if getattr(job, "prompt", "") == desired:
            return
        from bridge.scheduler.models import JobPatch

        await scheduler.update_job(job.id, JobPatch(prompt=desired))
        logger.info("briefer prompt refreshed to current code version (%s)", job.id)
    except Exception:  # noqa: BLE001
        logger.debug("briefer prompt refresh failed", exc_info=True)


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
