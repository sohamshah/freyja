"""Per-skill value (V) score computation.

Reads ``.events.jsonl`` for a single skill, projects the outcome events
through the category weights in ``categories.py``, and produces a single
scalar V plus a count breakdown per category for display.

This is the read-time consolidation surface. There is no "verify the
skill" step in MVP — outcomes accumulate from real loads in real
sessions and the V score reflects that lived history. Skills with
good outcomes float up the system-prompt ranking; skills with bad
outcomes sink. No curator, no decay model, no replay validation yet.

Caching
───────
The per-skill rollup is written to ``~/.freyja/skills/.value/<safe>.json``.
We recompute when:

  · the rollup file doesn't exist
  · the events file mtime is newer than the rollup file mtime

Recompute walks the entire events file filtering for this skill. With
the typical event volume (a few hundred lines/skill/year) this is
microseconds. We never need an in-memory cache.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, asdict, field
from typing import Any

from bridge.knowledge.learning import categories, events
from bridge.knowledge.learning.paths import (
    ensure_loop_dirs,
    value_dir,
    value_path_for,
)


@dataclass
class ValueRollup:
    """Per-skill summary derived from the event log.

    ``v_score`` is the headline number — positive = healthy, negative =
    problematic, magnitude reflects strength of signal × volume.

    ``confidence`` saturates over the first 30 observations so a
    brand-new skill with one ``clean`` outcome doesn't outrank a
    verified skill with 23 outcomes and a strong positive distribution.

    Counts are kept in a flat dict so display can render in any order
    + new categories never break the rollup file format.
    """

    skill: str
    computed_at: int = 0
    load_count: int = 0
    outcome_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    v_raw: float = 0.0
    v_score: float = 0.0
    confidence: float = 0.0
    last_outcome_at: int = 0
    last_load_at: int = 0
    last_positive_at: int = 0
    last_negative_at: int = 0
    archived: bool = False

    def has_signal(self) -> bool:
        """True when there's any event data backing this rollup. False on
        a brand-new skill with no loads yet — caller decides whether to
        show V=0 or 'no observations yet' in the listing."""
        return self.outcome_count > 0 or self.load_count > 0

    def to_json(self, recent_outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Serialize for IPC / renderer consumption. camelCase mirror of
        the on-disk dataclass for the TS side. Optionally embed the last
        N outcome events so the operator can see evidence text without a
        round-trip."""
        windowed = sum(self.counts.values())
        return {
            "skill": self.skill,
            "score": round(self.v_score, 4),
            "headline": self.headline(),
            "loadCount": self.load_count,
            "windowedLoadCount": windowed,
            "counts": dict(self.counts),
            "recentOutcomes": [
                {
                    "ts": int(o.get("ts") or 0),
                    "category": str(o.get("category") or ""),
                    "evidence": str(o.get("evidence") or ""),
                    "sessionId": str(o.get("session_id") or "") or None,
                    "turnId": str(o.get("turn_id") or "") or None,
                }
                for o in (recent_outcomes or [])
            ],
            "computedAt": self.computed_at,
            "confidence": round(self.confidence, 4),
            "archived": self.archived,
        }

    def headline(self) -> str:
        """Compact one-line summary suitable for the system-prompt skill
        listing.

        Examples:
            "V=+1.34 · 23 loads · 18 cited, 4 clean, 1 correction"
            "V=+1.50 · last 30 of 130 loads · 30 cited"   (H14: truncation surfaced)
            "limited data — 2 loads, 0 outcomes"
        """
        if self.outcome_count < 2:
            if self.load_count == 0:
                return "no observations yet"
            return f"limited data — {self.load_count} loads, {self.outcome_count} outcomes"
        # H14: when outcomes hit the rolling window cap and there are
        # historically more loads than outcomes in the window, the
        # headline should make clear that the displayed counts are the
        # last-N window rather than lifetime. Otherwise a skill with 100
        # corrections + 30 recent cited reads as "130 loads · 30 cited"
        # and the corrections vanish from view.
        truncated = (
            self.outcome_count >= _V_WINDOW
            and self.load_count > self.outcome_count
        )
        if truncated:
            loads_frag = f"last {self.outcome_count} of {self.load_count} loads"
        else:
            loads_frag = f"{self.load_count} loads"
        head = f"V={_format_v(self.v_score)} · {loads_frag}"
        breakdown = ", ".join(
            f"{count} {name}"
            for name, count in sorted(
                self.counts.items(),
                key=lambda kv: -kv[1],
            )
            if count > 0
        )
        if breakdown:
            head += f" · {breakdown}"
        return head


