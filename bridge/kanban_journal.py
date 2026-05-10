"""Bridge-side kanban board persistence.

Append-only JSONL log per session. Every board mutation writes a single
line; on session restore we replay the log to rebuild the in-memory
board before any tool calls land. Mirrors the renderer's event-sourced
`collectKanbanCards` reconstruction on the bridge side so a long
mission can survive a bridge restart.

Storage layout:
    ~/.freyja/sessions/{session_id}.kanban.jsonl

Event shapes (one per line):

    {"kind": "create", "ts": <ms>, "task": <full to_dict()>}
    {"kind": "update", "ts": <ms>, "id": <card_id>, "actor": "...",
     "fields": <delta dict>, "status": <maybe>}
    {"kind": "comment", "ts": <ms>, "id": <card_id>, "actor": "...",
     "body": "..."}
    {"kind": "link", "ts": <ms>, "parent": <pid>, "child": <cid>,
     "actor": "..."}
    {"kind": "unblock", "ts": <ms>, "id": <card_id>, "actor": "..."}

The replay path applies events to a fresh SessionKanbanBoard using the
same internal helpers as live mutations — there is exactly one source
of truth for what each kind means.
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
    return SESSIONS_DIR / f"{safe_id}.kanban.jsonl"


class KanbanJournal:
    """Append-only JSONL writer scoped to one session's board.

    Writes are best-effort: a disk failure does not block the in-memory
    mutation. The renderer remains the user-facing source of truth for
    the live UI; the journal is purely for cross-restart replay.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to ensure kanban journal directory at %s", path.parent)

    def append(self, event: dict[str, Any]) -> None:
        event = {"ts": int(time.time() * 1000), **event}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")))
                f.write("\n")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write kanban journal entry to %s", self.path)

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
                        # whole log — autonomy missions are too expensive
                        # to throw away because of one bad write.
                        logger.warning(
                            "Skipping malformed kanban journal entry in %s", self.path
                        )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read kanban journal at %s", self.path)
            return []
        return events

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete kanban journal at %s", self.path)
