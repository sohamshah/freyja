"""Per-session memory file tool (B5 from the design doc).

Each session owns a single ``memory.md`` file at
``~/.freyja/projects/<safe_sid>/memory.md`` that the agent writes to as
a scratchpad for "what I'm working on right now" notes. The file lives
*outside* the transcript, so compaction never touches it — by
construction, anything the agent commits here survives every future
summary.

This is distinct from the existing ``memory`` tool, which is
workspace-scoped and intended for cross-session user/project
preferences. session_memory is *per-session, ephemeral* — meaningful
only inside one chat.

Actions:
  • read    — return the current contents (or empty string if none).
  • write   — replace the file with the provided ``content``.
  • append  — append ``content`` (with a timestamped header) to the
              existing file.
  • clear   — delete the file (recoverable from disk versioning, but
              the agent gets a clean slate).

We deliberately keep the surface tiny. Anything more ergonomic (search,
diff, structured updates) should live in a separate tool so the
contract here stays a markdown blob the agent owns end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bridge.project_paths import project_output_dir
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)


# Defensive cap to keep the file readable + cheap to load every turn.
# Crosses the "this is no longer a scratchpad" boundary; if the agent
# blows past it we truncate from the front (oldest entries die) so the
# tail (most-recent notes) stays intact.
_MAX_BYTES = 64 * 1024


class SessionMemoryTool:
    """Action-based per-session scratchpad backed by a markdown file."""

    ACTIONS = ("read", "write", "append", "clear")

    def __init__(
        self,
        *,
        session_id: str,
        on_mutation: Callable[[str], None] | None = None,
    ) -> None:
        self._session_id = session_id
        # Sync, fire-and-forget callback invoked AFTER a successful mutation
        # (write|append|clear; never read; never on error). It runs on the
        # bridge event loop — `execute()` invokes it AFTER awaiting
        # `loop.run_in_executor(...)`, so the executor thread has already
        # handed control back. The bridge's wrapper uses
        # `call_soon_threadsafe` defensively so the same callback stays
        # correct if a future call site (e.g. invoking from inside
        # `_execute_sync`) does run it on a worker thread.
        self._on_mutation = on_mutation

    @property
    def _path(self) -> Path:
        return project_output_dir(self._session_id) / "memory.md"

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="session_memory",
            summary="Per-session scratchpad that survives compaction",
            tier=ToolTier.WARM,
            description=(
                "Read or write to a per-session memory file that lives outside the "
                "conversation transcript. Anything you write here SURVIVES "
                "compaction by construction — the file is never summarized. Use it "
                "to stash facts, plans, or notes that you need to keep retrievable "
                "even after the conversation history is condensed.\n\n"
                "ACTIONS:\n"
                "  • read    — return current contents (empty string if none).\n"
                "  • write   — REPLACE the file with `content` (use sparingly; "
                "consider append first).\n"
                "  • append  — add `content` to the file under a timestamp header. "
                "Preferred for incremental notes.\n"
                "  • clear   — wipe the file.\n\n"
                "WHEN TO USE:\n"
                "  • You finished a sub-task and want to record what was done before "
                "potentially compacting the working details away.\n"
                "  • You discovered a key fact (file path, error string, decision) "
                "that needs to outlast the current context window.\n"
                "  • You're about to call summarize_context() and want to preserve "
                "structured notes without relying on the summary template.\n\n"
                "WHEN NOT TO USE: cross-session user preferences (use `memory`); "
                "task tracking (use `task_board`); long-form artifacts (use "
                "`write_file` under the project output dir)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(self.ACTIONS),
                    },
                    "content": {
                        "type": "string",
                        "description": "Body text for write / append. Markdown-friendly.",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._execute_sync, call_id, arguments
        )
        # Fire mutation callback ONLY on successful writes — never on read,
        # never on error. Mutations are the agent's natural "consolidate now"
        # signal; the bridge hooks Call B (working-memory extraction) here.
        if self._on_mutation is not None and not result.is_error:
            action = str(arguments.get("action") or "").strip().lower()
            if action in {"write", "append", "clear"}:
                try:
                    self._on_mutation(action)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "session_memory on_mutation callback failed",
                        exc_info=True,
                    )
        return result

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in self.ACTIONS:
            return _err(call_id, f"Unknown action `{action}`. "
                                  f"Valid: {', '.join(self.ACTIONS)}")
        path = self._path
        try:
            if action == "read":
                if not path.exists():
                    return _ok(call_id, {
                        "action": "read",
                        "path": str(path),
                        "exists": False,
                        "content": "",
                    })
                content = path.read_text(encoding="utf-8", errors="replace")
                return _ok(call_id, {
                    "action": "read",
                    "path": str(path),
                    "exists": True,
                    "bytes": len(content.encode("utf-8")),
                    "content": content,
                })
            if action == "write":
                content = str(arguments.get("content") or "")
                path.parent.mkdir(parents=True, exist_ok=True)
                truncated = False
                if len(content.encode("utf-8")) > _MAX_BYTES:
                    # Keep the tail — most recent text — and drop the head.
                    encoded = content.encode("utf-8")
                    content = encoded[-_MAX_BYTES:].decode("utf-8", errors="replace")
                    truncated = True
                path.write_text(content, encoding="utf-8")
                return _ok(call_id, {
                    "action": "write",
                    "path": str(path),
                    "bytes": len(content.encode("utf-8")),
                    "truncated_head": truncated,
                })
            if action == "append":
                content = str(arguments.get("content") or "").strip()
                if not content:
                    return _err(call_id, "append requires non-empty `content`")
                path.parent.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                block = f"\n\n## {stamp}\n\n{content}\n"
                existing = ""
                if path.exists():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                else:
                    existing = "# Session memory\n"
                combined = existing + block
                truncated = False
                if len(combined.encode("utf-8")) > _MAX_BYTES:
                    encoded = combined.encode("utf-8")
                    combined = encoded[-_MAX_BYTES:].decode("utf-8", errors="replace")
                    truncated = True
                path.write_text(combined, encoding="utf-8")
                return _ok(call_id, {
                    "action": "append",
                    "path": str(path),
                    "added_bytes": len(block.encode("utf-8")),
                    "total_bytes": len(combined.encode("utf-8")),
                    "truncated_head": truncated,
                })
            if action == "clear":
                if path.exists():
                    path.unlink()
                    return _ok(call_id, {"action": "clear", "path": str(path), "removed": True})
                return _ok(call_id, {"action": "clear", "path": str(path), "removed": False})
        except Exception as exc:  # noqa: BLE001
            logger.exception("session_memory failed")
            return _err(call_id, f"session_memory {action} failed: {exc}")
        return _err(call_id, "unreachable")


def _err(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)


def _ok(call_id: str, payload: dict[str, Any]) -> ToolResult:
    return ToolResult(call_id=call_id, content=json.dumps(payload, indent=2))
