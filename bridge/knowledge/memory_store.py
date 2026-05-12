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

from bridge.knowledge.models import MemoryItem, MemoryRevision

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
        # Build a dedupe set from current JSONL texts AND every prior
        # text that appears in revision history. Without the second
        # half, the legacy MEMORY.md import resurrects pre-edit copies
        # of items that have since been updated or merged.
        seen_texts = {_dedupe_text(item.text) for item in items.values()}
        for item in items.values():
            for rev in item.revisions:
                if rev.prev_text:
                    seen_texts.add(_dedupe_text(rev.prev_text))
        for item in self._read_memory_md():
            key = _dedupe_text(item.text)
            if key in seen_texts:
                continue
            items.setdefault(item.id, item)
            seen_texts.add(key)
        self._items = items

    def list_items(
        self,
        *,
        limit: int | None = None,
        include_archived: bool = False,
    ) -> list[MemoryItem]:
        self.refresh()
        items = [
            m for m in self._items.values()
            if include_archived or not m.archived
        ]
        items.sort(
            key=lambda m: (m.updated_at or m.created_at or 0, m.id),
            reverse=True,
        )
        return items[:limit] if limit else items

    def get_item(self, item_id: str) -> MemoryItem | None:
        self.refresh()
        return self._items.get(item_id)

    def relevant(self, query: str = "", *, limit: int = 8) -> list[MemoryItem]:
        self.refresh()
        if not query.strip():
            return self.list_items(limit=limit)

        terms = [t for t in re.split(r"[^a-z0-9_./-]+", query.lower()) if len(t) > 2]
        scored: list[tuple[int, MemoryItem]] = []
        for item in self._items.values():
            # Archived items are excluded from retrieval entirely — the
            # agent shouldn't be served context the user explicitly
            # struck from the record.
            if item.archived:
                continue
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

    def record_preference(
        self,
        preference: str,
        category: str = "other",
        *,
        session_id: str = "",
        actor: str = "",
        scope: str = "user",
    ) -> MemoryItem:
        now = int(time.time() * 1000)
        text = preference.strip()
        item_id = _stable_id("memory", scope, category, text, str(now))
        revision = MemoryRevision(
            ts=now,
            session_id=session_id,
            actor=actor or "record_user_preference",
            action="create",
        )
        item = MemoryItem(
            id=item_id,
            scope=scope,
            kind=category or "other",
            text=text,
            summary=text[:140],
            tags=[category] if category else [],
            source="record_user_preference",
            path=str(self.structured_path),
            confidence="active",
            created_at=now,
            updated_at=now,
            created_by_session=session_id,
            created_by_actor=actor or "record_user_preference",
            revisions=[revision],
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

    def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        tags: list[str] | None = None,
        session_id: str = "",
        actor: str = "",
        note: str = "",
    ) -> MemoryItem | None:
        """Edit an existing memory in place. Pushes a revision capturing
        whatever changed, then rewrites the structured JSONL with the
        full set of items so the new state is the on-disk truth."""
        self.refresh()
        item = self._items.get(item_id)
        if item is None:
            return None
        now = int(time.time() * 1000)
        prev_text = item.text
        prev_kind = item.kind
        prev_scope = item.scope
        changed = False
        if text is not None and text.strip() and text.strip() != item.text:
            item.text = text.strip()
            item.summary = item.text[:140]
            changed = True
        if kind is not None and kind.strip() and kind.strip() != item.kind:
            item.kind = kind.strip()
            changed = True
        if scope is not None and scope.strip() and scope.strip() != item.scope:
            item.scope = scope.strip()
            changed = True
        if tags is not None:
            cleaned = [t for t in (tag.strip() for tag in tags) if t]
            if cleaned != item.tags:
                item.tags = cleaned
                changed = True
        if not changed and not note:
            return item
        item.updated_at = now
        item.revisions.append(
            MemoryRevision(
                ts=now,
                session_id=session_id,
                actor=actor or "user",
                action="update",
                note=note,
                prev_text=prev_text if text is not None and prev_text != item.text else "",
                prev_kind=prev_kind if kind is not None and prev_kind != item.kind else "",
                prev_scope=prev_scope if scope is not None and prev_scope != item.scope else "",
            )
        )
        self._items[item.id] = item
        self._rewrite_structured()
        return item

    def delete_item(
        self,
        item_id: str,
        *,
        session_id: str = "",
        actor: str = "",
        hard: bool = False,
        note: str = "",
    ) -> MemoryItem | None:
        """Soft-delete by default (sets archived=True). Pass hard=True
        to remove from the JSONL entirely. Soft delete keeps the
        revision trail intact for forensics."""
        self.refresh()
        item = self._items.get(item_id)
        if item is None:
            return None
        now = int(time.time() * 1000)
        if hard:
            self._items.pop(item.id, None)
            self._rewrite_structured()
            return item
        item.archived = True
        item.updated_at = now
        item.revisions.append(
            MemoryRevision(
                ts=now,
                session_id=session_id,
                actor=actor or "user",
                action="archive",
                note=note,
            )
        )
        self._items[item.id] = item
        self._rewrite_structured()
        return item

    def restore_item(
        self,
        item_id: str,
        *,
        session_id: str = "",
        actor: str = "",
        note: str = "",
    ) -> MemoryItem | None:
        self.refresh()
        item = self._items.get(item_id)
        if item is None or not item.archived:
            return item
        now = int(time.time() * 1000)
        item.archived = False
        item.updated_at = now
        item.revisions.append(
            MemoryRevision(
                ts=now,
                session_id=session_id,
                actor=actor or "user",
                action="restore",
                note=note,
            )
        )
        self._items[item.id] = item
        self._rewrite_structured()
        return item

    def merge_items(
        self,
        ids: list[str],
        *,
        text: str,
        kind: str = "",
        scope: str = "",
        tags: list[str] | None = None,
        session_id: str = "",
        actor: str = "",
        note: str = "",
    ) -> MemoryItem | None:
        """Combine N existing memories into one canonical entry.
        Archives the originals (each gets a `merge_into` revision pointing
        at the new id) and creates a fresh item whose `supersedes` lists
        the originals. Returns the new merged item."""
        self.refresh()
        sources = [self._items.get(i) for i in ids if self._items.get(i) is not None]
        if not sources or not text.strip():
            return None
        now = int(time.time() * 1000)
        merged_id = _stable_id("memory_merge", "user", text.strip(), str(now))
        # Resolve fallback kind/scope from the most recent source.
        sources_sorted = sorted(sources, key=lambda m: m.updated_at or 0, reverse=True)
        primary = sources_sorted[0]
        merged = MemoryItem(
            id=merged_id,
            scope=(scope or primary.scope or "user").strip(),
            kind=(kind or primary.kind or "other").strip(),
            text=text.strip(),
            summary=text.strip()[:140],
            tags=tags if tags is not None else list(primary.tags),
            source="merge",
            path=str(self.structured_path),
            confidence="active",
            created_at=now,
            updated_at=now,
            created_by_session=session_id,
            created_by_actor=actor or "user",
            revisions=[
                MemoryRevision(
                    ts=now,
                    session_id=session_id,
                    actor=actor or "user",
                    action="merge_from",
                    note=note or f"merged from {len(sources)} entries",
                )
            ],
            supersedes=[s.id for s in sources],
        )
        for s in sources:
            s.archived = True
            s.superseded_by = merged_id
            s.updated_at = now
            s.revisions.append(
                MemoryRevision(
                    ts=now,
                    session_id=session_id,
                    actor=actor or "user",
                    action="merge_into",
                    note=f"merged into {merged_id}",
                )
            )
            self._items[s.id] = s
        self._items[merged.id] = merged
        self._rewrite_structured()
        return merged

    def _rewrite_structured(self) -> None:
        """Re-serialize the entire structured JSONL from the in-memory
        dict. We do a full rewrite (not append) so updates/deletes leave
        a clean file rather than a chain of overrides. Atomic via
        tmp + rename so a crash mid-write doesn't corrupt the store."""
        self.structured_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.structured_path.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for item in self._items.values():
                    f.write(json.dumps(item.to_json(), ensure_ascii=False))
                    f.write("\n")
            tmp.replace(self.structured_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

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
