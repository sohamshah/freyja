"""Compaction telemetry collection.

Persists every pressure-band crossing and every actual compaction event
to a JSONL file under ~/.freyja/telemetry/. The renderer reads this
file via IPC for the cross-session metrics dashboard.

Schema (one JSON object per line; ``ts`` on every row, filled in here
when the writer leaves it out):

  type: 'pressure_signal'
    session_id, agent_type, parent_session_id, band, pressure_pct,
    used_tokens, effective_window, model

  type: 'compaction_event'
    session_id, agent_type, parent_session_id, model,
    subtype,                           # 'compaction_start' | 'compaction_complete'
                                       # | 'compaction_skipped' | 'compaction_failed'
                                       # | 'context_pruning' | 'media_pruning'
                                       # | 'thrash_skip'
    trigger,                           # 'pre_request' | 'overflow_cascade'
                                       # | 'overflow_cascade_image_elision'
                                       # | 'agent_summarize_context' | 'manual'
                                       # | 'restore' | 'provider_error'
                                       # | 'thrash_detector' | null
    mechanism,                         # the event's strategy ('llm_summary',
                                       # 'recent_computer_screenshots', ...), else
                                       # 'summary' | 'summary_iterative' | 'tool_halve'
                                       # | 'image_prune' | 'tool_results_only'
                                       # | 'exploration_only' | 'thrash_skip' | subtype
    tokens_before, tokens_after,
    effective_window,                  # context_window - max_tokens_per_turn
    pressure_pct_before, pressure_pct_after,
    trigger_tokens, trigger_pressure_pct,
                                       # the reading that set a pre-request or
                                       # overflow compaction off
    omitted_images,                    # an image prune's count of the images
                                       # that left the context
    scope, reason,                     # the agent's scope and rationale, the
                                       # restore reason, or a skip's error
    resumed_from_previous, summary_tokens,
    summarizer_input_tokens, summarizer_output_tokens,
    summarizer_duration_ms, summarizer_cost_usd,
    summary_excerpt, summary_text,     # first 240 chars; the full summary
    ineffective_run                    # thrash_skip: compactions in a row that
                                       # saved < 10%

    _mirror_compaction_event (the runner's events, manual and restore
    compactions) and append_subagent_compaction (a sub-agent's) write all
    but ineffective_run: null, or 0 for a count, where the event carried
    none. _emit_summarize_event (the agent's summarize_context) also leaves
    out trigger_tokens, trigger_pressure_pct and omitted_images. The
    runner's thrash detector writes session_id, subtype, mechanism, trigger,
    tokens_before, tokens_after and ineffective_run.

  type: 'llm_call_metric'
    session_id, turn_id, agent_type, parent_session_id, model,
    input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens,
    cost_usd, duration_ms,
    call_kind,                         # 'main' | 'summarizer'
                                       # | 'working_memory_extraction'
                                       # | 'cache_keepalive'
    iterative_summarizer               # summarizer calls; else null

  Other rows: 'summarize_context_call' (each summarize_context decision),
  'tool_call_metric', 'forgetting_detected', 'profile_invocation' and
  'profile_completion'. docs/COMPACTION-FLOW.md has the compaction rows
  field by field.

The JSONL file is the durable record. Live events are also emitted to
stdout for the renderer to consume in real time.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


TELEMETRY_DIR = Path.home() / ".freyja" / "telemetry"
TELEMETRY_FILE = TELEMETRY_DIR / "compaction.jsonl"

# Cap the on-disk telemetry log so it doesn't grow forever. ~256 MB is
# enough for several months of heavy daily use; we rotate beyond that.
_MAX_BYTES = 256 * 1024 * 1024
_ROTATED = TELEMETRY_DIR / "compaction.jsonl.old"


def _ensure_dir() -> None:
    try:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _maybe_rotate() -> None:
    try:
        if TELEMETRY_FILE.exists() and TELEMETRY_FILE.stat().st_size > _MAX_BYTES:
            # One-deep rotation: drop the previous .old, slide current to .old.
            try:
                _ROTATED.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            TELEMETRY_FILE.replace(_ROTATED)
    except Exception:  # noqa: BLE001
        pass


def append_telemetry(event: dict[str, Any]) -> None:
    """Append one event to the telemetry JSONL.

    Best-effort — failures are swallowed so telemetry never blocks an
    agent turn. The event must include a `type` key; `ts` is filled in
    if absent.
    """
    if "ts" not in event:
        event["ts"] = time.time()
    _ensure_dir()
    _maybe_rotate()
    try:
        with TELEMETRY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str))
            f.write("\n")
    except Exception:  # noqa: BLE001
        pass


def read_telemetry(limit: int | None = None) -> list[dict[str, Any]]:
    """Read recent telemetry events. Used by the metrics IPC route."""
    if not TELEMETRY_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with TELEMETRY_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
                if limit is not None and len(rows) >= limit:
                    break
    except Exception:  # noqa: BLE001
        pass
    return rows


def telemetry_path() -> str:
    return str(TELEMETRY_FILE)
