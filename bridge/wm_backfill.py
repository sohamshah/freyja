"""Offline working-memory backfill — session summaries for idle sessions.

The live pipeline keeps ``working_memory.json`` fresh only for sessions
that are actively running: compaction fires Call B, and the mid-session
trigger (``_trigger_wm_extract``) fires on session_memory mutations. A
session that never compacts and then goes idle — most short sessions, all
pre-Grounded-Memory sessions — has a missing or stale summary forever.

This module closes that gap with a deterministic offline pass:

  1. **Scan** every persisted transcript under ``~/.freyja/sessions/``,
     derive the TRUE session id from the JSON body (filenames are
     sanitized with ``_`` while project dirs sanitize with ``-``, so the
     filename can't be trusted for path resolution).
  2. **Filter** to user-facing, idle, substantial sessions whose working
     memory is missing or older than the transcript's last activity.
  3. **Extract** via the same Call B used everywhere else
     (``SummaryCompaction._extract_working_memory``) with the session's
     ledger ground truth and current WM state as anchors, on a cheap
     dedicated model.
  4. **Apply** through the shared projection (``apply_wm_result`` +
     ``refresh_wm_artifacts_from_ledger``) so offline results land
     byte-identically to live ones.

Every attempt is recorded in ``<project_dir>/wm_backfill.json`` so a
failed or empty extraction isn't retried until the transcript changes
(or a retry window elapses). A cross-process flock around each pass
prevents the Electron bridge, gateway, and LaunchAgent daemon from
duplicating spend when several are alive.

The hourly loop (``backfill_loop``) is started by both bridge mains.
Disable with ``FREYJA_WM_BACKFILL=0``; override the extraction model
with ``FREYJA_WM_BACKFILL_MODEL``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("freyja.wm_backfill")

# Sessions are eligible only after this much idle time. Live sessions
# are kept fresh by the in-process triggers, and writing the WM doc from
# a second process would race the live instance's whole-doc rewrites.
IDLE_MIN_SECONDS = 30 * 60

# A failed/empty attempt is not retried until either the transcript
# changes or this window elapses.
RETRY_AFTER_SECONDS = 6 * 3600

# Smallest session worth a summary: at least this many message-bearing
# entries AND this much formatted conversation text.
MIN_MESSAGES = 2
MIN_CONVERSATION_CHARS = 600

# Per-pass cap — bounds spend per hour. The loop catches up over a day.
DEFAULT_PASS_LIMIT = 8

# Cheap, structured-output-capable default. Sessions are summaries, not
# prose art — haiku-class is plenty.
DEFAULT_MODEL = "claude-haiku-4-5"

# Session-id prefixes that never get standalone summaries:
#   sub_*        sub-agent workers (their learnings roll up to the parent)
#   comp_*       compaction worker sessions
#   scheduler*   scheduler-owned sessions (jobs carry their own memory)
#   session-boot the empty placeholder created at app start
SKIP_PREFIXES = ("sub_", "comp_", "scheduler")
SKIP_IDS = ("session-boot",)


def _sessions_dir() -> Path:
    from bridge.transcript_persistence import SESSIONS_DIR

    return SESSIONS_DIR


def _marker_path(project_dir: Path) -> Path:
    return project_dir / "wm_backfill.json"


def _read_marker(project_dir: Path) -> dict[str, Any] | None:
    p = _marker_path(project_dir)
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_marker(project_dir: Path, marker: dict[str, Any]) -> None:
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        tmp = _marker_path(project_dir).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
        tmp.replace(_marker_path(project_dir))
    except OSError:
        logger.debug("wm_backfill marker write failed", exc_info=True)


# ─── Scan ──────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """One session the scan deems worth (re)summarizing."""

    session_id: str            # TRUE id from the transcript body
    transcript_path: Path
    project_dir: Path
    last_activity: float       # epoch seconds, from the transcript body
    message_count: int
    reason: str                # "missing" | "stale" | "empty_overview"


@dataclass
class ScanReport:
    candidates: list[Candidate] = field(default_factory=list)
    skipped_prefix: int = 0
    skipped_small: int = 0
    skipped_active: int = 0
    skipped_fresh: int = 0
    skipped_marker: int = 0
    errors: int = 0


def _count_message_entries(transcript_d: dict[str, Any]) -> int:
    entries = transcript_d.get("entries")
    if not isinstance(entries, list):
        return 0
    return sum(1 for e in entries if isinstance(e, dict) and e.get("message"))


def scan_sessions(*, now: float | None = None) -> ScanReport:
    """Walk all persisted transcripts and return the stale set.

    Cheap by design: one ``json.loads`` per transcript (they're compact —
    compaction keeps them bounded), one stat + small read per working
    memory file. No LLM calls.
    """
    from bridge.project_paths import project_output_dir

    now = now or time.time()
    report = ScanReport()
    sess_dir = _sessions_dir()
    if not sess_dir.exists():
        return report

    for tpath in sorted(sess_dir.glob("*.transcript.json")):
        try:
            data = json.loads(tpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            report.errors += 1
            continue
        if not isinstance(data, dict) or data.get("version") != 1:
            report.errors += 1
            continue

        session_id = str(data.get("session_id") or "")
        if not session_id or session_id in SKIP_IDS or session_id.startswith(SKIP_PREFIXES):
            report.skipped_prefix += 1
            continue

        transcript_d = data.get("transcript") or {}
        n_msgs = _count_message_entries(transcript_d)
        if n_msgs < MIN_MESSAGES:
            report.skipped_small += 1
            continue

        last_activity = float(data.get("last_activity") or 0.0)
        if now - last_activity < IDLE_MIN_SECONDS:
            report.skipped_active += 1
            continue

        project_dir = project_output_dir(session_id)

        # Freshness: does the existing WM cover the transcript's tail?
        reason: str | None = None
        wm_path = project_dir / "working_memory.json"
        if not wm_path.exists():
            reason = "missing"
        else:
            try:
                doc = json.loads(wm_path.read_text(encoding="utf-8"))
                ov = doc.get("overview") if isinstance(doc, dict) else None
                ov_summary = (ov or {}).get("summary") if isinstance(ov, dict) else None
                ov_updated_ms = (ov or {}).get("updatedAt") if isinstance(ov, dict) else None
                if not (isinstance(ov_summary, str) and ov_summary.strip()):
                    reason = "empty_overview"
                elif (
                    isinstance(ov_updated_ms, (int, float))
                    and (ov_updated_ms / 1000.0) < (last_activity - 60)
                ):
                    reason = "stale"
            except Exception:  # noqa: BLE001
                reason = "missing"
        if reason is None:
            report.skipped_fresh += 1
            continue

        # Attempt marker: don't hammer a session whose extraction already
        # ran (or failed) against this exact transcript state.
        marker = _read_marker(project_dir)
        if marker is not None:
            same_state = (
                float(marker.get("transcript_last_activity") or 0.0) == last_activity
            )
            attempted_at = float(marker.get("attempted_at") or 0.0)
            status = str(marker.get("status") or "")
            if same_state and status == "ok" and reason == "stale":
                # The WM doc carries a summary but its updatedAt trails the
                # transcript slightly — the marker proves we already
                # summarized this exact transcript state, so the lag is
                # clock skew, not missed content. Trust the marker.
                #
                # For reason == missing/empty_overview we deliberately do
                # NOT trust an ok marker: the file itself is direct
                # evidence nothing usable was persisted (vacuous result,
                # swallowed save failure, external deletion) — fall
                # through to the retry-window check below instead of
                # stranding the session forever.
                report.skipped_marker += 1
                continue
            if same_state and (
                now - attempted_at < RETRY_AFTER_SECONDS
            ):
                # failed/empty — and ok-markers contradicted by the WM
                # file — retry only after the window.
                report.skipped_marker += 1
                continue

        report.candidates.append(
            Candidate(
                session_id=session_id,
                transcript_path=tpath,
                project_dir=project_dir,
                last_activity=last_activity,
                message_count=n_msgs,
                reason=reason,
            )
        )

    # Oldest-idle first — they've waited longest and are least likely to
    # go live mid-extraction.
    report.candidates.sort(key=lambda c: c.last_activity)
    return report


# ─── Extract + apply (one session) ─────────────────────────────────────


def backfill_session(
    candidate: Candidate,
    provider: Any,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run Call B against one idle session's transcript and persist the
    result through the shared projection. Synchronous — call from a
    worker thread in async contexts (the extraction uses ``asyncio.run``
    internally).

    Returns a result dict: ``{session_id, status, reason, overview,
    upserts, artifact_notes, input_tokens, output_tokens, duration_ms,
    error?}`` with status ∈ ok | empty | failed | dry_run.
    """
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from bridge.session_ledger import SessionLedger
    from bridge.working_memory import (
        WorkingMemory,
        apply_wm_result,
        refresh_wm_artifacts_from_ledger,
    )

    out: dict[str, Any] = {
        "session_id": candidate.session_id,
        "reason": candidate.reason,
        "status": "failed",
        "overview": 0,
        "upserts": 0,
        "artifact_notes": 0,
    }

    try:
        data = json.loads(candidate.transcript_path.read_text(encoding="utf-8"))
        transcript = TranscriptManager.from_dict(data.get("transcript") or {})
        messages = transcript.get_messages()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"transcript load failed: {exc}"
        # Marker required: without it this permanently-broken transcript
        # becomes a candidate again on EVERY pass (it sorts oldest-first,
        # so >=8 broken legacy transcripts would starve the whole pass
        # budget forever).
        if not dry_run:
            _write_marker(candidate.project_dir, {
                "attempted_at": time.time(),
                "status": "failed",
                "transcript_last_activity": candidate.last_activity,
                "error": str(exc)[:500],
            })
        return out
    if not messages:
        out["status"] = "empty"
        out["error"] = "no messages"
        if not dry_run:
            _write_marker(candidate.project_dir, {
                "attempted_at": time.time(),
                "status": "empty",
                "transcript_last_activity": candidate.last_activity,
            })
        return out

    # Fire-time idle re-check: the scan's idle gate can be minutes stale
    # by the time this candidate reaches the front of a long pass (each
    # earlier candidate burns an LLM call). ``data`` was read just now,
    # so its last_activity is current. If the session went live in
    # between, skip — the live triggers own it, and a cross-process
    # whole-doc WM write would clobber the live instance's memory.
    try:
        last_act = float(data.get("last_activity") or 0.0)
        if time.time() - last_act < IDLE_MIN_SECONDS:
            out["status"] = "empty"
            out["error"] = "session went live since scan; deferred"
            return out  # no marker — retry next pass once idle again
    except Exception:  # noqa: BLE001
        pass

    compactor = SummaryCompaction()
    conversation = compactor._format_conversation(messages)  # noqa: SLF001
    if len(conversation) < MIN_CONVERSATION_CHARS:
        out["status"] = "empty"
        out["error"] = "conversation below minimum size"
        if not dry_run:
            _write_marker(candidate.project_dir, {
                "attempted_at": time.time(),
                "status": "empty",
                "transcript_last_activity": candidate.last_activity,
            })
        return out

    # Ledger ground truth — filter by the session's own creator id, fall
    # back to all rows when the filter matches nothing (creator labels for
    # gateway sessions vary across bridge versions).
    ground_truth: str | None = None
    ledger: Any = None
    try:
        ledger = SessionLedger(
            session_id=candidate.session_id, project_dir=candidate.project_dir
        )
        ledger.ensure()
        rows = ledger.effects(creator_id=candidate.session_id)
        if not rows:
            rows = ledger.effects(creator_id=None)
        lines = [f"- {r['summary']}" for r in rows[:40] if r.get("summary")]
        try:
            for fact in ledger.pinned_facts(creator_id=None)[:10]:
                lines.append(f"- (pinned) {fact}")
        except Exception:  # noqa: BLE001
            pass
        ground_truth = "\n".join(lines) if lines else None
    except Exception:  # noqa: BLE001
        logger.debug("backfill ledger load failed", exc_info=True)

    wm = WorkingMemory(
        session_id=candidate.session_id, project_dir=candidate.project_dir
    )
    wm.ensure()
    wm_state = None
    try:
        wm_state = wm.render()
    except Exception:  # noqa: BLE001
        pass

    if dry_run:
        out["status"] = "dry_run"
        out["conversation_chars"] = len(conversation)
        out["has_ground_truth"] = bool(ground_truth)
        return out

    stats: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        wm_result = compactor._extract_working_memory(  # noqa: SLF001
            conversation,
            provider,
            working_memory_state=wm_state,
            ground_truth=ground_truth,
            stats_out=stats,
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"extraction failed: {exc}"
        _write_marker(candidate.project_dir, {
            "attempted_at": time.time(),
            "status": "failed",
            "transcript_last_activity": candidate.last_activity,
            "error": str(exc)[:500],
        })
        return out

    out["input_tokens"] = int(stats.get("input_tokens", 0) or 0)
    out["output_tokens"] = int(stats.get("output_tokens", 0) or 0)
    out["duration_ms"] = int((time.perf_counter() - started) * 1000)

    if wm_result is None:
        out["status"] = "empty"
        out["error"] = stats.get("error") or "extraction returned nothing"
        _write_marker(candidate.project_dir, {
            "attempted_at": time.time(),
            "status": "empty",
            "transcript_last_activity": candidate.last_activity,
            "error": str(out["error"])[:500],
        })
        return out

    counts = apply_wm_result(wm, wm_result)
    out["overview"] = counts.get("overview", 0)
    out["upserts"] = counts.get("upserts", 0)
    if ledger is not None:
        try:
            out["artifact_notes"] = refresh_wm_artifacts_from_ledger(
                wm, ledger, creator_id=None
            )
        except Exception:  # noqa: BLE001
            pass

    # Marker status must reflect what was actually PERSISTED, not just
    # that the extraction call returned. A vacuous Call B result (empty
    # summary + no applicable entities) applies nothing — recording it
    # as 'ok' would permanently suppress retries for a session whose
    # working memory is still demonstrably missing.
    applied_something = bool(out["overview"] or out["upserts"] or out["artifact_notes"])
    out["status"] = "ok" if applied_something else "empty"
    if out["status"] == "empty":
        out["error"] = "extraction returned a vacuous result; nothing applied"
    _write_marker(candidate.project_dir, {
        "attempted_at": time.time(),
        "status": out["status"],
        "transcript_last_activity": candidate.last_activity,
        "model": getattr(provider, "model_id", "unknown"),
        "overview": out["overview"],
        "upserts": out["upserts"],
    })
    return out


# ─── Pass runner ───────────────────────────────────────────────────────


def _pass_lock_path() -> Path:
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    p = Path(base) / ".locks"
    p.mkdir(parents=True, exist_ok=True)
    return p / "wm-backfill.lock"


def run_backfill_pass(
    *,
    limit: int = DEFAULT_PASS_LIMIT,
    model: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan, then summarize up to ``limit`` stale sessions sequentially.

    Cross-process safe: skips entirely (status=locked) when another
    process holds the pass lock. Returns a report dict suitable for an
    IPC response / system event."""
    from bridge.scheduler.persistence import FileLock

    report: dict[str, Any] = {"status": "ok", "results": []}
    lock = FileLock(_pass_lock_path())
    if not lock.acquire():
        report["status"] = "locked"
        return report
    try:
        scan = scan_sessions()
        report["scan"] = {
            "candidates": len(scan.candidates),
            "skipped_prefix": scan.skipped_prefix,
            "skipped_small": scan.skipped_small,
            "skipped_active": scan.skipped_active,
            "skipped_fresh": scan.skipped_fresh,
            "skipped_marker": scan.skipped_marker,
            "errors": scan.errors,
        }
        todo = scan.candidates[: max(0, int(limit))]
        if not todo:
            return report

        provider: Any = None
        if not dry_run:
            from engine.providers import create_provider

            model_id = (
                model
                or os.environ.get("FREYJA_WM_BACKFILL_MODEL")
                or DEFAULT_MODEL
            )
            try:
                provider = create_provider(model_id, max_tokens=32_000)
            except Exception as exc:  # noqa: BLE001
                report["status"] = "failed"
                report["error"] = f"provider create failed: {exc}"
                return report

        for cand in todo:
            res = backfill_session(cand, provider, dry_run=dry_run)
            report["results"].append(res)
            logger.info(
                "wm backfill %s: %s (reason=%s, overview=%s, upserts=%s)",
                cand.session_id,
                res.get("status"),
                cand.reason,
                res.get("overview"),
                res.get("upserts"),
            )
        return report
    finally:
        lock.release()


# ─── Background loop ───────────────────────────────────────────────────


def backfill_enabled() -> bool:
    return os.environ.get("FREYJA_WM_BACKFILL", "1").lower() not in (
        "0", "false", "no", "off",
    )


async def backfill_loop(
    *,
    interval_seconds: float = 3600.0,
    initial_delay_seconds: float = 120.0,
    limit: int = DEFAULT_PASS_LIMIT,
) -> None:
    """Hourly offline pass, started by both bridge mains. The initial
    delay keeps app boot snappy and gives live sessions time to register
    before the first idle scan."""
    if not backfill_enabled():
        logger.info("wm backfill disabled via FREYJA_WM_BACKFILL")
        return
    try:
        await asyncio.sleep(initial_delay_seconds)
    except asyncio.CancelledError:
        return
    while True:
        try:
            report = await asyncio.to_thread(run_backfill_pass, limit=limit)
            n_done = sum(
                1 for r in report.get("results", []) if r.get("status") == "ok"
            )
            if n_done or report.get("scan", {}).get("candidates"):
                logger.info(
                    "wm backfill pass: %d summarized, scan=%s, status=%s",
                    n_done,
                    report.get("scan"),
                    report.get("status"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("wm backfill pass crashed")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
