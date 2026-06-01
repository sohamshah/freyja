"""Simple file-backed skill discovery and loading."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from bridge.knowledge.models import SkillRecord


class SkillStore:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.usage_path = Path.home() / ".freyja" / "knowledge" / "skill_usage.jsonl"
        self._skills: dict[str, SkillRecord] = {}
        self._by_name: dict[str, SkillRecord] = {}
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self.refresh()

    def refresh(self) -> None:
        fingerprint = self._build_fingerprint()
        if self._skills and fingerprint == self._fingerprint:
            return
        usage = self._read_usage()
        discovered: dict[str, SkillRecord] = {}

        # Lowest precedence first; later scopes shadow earlier ones.
        for skill in self._read_index_jsonl():
            self._put(discovered, skill)
        for skill in self._read_skill_dirs(Path.home() / ".freyja" / "skills", "user"):
            self._put(discovered, skill)
        for skill in self._read_skill_dirs(Path.home() / ".claude" / "skills", "compat"):
            self._put(discovered, skill)
        for skill in self._read_skill_dirs(self.workspace / "knowledge", "project"):
            self._put(discovered, skill)
        for skill in self._read_skill_dirs(self.workspace / ".freyja" / "skills", "project"):
            self._put(discovered, skill)

        self._skills = {}
        self._by_name = {}
        for skill in discovered.values():
            stats = usage.get(skill.id) or usage.get(_usage_key(skill.name, skill.skill_type))
            if stats:
                skill = replace(
                    skill,
                    retrieval_count=max(skill.retrieval_count, int(stats.get("retrieval_count", 0))),
                    load_count=int(stats.get("load_count", 0)),
                    success_signals=max(skill.success_signals, float(stats.get("success_signals", 0))),
                    failure_signals=max(skill.failure_signals, float(stats.get("failure_signals", 0))),
                    confidence=str(stats.get("confidence") or skill.confidence),
                )
            self._skills[skill.id] = skill
            self._by_name[skill.name.lower()] = skill
        self._fingerprint = fingerprint

    def list_skills(self) -> list[SkillRecord]:
        self.refresh()
        return sorted(
            self._skills.values(),
            key=lambda s: (_confidence_rank(s.confidence), -s.retrieval_count, s.name),
        )

    def search(self, query: str, *, limit: int = 8) -> list[tuple[SkillRecord, int, str]]:
        self.refresh()
        terms = [t for t in re.split(r"[^a-z0-9_./-]+", query.lower()) if len(t) > 1]
        if not terms:
            return [(skill, 0, "listed") for skill in self.list_skills()[:limit]]

        scored: list[tuple[int, SkillRecord, str]] = []
        for skill in self._skills.values():
            fields = {
                "name": skill.name,
                "description": skill.description,
                "triggers": " ".join(skill.triggers),
                "tags": " ".join(skill.tags),
                "error_patterns": " ".join(skill.error_patterns),
                "tools": " ".join(t for t in skill.tags if t.endswith("_tool")),
            }
            score = 0
            reasons: list[str] = []
            for field, value in fields.items():
                haystack = value.lower()
                hits = sum(1 for term in terms if term in haystack)
                if not hits:
                    continue
                weight = 4 if field == "name" else 3 if field in {"triggers", "error_patterns"} else 2
                score += hits * weight
                reasons.append(field)
            if score:
                scored.append((score + _confidence_boost(skill.confidence), skill, ", ".join(reasons)))
        scored.sort(key=lambda item: (item[0], item[1].retrieval_count), reverse=True)
        return [(skill, score, reason) for score, skill, reason in scored[:limit]]

    def get(self, name: str) -> SkillRecord | None:
        self.refresh()
        return self._by_name.get(name.strip().lower())

    def load(self, name: str) -> tuple[SkillRecord | None, str]:
        skill = self.get(name)
        if skill is None:
            return None, ""
        body = skill.instructions
        if not body and skill.path:
            path = Path(skill.path)
            if path.is_file():
                parsed = _parse_skill_md(path, skill.scope)
                if parsed is not None:
                    body = parsed.instructions
        content = f"[Skill: {skill.name} | Type: {skill.skill_type}]\n\n{body}".rstrip()
        return skill, content

    def record_load(self, skill: SkillRecord) -> SkillRecord:
        now = int(time.time() * 1000)
        event = {
            "event": "loaded",
            "skill_id": skill.id,
            "name": skill.name,
            "skill_type": skill.skill_type,
            "at": now,
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False))
            f.write("\n")
        updated = replace(
            skill,
            load_count=skill.load_count + 1,
            retrieval_count=skill.retrieval_count + 1,
        )
        self._skills[updated.id] = updated
        self._by_name[updated.name.lower()] = updated
        return updated

    def record_review_decision(
        self,
        *,
        name: str,
        skill_type: str,
        action: str,
        reason: str,
    ) -> dict[str, Any]:
        success_delta, failure_delta = _review_signal_delta(reason)
        normalized_type = _normalize_skill_type(skill_type)
        event = {
            "event": "review",
            "skill_id": _skill_id_for("", name, normalized_type),
            "name": name,
            "skill_type": normalized_type,
            "action": action,
            "reason": reason,
            "success_delta": success_delta,
            "failure_delta": failure_delta,
            "at": int(time.time() * 1000),
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False))
            f.write("\n")
        return event

    def build_prompt(self, query: str = "", *, limit: int = 12) -> str:
        # Lazy import — value_score depends on bridge.knowledge.learning,
        # which is younger than this module. Importing at module scope
        # would force a hard dep on the learning loop being present even
        # for installations that don't use it yet.
        try:
            from bridge.knowledge.learning import value_score as _vs
        except Exception:
            _vs = None

        matches = self.search(query, limit=limit * 2) if query else [
            (skill, 0, "") for skill in self.list_skills()
        ]
        if not matches:
            return ""

        # Pull V rollups for every candidate and re-rank by measured V
        # before truncating to ``limit``. The match-score from search()
        # is used as a tiebreaker so a freshly-promoted skill with no V
        # data still respects query relevance.
        rollups: dict[str, Any] = {}
        if _vs is not None:
            try:
                rollups = _vs.compute_all([s.name for s, _, _ in matches])
            except Exception:
                rollups = {}

        def _rank_key(item: tuple[SkillRecord, int, str]) -> tuple[float, int, float, int]:
            skill, search_score, _reason = item
            rollup = rollups.get(skill.name)
            v = float(getattr(rollup, "v_score", 0.0)) if rollup is not None else 0.0
            # Push archived skills to the end. Per-skill flag is set by
            # ``value_score`` from the events log so this stays accurate
            # without the SkillRecord needing to know.
            archived = bool(getattr(rollup, "archived", False))
            return (
                1 if archived else 0,                 # archived → last
                -int(round(v * 1000)),                 # higher V first
                -search_score,                          # then query relevance
                -_confidence_boost(skill.confidence),   # then declared confidence
            )

        matches.sort(key=_rank_key)
        matches = matches[:limit]

        lines = [
            "## Available Skills",
            "",
            "Call `load_skill(name)` when one of these skills is relevant and you need full instructions.",
            "Each skill carries an empirical value score (V) computed from prior outcomes:",
            "  V is signed; positive means past loads of this skill correlated with cleaner",
            "  outcomes, negative means corrections / abandonment / decay. Treat low-V skills",
            "  with care; they may apply but ask whether the task is actually what they govern.",
            "",
        ]
        for skill, _score, reason in matches:
            why = f" ({reason})" if reason else ""
            rollup = rollups.get(skill.name)
            if rollup is not None and getattr(rollup, "has_signal", lambda: False)():
                head = rollup.headline()  # already starts with "V=..."
            else:
                head = "no prior data"
            tags = f"{skill.skill_type}/{skill.confidence}"
            lines.append(
                f"- `{skill.name}` [{tags}] · {head}{why}: {skill.description}"
            )
        return "\n".join(lines)

    def _put(self, target: dict[str, SkillRecord], skill: SkillRecord) -> None:
        key = skill.name.lower()
        current = target.get(key)
        if current is None or _scope_rank(skill.scope) >= _scope_rank(current.scope):
            target[key] = skill

    def _read_index_jsonl(self) -> list[SkillRecord]:
        path = self.workspace / "knowledge" / "index.jsonl"
        if not path.exists():
            return []
        out: list[SkillRecord] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            skill = SkillRecord.from_index_entry(entry, workspace=self.workspace)
            if skill is not None:
                out.append(skill)
        return out

    def _read_skill_dirs(self, base: Path, scope: str) -> list[SkillRecord]:
        if not base.exists():
            return []
        skill_files: list[Path] = []
        if (base / "SKILL.md").is_file():
            skill_files.append(base / "SKILL.md")
        for path in base.rglob("SKILL.md"):
            if path not in skill_files:
                skill_files.append(path)
        out: list[SkillRecord] = []
        for path in sorted(skill_files):
            parsed = _parse_skill_md(path, scope)
            if parsed is not None:
                out.append(parsed)
        return out

    def _build_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        paths = {self.usage_path, self.workspace / "knowledge" / "index.jsonl"}
        for base in (
            Path.home() / ".freyja" / "skills",
            Path.home() / ".claude" / "skills",
            self.workspace / "knowledge",
            self.workspace / ".freyja" / "skills",
        ):
            if not base.exists():
                continue
            direct = base / "SKILL.md"
            if direct.is_file():
                paths.add(direct)
            try:
                paths.update(base.rglob("SKILL.md"))
            except Exception:
                continue

        records: list[tuple[str, int, int]] = []
        for path in sorted(paths):
            try:
                stat = path.stat()
            except Exception:
                continue
            records.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(records)

    def _read_usage(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        if not self.usage_path.exists():
            return stats
        try:
            lines = self.usage_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return stats
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            name = str(event.get("name") or "")
            skill_type = _normalize_skill_type(str(event.get("skill_type") or "build"))
            key = str(event.get("skill_id") or _usage_key(name, skill_type))
            fallback_key = _usage_key(name, skill_type)
            bucket = stats.setdefault(
                key,
                {
                    "retrieval_count": 0,
                    "load_count": 0,
                    "success_signals": 0.0,
                    "failure_signals": 0.0,
                    "confidence": "unvalidated",
                },
            )
            if event.get("event") == "loaded":
                bucket["retrieval_count"] += 1
                bucket["load_count"] += 1
            elif event.get("event") == "review":
                bucket["success_signals"] += float(event.get("success_delta") or 0)
                bucket["failure_signals"] += float(event.get("failure_delta") or 0)
            bucket["confidence"] = _confidence_for(bucket)
            stats[fallback_key] = bucket
        return stats


def _parse_skill_md(path: Path, scope: str) -> SkillRecord | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    metadata: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            metadata = _parse_frontmatter(parts[1])
            body = parts[2].strip()
    name = str(metadata.get("name") or path.parent.name).strip()
    if not name:
        return None
    skill_type = _normalize_skill_type(
        str(metadata.get("type") or metadata.get("skill_type") or _type_from_path(path))
    )
    description = str(metadata.get("description") or "").strip()
    if not description:
        first_heading = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
        description = first_heading[:160]
    skill_id = _skill_id_for(scope, name, skill_type)
    return SkillRecord(
        id=skill_id,
        name=name,
        skill_type=_normalize_skill_type(skill_type),
        description=description,
        instructions=body,
        triggers=_as_list(metadata.get("triggers")),
        tags=_as_list(metadata.get("tags")),
        error_patterns=_as_list(metadata.get("error_patterns")),
        severity=str(metadata.get("severity") or "warning"),
        source=str(metadata.get("source") or ""),
        scope=scope,
        path=str(path),
        confidence=str(metadata.get("confidence") or "unvalidated"),
        updated_at=int(path.stat().st_mtime * 1000),
    )


def _parse_frontmatter(text: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- ") and current_key:
            metadata.setdefault(current_key, [])
            if isinstance(metadata[current_key], list):
                metadata[current_key].append(line[2:].strip().strip("\"'"))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            metadata[key] = []
        elif value.startswith("[") and value.endswith("]"):
            metadata[key] = [
                v.strip().strip("\"'")
                for v in value[1:-1].split(",")
                if v.strip()
            ]
        else:
            metadata[key] = value.strip().strip("\"'")
    return metadata


def _type_from_path(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    if "guard" in parts:
        return "guard"
    if "reference" in parts:
        return "reference"
    return "build"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_skill_type(value: str) -> str:
    normalized = (value or "build").strip().lower()
    if normalized in {"build", "guard", "reference", "workflow", "tool"}:
        return normalized
    return "build"


def _skill_id_for(scope: str, name: str, skill_type: str) -> str:
    prefix = f"{scope}:" if scope else ""
    return f"{prefix}{_normalize_skill_type(skill_type)}:{name}"


def _scope_rank(scope: str) -> int:
    return {"compat": 0, "plugin": 1, "user": 2, "project": 3}.get(scope, 0)


def _confidence_rank(confidence: str) -> int:
    return {"verified": 0, "experimental": 1, "unvalidated": 2, "deprecated": 3}.get(confidence, 2)


def _confidence_boost(confidence: str) -> int:
    return {"verified": 4, "experimental": 2, "unvalidated": 0, "deprecated": -4}.get(confidence, 0)


def _usage_key(name: str, skill_type: str) -> str:
    digest = hashlib.sha1(f"{_normalize_skill_type(skill_type)}:{name}".encode("utf-8")).hexdigest()[:12]
    return f"skill_usage:{digest}"


def _review_signal_delta(reason: str) -> tuple[float, float]:
    return {
        "actively_using": (1.0, 0.0),
        "needed_soon": (0.0, 0.0),
        "task_completed": (1.0, 0.0),
        "never_relevant": (0.0, 1.0),
        "superseded": (0.0, 0.3),
        "low_value": (0.0, 0.0),
        "causing_confusion": (0.0, 1.5),
    }.get(reason, (0.0, 0.0))


def _confidence_for(stats: dict[str, Any]) -> str:
    retrievals = int(stats.get("retrieval_count") or 0)
    success = float(stats.get("success_signals") or 0)
    failure = float(stats.get("failure_signals") or 0)
    total = success + failure
    rate = success / total if total > 0 else 0.0
    if retrievals >= 10 and total > 0 and rate < 0.5:
        return "deprecated"
    if retrievals >= 10 and total > 0 and rate >= 0.8:
        return "verified"
    if retrievals >= 3:
        return "experimental"
    return "unvalidated"
