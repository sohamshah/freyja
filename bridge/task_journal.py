"""Bridge-side task ledger persistence.

Append-only JSONL log per session. Every task mutation writes a single
line; on session restore we replay the log to rebuild the in-memory
ledger before any tool calls land. Mirrors `kanban_journal.py`'s shape
so the two persistence layers behave consistently.

Storage layout:
    ~/.freyja/sessions/{session_id}.tasks.jsonl

Event shapes (one per line):

    {"kind": "create", "ts": <ms>, "task": <full to_dict()>}
    {"kind": "update", "ts": <ms>, "id": <task_id>, "actor": "...",
     "fields": <delta dict>, "note": "..."}
    {"kind": "link", "ts": <ms>, "blocker": <id>, "dependent": <id>,
     "actor": "..."}
    {"kind": "unlink", "ts": <ms>, "blocker": <id>, "dependent": <id>,
     "actor": "..."}
    {"kind": "restarted", "ts": <ms>}

Replay applies events to a fresh SessionTaskBoard using the same
internal helpers as live mutations. The bridge is the persistence
authority; the renderer's task slice is rebuilt from the event stream
the bridge emits at replay-time + live.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".freyja" / "sessions"


def journal_path(session_id: str) -> Path:
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "_-.")[:160]
    return SESSIONS_DIR / f"{safe_id}.tasks.jsonl"


class TaskJournal:
    """Append-only JSONL writer scoped to one session's task ledger.

    Writes are best-effort: a disk failure does not block the in-memory
    mutation. The bridge is the persistence authority for tasks; this
    log is purely for cross-restart replay so a long-running session
    keeps its planning surface intact when the bridge crashes/restarts.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to ensure task journal directory at %s", path.parent)

    def append(self, event: dict[str, Any]) -> None:
        event = {"ts": int(time.time() * 1000), **event}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")))
                f.write("\n")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write task journal entry to %s", self.path)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Skip corrupt lines rather than abandoning the
                        # whole log — task state is too valuable to
                        # throw away because of one bad write.
                        logger.warning(
                            "Skipping malformed task journal entry in %s", self.path
                        )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read task journal at %s", self.path)
            return []
        return events

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete task journal at %s", self.path)
