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
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Collection, Iterator

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".freyja" / "sessions"


def _sanitize_session_id(session_id: str) -> str:
    """Convert a session id to a filename-safe form.

    Mirrors the renderer-side ``src/main/persistence.ts:sanitizeId`` —
    REPLACES invalid chars with ``_`` (rather than stripping them).
    Both sides must use the same scheme or the renderer will look for
    files at a different path than the daemon wrote them. Keep these
    two functions in lockstep.
    """
    out: list[str] = []
    for c in session_id:
        if c.isalnum() or c in "_-.":
            out.append(c)
        else:
            out.append("_")
    return "".join(out)[:160]


def _transcript_path(session_id: str) -> Path:
    """Return the path to a session's transcript file."""
    return SESSIONS_DIR / f"{_sanitize_session_id(session_id)}.transcript.json"


def _goal_path(session_id: str) -> Path:
    """Return the path to a session's goal-state sidecar file."""
    return SESSIONS_DIR / f"{_sanitize_session_id(session_id)}.goal.json"


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


def _inbox_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.inbox.json"


@contextmanager
def _inbox_lock() -> Iterator[None]:
    """The exclusive flock that every read and write of an inbox sidecar
    holds, in every process. The app, the gateway and the scheduler daemon
    each queue messages into these files (append_inbox_messages), and the
    process that loads a session reads its file once, then saves its own
    copy over it. Unlocked, a load or a second queue that fell between a
    queue's read and its write missed the message, and the next save
    dropped it: the gateway, starting, could load a Slack thread just as
    the app queued a talk for it. One lock for all sessions: each hold is
    one small file's read or write."""
    import fcntl

    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        fp = (SESSIONS_DIR / ".inbox.lock").open("a")
    except OSError:
        yield  # no lock file: go on unlocked, as before
        return
    with fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass  # no flock on this filesystem: go on unlocked
        yield  # closing the file releases the lock


def save_inbox_state(
    session_id: str,
    data: dict[str, Any],
    *,
    known: Collection[str] | None = None,
) -> None:
    """Persist a session's inbox queue + recent delivered history.

    Schema mirrors SessionInbox.to_dict(); see bridge/inbox.py.
    Writes atomically; safe across crashes. With ``known``
    (SessionInbox.known_ids), an unread message in the file outside it was
    queued by another process after this one read the file, and is kept.
    Written over, it was lost: the scheduler's tick owner keeps a job's
    session loaded between runs while the app queues talks for it here.
    """
    with _inbox_lock():
        if known is not None:
            queued = _queued_in(_read_inbox(session_id), known)
            if queued:
                data = {**data, "unread": [*(data.get("unread") or []), *queued]}
        _write_inbox(session_id, data)


def queued_inbox_messages(session_id: str, known: Collection[str]) -> list[dict[str, Any]]:
    """The unread messages in a session's inbox file outside ``known``
    (SessionInbox.known_ids): queued by another process since this one
    last read or wrote the file."""
    with _inbox_lock():
        return _queued_in(_read_inbox(session_id), known)