def _format_v(v: float) -> str:
    """Sign + 2-decimal V for display. Lines up so the model can scan."""
    if v > 0:
        return f"+{v:.2f}"
    if v < 0:
        return f"{v:.2f}"
    return "+0.00"


# Knob definitions live in constants.py. Local alias for the existing
# usages so we don't touch every line in the file.
from bridge.knowledge.learning.constants import (
    VALUE_ROLLING_WINDOW as _V_WINDOW,
)


def _compute_from_events_for(skill_name: str) -> ValueRollup:
    """Walk the event log, compute the rollup for one skill.

    H13: ``computed_at`` is pinned to the events-file mtime captured
    BEFORE the read — not wall-clock time. Wall-clock would race against
    a concurrent append (T1 read, T2 append, T3 finish-persist with
    computed_at=T3 > append-mtime → next reader sees cache as fresh and
    misses the appended event forever). With mtime-equal-not-greater
    cache freshness, mtime advances past computed_at on any new event
    and the next compute correctly recomputes.

    M14: outcome events with categories outside the known taxonomy are
    dropped before tallying — they would otherwise inflate
    ``outcome_count`` while contributing zero weight, biasing V toward
    0 for skills with malformed logs.
    """
    # Snapshot mtime BEFORE the scan. Any append after this point bumps
    # the mtime and forces the next compute_rollup to re-walk.
    events_mtime_at_read = events.latest_ts()
    rollup = ValueRollup(skill=skill_name)
    outcome_events: list[dict[str, Any]] = []
    for ev in events.iter_events(skill_name=skill_name):
        kind = ev.get("event")
        ts = int(ev.get("ts") or 0)
        if kind == events.EVENT_LOADED:
            rollup.load_count += 1
            if ts > rollup.last_load_at:
                rollup.last_load_at = ts
        elif kind == events.EVENT_OUTCOME:
            # M14: drop events whose category isn't in the known
            # taxonomy. Otherwise they'd land in the window and dilute V.
            cat = str(ev.get("category") or "")
            if categories.get(cat) is None:
                continue
            outcome_events.append(ev)
        elif kind == events.EVENT_ARCHIVED:
            rollup.archived = True
        elif kind == events.EVENT_RESTORED:
            rollup.archived = False

    # Trim outcomes to the rolling window — sorted by ts, keep last N.
    outcome_events.sort(key=lambda e: int(e.get("ts") or 0))
    window = outcome_events[-_V_WINDOW:]
    rollup.outcome_count = len(window)

    weighted_sum = 0.0
    for ev in window:
        name = str(ev.get("category") or "")
        ts = int(ev.get("ts") or 0)
        rollup.counts[name] = rollup.counts.get(name, 0) + 1
        w = categories.weight_for(name)
        weighted_sum += w
        if ts > rollup.last_outcome_at:
            rollup.last_outcome_at = ts
        if w > 0 and ts > rollup.last_positive_at:
            rollup.last_positive_at = ts
        if w < 0 and ts > rollup.last_negative_at:
            rollup.last_negative_at = ts

    if rollup.outcome_count > 0:
        # Per-outcome average × confidence ramp. Confidence ramps from
        # 0 → 1 over the first ``_V_WINDOW`` outcomes — log-shaped so a
        # skill with 5 outcomes is already meaningfully scored, but a
        # skill with 1 outcome is heavily damped.
        rollup.v_raw = weighted_sum / rollup.outcome_count
        confidence = math.log(rollup.outcome_count + 1) / math.log(_V_WINDOW + 1)
        rollup.confidence = min(1.0, max(0.0, confidence))
        rollup.v_score = rollup.v_raw * rollup.confidence
    # H13: pin computed_at to the pre-read mtime so cache compare is
    # mtime-equal, not wall-clock-greater. Fall back to wall clock when
    # the events file doesn't exist yet so a brand-new install still
    # produces a sortable timestamp.
    rollup.computed_at = events_mtime_at_read or int(time.time() * 1000)
    return rollup


