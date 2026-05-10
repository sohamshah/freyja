"""Session-scoped artifact manifest and path resolution."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FILE_TOOL_NAMES = frozenset({"read_file", "write_file", "edit_file", "edit_json"})
MUTATING_FILE_TOOL_NAMES = frozenset({"write_file", "edit_file", "edit_json"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text_line_count(path: Path) -> int | None:
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _file_record_stats(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except Exception:
        return {"exists": False}
    return {
        "exists": path.exists(),
        "bytes": int(stat.st_size),
        "lines": _text_line_count(path),
        "sha256": _file_hash(path),
    }


@dataclass
class FilePathResolver:
    """Resolve tool paths against workspace and session project directories."""

    workspace: Path
    project_dir: Path

    def __post_init__(self) -> None:
        self.workspace = self.workspace.expanduser().resolve()
        self.project_dir = self.project_dir.expanduser().resolve()

    def resolve(
        self,
        raw_path: str,
        *,
        tool_name: str,
        base: str | None = None,
    ) -> Path:
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            raise ValueError("path is required")

        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path.resolve()

        clean_base = str(base or "auto").strip().lower()
        if clean_base in {"project", "artifact", "artifacts", "output"}:
            return (self.project_dir / path).resolve()
        if clean_base in {"workspace", "repo", "cwd"}:
            return (self.workspace / path).resolve()

        workspace_path = (self.workspace / path).resolve()
        project_path = (self.project_dir / path).resolve()

        if tool_name == "write_file":
            if workspace_path.exists():
                return workspace_path
            if project_path.exists():
                return project_path
            return project_path

        if tool_name in {"read_file", "edit_file", "edit_json"}:
            if workspace_path.exists():
                return workspace_path
            if project_path.exists():
                return project_path
            return workspace_path

        return workspace_path

    def normalize_tool_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in FILE_TOOL_NAMES or "path" not in arguments:
            return dict(arguments)
        next_args = dict(arguments)
        try:
            resolved = self.resolve(
                str(next_args.get("path") or ""),
                tool_name=tool_name,
                base=str(next_args.get("base") or next_args.get("location") or "auto"),
            )
        except Exception:
            return next_args
        next_args["path"] = str(resolved)
        next_args["resolvedPath"] = str(resolved)
        return next_args


class SessionArtifactStore:
    """Append-only artifact manifest for one parent session."""

    def __init__(self, *, session_id: str, project_dir: Path) -> None:
        self.session_id = session_id
        self.project_dir = project_dir.expanduser().resolve()
        self.manifest_path = self.project_dir / "manifest.jsonl"
        self._lock = threading.Lock()

    def ensure(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.import_legacy_artifacts()

    def import_legacy_artifacts(self) -> list[dict[str, Any]]:
        legacy_dir = Path.home() / ".freyja" / "sessions" / self.session_id / "artifacts"
        if not legacy_dir.exists() or not legacy_dir.is_dir():
            return []
        imported: list[dict[str, Any]] = []
        target_dir = self.project_dir / "artifacts" / "legacy"
        target_dir.mkdir(parents=True, exist_ok=True)
        seen_paths = {entry.get("path") for entry in self.list(include_legacy=False)}
        for source in sorted(legacy_dir.glob("*")):
            if not source.is_file():
                continue
            target = target_dir / source.name
            try:
                if not target.exists():
                    shutil.copy2(source, target)
                if str(target) in seen_paths:
                    continue
                imported.append(
                    self.record_file(
                        target,
                        creator_id="legacy",
                        creator_label="Legacy session artifact",
                        operation="subagent_artifact",
                        source="legacy",
                        metadata={"legacyPath": str(source)},
                    )
                )
            except Exception:
                continue
        return imported

    def record_file(
        self,
        path: str | Path,
        *,
        creator_id: str,
        creator_label: str,
        operation: str,
        source: str,
        tool_call_id: str | None = None,
        change_set_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = Path(path).expanduser().resolve()
        stats = _file_record_stats(path)
        entry = {
            "id": f"art_{hashlib.sha1(f'{path}:{time.time_ns()}'.encode()).hexdigest()[:12]}",
            "sessionId": self.session_id,
            "creatorId": creator_id,
            "creatorLabel": creator_label,
            "operation": operation,
            "source": source,
            "path": str(path),
            "filename": path.name,
            "fileType": path.suffix.lstrip(".").lower(),
            "createdAt": _now_ms(),
            "toolCallId": tool_call_id,
            "changeSetId": change_set_id,
            "metadata": metadata or {},
            **stats,
        }
        self._append(entry)
        return entry

    def record_change_set(
        self,
        change_set: dict[str, Any],
        *,
        creator_id: str,
        creator_label: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        files = change_set.get("files") or change_set.get("changes") or []
        for change in files:
            path = change.get("path")
            if not path:
                continue
            records.append(
                self.record_file(
                    path,
                    creator_id=creator_id,
                    creator_label=creator_label,
                    operation=str(change.get("operation") or "update"),
                    source=str(change_set.get("source") or "tool"),
                    tool_call_id=str(change_set.get("toolCallId") or change_set.get("callId") or ""),
                    change_set_id=str(change_set.get("id") or ""),
                    metadata={
                        "additions": change.get("additions"),
                        "deletions": change.get("deletions"),
                        "binary": change.get("binary"),
                        "diffTruncated": change.get("diffTruncated"),
                    },
                )
            )
        return records

    def list(
        self,
        *,
        creator_id: str | None = None,
        include_legacy: bool = True,
    ) -> list[dict[str, Any]]:
        if include_legacy:
            # Safe: import routine de-dupes copied paths.
            try:
                self.import_legacy_artifacts()
            except Exception:
                pass
        if not self.manifest_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if creator_id and row.get("creatorId") != creator_id:
                    continue
                rows.append(row)
        except Exception:
            return []
        return rows

    def latest_by_path(self, *, creator_id: str | None = None) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.list(creator_id=creator_id):
            path = str(row.get("path") or "")
            if not path:
                continue
            if path not in latest or int(row.get("createdAt") or 0) >= int(latest[path].get("createdAt") or 0):
                latest[path] = row
        return sorted(latest.values(), key=lambda row: int(row.get("createdAt") or 0), reverse=True)

    def paths_for_creator(self, creator_id: str) -> list[str]:
        return [str(row["path"]) for row in self.latest_by_path(creator_id=creator_id) if row.get("path")]

    def resolve_ref(self, ref: str) -> Path | None:
        ref = str(ref or "").strip()
        if not ref:
            return None
        path = Path(ref).expanduser()
        if path.is_absolute() and path.exists():
            return path.resolve()
        for row in self.list():
            if ref in {str(row.get("id") or ""), str(row.get("path") or ""), str(row.get("filename") or "")}:
                p = Path(str(row.get("path") or "")).expanduser()
                if p.exists():
                    return p.resolve()
        return None

    def _append(self, entry: dict[str, Any]) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        with self._lock:
            with self.manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
