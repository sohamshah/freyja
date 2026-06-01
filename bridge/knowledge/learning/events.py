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

# Event types. Strings (not enums) so old logs stay forward-compatible —
# we never want to fail a read because a future writer added a new event
# kind. Readers ignore unknown event types.

EVENT_DRAFTED = "drafted"                 # candidate written to .candidates/
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
    """
    event.setdefault("ts", _now_ms())
    ensure_loop_dirs()
    try:
        path = events_path()
        line = json.dumps(event, ensure_ascii=False, default=str)
        # POSIX guarantees atomic append of writes ≤ PIPE_BUF (4kb on Linux,
        # 512b on macOS). Our lines are well under that — typical event is
        # ~200 bytes. We open with mode 'a' which uses O_APPEND under the
        # hood; concurrent writers from any process interleave at line
        # boundaries.
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line)
            fp.write("\n")
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


def append_outcome(
    skill_name: str,
    session_id: str,
    *,
    category: str,
    load_ts: int,
    evidence: str = "",
    secondary: str | None = None,
) -> None:
    """Convenience writer for the outcome classifier."""
    payload: dict[str, Any] = {
        "event": EVENT_OUTCOME,
        "skill": skill_name,
        "session_id": session_id,
        "category": category,
        "load_ts": load_ts,
    }
    if evidence:
        payload["evidence"] = evidence
    if secondary:
        payload["secondary"] = secondary
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