def _read_cached(skill_name: str) -> ValueRollup | None:
    """Read a previously-written rollup from disk. Returns None if absent
    or unparseable — caller will recompute."""
    path = value_path_for(skill_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        rollup = ValueRollup(skill=skill_name)
        for field_name in (
            "computed_at", "load_count", "outcome_count",
            "v_raw", "v_score", "confidence",
            "last_outcome_at", "last_load_at",
            "last_positive_at", "last_negative_at",
        ):
            if field_name in data:
                setattr(rollup, field_name, type(getattr(rollup, field_name))(data[field_name]))
        rollup.counts = dict(data.get("counts") or {})
        rollup.archived = bool(data.get("archived"))
        return rollup
    except (TypeError, ValueError):
        return None


def _persist_rollup(rollup: ValueRollup) -> None:
    """Write the rollup atomically. Best-effort — value scoring must
    never break the live read path that the system-prompt builder is
    waiting on."""
    ensure_loop_dirs()
    path = value_path_for(rollup.skill)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".value_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(asdict(rollup), fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


# M19: in-process LRU keyed by (skill_name, events_mtime). Avoids the
# fsync churn of re-persisting a stale-cache compute on every call when
# the events file hasn't changed but the disk cache is older than the
# events mtime (a steady-state read path that previously paid an mkstemp
# + fsync per skill per system-prompt build).
#
# M20: a single global events.jsonl mtime invalidates every per-skill
# entry — even reading another skill's event evicts ours. We accept the
# over-invalidation tradeoff (the recompute is microseconds) and rely on
# this in-memory cache to absorb the read-after-other-skill-write case
# without disk I/O.
_ROLLUP_CACHE: dict[str, tuple[int, ValueRollup]] = {}
from bridge.knowledge.learning.constants import (
    VALUE_ROLLUP_CACHE_CAP as _ROLLUP_CACHE_CAP,
)


def _cache_get(skill_name: str, events_mtime: int) -> ValueRollup | None:
    entry = _ROLLUP_CACHE.get(skill_name)
    if entry is None:
        return None
    cached_mtime, rollup = entry
    if cached_mtime == events_mtime:
        return rollup
    return None


def _cache_put(skill_name: str, events_mtime: int, rollup: ValueRollup) -> None:
    if len(_ROLLUP_CACHE) >= _ROLLUP_CACHE_CAP:
        # Cheap eviction: drop the first inserted key. Steady-state usage
        # is a small fixed set of skill names (the user's library), so we
        # rarely hit the cap in practice — when we do, FIFO is fine.
        try:
            first_key = next(iter(_ROLLUP_CACHE))
            _ROLLUP_CACHE.pop(first_key, None)
        except StopIteration:
            pass
    _ROLLUP_CACHE[skill_name] = (events_mtime, rollup)


def compute_rollup(skill_name: str) -> ValueRollup:
    """Public entrypoint. Cached compute: if the on-disk rollup is at
    least as fresh as the events file, returns the cached value;
    otherwise walks the log + rewrites the cache.

    Cheap to call on every system-prompt build — the typical case is
    a no-op stat() comparison or an in-memory cache hit.

    H13 change: cache freshness is mtime-equal, not wall-clock-greater.
    M19 change: in-memory LRU avoids re-persisting unchanged rollups.
    """
    events_mtime = events.latest_ts()

    # 1. In-memory LRU hit — fastest path, no disk I/O at all.
    memo = _cache_get(skill_name, events_mtime)
    if memo is not None:
        return memo

    # 2. On-disk cache hit — read once, populate the LRU.
    cached = _read_cached(skill_name)
    if cached is not None and cached.computed_at == events_mtime:
        _cache_put(skill_name, events_mtime, cached)
        return cached

    # 3. Miss — recompute, persist, populate LRU.
    rollup = _compute_from_events_for(skill_name)
    # Only persist when the disk copy is actually stale, to keep idle
    # builds from re-fsyncing identical content.
    if cached is None or cached.computed_at != rollup.computed_at:
        _persist_rollup(rollup)
    _cache_put(skill_name, rollup.computed_at, rollup)
    return rollup


def compute_all(skill_names: list[str]) -> dict[str, ValueRollup]:
    """Compute rollups for many skills. Used by the system-prompt
    builder; caching means this is fast on the steady-state path."""
    return {name: compute_rollup(name) for name in skill_names}
