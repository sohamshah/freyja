"""Memory tools for the Freyja bridge.

Two tool surfaces:

1. `record_user_preference` — the original append-only tool. Kept for
   back-compat with prompts and sessions that already use it.
2. `memory` — action-based tool covering the full lifecycle:
   list, record, show, update, delete, restore, merge. Use this for
   ongoing curation so the store doesn't fill with fragmented
   duplicates.

Every write goes through `MemoryStore`, which carries the session id +
actor through to the per-item audit trail on disk.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from bridge.knowledge.memory_store import MemoryStore
from bridge.knowledge.models import MemoryItem
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)


class RecordUserPreferenceTool:
    """Back-compat append-only tool.

    This is what older session transcripts call. Internally it routes
    through the same `record_preference` path the new `memory` tool's
    `record` action uses, so the audit trail still works.
    """

    def __init__(
        self,
        workspace: Path | str | None = None,
        memory_store: MemoryStore | None = None,
        on_memory_updated: Callable[[MemoryItem, str], Awaitable[None] | None] | None = None,
        session_id: str = "",
        actor: str = "",
    ):
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.memory_store = memory_store or MemoryStore(self.workspace)
        self.on_memory_updated = on_memory_updated
        self.session_id = session_id
        self.actor = actor

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="record_user_preference",
            summary="Record a new user preference",
            tier=ToolTier.WARM,
            description="""Record a user preference for future sessions.

PURPOSE: Store personal user preferences that should persist across sessions. This is
exclusively for understanding HOW the user wants to work, not WHAT was learned technically.

WHEN TO USE: Watch for explicit signals — "I prefer…", "Always…", "Don't…", corrections,
or consistent stylistic feedback. Make each memory specific and actionable.

CURATION: If you've recorded multiple fragments on the same topic, switch to the
`memory` tool (action=`merge` or action=`update`) to consolidate them. Don't keep
adding superseding entries — clean up in place.""",
            parameters={
                "type": "object",
                "properties": {
                    "preference": {
                        "type": "string",
                        "description": "The user preference to remember. Be specific and actionable. Start with 'User prefers...', 'User likes...', 'User dislikes...', or similar.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "style",
                            "tone",
                            "formatting",
                            "workflow",
                            "code",
                            "communication",
                            "other",
                        ],
                        "description": "Category of preference.",
                    },
                },
                "required": ["preference"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        loop = asyncio.get_running_loop()
        result, item = await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )
        if item is not None and self.on_memory_updated is not None:
            emitted = self.on_memory_updated(item, "record_user_preference")
            if inspect.isawaitable(emitted):
                await emitted
        return result

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> tuple[ToolResult, MemoryItem | None]:
        preference = arguments.get("preference", "").strip()
        category = arguments.get("category", "").strip() or "other"

        if not preference:
            return (
                ToolResult(
                    call_id=call_id,
                    content="Error: preference is required",
                    is_error=True,
                ),
                None,
            )

        try:
            item = self.memory_store.record_preference(
                preference,
                category,
                session_id=self.session_id,
                actor=self.actor or "record_user_preference",
            )
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Remembered: {preference[:100]}{'...' if len(preference) > 100 else ''}",
                ),
                item,
            )
        except PermissionError:
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Error: Permission denied writing memory files in {self.workspace}",
                    is_error=True,
                ),
                None,
            )
        except Exception as e:
            return (
                ToolResult(
                    call_id=call_id,
                    content=f"Error writing memory: {e}",
                    is_error=True,
                ),
                None,
            )


class MemoryTool:
    """Action-based memory curation surface.

    Actions: list, show, record, update, delete, restore, merge.
    Every write carries the calling session id + actor label so the
    per-memory audit trail reflects who made the change.
    """

    def __init__(
        self,
        workspace: Path | str | None = None,
        memory_store: MemoryStore | None = None,
        on_memory_updated: Callable[[MemoryItem, str], Awaitable[None] | None] | None = None,
        session_id: str = "",
        actor: str = "",
    ):
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.memory_store = memory_store or MemoryStore(self.workspace)
        self.on_memory_updated = on_memory_updated
        self.session_id = session_id
        self.actor = actor

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="memory",
            summary="Curate persistent user/project memory",
            tier=ToolTier.WARM,
            description="""Inspect and curate the persistent memory store.

