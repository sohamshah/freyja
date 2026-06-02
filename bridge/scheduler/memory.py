"""Per-job working memory across scheduled fires.

The memory layer is intentionally minimal:

  · One free-form notes file (``notes.md``) that the agent reads at
    fire start and edits during the run using its existing file tools
    (``read_file`` / ``edit_file`` / ``write_file`` / ``bash``). No
    scheduler-specific tools are added — the agent already has
    everything it needs.

  · Per-run deltas captured by snapshotting the notes file before the
    turn and diffing afterwards. These small deltas (``deltas/{run_id}.notes``)
    are what get injected as "recent run notes" on the NEXT fire,
    which lets the agent see the last N runs at low cost without
    re-reading the full notes file.

Storage layout (per job):

  ~/.freyja/schedules/memory/{job_id}/
    notes.md             — the cumulative working notes (free-form)
    deltas/
      {run_id}.notes     — what got appended to notes.md this fire
      {run_id}.snapshot  — pre-turn notes snapshot (for diffing)

No caps by default — the user explicitly asked for no arbitrary
limits. ``MemorySpec.max_notes_chars`` lets them opt into one.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.scheduler.models import JobRecord, MemorySpec

logger = logging.getLogger("freyja.scheduler.memory")


def memory_root() -> Path:
    base = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    return Path(base) / "schedules" / "memory"


def job_memory_dir(job_id: str) -> Path:
    p = memory_root() / job_id
    p.mkdir(parents=True, exist_ok=True)
    (p / "deltas").mkdir(parents=True, exist_ok=True)
    return p


def notes_path_for(job: "JobRecord") -> Path:
    """Resolve the notes path for a job. Honors an explicit override on
    ``MemorySpec.notes_path`` (so users can route notes to a project
    folder under version control); otherwise auto-allocates per job."""
    override = getattr(getattr(job, "memory", None), "notes_path", None)
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override))).resolve()
    return job_memory_dir(job.id) / "notes.md"


def deltas_dir_for(job: "JobRecord") -> Path:
    """Per-job deltas dir. Lives next to the default notes file even
    when notes_path was overridden — the deltas are scheduler
    internals, not part of the user's authoritative notes file."""
    return job_memory_dir(job.id) / "deltas"


# ─── Read paths ────────────────────────────────────────────────────────


def read_notes(job: "JobRecord") -> str:
    """Full contents of the working notes. Returns an empty string when
    the file doesn't exist yet (first fire) or can't be read."""
    if not getattr(job.memory, "enabled", True):
        return ""
    path = notes_path_for(job)
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("read_notes failed for %s: %s", job.id, exc)
        return ""


def read_recent_deltas(job: "JobRecord", *, limit: int) -> list[dict[str, str]]:
    """Most-recent-first list of ``{run_id, text, ts}`` entries.
    ``limit <= 0`` returns []. Missing dir returns []."""
    if limit <= 0:
        return []
    d = deltas_dir_for(job)
    if not d.exists():
        return []
    files = [p for p in d.glob("*.notes") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, str]] = []
    for p in files[:limit]:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append({
            "run_id": p.stem,
            "text": text,
            "ts": p.stat().st_mtime,  # type: ignore[dict-item]
        })
    return out


# ─── Snapshot / delta capture ──────────────────────────────────────────


def snapshot_notes(job: "JobRecord", run_id: str) -> str:
    """Capture the current notes content + mtime BEFORE the agent's
    turn runs, so the post-turn diff knows what's new. Stores a copy
    under ``deltas/{run_id}.snapshot`` for the post-turn diff and
    returns the snapshot text."""
    if not getattr(job.memory, "enabled", True):
        return ""
    current = read_notes(job)
    try:
        snap = deltas_dir_for(job) / f"{run_id}.snapshot"
        snap.write_text(current, encoding="utf-8")
    except OSError as exc:
        logger.debug("snapshot_notes write failed: %s", exc)
    return current


def capture_notes_delta(job: "JobRecord", run_id: str) -> str:
    """After the turn finishes, diff the current notes file against the
    pre-turn snapshot and persist the delta. Returns the captured
    delta text (or empty string when nothing changed). The persisted
    delta file is what the NEXT fire reads via ``read_recent_deltas``.
    """
    if not getattr(job.memory, "enabled", True):
        return ""
    after = read_notes(job)
    snap_path = deltas_dir_for(job) / f"{run_id}.snapshot"
    before = ""
    if snap_path.exists():
        try:
            before = snap_path.read_text(encoding="utf-8")
        except OSError:
            before = ""
    delta = _compute_delta(before, after)
    if not delta.strip():
        # Clean up the snapshot so we don't leak files for fires that
        # didn't update notes — keeps deltas/ scannable.
        try:
            snap_path.unlink()
        except OSError:
            pass
        return ""
    try:
        out = deltas_dir_for(job) / f"{run_id}.notes"
        out.write_text(delta, encoding="utf-8")
    except OSError as exc:
        logger.warning("capture_notes_delta write failed for %s: %s", job.id, exc)
    return delta


def _compute_delta(before: str, after: str) -> str:
    """Cheap delta: lines present in ``after`` that aren't present
    verbatim in ``before``. Doesn't try to be a real diff — that would
    capture noise (whitespace, reorder). The agent's notes update
    pattern is "append a section"; this catches that cleanly.

    If ``after`` is longer than ``before`` and starts with it, we
    return the suffix (the natural append case). Otherwise we fall
    back to the line-set comparison.
    """
    if not before:
        return after
    if after.startswith(before):
        return after[len(before):].lstrip("\n")
    before_lines = set(before.splitlines())
    new_lines = [
        line for line in after.splitlines()
        if line not in before_lines and line.strip()
    ]
    return "\n".join(new_lines)


# ─── Prompt-injection helpers ──────────────────────────────────────────


def render_memory_for_prompt(job: "JobRecord") -> str | None:
    """Build the memory section of the fire-time prompt. Returns None
    when memory is disabled or empty so the caller can skip the
    section header cleanly."""
    memspec = getattr(job, "memory", None)
    if memspec is None or not getattr(memspec, "enabled", True):
        return None
    parts: list[str] = []
    notes = read_notes(job)
    if notes:
        cap = getattr(memspec, "max_notes_chars", None)
        if cap and cap > 0 and len(notes) > cap:
            # Trim the OLDEST content, preserve the most recent. The
            # agent's append pattern means newer notes are at the tail.
            notes = "...[older notes truncated to fit max_notes_chars]...\n\n" + notes[-cap:]
        parts.append("## Working notes (your accumulated learnings)\n\n" + notes)
    n = max(0, int(getattr(memspec, "include_last_n_deltas", 3) or 0))
    if n > 0:
        deltas = read_recent_deltas(job, limit=n)
        if deltas:
            blocks = []
            for d in deltas:
                blocks.append(f"### run `{d['run_id']}`\n{d['text']}")
            parts.append("## What you added in recent runs\n\n" + "\n\n".join(blocks))
    if not parts:
        return None
    return "\n\n".join(parts)


# ─── Direct manipulation (used by tool actions) ────────────────────────


def append_notes(job: "JobRecord", text: str) -> Path:
    """Append ``text`` to the notes file. Creates the file if needed.
    Returns the notes path."""
    path = notes_path_for(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        if path.exists() and path.stat().st_size > 0:
            f.write("\n\n")
        f.write(text.rstrip())
        f.write("\n")
    return path


def overwrite_notes(job: "JobRecord", text: str) -> Path:
    path = notes_path_for(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
