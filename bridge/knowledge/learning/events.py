"""Append-only event log for the skill-learning loop.

One JSONL file (``~/.freyja/skills/.events.jsonl``) shared across every
skill in the library. Per-skill stats are derived on read by
``value_score.compute_rollup``.

Why append-only and not per-skill sidecars
──────────────────────────────────────────
Hermes' ``.usage.json`` is a single JSON object mutated read-modify-write
with fcntl/msvcrt file-locking + tempfile + os.replace. That's correct
but heavyweight for what we need. With append-only JSONL:

  · Concurrent writers from any process get atomic single-line appends
    on POSIX (writes ≤ PIPE_BUF are atomic). No locks needed.
  · Crashes mid-write leave at most one truncated last line that the
    reader can skip without corrupting anything.
  · Roll-forward debugging is a one-pass scan: "what was the V of this
    skill on date X" is a simple filter on ts.
  · No schema migrations — old events stay readable forever because each
    line is self-describing.

The cost: per-skill rollups are not free on read. We mitigate via
``value_score`` mtime caching, recomputing only when the events file
changed.

Event taxonomy
──────────────
Every line is a JSON object with:

  · ``ts``        — int ms epoch
  · ``event``     — one of EVENT_* below
  · ``skill``     — skill name string (empty for events not bound to a skill)
  · plus event-specific fields

Two adjacent timestamps with the same skill + event type are not
deduplicated by the writer. The reader (value_score) handles dup-by-
event-id where it matters.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from bridge.knowledge.learning.paths import events_path, ensure_loop_dirs

# Free-text truncation knobs — definition lives in constants.py.
from bridge.knowledge.learning.constants import (
    EVENT_FREE_TEXT_MAX_CHARS,
    EVENT_TRUNCATED_FIELDS,
)

# Try to import fcntl for POSIX file locking. On Windows fcntl isn't
# available and we fall back to the single-write atomicity that O_APPEND
# gives us — which is "good enough" for the small payloads we emit.
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    fcntl = None  # type: ignore[assignment]


def _truncate_free_text_fields(event: dict[str, Any]) -> None:
    """Cap long free-text fields in-place before serialization. M23 fix.

    Two reasons: (1) macOS PIPE_BUF is 512B, so a single long evidence
    quote can break the POSIX single-write-is-atomic invariant the
    file-append discipline relies on; (2) operators tend to dump huge
    paste-quotes into evidence fields — the operator value lives in
    the first sentence, the rest just bloats the log. Truncation adds
    an ellipsis marker so a reader can tell a value was cut.
    """
    for key in EVENT_TRUNCATED_FIELDS:
        val = event.get(key)
        if isinstance(val, str) and len(val) > EVENT_FREE_TEXT_MAX_CHARS:
            event[key] = val[: EVENT_FREE_TEXT_MAX_CHARS - 1] + "…"

# Event types. Strings (not enums) so old logs stay forward-compatible —
# we never want to fail a read because a future writer added a new event
# kind. Readers ignore unknown event types.

EVENT_DRAFTER_TRIP = "drafter_trip"        # cadence tripped, drafter call about to start
EVENT_DRAFTER_DECISION = "drafter_decision"  # drafter returned: result + rationale
EVENT_DRAFTED = "drafted"                 # candidate written to .candidates/
EVENT_DRAFTER_SKIP = "drafter_skip"        # cadence tripped, drafter ran, decided skip
EVENT_GUARD_VERDICT = "guard_verdict"     # Skills Guard scan result attached
EVENT_PROMOTED = "promoted"                # candidate accepted, written to SKILL.md
EVENT_DISCARDED = "discarded"              # candidate rejected (operator or auto)
EVENT_LOADED = "loaded"                    # skill was loaded into session context
EVENT_OUTCOME = "outcome"                  # classifier verdict for a prior load
EVENT_ARCHIVED = "archived"                # skill moved to .archived/
EVENT_RESTORED = "restored"                # skill moved out of .archived/
EVENT_PATCHED = "patched"                  # Phase 4 patch applied
EVENT_DECAY_CHECK = "decay_check"          # Phase 4 decay score computed


def _now_ms() -> int:
    return int(time.time() * 1000)


def append(event: dict[str, Any]) -> None:
    """Append a single event to the log. Best-effort.

    Failures (disk full, perms, FS read-only) are silently swallowed —
    losing telemetry must never break the live skill load path that the
    operator's turn is depending on. Caller is responsible for putting
    ``ts`` and ``skill`` on the dict; ``ts`` is filled if missing.

    M23: free-text fields are truncated to ``EVENT_FREE_TEXT_MAX_CHARS``
    before serialization, and the write is bracketed by an ``fcntl``
    exclusive flock on POSIX. The O_APPEND atomicity only holds for
    writes ≤ PIPE_BUF (4kB Linux, 512B macOS); a long evidence quote
    plus a long summary can blow past that and interleave with a
    concurrent writer. The flock keeps the write contiguous regardless.
    """
    event.setdefault("ts", _now_ms())
    _truncate_free_text_fields(event)
    ensure_loop_dirs()
    try:
        path = events_path()
        line = json.dumps(event, ensure_ascii=False, default=str)
        # Open with mode 'a' which uses O_APPEND. We additionally hold an
        # advisory exclusive lock across the two writes so a partner
        # process can't interleave between the JSON line and the
        # trailing newline. fcntl is POSIX-only; on Windows we fall
        # through to plain O_APPEND, which is still single-write atomic
        # for our small payloads.
        with path.open("a", encoding="utf-8") as fp:
            if fcntl is not None:
                try:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                except OSError:
                    # Lock unavailable on this filesystem (NFS without
                    # rpc.lockd, exotic FUSE, etc.) — best-effort fall
                    # through to bare write. The dropped line is rare
                    # enough to be acceptable telemetry loss.
                    pass
            try:
                fp.write(line)
                fp.write("\n")
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass


def append_loaded(skill_name: str, session_id: str, *, turn_id: str = "", extra: dict[str, Any] | None = None) -> None:
    """Convenience writer for skill-load events. Pairs with later outcome
    classification (the outcome carries the same ``load_ts`` so they can
    be joined)."""
    payload: dict[str, Any] = {
        "event": EVENT_LOADED,
        "skill": skill_name,
        "session_id": session_id,
    }
    if turn_id:
        payload["turn_id"] = turn_id
    if extra:
        payload.update(extra)
    append(payload)


def append_drafter_trip(session_id: str, *, turn_id: str | None = None) -> None:
    """Emit a ``drafter_trip`` event: cadence fired, drafter is about to
    spawn. Pairs with a subsequent ``drafter_decision`` so the audit log
    can prove the drafter actually ran (and didn't silently fail to
    start). M-fix: previously there was no telemetry between cadence
    and candidate emit, so "drafter never ran" and "drafter ran and
    skipped" looked identical from the outside."""
    payload: dict[str, Any] = {
        "event": EVENT_DRAFTER_TRIP,
        "skill": "",
        "session_id": session_id,
    }
    if turn_id:
        payload["turn_id"] = turn_id
    append(payload)


