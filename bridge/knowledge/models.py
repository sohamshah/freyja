"""Shared data models for Freyja's file-backed knowledge layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _millis(ts: float | int | None) -> int:
    if not ts:
        return 0
    # Treat already-millisecond values as-is.
    if ts > 10_000_000_000:
        return int(ts)
    return int(float(ts) * 1000)


@dataclass
class MemoryRevision:
    """One entry in a memory's audit trail. Recorded every time the
    memory is created, updated, archived/restored, or merged."""
    ts: int
    session_id: str
    actor: str
    action: str  # 'create' | 'update' | 'archive' | 'restore' | 'merge_into' | 'merge_from'
    note: str = ""
    # Snapshot of prior values for diffing in the audit UI. Optional;
    # only populated when the change overwrote something meaningful.
    prev_text: str = ""
    prev_kind: str = ""
    prev_scope: str = ""

    def to_json(self) -> dict[str, Any]:
        out = {
            "ts": self.ts,
            "session_id": self.session_id,
            "actor": self.actor,
            "action": self.action,
        }
        if self.note:
            out["note"] = self.note
        if self.prev_text:
            out["prev_text"] = self.prev_text
        if self.prev_kind:
            out["prev_kind"] = self.prev_kind
        if self.prev_scope:
            out["prev_scope"] = self.prev_scope
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryRevision":
        return cls(
            ts=_millis(raw.get("ts") or 0),
            session_id=str(raw.get("session_id") or ""),
            actor=str(raw.get("actor") or ""),
            action=str(raw.get("action") or "update"),
            note=str(raw.get("note") or ""),
            prev_text=str(raw.get("prev_text") or ""),
            prev_kind=str(raw.get("prev_kind") or ""),
            prev_scope=str(raw.get("prev_scope") or ""),
        )


@dataclass
class MemoryItem:
    id: str
    scope: str
    kind: str
    text: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    path: str = ""
    confidence: str = "active"
    created_at: int = 0
    updated_at: int = 0
    # Audit trail — who created this and every change since.
    created_by_session: str = ""
    created_by_actor: str = ""
    revisions: list[MemoryRevision] = field(default_factory=list)
    # Explicit links: this entry replaces the ids in `supersedes`.
    supersedes: list[str] = field(default_factory=list)
    # If this entry has itself been replaced, the new entry's id.
    superseded_by: str = ""
    # Soft delete — hidden from list_items + tool listings, but kept
    # in the JSONL so the audit trail is intact.
    archived: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryItem":
        text = str(raw.get("text") or raw.get("preference") or "").strip()
        summary = str(raw.get("summary") or text[:120]).strip()
        revisions_raw = raw.get("revisions") or []
        revisions: list[MemoryRevision] = []
        if isinstance(revisions_raw, list):
            for r in revisions_raw:
                if isinstance(r, dict):
                    revisions.append(MemoryRevision.from_dict(r))
        return cls(
            id=str(raw.get("id") or raw.get("memory_id") or ""),
            scope=str(raw.get("scope") or "user"),
            kind=str(raw.get("kind") or raw.get("category") or "note"),
            text=text,
            summary=summary,
            tags=_as_list(raw.get("tags")),
            source=str(raw.get("source") or ""),
            path=str(raw.get("path") or ""),
            confidence=str(raw.get("confidence") or "active"),
            created_at=_millis(raw.get("createdAt") or raw.get("created_at")),
            updated_at=_millis(raw.get("updatedAt") or raw.get("updated_at")),
            created_by_session=str(raw.get("created_by_session") or raw.get("createdBySession") or ""),
            created_by_actor=str(raw.get("created_by_actor") or raw.get("createdByActor") or ""),
            revisions=revisions,
            supersedes=_as_list(raw.get("supersedes")),
            superseded_by=str(raw.get("superseded_by") or raw.get("supersededBy") or ""),
            archived=bool(raw.get("archived") or False),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "kind": self.kind,
            "text": self.text,
            "summary": self.summary,
            "tags": self.tags,
            "source": self.source,
            "path": self.path,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by_session": self.created_by_session,
            "created_by_actor": self.created_by_actor,
            "revisions": [r.to_json() for r in self.revisions],
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "archived": self.archived,
        }

    def to_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "kind": self.kind,
            "text": self.text,
            "summary": self.summary or self.text[:120],
            "tags": self.tags,
            "source": self.source,
            "path": self.path,
            "confidence": self.confidence,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "createdBySession": self.created_by_session,
            "createdByActor": self.created_by_actor,
            "revisions": [r.to_json() for r in self.revisions],
            "supersedes": self.supersedes,
            "supersededBy": self.superseded_by,
            "archived": self.archived,
        }


@dataclass
class SkillRecord:
    id: str
    name: str
    skill_type: str
    description: str
    instructions: str = ""
    triggers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    error_patterns: list[str] = field(default_factory=list)
    severity: str = "warning"
    source: str = ""
    scope: str = "project"
    path: str = ""
    confidence: str = "unvalidated"
    retrieval_count: int = 0
    success_signals: float = 0
    failure_signals: float = 0
    load_count: int = 0
    updated_at: int = 0

    @classmethod
    def from_index_entry(
        cls,
        entry: dict[str, Any],
        *,
        workspace: Path,
        scope: str = "compat",
    ) -> "SkillRecord | None":
        meta = entry.get("metadata") or {}
        name = str(meta.get("name") or entry.get("name") or "").strip()
        if not name:
            return None
        skill_type = _normalize_skill_type(str(meta.get("skill_type") or meta.get("type") or "build"))
        skill_dir = str(entry.get("skill_dir") or "")
        path = skill_dir
        if skill_dir and not Path(skill_dir).is_absolute():
            path = str((workspace / skill_dir).resolve())
        skill_path = Path(path) / "SKILL.md" if path else Path()
        return cls(
            id=f"{scope}:{skill_type}:{name}",
            name=name,
            skill_type=_normalize_skill_type(skill_type),
            description=str(meta.get("description") or entry.get("description") or ""),
            instructions=str(entry.get("instructions") or ""),
            triggers=_as_list(meta.get("triggers")),
            tags=_as_list(meta.get("tags")),
            error_patterns=_as_list(meta.get("error_patterns")),
            severity=str(meta.get("severity") or "warning"),
            source=str(meta.get("source") or ""),
            scope=scope,
            path=str(skill_path if skill_path else path),
            confidence=str(entry.get("confidence") or "unvalidated"),
            retrieval_count=int(entry.get("retrieval_count") or 0),
            success_signals=float(entry.get("success_signals") or 0),
            failure_signals=float(entry.get("failure_signals") or 0),
            updated_at=_millis(entry.get("updated_at")),
        )

    def to_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "skillType": self.skill_type,
            "description": self.description,
            "triggers": self.triggers,
            "tags": self.tags,
            "confidence": _normalize_confidence(self.confidence),
            "retrievalCount": int(self.retrieval_count),
            "successSignals": int(self.success_signals),
            "failureSignals": int(self.failure_signals),
            "loadCount": int(self.load_count),
            "scope": self.scope,
            "path": self.path,
        }


def _normalize_skill_type(value: str) -> str:
    normalized = (value or "build").strip().lower()
    if normalized in {"build", "guard", "reference", "workflow", "tool"}:
        return normalized
    return "build"


def _normalize_confidence(value: str) -> str:
    normalized = (value or "unvalidated").strip().lower()
    if normalized in {"unvalidated", "experimental", "verified", "deprecated"}:
        return normalized
    return "unvalidated"
