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
