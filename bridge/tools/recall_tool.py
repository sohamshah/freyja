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

A sub-agent's archive is ``~/.freyja/sessions/<id>.raw_messages.jsonl``,
beside its sidecars (as is the archive of a session whose project dir is
another session's); a session's archive is read from both places.

The messages a log is missing (a restore stopped it before 2026-11-06, and a
sub-agent had none) are in ``raw_messages.recovered.jsonl`` beside it, which
``scripts/backfill_raw_messages.py`` writes. recall reads them among the live
rows, by time, and a live row wins, as exports do (src/main/sessionRecord.ts).

A log can be tens of MB, so it is read a line at a time and only what is
returned is kept, in a worker thread so the event loop keeps serving. A search
of one log reads it from the end and stops at the newest matches.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from bridge.project_paths import project_output_dir
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)

_MAX_SNIPPET = 280
_DEFAULT_LIMIT = 12
# A log read from the end is read this many bytes at a time.
_BLOCK = 1 << 20


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


def _ts(row: dict[str, Any]) -> float:
    ts = row.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else 0.0


def _message_keys(row: dict[str, Any]) -> set[str]:
    """The tool calls a row's message makes, and the call it answers."""
    message = row.get("message")
    if not isinstance(message, dict):
        return set()
    keys = {
        f"call:{call['id']}"
        for call in message.get("tool_calls") or []
        if isinstance(call, dict) and isinstance(call.get("id"), str)
    }
    if message.get("role") == "tool_result" and isinstance(message.get("tool_call_id"), str):
        keys.add(f"result:{message['tool_call_id']}")
    return keys


def _recovered_path(log: Path) -> Path:
    """Where scripts/backfill_raw_messages.py writes the rows ``log`` is missing."""
    return log.with_name(log.name.removesuffix(".jsonl") + ".recovered.jsonl")


def _parse(line: str) -> dict[str, Any] | None:
    """A log line's row. None for a blank line, a line an interrupted write
    cut short, or one that is not an object."""
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _lines(path: Path) -> Iterator[str]:
    """A log's lines, oldest first, one at a time."""
    with path.open("rb") as fh:
        for line in fh:
            yield line.decode("utf-8", "replace")


