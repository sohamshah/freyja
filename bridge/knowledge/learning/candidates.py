"""On-disk representation of drafter candidates.

The drafter (Phase 2 of the skill-learning loop) emits one YAML file per
candidate into ``~/.freyja/skills/.candidates/<uuid>.yaml`` awaiting
operator confirmation. Files the operator rejects move to
``~/.freyja/skills/.rejected/<uuid>.yaml`` and form the v1 negative
library: the drafter consults a short excerpt of recent rejections so it
doesn't keep proposing the same shape twice in a row.

Schema (one YAML doc per file)
──────────────────────────────

    candidate_id: <uuid hex>
    drafted_at:   <int ms epoch>
    source_session_id: <session id>
    source_turn_id:    <turn id or "">
    drafter_model:     <model id>
    decision:          save | skip
    rationale:         <one-line refusal reason if decision == skip>
    guard_verdict:     safe | caution | dangerous
    guard_findings:    [<dict from skills_guard.Finding.to_dict()>, ...]
    candidate:
      name:        <kebab-case>
      description: <one-line>
      skill_type:  build | guard | reference | workflow
      triggers:    [<str>, ...]
      tags:        [<str>, ...]
      body: |
        <markdown body>

Rejected files additionally carry:

    rejected_at: <int ms epoch>
    rejected_by: <actor — "operator" | "guard" | "auto">
    rejection_reason: <one-line>

Atomic write semantics: every write goes through ``tempfile.mkstemp`` +
``os.replace`` in the same directory as the destination so a partial
file is never observable by another reader. Best-effort failures are
logged + swallowed; raising would break the live drafter path.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bridge.knowledge.learning.paths import (
    candidates_dir,
    ensure_loop_dirs,
    rejected_dir,
)

logger = logging.getLogger(__name__)


# ── Dataclass ──


@dataclass
class Candidate:
    """A drafter-emitted skill candidate.

    Mirrors the YAML schema 1:1. ``guard_findings`` is the raw
    ``Finding.to_dict()`` form so we never have to re-import the guard
    module to read a candidate file back.

    The candidate body intentionally lives at the dataclass top level
    (not nested in a sub-object) so calling code reads
    ``candidate.name`` instead of ``candidate.candidate.name``. The
    nesting only appears in the on-disk YAML to keep the file
    self-describing and to match Hermes' candidate format.
    """

    candidate_id: str
    drafted_at: int
    source_session_id: str
    source_turn_id: str
    drafter_model: str
    decision: str            # "save" | "skip"
    rationale: str           # populated when decision == "skip"
    guard_verdict: str       # "safe" | "caution" | "dangerous"
    guard_findings: list[dict[str, Any]]
    name: str
    description: str
    triggers: list[str]
    tags: list[str]
    body: str
    skill_type: str = "build"   # "build" | "guard" | "reference" | "workflow"


# ── Serialization ──


def _now_ms() -> int:
    return int(time.time() * 1000)


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    """Render the dataclass into the on-disk schema (with the nested
    ``candidate`` sub-object). Field order is preserved by yaml.safe_dump
    with ``sort_keys=False`` so the resulting file reads in the natural
    "metadata first, then payload" order."""
    return {
        "candidate_id": c.candidate_id,
        "drafted_at": int(c.drafted_at),
        "source_session_id": c.source_session_id,
        "source_turn_id": c.source_turn_id or "",
        "drafter_model": c.drafter_model,
        "decision": c.decision,
        "rationale": c.rationale or "",
        "guard_verdict": c.guard_verdict,
        "guard_findings": list(c.guard_findings or []),
        "candidate": {
            "name": c.name,
            "description": c.description,
            "skill_type": c.skill_type or "build",
            "triggers": list(c.triggers or []),
            "tags": list(c.tags or []),
            "body": c.body or "",
        },
    }


def _candidate_from_dict(data: dict[str, Any]) -> Candidate | None:
    """Inverse of ``_candidate_to_dict``. Returns ``None`` when the file
    is missing required fields — the reader treats that as "skip this
    one" rather than failing the whole listing pass."""
    if not isinstance(data, dict):
        return None
    inner = data.get("candidate")
    if not isinstance(inner, dict):
        return None
    cid = data.get("candidate_id")
    name = inner.get("name")
    if not isinstance(cid, str) or not cid:
        return None
    if not isinstance(name, str) or not name:
        return None
    try:
        return Candidate(
            candidate_id=cid,
            drafted_at=int(data.get("drafted_at") or 0),
            source_session_id=str(data.get("source_session_id") or ""),
            source_turn_id=str(data.get("source_turn_id") or ""),
            drafter_model=str(data.get("drafter_model") or ""),
            decision=str(data.get("decision") or "save"),
            rationale=str(data.get("rationale") or ""),
            guard_verdict=str(data.get("guard_verdict") or "safe"),
            guard_findings=list(data.get("guard_findings") or []),
            name=name,
            description=str(inner.get("description") or ""),
            skill_type=str(inner.get("skill_type") or "build"),
            triggers=list(inner.get("triggers") or []),
            tags=list(inner.get("tags") or []),
            body=str(inner.get("body") or ""),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("candidate.parse failed: %s", exc)
        return None


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Atomic YAML write via tempfile in the destination's directory.

    Matches ``value_score._persist_rollup``: mkstemp in the same
    directory so ``os.replace`` is a same-filesystem rename (atomic on
    POSIX). On any failure mid-write the temp file is unlinked so we
    never leak ``.tmp`` debris.
    """
    ensure_loop_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            # sort_keys=False: preserve the metadata-first ordering.
            # allow_unicode=True: keep operator-facing markdown readable.
            # default_flow_style=False: emit block style (more diffable).
            yaml.safe_dump(
                payload,
                fp,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Writers ──


def write_pending(c: Candidate) -> Path:
    """Serialize a candidate awaiting operator confirmation.

    Returns the destination path. Raises ``OSError`` on a hard write
    failure — the drafter caller swallows + logs because we can't
    silently lose a candidate (it would never show up in the operator
    UI) but we also won't take down the whole bridge.
    """
    ensure_loop_dirs()
    path = candidates_dir() / f"{c.candidate_id}.yaml"
    payload = _candidate_to_dict(c)
    _atomic_write_yaml(path, payload)
    logger.info(
        "candidate.write_pending name=%s id=%s verdict=%s",
        c.name, c.candidate_id, c.guard_verdict,
    )
    return path


def write_rejected(c: Candidate, reason: str, actor: str) -> Path:
    """Serialize a rejected candidate into the negative-library
    directory.

    ``reason`` is a single-line rejection rationale shown back to the
    drafter on subsequent runs. ``actor`` is who rejected it —
    typically one of ``"operator"``, ``"guard"`` (auto-rejected on
    dangerous verdict), or ``"auto"`` (rate-limited / dedup).
    """
    ensure_loop_dirs()
    path = rejected_dir() / f"{c.candidate_id}.yaml"
    payload = _candidate_to_dict(c)
    payload["rejected_at"] = _now_ms()
    payload["rejected_by"] = actor or "unknown"
    payload["rejection_reason"] = (reason or "").strip()
    _atomic_write_yaml(path, payload)
    logger.info(
        "candidate.write_rejected name=%s id=%s actor=%s",
        c.name, c.candidate_id, actor,
    )
    return path


# ── Readers ──


def _read_yaml(path: Path) -> dict[str, Any] | None:
    """Read one candidate YAML file. Returns ``None`` on read or parse
    failure — callers treat missing/garbled files as "skip" rather than
    aborting a listing pass."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("candidate.read failed path=%s err=%s", path, exc)
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("candidate.parse_yaml failed path=%s err=%s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_pending() -> list[Candidate]:
    """Read every candidate awaiting confirmation.

    Sorted by ``drafted_at`` descending so the most recent candidates
    surface first in the operator UI. Malformed files are silently
    skipped (best-effort; the negative library should never block the
    happy path)."""
    out: list[Candidate] = []
    d = candidates_dir()
    if not d.exists():
        return out
    try:
        entries = sorted(d.glob("*.yaml"))
    except OSError:
        return out
    for path in entries:
        data = _read_yaml(path)
        if data is None:
            continue
        c = _candidate_from_dict(data)
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: c.drafted_at, reverse=True)
    return out


def get_pending(candidate_id: str) -> Candidate | None:
    """Look up a single candidate by id. ``None`` if the file is
    missing, unreadable, or fails schema validation."""
    if not candidate_id:
        return None
    path = candidates_dir() / f"{candidate_id}.yaml"
    if not path.exists():
        return None
    data = _read_yaml(path)
    if data is None:
        return None
    return _candidate_from_dict(data)


def delete_pending(candidate_id: str) -> bool:
    """Remove a candidate from the pending directory. Returns True iff
    a file was actually deleted. Used by the confirmation flow on both
    accept (after promotion to SKILL.md) and reject (after moving to
    .rejected/)."""
    if not candidate_id:
        return False
    path = candidates_dir() / f"{candidate_id}.yaml"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("candidate.delete failed id=%s err=%s", candidate_id, exc)
        return False


# ── Negative library ──


def negative_library_excerpt(limit: int = 10) -> str:
    """Render a short text excerpt of recently-rejected candidates.

    The drafter prompt includes this verbatim so the model can avoid
    re-proposing the same shape. Format is deliberately terse — one
    line per rejection, ``- <name>: <rejection_reason>`` — so it fits
    in the prompt budget even when the rejected directory is large.

    Sort order is rejected_at descending. Falls back to drafted_at if
    the file pre-dates the rejection-metadata fields. Returns an empty
    string when no rejections exist so the prompt builder can use the
    result directly without a special-case.
    """
    if limit <= 0:
        return ""
    d = rejected_dir()
    if not d.exists():
        return ""

    try:
        paths = list(d.glob("*.yaml"))
    except OSError:
        return ""

    entries: list[tuple[int, str, str]] = []
    for path in paths:
        data = _read_yaml(path)
        if data is None:
            continue
        ts = data.get("rejected_at") or data.get("drafted_at") or 0
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            ts_int = 0
        inner = data.get("candidate") or {}
        name = ""
        if isinstance(inner, dict):
            name = str(inner.get("name") or "")
        if not name:
            name = str(data.get("candidate_id") or "")[:8]
        reason = str(
            data.get("rejection_reason")
            or data.get("rationale")
            or ""
        ).strip()
        # Collapse to one line so the prompt stays compact.
        reason = reason.replace("\n", " ").replace("\r", " ").strip()
        if len(reason) > 200:
            reason = reason[:197] + "..."
        entries.append((ts_int, name, reason))

    if not entries:
        return ""

    entries.sort(key=lambda e: e[0], reverse=True)
    lines = []
    for _ts, name, reason in entries[:limit]:
        if reason:
            lines.append(f"- {name}: {reason}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)
