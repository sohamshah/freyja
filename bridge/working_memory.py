"""Structured working memory — the semantic, agent-authored memory layer.

Milestone 2 of Grounded Memory (see ``docs/GROUNDED-MEMORY-DESIGN.md``). Where
the action ledger (``session_ledger.py``) is the runtime's deterministic record
of *what happened*, this is the agent's organized record of *what it means*:
work organized by entity (workstream / decision / finding / open thread /
artifact note), durable across compaction and queryable.

Authorship is mostly the agent (the high-level intent only it knows), with
artifact facts seeded from the ledger so the store can't be blank about files —
the same anti-amnesia principle as the ledger, one level up the abstraction.

One JSON document per session at ``<project_dir>/working_memory.json``. Small
(it holds summaries, not transcripts), so we rewrite the whole doc on each
mutation. Pure helpers (id/slug/render) are free functions for unit testing.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENTITY_TYPES = ("workstream", "decision", "finding", "open_thread", "artifact_note")

_TYPE_PREFIX = {
    "workstream": "ws",
    "decision": "dec",
    "finding": "find",
    "open_thread": "thr",
    "artifact_note": "art",
}

# The field that names each entity (used for id slugs + render headlines).
_PRIMARY_FIELD = {
    "workstream": "title",
    "decision": "title",
    "finding": "text",
    "open_thread": "text",
    "artifact_note": "path",
}


def _slug(text: str, *, max_len: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].strip("-") or "item"


def _diff_suffix(entity: dict[str, Any]) -> str:
    """Compact ``" (+N −M)"`` for an artifact_note carrying diff stats.

    Returns ``""`` when neither additions nor deletions are present so a note
    without a recorded diff renders unchanged. The +N/−M mirrors the panel's
    MarginMark (text-ok additions / text-danger deletions)."""
    add = entity.get("additions")
    rem = entity.get("deletions")
    if add is None and rem is None:
        return ""
    return f" (+{int(add or 0)} −{int(rem or 0)})"


def render_working_memory(
    entities: list[dict[str, Any]],
    *,
    ledger_effects: list[dict[str, Any]] | None = None,
    include_done: bool = False,
) -> str | None:
    """Render the structured memory as a compact, first-person block suitable
    for post-compaction injection. Groups by workstream; folds in ledger-derived
    file artifacts under each (matched by path prefix is overkill — we just list
    unattached ledger files once). Returns None when there's nothing to show.
    """
    by_type: dict[str, list[dict[str, Any]]] = {t: [] for t in ENTITY_TYPES}
    for e in entities:
        t = e.get("type")
        if t in by_type:
            by_type[t].append(e)

    workstreams = by_type["workstream"]
    if not include_done:
        workstreams = [w for w in workstreams if w.get("status") != "done"]
    have_any = any(by_type[t] for t in ENTITY_TYPES) or bool(ledger_effects)
    if not have_any:
        return None

    lines: list[str] = ["## Working memory (your own structured notes)"]

    def _children(kind: str, ws_id: str | None) -> list[dict[str, Any]]:
        return [e for e in by_type[kind] if e.get("workstreamId") == ws_id]

    for ws in workstreams:
        ws_id = ws.get("id")
        status = ws.get("status") or "active"
        lines.append("")
        lines.append(f"### {ws.get('title')}  [{status}]")
        if ws.get("request"):
            lines.append(f"- goal: {ws['request']}")
        for d in _children("decision", ws_id):
            r = f" — {d['rationale']}" if d.get("rationale") else ""
            lines.append(f"- decided: {d.get('title')}{r}")
        for f in _children("finding", ws_id):
            src = f" ({f['source']})" if f.get("source") else ""
            lines.append(f"- found: {f.get('text')}{src}")
        notes = _children("artifact_note", ws_id)
        for n in notes:
            lines.append(f"- file: {n.get('path')} — {n.get('note')}{_diff_suffix(n)}")
        open_threads = [t for t in _children("open_thread", ws_id) if t.get("status") != "resolved"]
        for t in open_threads:
            lines.append(f"- open: {t.get('text')}")

    # Files the runtime recorded the agent changing — folded in (for the
    # `read` surface) so the agent sees its semantic notes next to ground
    # truth. Injection paths pass ledger_effects=None (the standing ledger
    # reminder already carries these, so we don't duplicate them there).
    if ledger_effects:
        seen: set[str] = set()
        file_lines: list[str] = []
        for e in ledger_effects:
            s = str(e.get("summary") or "").strip()
            if s and s not in seen:
                seen.add(s)
                file_lines.append(f"- {s}")
        if file_lines:
            lines.append("")
            lines.append("### Files you changed this session (runtime ledger)")
            lines.extend(file_lines)

    # Orphan entities (no workstream) — surface so nothing is lost.
    orphan_findings = [f for f in by_type["finding"] if not f.get("workstreamId")]
    orphan_threads = [
        t for t in by_type["open_thread"]
        if not t.get("workstreamId") and t.get("status") != "resolved"
    ]
    if orphan_findings or orphan_threads:
        lines.append("")
        lines.append("### Unfiled")
        for f in orphan_findings:
            lines.append(f"- found: {f.get('text')}")
        for t in orphan_threads:
            lines.append(f"- open: {t.get('text')}")

    return "\n".join(lines)


def apply_working_memory_upserts(
    wm: "WorkingMemory", upserts: list[dict[str, Any]]
) -> int:
    """Apply structured upserts produced by the compaction summarizer
    (Milestone 2b). Each upsert is ``{type, title|text, rationale?, source?,
    workstream?}`` where ``workstream`` references a parent by TITLE (the LLM
    doesn't know ids). Workstreams are resolved/created first so children can
    link; decisions/findings/threads dedup by primary value within their
    workstream so re-emitting the same entry across compaction rounds updates
    rather than duplicates. Returns the number applied. Best-effort per item.
    """
    if not upserts:
        return 0
    applied = 0
    ws_by_title: dict[str, str] = {}
    for e in wm.list(type="workstream"):
        t = str(e.get("title") or "").strip().lower()
        if t:
            ws_by_title[t] = str(e.get("id"))

    def _resolve_ws(ref: str | None) -> str | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        key = ref.lower()
        if key in ws_by_title:
            return ws_by_title[key]
        ent = wm.upsert(type="workstream", fields={"title": ref})
        if ent:
            ws_by_title[key] = ent["id"]
            return ent["id"]
        return None

    # Pass 1: workstreams (so children can link).
    for u in upserts:
        if not isinstance(u, dict) or u.get("type") != "workstream":
            continue
        title = str(u.get("title") or "").strip()
        if not title:
            continue
        existing = ws_by_title.get(title.lower())
        ent = wm.upsert(
            type="workstream",
            entity_id=existing,
            fields={"title": title, "request": u.get("request"), "status": u.get("status")},
        )
        if ent:
            ws_by_title[title.lower()] = ent["id"]
            applied += 1

    # Pass 2: children (decision/finding/open_thread).
    for u in upserts:
        if not isinstance(u, dict):
            continue
        t = u.get("type")
        if t not in ("decision", "finding", "open_thread"):
            continue
        ws_id = _resolve_ws(u.get("workstream"))
        fields: dict[str, Any] = {}
        if ws_id:
            fields["workstreamId"] = ws_id
        if t == "decision":
            primary = str(u.get("title") or "").strip()
            fields.update(title=u.get("title"), rationale=u.get("rationale"))
        else:  # finding | open_thread
            primary = str(u.get("text") or "").strip()
            fields.update(text=u.get("text"))
            if t == "finding" and u.get("source"):
                fields["source"] = u.get("source")
        if not primary:
            continue
        existing = wm.find_by_primary(type=t, value=primary, workstream_id=ws_id)
        if wm.upsert(type=t, entity_id=existing, fields=fields):
            applied += 1
    return applied


class WorkingMemory:
    """Per-session structured working memory backed by one JSON document."""

    def __init__(self, *, session_id: str, project_dir: Path) -> None:
        self.session_id = session_id
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.path = self.project_dir / "working_memory.json"
        # RLock: the compaction projection mutates from a worker thread while
        # the event loop may render(); upsert/resolve nest into _save which also
        # takes the lock, so a re-entrant lock is required.
        self._lock = threading.RLock()
        self._entities: dict[str, dict[str, Any]] = {}
        # High-level overview produced by compaction's extraction call (Call B):
        # {summary, actionsCompleted, updatedAt}. The explicit "what happened /
        # what was done" surface, kept distinct from the entity graph.
        self._overview: dict[str, Any] | None = None
        self._dir_ready = False

    def ensure(self) -> None:
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True
        except Exception:  # noqa: BLE001
            logger.debug("working memory ensure() failed", exc_info=True)
        self._load()

    def _load(self) -> None:
        self._entities = {}
        self._overview = None
        try:
            if not self.path.exists():
                return
            doc = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
            ents = doc.get("entities") if isinstance(doc, dict) else None
            if isinstance(ents, dict):
                self._entities = {str(k): v for k, v in ents.items() if isinstance(v, dict)}
            ov = doc.get("overview") if isinstance(doc, dict) else None
            if isinstance(ov, dict):
                self._overview = ov
        except Exception:  # noqa: BLE001
            logger.debug("working memory load failed", exc_info=True)

    # ── mutation ─────────────────────────────────────────────────────────

    def upsert(
        self, *, type: str, fields: dict[str, Any], entity_id: str | None = None
    ) -> dict[str, Any] | None:
        if type not in ENTITY_TYPES:
            return None
        now = int(time.time() * 1000)
        with self._lock:
            if entity_id and entity_id in self._entities:
                ent = self._entities[entity_id]
                for k, v in (fields or {}).items():
                    if v is not None:
                        ent[k] = v
                ent["updatedAt"] = now
                self._save()
                return ent
            # New entity.
            eid = entity_id or self._new_id(type, fields)
            ent = {
                "id": eid,
                "type": type,
                "status": fields.get("status") or ("active" if type == "workstream"
                                                   else "open" if type == "open_thread"
                                                   else "noted"),
                "createdAt": now,
                "updatedAt": now,
            }
            for k, v in (fields or {}).items():
                if v is not None and k != "status":
                    ent[k] = v
            self._entities[eid] = ent
            self._save()
            return ent

    def set_overview(
        self, *, summary: str | None, actions_completed: list[str] | None = None
    ) -> dict[str, Any]:
        """Set the high-level overview (Call B's summary + actions-completed).

        Replaces the prior overview wholesale — Call B is given the current
        working memory and asked to produce the full, up-to-date summary each
        round, so this is a full-state set rather than a merge. Empty/missing
        fields are normalized so the renderer/persistence stay simple."""
        now = int(time.time() * 1000)
        clean_summary = summary.strip() if isinstance(summary, str) else ""
        clean_actions: list[str] = []
        for a in actions_completed or []:
            if isinstance(a, str) and a.strip():
                clean_actions.append(a.strip())
        with self._lock:
            self._overview = {
                "summary": clean_summary,
                "actionsCompleted": clean_actions,
                "updatedAt": now,
            }
            self._save()
            return self._overview

    def overview(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._overview) if self._overview else None

    def resolve(self, entity_id: str) -> dict[str, Any] | None:
        with self._lock:
            ent = self._entities.get(entity_id)
            if ent is None:
                return None
            ent["status"] = "resolved" if ent.get("type") == "open_thread" else "done"
            ent["updatedAt"] = int(time.time() * 1000)
            self._save()
            return ent

    def _new_id(self, type: str, fields: dict[str, Any]) -> str:
        prefix = _TYPE_PREFIX.get(type, "ent")
        primary = str(fields.get(_PRIMARY_FIELD.get(type, "")) or "")
        base = f"{prefix}-{_slug(primary)}"
        eid = base
        n = 2
        while eid in self._entities:
            eid = f"{base}-{n}"
            n += 1
        return eid

    # ── read ─────────────────────────────────────────────────────────────

    def list(
        self, *, type: str | None = None, workstream_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            snapshot = list(self._entities.values())
        out = []
        for e in snapshot:
            if type is not None and e.get("type") != type:
                continue
            if workstream_id is not None and e.get("workstreamId") != workstream_id:
                continue
            out.append(e)
        out.sort(key=lambda e: int(e.get("createdAt") or 0))
        return out

    def all(self) -> list[dict[str, Any]]:
        return self.list()

    def find_by_primary(
        self, *, type: str, value: str, workstream_id: str | None = None
    ) -> str | None:
        """Return the id of an existing entity of ``type`` whose primary field
        (case-insensitive) equals ``value``, optionally scoped to a workstream.
        Used by the compaction projection to update rather than duplicate when
        the summarizer re-emits the same decision/finding across rounds."""
        pf = _PRIMARY_FIELD.get(type)
        target = (value or "").strip().lower()
        if not pf or not target:
            return None
        with self._lock:
            snapshot = list(self._entities.values())
        for e in snapshot:
            if e.get("type") != type:
                continue
            if workstream_id is not None and e.get("workstreamId") != workstream_id:
                continue
            if str(e.get(pf) or "").strip().lower() == target:
                return str(e.get("id"))
        return None

    def render(self, *, ledger_effects: list[dict[str, Any]] | None = None) -> str | None:
        entity_text = render_working_memory(self.all(), ledger_effects=ledger_effects)
        overview_text = self._render_overview()
        if overview_text and entity_text:
            return f"{overview_text}\n\n{entity_text}"
        return overview_text or entity_text

    def _render_overview(self) -> str | None:
        with self._lock:
            ov = dict(self._overview) if self._overview else None
        if not ov:
            return None
        summary = str(ov.get("summary") or "").strip()
        actions = [a for a in (ov.get("actionsCompleted") or []) if str(a).strip()]
        if not summary and not actions:
            return None
        lines: list[str] = ["## Summary"]
        if summary:
            lines.append(summary)
        if actions:
            lines.append("")
            lines.append("## Actions completed")
            for a in actions:
                lines.append(f"- {a}")
        return "\n".join(lines)

    def is_empty(self) -> bool:
        with self._lock:
            if self._entities:
                return False
            ov = self._overview or {}
            return not (ov.get("summary") or ov.get("actionsCompleted"))

    # ── persistence ──────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            if not self._dir_ready:
                self.project_dir.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            doc = {
                "version": 1,
                "sessionId": self.session_id,
                "overview": self._overview,
                "entities": self._entities,
            }
            tmp = self.path.with_suffix(".json.tmp")
            with self._lock:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            logger.debug("working memory save failed", exc_info=True)