ACTIONS:
- list: enumerate all active (non-archived) memories. Use this before recording
  a new preference to check for duplicates or candidates to update/merge.
- show: full body + audit trail for one memory id.
- record: create a new memory. Equivalent to the legacy `record_user_preference`
  tool but with session attribution.
- update: edit an existing memory in place by id (text, kind, scope, or tags).
- delete: archive a memory by id. Archived items stop surfacing in agent
  context but remain on disk with their audit trail intact.
- restore: un-archive a previously-deleted memory.
- merge: combine multiple memories into one canonical entry. Archives the
  sources, creates a new item whose `supersedes` lists their ids.

CURATION GUIDANCE:
- Prefer one canonical entry over many fragments. If you find yourself
  about to record a preference that's a refinement of an existing one,
  use `update` or `merge` instead of `record`.
- Every change is logged with your session id + actor label, so undoing
  bad agent edits is straightforward.""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "show",
                            "record",
                            "update",
                            "delete",
                            "restore",
                            "merge",
                        ],
                    },
                    "id": {
                        "type": "string",
                        "description": "Memory id (for show / update / delete / restore).",
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Memory ids to merge into a new canonical entry.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Body text (for record / update / merge).",
                    },
                    "category": {
                        "type": "string",
                        "description": "Memory category / kind. Accepts style, tone, formatting, workflow, code, communication, other, or any custom string.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["user", "project", "session", "subagent"],
                        "description": "Scope of the memory. Defaults to user.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags to attach (replaces the existing list on update).",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional audit note (why this change was made).",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "For list — include soft-deleted items.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "For list — cap the number returned.",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        loop = asyncio.get_running_loop()
        result, item = await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )
        if item is not None and self.on_memory_updated is not None:
            emitted = self.on_memory_updated(item, f"memory_{arguments.get('action', 'unknown')}")
            if inspect.isawaitable(emitted):
                await emitted
        return result

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> tuple[ToolResult, MemoryItem | None]:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return self._err(call_id, "action is required"), None

        try:
            if action == "list":
                include_archived = bool(arguments.get("include_archived") or False)
                limit_raw = arguments.get("limit")
                limit = int(limit_raw) if isinstance(limit_raw, (int, str)) and str(limit_raw).strip() else None
                items = self.memory_store.list_items(limit=limit, include_archived=include_archived)
                payload = {
                    "action": "list",
                    "count": len(items),
                    "items": [self._summarize(i) for i in items],
                }
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps(payload, indent=2),
                    ),
                    None,
                )

            if action == "show":
                item_id = str(arguments.get("id") or "").strip()
                if not item_id:
                    return self._err(call_id, "id is required for show"), None
                item = self.memory_store.get_item(item_id)
                if item is None:
                    return self._err(call_id, f"memory {item_id!r} not found"), None
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "show", "item": item.to_event()}, indent=2),
                    ),
                    None,
                )

            if action == "record":
                text = str(arguments.get("text") or arguments.get("preference") or "").strip()
                if not text:
                    return self._err(call_id, "text is required for record"), None
                category = (str(arguments.get("category") or "other")).strip() or "other"
                scope = (str(arguments.get("scope") or "user")).strip() or "user"
                item = self.memory_store.record_preference(
                    text,
                    category,
                    session_id=self.session_id,
                    actor=self.actor or "memory_tool",
                    scope=scope,
                )
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "record", "item": item.to_event()}, indent=2),
                    ),
                    item,
                )

            if action == "update":
                item_id = str(arguments.get("id") or "").strip()
                if not item_id:
                    return self._err(call_id, "id is required for update"), None
                text = arguments.get("text")
                kind = arguments.get("category") or arguments.get("kind")
                scope = arguments.get("scope")
                tags = arguments.get("tags")
                note = str(arguments.get("note") or "")
                tags_list: list[str] | None = None
                if isinstance(tags, list):
                    tags_list = [str(t) for t in tags]
                item = self.memory_store.update_item(
                    item_id,
                    text=str(text) if isinstance(text, str) else None,
                    kind=str(kind) if isinstance(kind, str) else None,
                    scope=str(scope) if isinstance(scope, str) else None,
                    tags=tags_list,
                    session_id=self.session_id,
                    actor=self.actor or "memory_tool",
                    note=note,
                )
                if item is None:
                    return self._err(call_id, f"memory {item_id!r} not found"), None
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "update", "item": item.to_event()}, indent=2),
                    ),
                    item,
                )

            if action == "delete":
                item_id = str(arguments.get("id") or "").strip()
                if not item_id:
                    return self._err(call_id, "id is required for delete"), None
                note = str(arguments.get("note") or "")
                item = self.memory_store.delete_item(
                    item_id,
                    session_id=self.session_id,
                    actor=self.actor or "memory_tool",
                    note=note,
                )
                if item is None:
                    return self._err(call_id, f"memory {item_id!r} not found"), None
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "delete", "item": item.to_event()}, indent=2),
                    ),
                    item,
                )

            if action == "restore":
                item_id = str(arguments.get("id") or "").strip()
                if not item_id:
                    return self._err(call_id, "id is required for restore"), None
                note = str(arguments.get("note") or "")
                item = self.memory_store.restore_item(
                    item_id,
                    session_id=self.session_id,
                    actor=self.actor or "memory_tool",
                    note=note,
                )
                if item is None:
                    return self._err(call_id, f"memory {item_id!r} not found"), None
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "restore", "item": item.to_event()}, indent=2),
                    ),
                    item,
                )

            if action == "merge":
                ids_raw = arguments.get("ids") or []
                if not isinstance(ids_raw, list) or len(ids_raw) < 2:
                    return self._err(call_id, "merge requires at least 2 ids"), None
                ids = [str(i) for i in ids_raw if str(i).strip()]
                text = str(arguments.get("text") or "").strip()
                if not text:
                    return self._err(call_id, "merge requires text for the new canonical entry"), None
                kind = (str(arguments.get("category") or arguments.get("kind") or "")).strip()
                scope = (str(arguments.get("scope") or "")).strip()
                tags_in = arguments.get("tags")
                tags_list: list[str] | None = None
                if isinstance(tags_in, list):
                    tags_list = [str(t) for t in tags_in]
                note = str(arguments.get("note") or "")
                item = self.memory_store.merge_items(
                    ids,
                    text=text,
                    kind=kind,
                    scope=scope,
                    tags=tags_list,
                    session_id=self.session_id,
                    actor=self.actor or "memory_tool",
                    note=note,
                )
                if item is None:
                    return self._err(call_id, "merge failed — no matching source memories"), None
                return (
                    ToolResult(
                        call_id=call_id,
                        content=json.dumps({"action": "merge", "item": item.to_event()}, indent=2),
                    ),
                    item,
                )

            return self._err(call_id, f"unknown action {action!r}"), None

        except Exception as e:
            logger.exception("memory tool error")
            return self._err(call_id, f"memory tool error: {e}"), None

    @staticmethod
    def _err(call_id: str, message: str) -> ToolResult:
        return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)

    @staticmethod
    def _summarize(item: MemoryItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "scope": item.scope,
            "kind": item.kind,
            "summary": item.summary or item.text[:140],
            "tags": item.tags,
            "archived": item.archived,
            "createdAt": item.created_at,
            "updatedAt": item.updated_at,
            "createdBySession": item.created_by_session,
            "createdByActor": item.created_by_actor,
            "supersedes": item.supersedes,
            "supersededBy": item.superseded_by,
            "revisionCount": len(item.revisions),
        }
