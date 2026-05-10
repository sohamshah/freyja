"""Tool for listing and reading verified session artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bridge.artifact_store import SessionArtifactStore
from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


class ArtifactsTool:
    """Expose the session artifact manifest to agents."""

    def __init__(self, artifact_store: SessionArtifactStore) -> None:
        self._store = artifact_store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="artifacts",
            summary="List and read verified files produced in this session",
            tier=ToolTier.HOT,
            description="""List or read verified files produced by the parent or sub-agents.

Use this instead of filesystem guessing when you need handoff files, generated
reports, subagent artifacts, or any files created during the session. The
manifest records resolved absolute paths, creator session, operation, size,
and timestamps.""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "manifest"],
                        "description": "list artifacts, read one artifact, or show manifest location",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Artifact id, filename, or absolute path for read",
                    },
                    "creator_id": {
                        "type": "string",
                        "description": "Optional creator/subagent session id filter for list",
                    },
                    "start_line": {"type": "integer", "description": "1-indexed start line for read"},
                    "end_line": {"type": "integer", "description": "Inclusive end line for read"},
                },
                "required": ["action"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            if action == "manifest":
                return ToolResult(
                    call_id=call_id,
                    content=json.dumps(
                        {
                            "project_dir": str(self._store.project_dir),
                            "manifest": str(self._store.manifest_path),
                        },
                        indent=2,
                    ),
                    is_error=False,
                )
            if action == "list":
                rows = self._store.latest_by_path(
                    creator_id=str(arguments.get("creator_id") or "").strip() or None
                )
                return ToolResult(
                    call_id=call_id,
                    content=json.dumps(
                        {
                            "project_dir": str(self._store.project_dir),
                            "manifest": str(self._store.manifest_path),
                            "count": len(rows),
                            "artifacts": rows,
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    is_error=False,
                )
            if action == "read":
                ref = str(arguments.get("ref") or "").strip()
                if not ref:
                    return ToolResult(call_id=call_id, content="Error: ref is required", is_error=True)
                path = self._store.resolve_ref(ref)
                if path is None:
                    return ToolResult(call_id=call_id, content=f"Error: artifact not found: {ref}", is_error=True)
                return self._read_file(call_id, path, arguments)
            return ToolResult(call_id=call_id, content=f"Error: unknown action {action}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call_id=call_id, content=f"Artifacts error: {exc}", is_error=True)

    def _read_file(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        if not path.exists() or not path.is_file():
            return ToolResult(call_id=call_id, content=f"Error: not a file: {path}", is_error=True)
        start_line = max(1, int(arguments.get("start_line") or 1))
        end_arg = arguments.get("end_line")
        end_line = int(end_arg) if end_arg is not None else None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call_id=call_id, content=f"Error reading artifact: {exc}", is_error=True)
        start_idx = start_line - 1
        end_idx = end_line if end_line is not None else len(lines)
        selected = lines[start_idx:end_idx]
        body = "\n".join(
            f"{idx:6d}| {line}"
            for idx, line in enumerate(selected, start=start_idx + 1)
        )
        return ToolResult(
            call_id=call_id,
            content=(
                f"Artifact: {path}\n"
                f"Lines: {start_idx + 1}-{min(end_idx, len(lines))} of {len(lines)}\n"
                f"{'-' * 60}\n"
                f"{body}"
            ),
            is_error=False,
        )
