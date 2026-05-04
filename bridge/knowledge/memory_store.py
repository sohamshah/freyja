"""Simple file-backed memory store.

The store reads two sources:
- <workspace>/MEMORY.md for compatibility with existing Freyja behavior.
- ~/.freyja/knowledge/memory.jsonl for structured user/project/session memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from bridge.knowledge.models import MemoryItem

_MEMORY_LINE_RE = re.compile(
    r"^-\s+\[(?P<ts>[^\]]+)\]\s+\*\*(?P<kind>[^*]+)\*\*:\s*(?P<text>.+)$"
)


class MemoryStore:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory_md = self.workspace / "MEMORY.md"
        self.structured_path = Path.home() / ".freyja" / "knowledge" / "memory.jsonl"
        self._items: dict[str, MemoryItem] = {}
        self.refresh()

    def refresh(self) -> None:
        items: dict[str, MemoryItem] = {}
        for item in self._read_structured():
            if item.id:
                items[item.id] = item
        seen_texts = {_dedupe_text(item.text) for item in items.values()}
        for item in self._read_memory_md():
            key = _dedupe_text(item.text)
            if key in seen_texts:
                continue
            items.setdefault(item.id, item)
            seen_texts.add(key)
        self._items = items

    def list_items(self, *, limit: int | None = None) -> list[MemoryItem]:
        self.refresh()
        items = sorted(
            self._items.values(),
            key=lambda m: (m.updated_at or m.created_at or 0, m.id),
            reverse=True,
        )
        return items[:limit] if limit else items

    def relevant(self, query: str = "", *, limit: int = 8) -> list[MemoryItem]:
        self.refresh()
        if not query.strip():
            return self.list_items(limit=limit)

        terms = [t for t in re.split(r"[^a-z0-9_./-]+", query.lower()) if len(t) > 2]
        scored: list[tuple[int, MemoryItem]] = []
        for item in self._items.values():
            haystack = " ".join(
                [
                    item.text,
                    item.summary,
                    item.kind,
                    item.scope,
                    " ".join(item.tags),
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at or pair[1].created_at), reverse=True)
        if scored:
            return [item for _, item in scored[:limit]]
        return self.list_items(limit=limit)

    def record_preference(self, preference: str, category: str = "other") -> MemoryItem:
        now = int(time.time() * 1000)
        text = preference.strip()
        item_id = _stable_id("memory", "user", category, text, str(now))
        item = MemoryItem(
            id=item_id,
            scope="user",
            kind=category or "other",
            text=text,
            summary=text[:140],
            tags=[category] if category else [],
            source="record_user_preference",
            path=str(self.structured_path),
            confidence="active",
            created_at=now,
            updated_at=now,
        )

        self.structured_path.parent.mkdir(parents=True, exist_ok=True)
        with self.structured_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item.to_json(), ensure_ascii=False))
            f.write("\n")

        # Keep the existing workspace MEMORY.md compatibility path.
        ts = time.strftime("%Y-%m-%d %H:%M")
        entry = f"- [{ts}] **{category or 'other'}**: {text}\n"
        if not self.memory_md.exists():
            self.memory_md.write_text(
                "# User Preferences\n\n"
                "Personal preferences remembered across sessions.\n\n",
                encoding="utf-8",
            )
        with self.memory_md.open("a", encoding="utf-8") as f:
            f.write(entry)

        self._items[item.id] = item
        return item

    def build_prompt(self, query: str = "", *, limit: int = 8) -> str:
        items = self.relevant(query, limit=limit)
        if not items:
            return ""
        lines = [
            "## Relevant Memory",
            "",
            "Use these remembered preferences and project notes when relevant.",
        ]
        for item in items:
            prefix = f"{item.scope}/{item.kind}"
            lines.append(f"- [{prefix}] {item.text}")
        return "\n".join(lines)

    def _read_structured(self) -> list[MemoryItem]:
        if not self.structured_path.exists():
            return []
        out: list[MemoryItem] = []
        try:
            for line in self.structured_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = MemoryItem.from_dict(json.loads(line))
                except Exception:
                    continue
                if item.text:
                    out.append(item)
        except Exception:
            return []
        return out

    def _read_memory_md(self) -> list[MemoryItem]:
        if not self.memory_md.exists():
            return []
        out: list[MemoryItem] = []
        try:
            lines = self.memory_md.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        mtime = int(self.memory_md.stat().st_mtime * 1000)
        for idx, line in enumerate(lines):
            match = _MEMORY_LINE_RE.match(line.strip())
            if not match:
                continue
            kind = match.group("kind").strip() or "other"
            text = match.group("text").strip()
            if not text:
                continue
            item_id = _stable_id("memory_md", str(self.memory_md), str(idx), text)
            out.append(
                MemoryItem(
                    id=item_id,
                    scope="project",
                    kind=kind,
                    text=text,
                    summary=text[:140],
                    tags=[kind],
                    source="MEMORY.md",
                    path=str(self.memory_md),
                    confidence="active",
                    created_at=mtime,
                    updated_at=mtime,
                )
            )
        return out


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"mem_{digest}"


def _dedupe_text(text: str) -> str:
    return " ".join(text.lower().split())