def append_drafter_decision(
    session_id: str,
    *,
    turn_id: str | None,
    result: str,
    candidate_id: str = "",
    rationale: str = "",
) -> None:
    """Emit a ``drafter_decision`` event after the drafter returns.

    ``result`` is one of ``"candidate"`` (a candidate was emitted),
    ``"skip"`` (drafter declined to produce anything), or ``"error"``
    (the drafter call raised). The rationale field carries either the
    drafter's free-text "why I skipped" or the exception class on
    error. ``candidate_id`` is populated only on the candidate path so
    the audit log can be joined with the actual candidate file.
    """
    payload: dict[str, Any] = {
        "event": EVENT_DRAFTER_DECISION,
        "skill": "",
        "session_id": session_id,
        "result": result,
    }
    if turn_id:
        payload["turn_id"] = turn_id
    if candidate_id:
        payload["candidate_id"] = candidate_id
    if rationale:
        payload["rationale"] = rationale
    append(payload)


def append_outcome(
    skill_name: str,
    session_id: str,
    *,
    category: str,
    load_ts: int,
    evidence: str = "",
) -> None:
    """Convenience writer for the outcome classifier.

    M13: previously accepted a ``secondary`` label; the V rollup never
    consumed it so it was dropped from the classifier schema/prompt and
    here, to keep the on-disk event shape minimal.
    """
    payload: dict[str, Any] = {
        "event": EVENT_OUTCOME,
        "skill": skill_name,
        "session_id": session_id,
        "category": category,
        "load_ts": load_ts,
    }
    if evidence:
        payload["evidence"] = evidence
    append(payload)


def iter_events(skill_name: str | None = None) -> Iterator[dict[str, Any]]:
    """Stream events from the log.

    With ``skill_name`` set, yields only matching events — used by
    value_score to compute a single-skill rollup without materializing
    the whole log.

    Skips lines that fail to parse so a partial write doesn't break the
    reader. Skips events without a ``skill`` field when filtering by
    name.
    """
    path = events_path()
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if skill_name is not None and ev.get("skill") != skill_name:
                    continue
                yield ev
    except OSError:
        return


def latest_ts() -> int:
    """Mtime of the events file in ms. Used by value_score as the cache
    key — rollups only recompute when this advances."""
    path = events_path()
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0
