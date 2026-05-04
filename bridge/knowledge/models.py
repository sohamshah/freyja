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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryItem":
        text = str(raw.get("text") or raw.get("preference") or "").strip()
        summary = str(raw.get("summary") or text[:120]).strip()
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
