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
from pathlib import Path
from typing import Any

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


def save_inbox_state(session_id: str, data: dict[str, Any]) -> None:
    """Persist a session's inbox queue + recent delivered history.

    Schema mirrors SessionInbox.to_dict(); see bridge/inbox.py.
    Writes atomically; safe across crashes.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
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


def load_inbox_state(session_id: str) -> dict[str, Any] | None:
    """Load a session's inbox from disk. Returns None if absent/corrupt."""
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
    and kept. Returns ``(kept, head_id)``.
    """
    kept: list[Any] = []
    head: str | None = None
    for entry in entries:
        ts = entry.get("timestamp")
        try:
            ts_f = float(ts) if ts is not None else 0.0
        except (TypeError, ValueError):
            ts_f = 0.0
        if ts_f >= cutoff_ts:
            break
        kept.append(entry)
        entry_id = entry.get("id")
        if entry_id is not None:
            head = str(entry_id)
    return kept, head


def clone_transcript(
    old_id: str,
    new_id: str,
    *,
    truncate_to_message_ordinal: int | None = None,
    cutoff_ts: float | None = None,
    id_remap: dict[str, str] | None = None,
    metadata_overrides: dict[str, Any] | None = None,
) -> bool:
    """Deep-copy a transcript on disk under a new session id.

    Used by the branch operation.

    - ``cutoff_ts`` (preferred): keep only entries created before this
      UNIX timestamp; see ``truncate_entries_before``.
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
        kept, new_head = truncate_entries_before(entries, cutoff_ts)
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
) -> bool:
    """Copy a JSONL file with ids remapped. When ``cutoff_ms`` and
    ``ts_fields`` are given, rows stamped at/after the cutoff are
    dropped; rows without a recognizable stamp are kept."""
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
                    stamp = _row_ms(row, *ts_fields)
                    if stamp is not None and stamp >= cutoff_ms:
                        continue
                fout.write(json.dumps(remap_ids(row, id_remap), ensure_ascii=False) + "\n")
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
) -> list[str]:
    """Clone every per-session sidecar file from ``old_id`` to ``new_id``.

    Covers ``.goal.json`` (goal loop + judge rules), ``.inbox.json``
    (queued/delivered messages), ``.subagent.json`` (re-wake record),
    ``.events.jsonl`` (renderer event mirror), ``.tasks.jsonl`` and
    ``.kanban.jsonl`` (append-only journals replayed on resume). Ids are
    rewritten via ``id_remap``. With ``cutoff_ms`` (renderer ms), rows
    that carry a timestamp at/after the cutoff are dropped from the
    inbox and journals; the event mirror has no timestamps and is
    copied whole. Returns the list of suffixes that were cloned.
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

    jsonl_jobs: list[tuple[str, Path, Path, tuple[str, ...]]] = [
        # Event lines are stamped ``_t`` (ms) by the bridge's mirror
        # writer; lines from before that stamp existed carry no ``_t``
        # and are kept (see _clone_jsonl_file).
        (".events.jsonl", _events_path(old_id), _events_path(new_id), ("_t",)),
        (
            ".tasks.jsonl",
            _journal_path(old_id, ".tasks.jsonl"),
            _journal_path(new_id, ".tasks.jsonl"),
            ("ts",),
        ),
        (
            ".kanban.jsonl",
            _journal_path(old_id, ".kanban.jsonl"),
            _journal_path(new_id, ".kanban.jsonl"),
            ("ts",),
        ),
    ]
    for suffix, src, dst, ts_fields in jsonl_jobs:
        if _clone_jsonl_file(
            src,
            dst,
            id_remap=id_remap,
            cutoff_ms=cutoff_ms if ts_fields else None,
            ts_fields=ts_fields,
        ):
            cloned.append(suffix)
    if clone_compaction_snapshots(old_id, new_id, id_remap=id_remap, cutoff_ms=cutoff_ms) > 0:
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
    a branch-before-message keeps only snapshots taken before the branch
    point. JSON snapshots get ids remapped structurally; markdown ones
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


# Project-directory files whose rows carry ids and/or creation stamps.
_PROJECT_JSONL = (
    ("manifest.jsonl", ("createdAt",)),
    ("action_ledger.jsonl", ("createdAt",)),
    ("compactions.jsonl", ("createdAt", "ts")),
)
_PROJECT_JSON = ("working_memory.json",)


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
) -> bool:
    """Clone a session's project output directory (artifacts, manifest,
    action ledger, working memory, compaction log) for a fork.

    The tree is copied as-is, then the ledger-style files are rewritten
    with ids remapped and — when ``cutoff_ms`` is given — rows created
    at/after the cutoff dropped. Artifact files themselves are never
    deleted (a file written after the branch point stays on disk; only
    its manifest row is dropped). Returns False when there is nothing
    to clone or the destination already exists.
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
    for name, ts_fields in _PROJECT_JSONL:
        path = dst_dir / name
        if path.exists():
            _clone_jsonl_file(
                path,
                path,
                id_remap=id_remap,
                cutoff_ms=cutoff_ms,
                ts_fields=ts_fields,
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