def _lines_newest_first(path: Path) -> Iterator[str]:
    """A log's lines, newest first, read from the end a block at a time."""
    with path.open("rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        line_end: list[bytes] = []  # the end of the line being read, its last block first
        while end > 0:
            start = max(0, end - _BLOCK)
            fh.seek(start)
            pieces = fh.read(end - start).split(b"\n")
            end = start
            if len(pieces) > 1:
                yield (pieces[-1] + b"".join(reversed(line_end))).decode("utf-8", "replace")
                for piece in reversed(pieces[1:-1]):
                    yield piece.decode("utf-8", "replace")
                line_end = []
            line_end.append(pieces[0])
        yield b"".join(reversed(line_end)).decode("utf-8", "replace")


def _newest(
    logs: list[Path],
    recovered: list[dict[str, Any]],
    pick: Callable[[dict[str, Any]], Any],
    limit: int,
    *,
    count_all: bool = False,
) -> tuple[list[Any], int]:
    """What ``pick`` makes of the newest ``limit`` rows it takes (``[-limit:]``
    of them all when ``limit`` < 1), oldest first, and how many of the
    timeline's rows were read (all of them with ``count_all``).

    The timeline is each live log in its own order and the recovered rows
    sorted, merged by time; on a tie the live row goes first. A recovered row
    whose message a live row also has (the same call, or its result) is left
    out: the live one stands. One log with no recovered rows is the timeline
    as written, so it is read from the end and the read stops at the
    ``limit``-th row taken, unless ``count_all``. Otherwise every row is
    read, a line at a time.
    """
    bound = limit if limit > 0 else None
    items: list[Any] = []
    count = 0
    if len(logs) == 1 and not recovered:
        for line in _lines_newest_first(logs[0]):
            row = _parse(line)
            if row is None:
                continue
            count += 1
            if len(items) != bound and (item := pick(row)) is not None:
                items.append(item)
                if len(items) == bound and not count_all:
                    break
        items.reverse()
        return items[-limit:], count

    keys = set().union(*map(_message_keys, recovered))
    held: set[str] = set()  # the ones a live row has too
    newest: deque[tuple[int, Any]] = deque(maxlen=bound)
    # Recovered rows a live row may turn out to have, with their keys.
    held_back: list[tuple[int, Any, set[str]]] = []
    sources: list[Iterator[tuple[dict[str, Any], bool]]] = [
        ((row, False) for row in map(_parse, _lines(p)) if row is not None) for p in logs
    ]
    sources.append((row, True) for row in sorted(recovered, key=_ts))
    for position, (row, is_recovered) in enumerate(
        heapq.merge(*sources, key=lambda pair: _ts(pair[0]))
    ):
        own = _message_keys(row)
        if not is_recovered:
            held |= own & keys
        count += 1
        item = pick(row)
        if item is None:
            continue
        if is_recovered and own:
            held_back.append((position, item, own))
        else:
            newest.append((position, item))
    count -= sum(1 for row in recovered if _message_keys(row) & held)
    kept = [(position, item) for position, item, own in held_back if not own & held]
    items = [item for _position, item in heapq.merge(newest, kept)]
    return items[-limit:], count


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

    def __init__(
        self,
        *,
        session_id: str,
        project_output_dir: Path | str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        self._session_id = session_id
        # Allow an explicit override (tests); otherwise resolve per session id.
        self._override_dir = Path(project_output_dir) if project_output_dir else None
        # Set for a sub-agent's own instance: its parent's archive is one
        # session_id away, and the description says which.
        self._parent_session_id = parent_session_id

    def _archive_paths(self, session_id: str | None) -> list[Path]:
        """The logs that hold a session's messages: beside its sidecars (a
        sub-agent's), then in its project dir (a root session's)."""
        from bridge.transcript_persistence import raw_messages_path

        sid = (session_id or self._session_id) or ""
        if self._override_dir is not None and (not session_id or session_id == self._session_id):
            return [self._override_dir / "raw_messages.jsonl"]
        return [raw_messages_path(sid), project_output_dir(sid) / "raw_messages.jsonl"]

    @property
    def definition(self) -> ToolDefinition:
        parent_note = (
            "\n\nYou are a sub-agent: this searches your own history. Pass "
            f'session_id="{self._parent_session_id}" to search your parent\'s.'
            if self._parent_session_id
            else ""
        )
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
                + parent_note
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
        archives = self._archive_paths(session_id)
        paths = [p for p in archives if p.exists()]
        recovered = [p for p in map(_recovered_path, archives) if p.exists()]
        path = ", ".join(str(p) for p in paths + recovered) or str(archives[-1])
        query = str(arguments.get("query") or "").strip()
        try:
            # A read of the 96 MB log takes 0.14 s: on the loop it would
            # hold the bridge for all of it.
            rows, count, empty = await asyncio.to_thread(
                _read, paths, recovered, action, query, limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall load failed", exc_info=True)
            return _err(call_id, f"recall failed to read archive: {exc}")

        if empty:
            return _ok(call_id, {
                "action": action,
                "archive": path,
                "exists": bool(paths or recovered),
                "results": [],
                "note": "No archived history found for this session yet.",
            })

        if action == "timeline":
            return self._timeline(call_id, path, rows, count)
        return self._search(call_id, path, query, rows)

    def _search(self, call_id, path, query, hits) -> ToolResult:
        if not query:
            return _err(call_id, "search requires a non-empty `query`")
        return _ok(call_id, {
            "action": "search",
            "query": query,
            "archive": path,
            "match_count": len(hits),
            "results": hits,
        })

    def _timeline(self, call_id, path, rows, count) -> ToolResult:
        out = []
        for row in rows:
            msg = row.get("message") or {}
            text = _message_text(msg).strip().splitlines()
            out.append({
                "turn_id": row.get("turn_id"),
                "role": msg.get("role"),
                "first_line": (text[0][:160] if text else ""),
            })
        return _ok(call_id, {
            "action": "timeline",
            "archive": path,
            "turn_count": count,
            "results": out,
        })


def _read(
    paths: list[Path], recovered: list[Path], action: str, query: str, limit: int
) -> tuple[list[Any], int, bool]:
    """What ``action`` returns from the live logs ``paths`` and the recovered
    ones: its rows, how many rows the timeline has (for a timeline), and
    whether the logs hold no row at all. It blocks while it reads."""
    # The recovered rows are a transcript's worth at most.
    recovered_rows = [
        row for p in recovered for row in map(_parse, _lines(p)) if row is not None
    ]
    if action == "timeline":
        rows, count = _newest(paths, recovered_rows, lambda row: row, limit, count_all=True)
        return rows, count, not count
    rows = []
    if query:
        rows, _ = _newest(paths, recovered_rows, _hit(query), limit)
    empty = not rows and not recovered_rows and not any(
        _parse(line) is not None for p in paths for line in _lines(p)
    )
    return rows, 0, empty


def _hit(query: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """A row as a search result, when its text holds ``query``, case aside."""
    q = query.lower()

    def hit(row: dict[str, Any]) -> dict[str, Any] | None:
        msg = row.get("message") or {}
        text = _message_text(msg)
        if q not in text.lower():
            return None
        return {
            "turn_id": row.get("turn_id"),
            "role": msg.get("role"),
            "ts": row.get("ts"),
            "snippet": _snippet(text, query),
        }

    return hit


def _err(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)


def _ok(call_id: str, payload: dict[str, Any]) -> ToolResult:
    return ToolResult(call_id=call_id, content=json.dumps(payload, indent=2))
