"""`recall` — search this session's full pre-compaction transcript.

Compaction (and the cheap pruning path) condense or evict old turns from the
live transcript, but the runtime preserves every message verbatim in
``<project_dir>/raw_messages.jsonl`` (see ``_append_raw_message_log``). Until
now that archive was write-only — the agent had no way to look back at detail
the summary dropped. ``recall`` closes that gap: grep the archive for a query
and get back the matching turns, so dropped detail is *recoverable on demand*
instead of permanently lost (the "evict + retrieve" half of Grounded Memory).

Cross-session: pass ``session_id`` to search another session's archive — the
substrate for "resume a prior workstream" / answering "what did I find last
time".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bridge.project_paths import project_output_dir
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)

_MAX_SNIPPET = 280
_DEFAULT_LIMIT = 12


def _message_text(message: dict[str, Any] | None) -> str:
    """Best-effort flatten of a serialized Message's content to text."""
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
                elif isinstance(block.get("content"), list):
                    for sub in block["content"]:
                        if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                            parts.append(sub["text"])
            elif isinstance(block, str):
                parts.append(block)
    return "\n".join(p for p in parts if p)


def _snippet(text: str, query: str) -> str:
    """A window of ``text`` centered on the first case-insensitive match."""
    lo = text.lower()
    idx = lo.find(query.lower())
    if idx < 0:
        return text[:_MAX_SNIPPET]
    start = max(0, idx - _MAX_SNIPPET // 3)
    end = min(len(text), idx + (2 * _MAX_SNIPPET) // 3)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


class RecallTool:
    """Search the verbatim per-session archive (``raw_messages.jsonl``)."""

    def __init__(self, *, session_id: str, project_output_dir: Path | str | None = None) -> None:
        self._session_id = session_id
        # Allow an explicit override (tests); otherwise resolve per session id.
        self._override_dir = Path(project_output_dir) if project_output_dir else None

    def _archive_path(self, session_id: str | None) -> Path:
        sid = (session_id or self._session_id) or ""
        if self._override_dir is not None and (not session_id or session_id == self._session_id):
            base = self._override_dir
        else:
            base = project_output_dir(sid)
        return Path(base) / "raw_messages.jsonl"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="recall",
            summary="Search this session's full pre-compaction history",
            tier=ToolTier.WARM,
            description=(
                "Search the complete, verbatim transcript of this session (or "
                "another session) — including turns that compaction has since "
                "condensed or removed from your active context. Use it to "
                "recover detail you no longer see: an earlier tool result, what "
                "a file looked like, a decision you made, what a web search "
                "returned.\n\n"
                "ACTIONS:\n"
                "  • search (default) — return turns matching `query`.\n"
                "  • timeline — a compact index of recent turns (role + first "
                "line) to orient yourself.\n\n"
                "Pass `session_id` to search a DIFFERENT session's history "
                "(e.g. to continue related work). This complements the "
                "`artifacts` tool (files you produced) and the runtime "
                "write-ledger reminder (what you changed): `recall` is for the "
                "conversational/observational detail behind them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "timeline"]},
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring to search for.",
                    },
                    "limit": {"type": "integer", "description": "Max turns (default 12)."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional: search another session's archive.",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "search").strip().lower()
        session_id = str(arguments.get("session_id") or "").strip() or None
        limit = int(arguments.get("limit") or _DEFAULT_LIMIT)
        path = self._archive_path(session_id)
        try:
            rows = self._load(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall load failed", exc_info=True)
            return _err(call_id, f"recall failed to read archive: {exc}")

        if not rows:
            return _ok(call_id, {
                "action": action,
                "archive": str(path),
                "exists": path.exists(),
                "results": [],
                "note": "No archived history found for this session yet.",
            })

        if action == "timeline":
            return self._timeline(call_id, path, rows, limit)
        return self._search(call_id, path, rows, arguments, limit)

    def _search(self, call_id, path, rows, arguments, limit) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _err(call_id, "search requires a non-empty `query`")
        hits: list[dict[str, Any]] = []
        for row in rows:
            msg = row.get("message") or {}
            text = _message_text(msg)
            if query.lower() in text.lower():
                hits.append({
                    "turn_id": row.get("turn_id"),
                    "role": msg.get("role"),
                    "ts": row.get("ts"),
                    "snippet": _snippet(text, query),
                })
        hits = hits[-limit:]  # most recent matches
        return _ok(call_id, {
            "action": "search",
            "query": query,
            "archive": str(path),
            "match_count": len(hits),
            "results": hits,
        })

    def _timeline(self, call_id, path, rows, limit) -> ToolResult:
        out = []
        for row in rows[-limit:]:
            msg = row.get("message") or {}
            text = _message_text(msg).strip().splitlines()
            out.append({
                "turn_id": row.get("turn_id"),
                "role": msg.get("role"),
                "first_line": (text[0][:160] if text else ""),
            })
        return _ok(call_id, {
            "action": "timeline",
            "archive": str(path),
            "turn_count": len(rows),
            "results": out,
        })

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def _err(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)


def _ok(call_id: str, payload: dict[str, Any]) -> ToolResult:
    return ToolResult(call_id=call_id, content=json.dumps(payload, indent=2))
