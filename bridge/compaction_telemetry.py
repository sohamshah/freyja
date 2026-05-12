"""Compaction telemetry collection.

Persists every pressure-band crossing and every actual compaction event
to a JSONL file under ~/.freyja/telemetry/. The renderer reads this
file via IPC for the cross-session metrics dashboard.

Schema (one JSON object per line):

  type: 'pressure_signal'
    session_id, turn_id, ts, band, pressure_pct, used_tokens,
    effective_window, model

  type: 'compaction_event'
    session_id, turn_id, ts, trigger,  # 'pre_request' | 'post_response'
                                       # | 'manual' | 'overflow' | 'fallback'
    mechanism,                         # 'cheap_pruning' | 'tool_halve'
                                       # | 'summary' | 'iterative' | 'image_prune'
    tokens_before, tokens_after,
    saved_pct, duration_ms, model

  type: 'llm_call_metric'
    session_id, turn_id, ts, model,
    input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens,
    cost_usd

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