def _queued_in(data: dict[str, Any] | None, known: Collection[str]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    return [
        u
        for u in data.get("unread") or []
        if isinstance(u, dict) and isinstance(u.get("id"), str) and u["id"] not in known
    ]


def load_inbox_state(session_id: str) -> dict[str, Any] | None:
    """Load a session's inbox from disk. Returns None if absent/corrupt."""
    with _inbox_lock():
        return _read_inbox(session_id)


def append_inbox_messages(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    unless: Callable[[], bool] | None = None,
) -> bool:
    """Add ``messages`` (``InboxMessage.to_dict()``) to the unread queue in
    a session's inbox sidecar, skipping any it already holds. The read and
    the write are one hold of the inbox lock, so a message another process
    queues at the same time, or a load at the same time, is not lost.
    ``unless`` is asked under the lock first: when it is true, nothing is
    written and this returns False (see talk_tool.send_to_gateway)."""
    with _inbox_lock():
        if unless is not None and unless():
            return False
        data = _read_inbox(session_id) or {"sessionId": session_id, "unread": [], "delivered": []}
        unread = list(data.get("unread") or [])
        known = {u.get("id") for u in unread if isinstance(u, dict)}
        data["unread"] = unread + [m for m in messages if m.get("id") not in known]
        _write_inbox(session_id, data)
    return True


def fold_into_inbox_file(
    session_id: str,
    message: dict[str, Any],
    fold: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> None:
    """Queue ``message`` in a session's inbox sidecar, folded into the
    first unread message there that ``fold`` folds it into: ``fold``
    returns the message that takes that one's place, at the end of the
    queue, or None to leave it be. The read and the write are one hold of
    the inbox lock, as in append_inbox_messages."""
    with _inbox_lock():
        data = _read_inbox(session_id) or {"sessionId": session_id, "unread": [], "delivered": []}
        unread = [u for u in data.get("unread") or [] if isinstance(u, dict)]
        for i, queued in enumerate(unread):
            folded = fold(queued)
            if folded is not None:
                del unread[i]
                message = folded
                break
        data["unread"] = [*unread, message]
        _write_inbox(session_id, data)


def drop_from_inbox_file(session_id: str, drop: Callable[[dict[str, Any]], bool]) -> int:
    """Remove the unread messages ``drop`` picks from a session's inbox
    sidecar, under the inbox lock. Returns how many went."""
    with _inbox_lock():
        data = _read_inbox(session_id)
        if not isinstance(data, dict):
            return 0
        unread = [u for u in data.get("unread") or [] if isinstance(u, dict)]
        kept = [u for u in unread if not drop(u)]
        if len(kept) < len(unread):
            data["unread"] = kept
            _write_inbox(session_id, data)
        return len(unread) - len(kept)


def _write_inbox(session_id: str, data: dict[str, Any]) -> None:
    dest = _inbox_path(session_id)
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(dest)
    except Exception:
        logger.exception("Failed to save inbox for %s", session_id)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read_inbox(session_id: str) -> dict[str, Any] | None:
    path = _inbox_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load inbox for %s", session_id)
        return None


def delete_inbox_state(session_id: str) -> None:
    try:
        _inbox_path(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def jsonl_line(row: Any) -> str:
    """One JSONL row, text as it is. Text holding a lone surrogate (half of a
    pair, cut in JavaScript) cannot be written as UTF-8: that row escapes
    its non-ASCII text instead, and reads back the same."""
    line = json.dumps(row, ensure_ascii=False, default=str)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        line = json.dumps(row, default=str)
    return line


def raw_messages_path(session_id: str) -> Path:
    """A sub-agent's raw message log, beside its other sidecars. A root
    session's is ``<project dir>/raw_messages.jsonl``."""
    return SESSIONS_DIR / f"{_sanitize_session_id(session_id)}.raw_messages.jsonl"


def append_raw_message(
    path: Path, *, session_id: str, turn_id: str | None, message: Any
) -> None:
    """Append one message, as the transcript took it, to a raw message log.

    The log is append-only and outside the engine transcript, so compaction
    and halving never touch it; exports read each tool result whole from it.
    Image bytes go to the shared media store (``externalize_images``).
    """
    from engine.media_store import externalize_images

    payload: dict[str, Any] = {
        "ts": time.time(),
        "session_id": session_id,
        "turn_id": turn_id,
        "message": message.to_dict() if hasattr(message, "to_dict") else None,
    }
    externalize_images(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(jsonl_line(payload))
        fh.write("\n")


def append_subagent_raw_message(session_id: str, message: Any) -> None:
    """A sub-agent session's ``on_message_appended``: append the message to
    its raw message log. Never raises; the agent loop must not stop on it."""
    try:
        append_raw_message(
            raw_messages_path(session_id),
            session_id=session_id,
            turn_id=None,
            message=message,
        )
    except Exception:  # noqa: BLE001
        logger.debug("raw_messages append failed for %s", session_id, exc_info=True)


def compactions_path(session_id: str) -> Path:
    """A sub-agent's compaction log, beside its other sidecars. A root
    session's is ``<project dir>/compactions.jsonl``."""
    return SESSIONS_DIR / f"{_sanitize_session_id(session_id)}.compactions.jsonl"


# The system events a compaction log records, as a root session's
# _on_system_event mirrors them (bridge/freyja_bridge.py).
COMPACTION_LOG_SUBTYPES = frozenset({
    "compaction_start", "compaction_complete", "compaction_skipped",
    "context_pruning", "media_pruning",
})


def append_subagent_compaction(
    session_id: str,
    *,
    model: str,
    subtype: str,
    details: dict[str, Any],
    default_trigger: str,
    summary_text: str | None = None,
    agent_type: str | None = None,
    parent_session_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Record one of a sub-agent's compaction events, as a root session's
    are (``_mirror_compaction_event``, ``_emit_summarize_event``): a row in
    its compaction log, and a ``compaction_event`` row in the cross-session
    telemetry the dashboard reads. ``summary_text`` is the full summary when
    the event carries only an excerpt; ``turn_id`` is the step it ran in, as
    the sub-agent's model calls name it. Never raises; the agent loop must
    not stop on it."""
    summary = (
        summary_text
        or details.get("summary_text")
        or details.get("summary_preview")
        or details.get("summary_excerpt")
        or ""
    )
    sizes = {
        "tokens_before": int(
            details.get("tokens_before") or details.get("context_tokens_before") or 0
        ),
        "tokens_after": int(
            details.get("tokens_after") or details.get("context_tokens_after") or 0
        ),
        "effective_window": details.get("effective_window"),
        "pressure_pct_before": details.get("pressure_pct_before"),
        "pressure_pct_after": details.get("pressure_pct_after"),
        # What set a pre-request or overflow compaction off.
        "trigger_tokens": details.get("trigger_tokens"),
        "trigger_pressure_pct": details.get("trigger_pressure_pct"),
    }
    accounting = {
        "summary_tokens": int(details.get("summary_tokens") or 0),
        "scope": details.get("scope"),
        "reason": details.get("reason"),
        "resumed_from_previous": bool(details.get("resumed_from_previous") or False),
        "summarizer_input_tokens": int(details.get("summarizer_input_tokens") or 0),
        "summarizer_output_tokens": int(details.get("summarizer_output_tokens") or 0),
        "summarizer_cost_usd": details.get("summarizer_cost_usd"),
    }
    trigger = details.get("trigger") or default_trigger
    try:
        row = {
            "session_id": session_id,
            "model": model,
            "turn_id": turn_id,
            "ts": time.time(),
            "subtype": subtype,
            "trigger": trigger,
            "mechanism": details.get("strategy") or details.get("mechanism") or subtype,
            **sizes,
            "entries_removed": int(details.get("entries_removed") or 0),
            "summary_text": summary,
            **accounting,
        }
        path = compactions_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(jsonl_line(row))
            fh.write("\n")
    except Exception:  # noqa: BLE001
        logger.debug("compactions log append failed for %s", session_id, exc_info=True)
    try:
        from bridge.compaction_telemetry import append_telemetry

        append_telemetry({
            "type": "compaction_event",
            "session_id": session_id,
            "turn_id": turn_id,
            "agent_type": agent_type,
            "parent_session_id": parent_session_id,
            "subtype": subtype,
            "model": model,
            **sizes,
            "mechanism": (
                details.get("strategy")
                or details.get("mechanism")
                or {
                    "compaction_complete": "summary",
                    "context_pruning": "tool_halve",
                    "media_pruning": "image_prune",
                }.get(subtype, subtype)
            ),
            "trigger": trigger,
            # An image prune's count of the images that left the context.
            "omitted_images": details.get("omitted_images"),
            **accounting,
            "reason": details.get("reason") or None,
            "summarizer_duration_ms": int(details.get("summarizer_duration_ms") or 0),
            "summary_excerpt": summary[:240] or None,
            "summary_text": summary or None,
        })
    except Exception:  # noqa: BLE001
        logger.debug("compaction telemetry failed for %s", session_id, exc_info=True)


def _subagent_path(session_id: str) -> Path:
    """Sidecar path for a paused / completed sub-agent that may be
    re-woken later via a talk() message."""
    return SESSIONS_DIR / f"{session_id}.subagent.json"


def save_subagent_state(session_id: str, data: dict[str, Any]) -> None:
    """Persist a sub-agent's full state for later re-wake.

    `data` shape (see bridge/freyja_bridge.py:_run_child finally hook):
        {
          "sessionId":          <id>,
          "parentSessionId":    <id>,
          "agentType":          <profile name>,
          "model":              <resolved model id>,
          "reasoningLevel":     <level>,
          "task":               <original task string>,
          "systemPrompt":       <fully-resolved system prompt>,
          "transcript":         <serialized transcript dict>,
          "coordinationStrategy": <strategy>,
          "label":              <display label>,
          "savedAt":            <ms>,
        }
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _subagent_path(session_id)
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(dest)
    except Exception:
        logger.exception("Failed to save subagent state for %s", session_id)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_subagent_state(session_id: str) -> dict[str, Any] | None:
    path = _subagent_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load subagent state for %s", session_id)
        return None


def delete_subagent_state(session_id: str) -> None:
    try:
        _subagent_path(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def save_transcript(session_id: str, data: dict[str, Any]) -> None:
    """Persist a serialized transcript to disk.

    Writes atomically via tmp+rename to avoid corruption on crash. Image
    bytes go to the shared media store and the file keeps their hashes
    (``engine.media_store.externalize_images``); ``data`` is modified in
    place.
    """
    from engine.media_store import externalize_images

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _transcript_path(session_id)
    tmp = dest.with_suffix(".tmp")
    try:
        externalize_images(data)
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


def set_transcript_aside(session_id: str) -> Path | None:
    """Start a session's next load on an empty conversation, keeping the
    saved one: its file moves to ``<file>.reset-<ms>``, a name no load or
    listing reads. The files a load reads back into the conversation go
    aside with it, under the same name: its goal, its inbox, and its
    kanban and task journals. Left, the next conversation took up the old
    one's board, goal and unread messages. Its event log and its raw
    message and compaction logs stay: they are traces, and no load reads
    them into the conversation. Nothing is read, so the moves take no
    time on an event loop, and a load after them finds no conversation;
    keep_settings puts the model, reasoning level and mode back. Returns
    where the conversation went, or None when none was saved."""
    stamp = int(time.time() * 1000)
    for sidecar in (
        _goal_path(session_id),
        _journal_path(session_id, ".kanban.jsonl"),
        _journal_path(session_id, ".tasks.jsonl"),
    ):
        _set_aside(sidecar, stamp)
    with _inbox_lock():  # another process queues into it under the lock
        _set_aside(_inbox_path(session_id), stamp)
    return _set_aside(_transcript_path(session_id), stamp)


def keep_settings(session_id: str, aside: Path) -> Path | None:
    """Put the model, reasoning level and mode of a conversation that
    set_transcript_aside set aside back in its place, in a file with no
    entries (as _save_settings_alone writes one). A file set aside that
    held no entries, only those, goes back as it was. Neither goes over a
    file the session saved there since. The whole file is parsed: a
    caller on an event loop runs this off the loop. Returns where the
    conversation went, or None when the file held none."""
    path = _transcript_path(session_id)
    try:
        data = json.loads(aside.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read %s", aside.name)
        return aside
    if not isinstance(data, dict):
        return aside
    if not (data.get("transcript") or {}).get("entries"):
        if _link_unless_there(aside, path):
            aside.unlink()  # back under its own name only: a rename
        return None
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    settings = {
        "version": 1,
        "session_id": session_id,
        "metadata": {
            k: meta[k] for k in ("model_id", "reasoning_level", "coordination_strategy") if k in meta
        },
        "transcript": {"entries": []},
    }
    tmp = aside.with_name(f"{aside.name}.tmp")
    try:
        tmp.write_text(json.dumps(settings, separators=(",", ":")), encoding="utf-8")
        _link_unless_there(tmp, path)
    except OSError:
        logger.exception("Failed to keep the settings of %s", session_id)
    finally:
        tmp.unlink(missing_ok=True)
    return aside


def _set_aside(path: Path, stamp: int) -> Path | None:
    aside = path.with_name(f"{path.name}.reset-{stamp}")
    try:
        path.replace(aside)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to set %s aside", path.name)
        return None
    return aside


def _link_unless_there(src: Path, dest: Path) -> bool:
    """Give ``src`` the name ``dest`` too, unless a file has it: one a
    session saved there is newer."""
    try:
        os.link(src, dest)
    except FileExistsError:
        return False
    return True


def remap_ids(obj: Any, id_remap: dict[str, str]) -> Any:
    """Return a deep copy of ``obj`` with every string that exactly equals
    an old session id replaced by its new id.

    Session ids show up in many places across the persisted files
    (``session_id``, ``metadata.parent_session_id``, ``creatorId`` in
    ledgers, ``fromSession`` in inboxes, sub-agent ids inside tool
    results...). Rather than chase each field by name, the branch
    operation rewrites every exact-match string so the clone refers
    only to its own world. Non-matching strings (including message
    prose) are left untouched.
    """
    if not id_remap:
        return obj
    if isinstance(obj, str):
        return id_remap.get(obj, obj)
    if isinstance(obj, list):
        return [remap_ids(v, id_remap) for v in obj]
    if isinstance(obj, dict):
        return {
            (id_remap.get(k, k) if isinstance(k, str) else k): remap_ids(v, id_remap)
            for k, v in obj.items()
        }
    return obj


def truncate_entries_before(
    entries: list[Any],
    cutoff_ts: float,
) -> tuple[list[Any], str | None]:
    """Keep the transcript entries created strictly before ``cutoff_ts``.

    ``cutoff_ts`` is a UNIX timestamp in seconds — the renderer sends the
    ``createdAt`` of the message it wants to branch *before*, and every
    engine entry carries the wall-clock at which it was appended. This is
    the only mapping that survives both compaction (which drops early
    entries) and tool-heavy turns (where one renderer message spans many
    engine entries), so it replaces ordinal counting for branch/edit/
    rerun/delete. Entries without a timestamp are treated as ancient
    and kept.

    Only message-bearing entries can end the walk, matching how
    ``freyja_bridge._find_message_entry_index`` resolves the anchor. A
    compaction entry sits in front of the older entries it kept but is
    stamped when the compaction ran, so its stamp says nothing about the
    branch point. Returns ``(kept, head_id)``.
    """
    kept: list[Any] = []
    head: str | None = None
    for entry in entries:
        ts = entry.get("timestamp")
        try:
            ts_f = float(ts) if ts is not None else 0.0
        except (TypeError, ValueError):
            ts_f = 0.0
        if entry.get("message") is not None and ts_f >= cutoff_ts:
            break
        kept.append(entry)
        entry_id = entry.get("id")
        if entry_id is not None:
            head = str(entry_id)
    return kept, head


class SummarizedAnchorError(Exception):
    """The message an anchor names was folded into a compaction summary,
    and no snapshot on disk holds it as written."""


def _field(entry: Any, name: str) -> Any:
    # A dict read from disk, or a live engine TranscriptEntry.
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def _entry_ts(entry: Any) -> float:
    try:
        return float(_field(entry, "timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _before_snapshots(session_id: str) -> list[tuple[int, Path]]:
    """The session's ``-before.json`` compaction snapshots, oldest first.
    The bridge writes one when a compaction starts; it holds the whole
    transcript as it was."""
    root = SESSIONS_DIR / "compactions"
    if not root.is_dir():
        return []
    pattern = re.compile(
        rf"^{re.escape(_compaction_safe_id(session_id))}-(\d+)-before\.json$"
    )
    found: list[tuple[int, Path]] = []
    for path in root.iterdir():
        m = pattern.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    return sorted(found)


def _snapshot_entries(path: Path) -> list[dict[str, Any]] | None:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["transcript"]["entries"]
    except Exception:
        return None
    return entries if isinstance(entries, list) else None


def summarized_through(session_id: str, summary: Any, entries: list[Any]) -> float | None:
    """Stamp of the newest message the compaction entry ``summary`` (one
    of ``entries``) stands in for. Summaries written before entries
    recorded it are read off the snapshot the bridge took when that
    compaction started. None when nothing tells."""
    through = _field(summary, "summarized_through")
    if through is not None:
        return float(through)
    return _snapshot_summarized_through(session_id, _entry_ts(summary) * 1000, entries)


def _snapshot_summarized_through(
    session_id: str, before_ms: float, entries: list[Any]
) -> float | None:
    snaps = [(stamp, path) for stamp, path in _before_snapshots(session_id) if stamp < before_ms]
    if not snaps:
        return None
    stamp, path = snaps[-1]
    snap = _snapshot_entries(path)
    if snap is None:
        return None
    snap_ids = {e.get("id") for e in snap}
    kept = next(
        (
            e
            for e in entries
            if _field(e, "message") is not None and _entry_ts(e) * 1000 < before_ms
        ),
        None,
    )
    if kept is not None and _field(kept, "id") not in snap_ids:
        # Taken before messages the compaction then summarized (by an
        # attempt that was skipped): it would understate the range.
        return None
    live = {_field(e, "id") for e in entries}
    stamps = [
        _entry_ts(e)
        for e in snap
        if e.get("message") is not None and e.get("id") not in live
    ]
    if stamps:
        return max(stamps)
    older = next((e for e in snap if e.get("is_compaction")), None)
    if older is None:
        return 0.0
    # Only an older summary went: this one covers what that one did.
    if older.get("summarized_through") is not None:
        return float(older["summarized_through"])
    return _snapshot_summarized_through(
        session_id, min(_entry_ts(older) * 1000, stamp), snap
    )


def _message_view(entry: Any) -> tuple[Any, str, list[Any]]:
    """Role, text and tool call ids of an entry's message, read off a dict
    from disk or a live engine Message."""
    msg = _field(entry, "message")
    content = _field(msg, "content")
    if isinstance(content, str):
        text = content
    else:
        text = " ".join(
            _field(block, "text") or ""
            for block in content or []
            if _field(block, "type") == "text"
        )
    calls = [_field(call, "id") for call in _field(msg, "tool_calls") or []]
    return _field(msg, "role"), text, calls


def _holds_anchor(entries: list[Any], index: int, fingerprint: Any) -> bool:
    """Whether the message at ``entries[index]``, the first one a summary
    kept, is where the renderer message ``fingerprint`` describes begins,
    rather than a later one its anchor resolves to because its own went
    into the summary.

    The renderer sends the message's role, its first tool call or the
    ends of its text (whitespace collapsed), and the stamp of the next
    user message (store.ts, ``messageFingerprint``). A user message is
    one entry. A reply begins at its first step, which a memo the turn
    opened with may precede. Either way the entry lands before the next
    user message, so an earlier message that said the same thing does
    not pass for it.
    """
    if not isinstance(fingerprint, dict):
        return False
    role = fingerprint.get("role")
    if role == "assistant":
        index = next(
            (i for i in range(index, len(entries)) if _message_view(entries[i])[0] == "assistant"),
            len(entries),
        )
    if index >= len(entries):
        return False
    entry_role, text, calls = _message_view(entries[index])
    if entry_role != role:
        return False
    next_user = fingerprint.get("nextUserCreatedAt")
    if (
        isinstance(next_user, (int, float))
        and not isinstance(next_user, bool)
        and _entry_ts(entries[index]) * 1000 >= next_user
    ):
        return False
    call_id = fingerprint.get("toolCallId")
    if call_id:
        return call_id in calls
    head, tail = fingerprint.get("textHead"), fingerprint.get("textTail")
    if not head or not isinstance(head, str) or not isinstance(tail, str):
        return False
    body = " ".join(text.split())
    return head in body and tail in body


def anchor_summarized(
    session_id: str, entries: list[Any], cutoff_ts: float, fingerprint: Any = None
) -> bool | None:
    """Whether the renderer message stamped ``cutoff_ts`` was folded into
    the transcript's compaction summary.

    The renderer keeps every message; the engine replaced the oldest ones
    with a summary entry. An anchor on one of those resolves to the first
    kept message instead, which is a different message, and the summary
    describes what came after the anchor. Only an anchor that resolves to
    the first message after the summary can be one: a later anchor has a
    kept message before it. When that summary predates
    ``summarized_through`` and no snapshot tells, the renderer's
    ``fingerprint`` of the message can still show the first kept message
    is the anchor's own (see ``_holds_anchor``); None when it does not.
    """
    summaries: list[Any] = []
    first: int | None = None
    for i, entry in enumerate(entries):
        if _field(entry, "message") is not None:
            if _entry_ts(entry) < cutoff_ts:
                return False
            first = i
            break
        if _field(entry, "is_compaction"):
            summaries.append(entry)
    through = 0.0
    for summary in summaries:
        covered = summarized_through(session_id, summary, entries)
        if covered is None:
            if first is not None and _holds_anchor(entries, first, fingerprint):
                return False
            return None
        through = max(through, covered)
    return bool(summaries) and cutoff_ts <= through


def fork_entries(
    session_id: str, entries: list[Any], cutoff_ts: float, fingerprint: Any = None
) -> list[Any]:
    """The entries a fork before ``cutoff_ts`` is cut from: the
    transcript's own, or, when compaction summarized the anchor away, the
    newest compaction snapshot that still holds it as written. Raises
    SummarizedAnchorError when none does. ``fingerprint``: see
    ``anchor_summarized``."""
    if anchor_summarized(session_id, entries, cutoff_ts, fingerprint) is False:
        return entries
    for _stamp, path in reversed(_before_snapshots(session_id)):
        snap = _snapshot_entries(path)
        if snap is None:
            continue
        if not any(
            e.get("message") is not None and _entry_ts(e) >= cutoff_ts for e in snap
        ):
            # Taken before the anchor was written; older ones are too.
            break
        if anchor_summarized(session_id, snap, cutoff_ts, fingerprint) is False:
            return snap
    raise SummarizedAnchorError(
        "Can't fork from this message: it is part of a compaction summary now, "
        "and no snapshot from before that compaction is left."
    )


# A compaction writes its last records (the complete event and log row,
# the receipt, the after-snapshot) within tens of ms of its summary
# entry; the next compaction starts at least a provider call later.
COMPACTION_RECORDS_GRACE_MS = 2_000


def compaction_records_cutoff_ms(
    session_id: str, entries: list[Any], cutoff_ms: float
) -> float:
    """Where a fork's compaction records end: its snapshots, compaction
    log rows and compaction events. ``entries`` are the fork's.

    A fork keeps the summary entry of a compaction that ran after its
    branch point when the anchor was in the kept tail. That compaction's
    records, and those of the earlier ones it folded in, are then the
    fork's history too, though they are stamped after the anchor: they
    run until the source's next compaction started.
    """
    summary = max(
        (e for e in entries if _field(e, "is_compaction")), key=_entry_ts, default=None
    )
    if summary is None:
        return cutoff_ms
    made_ms = _entry_ts(summary) * 1000
    end = made_ms + COMPACTION_RECORDS_GRACE_MS
    for stamp, _path in _before_snapshots(session_id):
        if stamp > made_ms:
            end = min(end, stamp)
            break
    return max(cutoff_ms, end)


def _is_compaction_record(row: Any) -> bool:
    return isinstance(row, dict) and str(row.get("subtype") or "").startswith("compaction_")


def clone_transcript(
    old_id: str,
    new_id: str,
    *,
    truncate_to_message_ordinal: int | None = None,
    cutoff_ts: float | None = None,
    id_remap: dict[str, str] | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    fingerprint: Any = None,
) -> bool:
    """Deep-copy a transcript on disk under a new session id.

    Used by the branch operation.

    - ``cutoff_ts`` (preferred): keep only entries created before this
      UNIX timestamp; see ``truncate_entries_before``. When compaction
      summarized the anchor away they come from a snapshot (see
      ``fork_entries``), which raises SummarizedAnchorError if none has it.
      ``fingerprint`` is the renderer's of the anchor message (see
      ``anchor_summarized``).
    - ``truncate_to_message_ordinal`` (legacy): keep the first N
      message-bearing entries. Only consulted when no ``cutoff_ts`` is
      given. Note this counts ``tool_result`` entries too, so it does
      not line up with the renderer's user/assistant numbering.
    - ``id_remap``: every exact-match old id inside the transcript
      (metadata + entries) is rewritten to its new id.
    - ``metadata_overrides``: applied last, so callers can pin e.g.
      ``project_session_id`` to a shared directory.

    The clone always records ``metadata.forked_from = old_id``. Returns
    False if the source transcript can't be read.
    """
    src = load_transcript(old_id)
    if src is None:
        return False
    # Deep copy via JSON round-trip — the structure is plain dicts +
    # lists + strings + numbers, so this is safe and avoids accidental
    # mutation of the in-memory original.
    dst = json.loads(json.dumps(src))
    transcript = dst.get("transcript") or {}
    entries = transcript.get("entries") or []
    if cutoff_ts is not None:
        kept, new_head = truncate_entries_before(
            fork_entries(old_id, entries, cutoff_ts, fingerprint), cutoff_ts
        )
        transcript["entries"] = kept
        transcript["head_id"] = new_head
        dst["transcript"] = transcript
    elif truncate_to_message_ordinal is not None:
        kept = []
        new_head = None
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
    if id_remap:
        dst = remap_ids(dst, id_remap)
    dst["session_id"] = new_id
    metadata = dst.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["forked_from"] = old_id
    if metadata_overrides:
        metadata.update(metadata_overrides)
    dst["metadata"] = metadata
    save_transcript(new_id, dst)
    return True


# ── Sidecar + project-directory cloning ──────────────────────────────
#
# A session is more than its transcript. The files below all hang off
# the session id and are what the branch operation copies alongside the
# transcript so the fork can be resumed, re-woken and browsed like the
# original.


def _events_path(session_id: str) -> Path:
    # Mirrors freyja_bridge._session_event_path (same rule as the
    # renderer's sanitizeId: replace, don't strip).
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", session_id)[:160]
    return SESSIONS_DIR / f"{safe}.events.jsonl"


def _journal_path(session_id: str, suffix: str) -> Path:
    # Mirrors bridge.task_journal.journal_path / kanban_journal.journal_path
    # (which STRIP invalid chars). Kept local so this module doesn't import
    # the journals.
    safe = "".join(c for c in session_id if c.isalnum() or c in "_-.")[:160]
    return SESSIONS_DIR / f"{safe}{suffix}"


def _row_ms(row: Any, *fields: str) -> float | None:
    if not isinstance(row, dict):
        return None
    for f in fields:
        v = row.get(f)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _clone_json_file(
    src: Path,
    dst: Path,
    *,
    id_remap: dict[str, str],
    transform: Any = None,
) -> bool:
    if not src.exists():
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("branch: cannot read %s", src)
        return False
    data = remap_ids(data, id_remap)
    if transform is not None:
        data = transform(data)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(dst)
        return True
    except Exception:
        logger.exception("branch: cannot write %s", dst)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _clone_jsonl_file(
    src: Path,
    dst: Path,
    *,
    id_remap: dict[str, str],
    cutoff_ms: float | None = None,
    ts_fields: tuple[str, ...] = (),
    records_cutoff_ms: float | None = None,
) -> bool:
    """Copy a JSONL file with ids remapped. When ``cutoff_ms`` and
    ``ts_fields`` are given, rows stamped at/after the cutoff are
    dropped; rows without a recognizable stamp are kept. Compaction rows
    are held to ``records_cutoff_ms`` when it is given (see
    ``compaction_records_cutoff_ms``)."""
    if not src.exists():
        return False
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open("r", encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
            for line in fin:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    # Preserve unparseable lines verbatim — they're
                    # someone else's problem, not ours to lose.
                    fout.write(line if line.endswith("\n") else line + "\n")
                    continue
                if cutoff_ms is not None and ts_fields:
                    limit = cutoff_ms
                    if records_cutoff_ms is not None and _is_compaction_record(row):
                        limit = records_cutoff_ms
                    stamp = _row_ms(row, *ts_fields)
                    if stamp is not None and stamp >= limit:
                        continue
                fout.write(jsonl_line(remap_ids(row, id_remap)) + "\n")
        tmp.replace(dst)
        return True
    except Exception:
        logger.exception("branch: cannot clone %s", src)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def clone_session_sidecars(
    old_id: str,
    new_id: str,
    *,
    id_remap: dict[str, str],
    cutoff_ms: float | None = None,
    records_cutoff_ms: float | None = None,
) -> list[str]:
    """Clone every per-session sidecar file from ``old_id`` to ``new_id``.

    Covers ``.goal.json`` (goal loop + judge rules), ``.inbox.json``
    (queued/delivered messages), ``.subagent.json`` (re-wake record),
    ``.events.jsonl`` (renderer event mirror), ``.tasks.jsonl`` and
    ``.kanban.jsonl`` (append-only journals replayed on resume), and a
    sub-agent's raw message, compaction and recovered logs. Ids are
    rewritten via ``id_remap``. With ``cutoff_ms`` (renderer ms), rows
    that carry a timestamp at/after the cutoff are dropped from the
    inbox and journals; the event mirror has no timestamps and is
    copied whole. Compaction snapshots and compaction events are held to
    ``records_cutoff_ms`` instead when it is given (see
    ``compaction_records_cutoff_ms``). Returns the list of suffixes that
    were cloned.
    """
    cloned: list[str] = []

    def _inbox_transform(data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data["sessionId"] = new_id
        if cutoff_ms is not None:
            for key in ("unread", "delivered"):
                rows = data.get(key)
                if isinstance(rows, list):
                    data[key] = [
                        r
                        for r in rows
                        if (_row_ms(r, "timestamp") or 0.0) < cutoff_ms
                    ]
        return data

    def _stamp_session(data: Any) -> Any:
        if isinstance(data, dict):
            data["sessionId"] = new_id
        return data

    json_jobs: list[tuple[str, Path, Path, Any]] = [
        (".goal.json", _goal_path(old_id), _goal_path(new_id), _stamp_session),
        (".inbox.json", _inbox_path(old_id), _inbox_path(new_id), _inbox_transform),
        (".subagent.json", _subagent_path(old_id), _subagent_path(new_id), _stamp_session),
    ]
    for suffix, src, dst, transform in json_jobs:
        if _clone_json_file(src, dst, id_remap=id_remap, transform=transform):
            cloned.append(suffix)

    # (suffix, source, destination, stamp fields, ms in one unit of them)
    jsonl_jobs: list[tuple[str, Path, Path, tuple[str, ...], int]] = [
        # Event lines are stamped ``_t`` (ms) by the bridge's mirror
        # writer; lines from before that stamp existed carry no ``_t``
        # and are kept (see _clone_jsonl_file).
        (".events.jsonl", _events_path(old_id), _events_path(new_id), ("_t",), 1),
        (
            ".tasks.jsonl",
            _journal_path(old_id, ".tasks.jsonl"),
            _journal_path(new_id, ".tasks.jsonl"),
            ("ts",),
            1,
        ),
        (
            ".kanban.jsonl",
            _journal_path(old_id, ".kanban.jsonl"),
            _journal_path(new_id, ".kanban.jsonl"),
            ("ts",),
            1,
        ),
    ]
    # A sub-agent's logs, which its export reads: its raw messages, its
    # compactions, and the rows scripts/backfill_raw_messages.py recovered.
    # Stamped ``ts`` in seconds.
    for name in ("raw_messages.jsonl", "compactions.jsonl", "raw_messages.recovered.jsonl"):
        jsonl_jobs.append((
            f".{name}",
            SESSIONS_DIR / f"{_sanitize_session_id(old_id)}.{name}",
            SESSIONS_DIR / f"{_sanitize_session_id(new_id)}.{name}",
            ("ts",),
            1000,
        ))
    for suffix, src, dst, ts_fields, unit_ms in jsonl_jobs:
        if _clone_jsonl_file(
            src,
            dst,
            id_remap=id_remap,
            cutoff_ms=cutoff_ms / unit_ms if cutoff_ms is not None and ts_fields else None,
            ts_fields=ts_fields,
            records_cutoff_ms=records_cutoff_ms / unit_ms if records_cutoff_ms is not None else None,
        ):
            cloned.append(suffix)
    if clone_compaction_snapshots(
        old_id,
        new_id,
        id_remap=id_remap,
        cutoff_ms=records_cutoff_ms if records_cutoff_ms is not None else cutoff_ms,
    ) > 0:
        cloned.append("compactions/")
    return cloned


def _compaction_safe_id(session_id: str) -> str:
    # Mirrors freyja_bridge._persist_compaction_snapshot's sanitizer
    # (strip, not replace; 120 chars).
    return "".join(c for c in session_id if c.isalnum() or c in ("-", "_", "."))[:120]


def clone_compaction_snapshots(
    old_id: str,
    new_id: str,
    *,
    id_remap: dict[str, str],
    cutoff_ms: float | None = None,
) -> int:
    """Copy the before/after compaction snapshots
    (``sessions/compactions/<id>-<ms>-<phase>.{md,json}``) to the fork's
    id. The stamp in the filename is the compaction's wall-clock ms, so
    a branch-before-message keeps only snapshots taken before
    ``cutoff_ms``. JSON snapshots get ids remapped structurally; markdown ones
    get a plain text substitution of each old id. Returns the number of
    files written.
    """
    root = SESSIONS_DIR / "compactions"
    if not root.is_dir():
        return 0
    old_safe = _compaction_safe_id(old_id)
    new_safe = _compaction_safe_id(new_id)
    pattern = re.compile(
        rf"^{re.escape(old_safe)}-(\d+)-([A-Za-z_]+)\.(md|json)$"
    )
    written = 0
    for path in sorted(root.iterdir()):
        m = pattern.match(path.name)
        if not m:
            continue
        stamp, phase, ext = m.groups()
        if cutoff_ms is not None and float(stamp) >= cutoff_ms:
            continue
        dst = root / f"{new_safe}-{stamp}-{phase}.{ext}"
        try:
            if ext == "json":
                data = json.loads(path.read_text(encoding="utf-8"))
                dst.write_text(
                    json.dumps(remap_ids(data, id_remap), separators=(",", ":")),
                    encoding="utf-8",
                )
            else:
                text = path.read_text(encoding="utf-8")
                for old, new in id_remap.items():
                    if old:
                        text = text.replace(old, new)
                dst.write_text(text, encoding="utf-8")
            written += 1
        except Exception:
            logger.exception("branch: cannot clone compaction snapshot %s", path)
    return written


# Project-directory files whose rows carry ids and/or creation stamps:
# the stamp fields, and how many ms one unit of them is.
_PROJECT_JSONL = (
    ("manifest.jsonl", ("createdAt",), 1),
    ("action_ledger.jsonl", ("createdAt",), 1),
    # The bridge's _append_compaction_log stamps ``ts`` in seconds.
    ("compactions.jsonl", ("ts",), 1000),
    # A root session's raw messages, and the rows
    # scripts/backfill_raw_messages.py recovered: ``ts`` in seconds too.
    ("raw_messages.jsonl", ("ts",), 1000),
    ("raw_messages.recovered.jsonl", ("ts",), 1000),
)
_PROJECT_JSON = ("working_memory.json",)
# The dir owner's own logs. A sub-agent, or a session opened on another
# session's dir, keeps its logs beside its sidecars instead.
_PROJECT_LOGS = ("compactions.jsonl", "raw_messages.jsonl", "raw_messages.recovered.jsonl")


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree. On macOS/APFS use ``cp -c`` so the copy is
    a copy-on-write clone (instant, no extra space until a file
    diverges); fall back to shutil elsewhere or on failure."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["cp", "-cR", str(src), str(dst)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            return
        except Exception:
            try:
                shutil.rmtree(dst, ignore_errors=True)
            except Exception:
                pass
    shutil.copytree(src, dst, symlinks=True)


def clone_project_dir(
    src_dir: Path,
    dst_dir: Path,
    *,
    id_remap: dict[str, str],
    cutoff_ms: float | None = None,
    records_cutoff_ms: float | None = None,
    with_logs: bool = True,
) -> bool:
    """Clone a session's project output directory (artifacts, manifest,
    action ledger, working memory, compaction log, raw message logs) for a
    fork.

    The tree is copied as-is, then the ledger-style files are rewritten
    with ids remapped and — when ``cutoff_ms`` is given — rows created
    at/after the cutoff dropped (compaction rows: at/after
    ``records_cutoff_ms`` when it is given). Artifact files themselves are never
    deleted (a file written after the branch point stays on disk; only
    its manifest row is dropped). Without ``with_logs`` the logs are left
    out: the source works in another session's dir, so they are that
    session's, and ``clone_session_sidecars`` copies the source's own.
    Returns False when there is nothing to clone or the destination
    already exists.
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if not src_dir.is_dir():
        return False
    if dst_dir.exists():
        logger.warning("branch: project dir %s already exists, not overwriting", dst_dir)
        return False
    try:
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(src_dir, dst_dir)
    except Exception:
        logger.exception("branch: project dir copy failed %s -> %s", src_dir, dst_dir)
        return False
    for name, ts_fields, unit_ms in _PROJECT_JSONL:
        path = dst_dir / name
        if not with_logs and name in _PROJECT_LOGS:
            path.unlink(missing_ok=True)
        elif path.exists():
            _clone_jsonl_file(
                path,
                path,
                id_remap=id_remap,
                cutoff_ms=cutoff_ms / unit_ms if cutoff_ms is not None else None,
                ts_fields=ts_fields,
                records_cutoff_ms=(
                    records_cutoff_ms / unit_ms if records_cutoff_ms is not None else None
                ),
            )
    for name in _PROJECT_JSON:
        path = dst_dir / name
        if path.exists():
            _clone_json_file(path, path, id_remap=id_remap)
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
