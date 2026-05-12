"""
Bridge-side transcript persistence.

Saves and loads the engine's serialized transcript state to disk so
sessions can be resumed after app restart with full LLM context.

Storage layout:
    ~/.freyja/sessions/{session_id}.transcript.json

The bridge owns this file — the renderer never reads or writes it.
The renderer's PersistedSession (UI slice) is a separate concern.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".freyja" / "sessions"


def _transcript_path(session_id: str) -> Path:
    """Return the path to a session's transcript file."""
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "_-.")[:160]
    return SESSIONS_DIR / f"{safe_id}.transcript.json"


def _goal_path(session_id: str) -> Path:
    """Return the path to a session's goal-state sidecar file."""
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "_-.")[:160]
    return SESSIONS_DIR / f"{safe_id}.goal.json"


def save_goal_state(session_id: str, data: dict[str, Any]) -> None:
    """Persist the goal loop's state, brief, and verdict history to disk.

    Lives in a sidecar `~/.freyja/sessions/{id}.goal.json` so it can be
    loaded independently of the transcript and reloaded incrementally
    when only goal state changes (every judge call, every brief edit).
    Atomic write via tmp+rename.

    Schema (camelCase to match the wire format):
      {
        "version": 1,
        "sessionId": str,
        "goalState": GoalState.to_dict() | None,
        "judgeRules": JudgeRules.to_dict(),
        "verdictHistory": [GoalVerdict.to_dict(), ...]
      }
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _goal_path(session_id)
    tmp = dest.with_suffix(".tmp")
    payload = dict(data)
    payload.setdefault("version", 1)
    payload.setdefault("sessionId", session_id)
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(dest)
    except Exception:
        logger.exception("Failed to save goal state for %s", session_id)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_goal_state(session_id: str) -> dict[str, Any] | None:
    """Load persisted goal state, brief, and verdict history. None if absent."""
    path = _goal_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            logger.warning("Goal state version mismatch for %s, ignoring", session_id)
            return None
        return data
    except Exception:
        logger.exception("Failed to load goal state for %s", session_id)
        return None


def delete_goal_state(session_id: str) -> None:
    """Remove a persisted goal-state sidecar file."""
    try:
        _goal_path(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def save_transcript(session_id: str, data: dict[str, Any]) -> None:
    """Persist a serialized transcript to disk.

    Writes atomically via tmp+rename to avoid corruption on crash.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _transcript_path(session_id)
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(dest)
    except Exception:
        logger.exception("Failed to save transcript for %s", session_id)
        # Clean up the tmp file if rename failed.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_transcript(session_id: str) -> dict[str, Any] | None:
    """Load a persisted transcript from disk. Returns None if absent/corrupt."""
    path = _transcript_path(session_id)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            logger.warning("Transcript version mismatch for %s, ignoring", session_id)
            return None
        return data
    except Exception:
        logger.exception("Failed to load transcript for %s", session_id)
        return None


def delete_transcript(session_id: str) -> None:
    """Remove a persisted transcript file."""
    try:
        _transcript_path(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def clone_transcript(
    old_id: str,
    new_id: str,
    *,
    truncate_to_message_ordinal: int | None = None,
) -> bool:
    """Deep-copy a transcript on disk under a new session id.

    Used by the branch operation. When ``truncate_to_message_ordinal`` is
    provided, the destination transcript is truncated so it contains
    only the first N message-bearing entries (i.e. messages 0..N-1
    counted across user + assistant entries; compaction entries don't
    count). The destination's ``head_id`` is updated to the last kept
    entry. Returns False if the source transcript can't be read.
    """
    src = load_transcript(old_id)
    if src is None:
        return False
    # Deep copy via JSON round-trip — the structure is plain dicts +
    # lists + strings + numbers, so this is safe and avoids accidental
    # mutation of the in-memory original.
    dst = json.loads(json.dumps(src))
    dst["session_id"] = new_id
    if truncate_to_message_ordinal is not None:
        transcript = dst.get("transcript") or {}
        entries = transcript.get("entries") or []
        kept: list[Any] = []
        new_head: str | None = None
        msg_count = 0
        for entry in entries:
            has_message = entry.get("message") is not None
            if has_message and msg_count >= truncate_to_message_ordinal:
                break
            kept.append(entry)
            entry_id = entry.get("id")
            if entry_id is not None:
                new_head = str(entry_id)
            if has_message:
                msg_count += 1
        transcript["entries"] = kept
        transcript["head_id"] = new_head
        dst["transcript"] = transcript
    save_transcript(new_id, dst)
    return True


def provider_family(model_id: str) -> str:
    """Classify a model ID into a provider family for cross-provider detection.

    Returns 'anthropic', 'openai', 'google', or 'unknown'.
    """
    m = model_id.lower()
    if any(k in m for k in ("claude", "opus", "sonnet", "haiku")):
        return "anthropic"
    if any(k in m for k in ("gpt", "o1", "o3", "o4")):
        return "openai"
    if any(k in m for k in ("gemini", "palm")):
        return "google"
    return "unknown"
