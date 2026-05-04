"""Passive file-change detection for Freyja bridge tools.

This module observes existing tools. It does not change tool permissions,
arguments, execution limits, or results. The bridge uses it to emit structured
file-change metadata after a successful mutating tool call.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DIRECT_FILE_TOOLS = frozenset({"write_file", "write", "edit_file", "edit", "edit_json"})

MAX_DIFF_CHARS = 60_000
MAX_DIRECT_FILE_BYTES = 1_000_000
MAX_BASH_FILE_BYTES = 256_000
MAX_BASH_TOTAL_BYTES = 3_000_000
MAX_BASH_FILES = 2_500
MAX_BASH_CHANGES = 80

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".turbo",
        ".cache",
        "node_modules",
        "dist",
        "dist-main",
        "dist-preload",
        "dist-renderer",
        "build",
        "out",
        "target",
        "python-bundle",
    }
)

BASH_MUTATION_HINTS = (
    ">",
    ">>",
    "tee ",
    "touch ",
    "mkdir ",
    "mv ",
    "cp ",
    "rm ",
    "sed -i",
    "perl -pi",
    "<<",
    "write_text",
    ".write(",
    "json.dump",
    "npm ",
    "pnpm ",
    "yarn ",
    "uv ",
    "pip ",
)


@dataclass
class FileSnapshot:
    path: Path
    exists: bool
    is_file: bool = False
    size: int = 0
    mtime_ns: int = 0
    sha256: str | None = None
    text: str | None = None
    binary: bool = False
    readable: bool = True
    too_large: bool = False


class FileChangeTracker:
    def finish(self, *, success: bool) -> dict[str, Any] | None:
        raise NotImplementedError


class NoopFileChangeTracker(FileChangeTracker):
    def finish(self, *, success: bool) -> dict[str, Any] | None:
        return None


class DirectFileChangeTracker(FileChangeTracker):
    def __init__(self, *, call_id: str, tool_name: str, path: Path, cwd: Path) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.path = path
        self.cwd = cwd
        self.before = read_file_snapshot(path, max_bytes=MAX_DIRECT_FILE_BYTES)

    def finish(self, *, success: bool) -> dict[str, Any] | None:
        if not success:
            return None
        after = read_file_snapshot(self.path, max_bytes=MAX_DIRECT_FILE_BYTES)
        change = build_file_change(self.path, self.before, after)
        if change is None:
            return None
        return build_change_set(
            call_id=self.call_id,
            tool_name=self.tool_name,
            source="tool",
            cwd=self.cwd,
            changes=[change],
        )


class BashFileChangeTracker(FileChangeTracker):
    def __init__(self, *, call_id: str, tool_name: str, root: Path) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.root = root
        self.before = WorkspaceSnapshot.capture(root)

    def finish(self, *, success: bool) -> dict[str, Any] | None:
        if not success or not self.before.complete:
            return None
        after = WorkspaceSnapshot.capture(self.root)
        if not after.complete:
            return None
        changes = self.before.diff(after)
        if not changes:
            return None
        truncated = len(changes) > MAX_BASH_CHANGES
        if truncated:
            changes = changes[:MAX_BASH_CHANGES]
        change_set = build_change_set(
            call_id=self.call_id,
            tool_name=self.tool_name,
            source="bash",
            cwd=self.root,
            changes=changes,
        )
        if change_set is not None:
            change_set["truncated"] = truncated
        return change_set


@dataclass
class WorkspaceSnapshot:
    root: Path
    files: dict[str, FileSnapshot]
    complete: bool

    @classmethod
    def capture(cls, root: Path) -> "WorkspaceSnapshot":
        root = root.expanduser().resolve()
        files: dict[str, FileSnapshot] = {}
        if not root.exists() or not root.is_dir():
            return cls(root=root, files=files, complete=False)

        total_bytes = 0
        file_count = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in SKIP_DIRS and not d.startswith(".freyja")
                ]
                base = Path(dirpath)
                for filename in filenames:
                    path = base / filename
                    file_count += 1
                    if file_count > MAX_BASH_FILES:
                        return cls(root=root, files=files, complete=False)
                    try:
                        rel = path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    snap = read_file_snapshot(path, max_bytes=MAX_BASH_FILE_BYTES)
                    if snap.size <= MAX_BASH_FILE_BYTES:
                        total_bytes += snap.size
                    if total_bytes > MAX_BASH_TOTAL_BYTES:
                        return cls(root=root, files=files, complete=False)
                    files[rel] = snap
        except Exception:
            return cls(root=root, files=files, complete=False)

        return cls(root=root, files=files, complete=True)

    def diff(self, after: "WorkspaceSnapshot") -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        all_paths = sorted(set(self.files) | set(after.files))
        for rel in all_paths:
            before_snap = self.files.get(rel)
            after_snap = after.files.get(rel)
            path = after_snap.path if after_snap is not None else (self.root / rel)
            change = build_file_change(path, before_snap, after_snap)
            if change is not None:
                changes.append(change)
        return changes


def create_file_change_tracker(
    *,
    call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> FileChangeTracker:
    try:
        if tool_name in DIRECT_FILE_TOOLS:
            path = _path_from_arguments(arguments)
            if path is None:
                return NoopFileChangeTracker()
            return DirectFileChangeTracker(
                call_id=call_id,
                tool_name=tool_name,
                path=path,
                cwd=Path.cwd(),
            )

        if tool_name == "bash" and _bash_change_scan_enabled(arguments):
            working_dir = Path(
                str(arguments.get("working_dir") or os.environ.get("FREYJA_WORKSPACE") or Path.cwd())
            ).expanduser()
            if not working_dir.is_absolute():
                working_dir = Path.cwd() / working_dir
            return BashFileChangeTracker(
                call_id=call_id,
                tool_name=tool_name,
                root=working_dir,
            )
    except Exception:
        return NoopFileChangeTracker()

    return NoopFileChangeTracker()


def read_file_snapshot(path: Path, *, max_bytes: int) -> FileSnapshot:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    try:
        stat = path.stat()
    except FileNotFoundError:
        return FileSnapshot(path=path, exists=False)
    except Exception:
        return FileSnapshot(path=path, exists=True, readable=False)

    if not path.is_file():
        return FileSnapshot(
            path=path,
            exists=True,
            is_file=False,
            size=int(getattr(stat, "st_size", 0) or 0),
            mtime_ns=int(getattr(stat, "st_mtime_ns", 0) or 0),
            readable=False,
        )

    size = int(stat.st_size)
    mtime_ns = int(stat.st_mtime_ns)
    if size > max_bytes:
        return FileSnapshot(
            path=path,
            exists=True,
            is_file=True,
            size=size,
            mtime_ns=mtime_ns,
            too_large=True,
        )

    try:
        data = path.read_bytes()
    except Exception:
        return FileSnapshot(
            path=path,
            exists=True,
            is_file=True,
            size=size,
            mtime_ns=mtime_ns,
            readable=False,
        )

    sha = hashlib.sha256(data).hexdigest()[:16]
    try:
        text = data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = None
        binary = True

    return FileSnapshot(
        path=path,
        exists=True,
        is_file=True,
        size=size,
        mtime_ns=mtime_ns,
        sha256=sha,
        text=text,
        binary=binary,
    )


def build_file_change(
    path: Path,
    before: FileSnapshot | None,
    after: FileSnapshot | None,
) -> dict[str, Any] | None:
    before = before or FileSnapshot(path=path, exists=False)
    after = after or FileSnapshot(path=path, exists=False)

    if not before.exists and not after.exists:
        return None

    if before.exists and not before.is_file and not after.exists:
        return None
    if after.exists and not after.is_file:
        return None

    if before.exists and after.exists:
        same_hash = before.sha256 is not None and before.sha256 == after.sha256
        same_meta = before.size == after.size and before.mtime_ns == after.mtime_ns
        if same_hash or (before.sha256 is None and after.sha256 is None and same_meta):
            return None
        operation = "update"
    elif after.exists:
        operation = "create"
    else:
        operation = "delete"

    before_text = before.text if before.exists else ""
    after_text = after.text if after.exists else ""
    binary = bool(before.binary or after.binary)
    too_large = bool(before.too_large or after.too_large)

    additions = 0
    deletions = 0
    diff_text: str | None = None
    diff_truncated = False

    if not binary and before_text is not None and after_text is not None and not too_large:
        additions, deletions = _line_stats(before_text, after_text)
        diff_text, diff_truncated = _unified_diff(
            before_text,
            after_text,
            fromfile=str(before.path),
            tofile=str(after.path),
        )
    else:
        if operation == "create" and after_text is not None:
            additions = _line_count(after_text)
        elif operation == "delete" and before_text is not None:
            deletions = _line_count(before_text)

    filename = path.name
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    return {
        "path": str(path),
        "filename": filename,
        "fileType": ext,
        "operation": operation,
        "additions": additions,
        "deletions": deletions,
        "beforeHash": before.sha256,
        "afterHash": after.sha256,
        "beforeSize": before.size if before.exists else 0,
        "afterSize": after.size if after.exists else 0,
        "beforeLineCount": _line_count(before_text) if before_text is not None else None,
        "afterLineCount": _line_count(after_text) if after_text is not None else None,
        "binary": binary,
        "tooLarge": too_large,
        "diff": diff_text,
        "diffTruncated": diff_truncated,
    }


def build_change_set(
    *,
    call_id: str,
    tool_name: str,
    source: str,
    cwd: Path,
    changes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not changes:
        return None
    additions = sum(int(c.get("additions") or 0) for c in changes)
    deletions = sum(int(c.get("deletions") or 0) for c in changes)
    return {
        "id": f"fcs_{call_id}",
        "toolCallId": call_id,
        "toolName": tool_name,
        "source": source,
        "cwd": str(cwd),
        "createdAt": int(time.time() * 1000),
        "files": changes,
        "totals": {
            "files": len(changes),
            "additions": additions,
            "deletions": deletions,
        },
        "summary": _summary_for_changes(changes, additions, deletions),
    }


def _path_from_arguments(arguments: dict[str, Any]) -> Path | None:
    raw = arguments.get("path") or arguments.get("file_path") or arguments.get("file")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _bash_change_scan_enabled(arguments: dict[str, Any]) -> bool:
    if os.environ.get("FREYJA_FILE_CHANGE_BASH", "1").lower() in {"0", "false", "no"}:
        return False
    command = str(arguments.get("command") or "")
    if not command.strip():
        return False
    compact = re.sub(r"\s+", " ", command.lower())
    return any(hint in compact for hint in BASH_MUTATION_HINTS)


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _line_stats(before_text: str, after_text: str) -> tuple[int, int]:
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    additions = 0
    deletions = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
        elif tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
    return additions, deletions


def _unified_diff(
    before_text: str,
    after_text: str,
    *,
    fromfile: str,
    tofile: str,
) -> tuple[str, bool]:
    lines = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
        n=4,
    )
    chunks: list[str] = []
    total = 0
    truncated = False
    for line in lines:
        if not line.endswith("\n"):
            line = f"{line}\n"
        total += len(line)
        if total > MAX_DIFF_CHARS:
            truncated = True
            break
        chunks.append(line)
    return "".join(chunks), truncated


def _summary_for_changes(
    changes: list[dict[str, Any]],
    additions: int,
    deletions: int,
) -> str:
    created = sum(1 for c in changes if c.get("operation") == "create")
    updated = sum(1 for c in changes if c.get("operation") == "update")
    deleted = sum(1 for c in changes if c.get("operation") == "delete")
    bits = []
    if created:
        bits.append(f"{created} created")
    if updated:
        bits.append(f"{updated} updated")
    if deleted:
        bits.append(f"{deleted} deleted")
    ops = ", ".join(bits) or f"{len(changes)} changed"
    return f"{ops} (+{additions} -{deletions})"
