"""
Search tools for the CLI agent.

Provides glob (file pattern matching) and grep (content search) functionality.
All tools are async-native, using executor for I/O operations.
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

# ── traversal bounds ────────────────────────────────────────────────────
#
# Both tools used to do `list(path.rglob("*"))` — materializing the whole
# tree before looking at a single file, then stat()ing every entry. Pointed
# at a monorepo checkout (~/work/services: 118 GB, 2.36M files) that never
# returns: the walk alone is minutes, and a pattern with no match never
# trips the max_results early-exit. A Slack turn did exactly this and hung
# indefinitely with the agent loop blocked on the tool.
#
# The fix is to stream the walk, prune the directories that hold nearly all
# of those files, and stop on a wall-clock deadline. Partial results are
# always labelled — a silently truncated search reads as "no matches",
# which is a wrong answer rather than a slow one.

#: Directories pruned in-place during os.walk so we never descend into
#: them. node_modules and .git dominate the file count in any real repo.
PRUNE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "site-packages", "__pycache__",
    "node_modules", "bower_components",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
    ".next", ".nuxt", ".svelte-kit", ".parcel-cache", ".cache",
    ".terraform", ".gradle", ".m2",
    ".worktrees", ".deps",
    "dist", "build", "target", "vendor",
})

#: Wall-clock ceiling for a single search. Generous for any sane tree,
#: decisive on a pathological one.
WALK_DEADLINE_SEC = 20.0

#: Hard cap on files opened and read, independent of the deadline.
MAX_FILES_SCANNED = 20_000

#: Files above this are skipped rather than read into memory. Source files
#: are far below it; minified bundles and data dumps are far above.
MAX_FILE_BYTES = 2 * 1024 * 1024


def iter_files(
    root: Path,
    *,
    deadline: float,
    prune: frozenset[str] = PRUNE_DIRS,
) -> Iterator[Path]:
    """Yield files under ``root``, pruning junk directories, lazily.

    Lazy matters as much as the pruning: the caller can stop on its own
    budget without waiting for the traversal to finish. Symlinks are not
    followed — a self-referential link would otherwise loop forever.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() > deadline:
            return
        # In-place mutation is what makes os.walk skip the subtree.
        dirnames[:] = [d for d in dirnames if d not in prune]
        for name in filenames:
            yield Path(dirpath) / name


class GlobTool:
    """
    Find files matching a glob pattern.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob",
            summary="Find files by glob pattern",
            tier=ToolTier.HOT,
            description="""Find files matching a glob pattern.

Supports standard glob patterns:
- * matches any characters except /
- ** matches any characters including /
- ? matches a single character
- [abc] matches any character in brackets

Examples:
- "*.py" - all Python files in current directory
- "**/*.py" - all Python files recursively
- "src/**/*.ts" - TypeScript files in src directory
- "test_*.py" - files starting with test_""",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The glob pattern to match files against",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory to search in (defaults to current directory)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 100)",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute glob search asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        pattern = arguments.get("pattern", "")
        base_path = arguments.get("path", ".")
        max_results = arguments.get("max_results", 100)

        if not pattern:
            return ToolResult(
                call_id=call_id,
                content="Error: pattern is required",
                is_error=True,
            )

        path = Path(base_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: Directory not found: {path}",
                is_error=True,
            )

        try:
            stopped_early: str | None = None
            deadline = time.monotonic() + WALK_DEADLINE_SEC

            # Recursive patterns walk with pruning + a deadline; a plain
            # glob is already bounded to one directory level.
            if "**" in pattern:
                leaf = pattern.replace("**/", "") or "*"
                pairs: list[tuple[float, Path]] = []
                for candidate in iter_files(path, deadline=deadline):
                    if time.monotonic() > deadline:
                        stopped_early = f"time limit ({WALK_DEADLINE_SEC:.0f}s)"
                        break
                    if not fnmatch.fnmatch(candidate.name, leaf):
                        continue
                    try:
                        pairs.append((candidate.stat().st_mtime, candidate))
                    except OSError:
                        continue
                    if len(pairs) >= MAX_FILES_SCANNED:
                        stopped_early = f"file limit ({MAX_FILES_SCANNED:,} files)"
                        break
                # Sort the survivors, not the whole tree.
                pairs.sort(key=lambda p: p[0], reverse=True)
                total_matches = len(pairs)
                matches = [p for _, p in pairs[:max_results]]
            else:
                matches = list(path.glob(pattern))
                if not pattern.endswith("/"):
                    matches = [m for m in matches if m.is_file()]
                matches.sort(
                    key=lambda x: x.stat().st_mtime if x.exists() else 0,
                    reverse=True,
                )
                total_matches = len(matches)
                matches = matches[:max_results]

            incomplete_note = (
                f"⚠️ INCOMPLETE: stopped at the {stopped_early}. This listing "
                f"is partial — narrow `path` and search again."
                if stopped_early else ""
            )

            if not matches:
                verdict = (
                    f"No files matched '{pattern}' before the {stopped_early} — "
                    "this is NOT a confirmed absence."
                    if stopped_early
                    else f"No files found matching pattern: {pattern}"
                )
                return ToolResult(
                    call_id=call_id,
                    content=f"{verdict}\nSearch directory: {path}",
                    is_error=False,
                )

            output_lines = [
                f"Found {total_matches} file(s) matching '{pattern}'",
                f"Search directory: {path}",
            ]
            if incomplete_note:
                output_lines.append(incomplete_note)
            if total_matches > max_results:
                output_lines.append(f"(showing first {max_results})")
            output_lines.append("-" * 60)

            for match in matches:
                try:
                    rel_path = match.relative_to(path)
                except ValueError:
                    rel_path = match
                output_lines.append(str(rel_path))

            return ToolResult(
                call_id=call_id,
                content="\n".join(output_lines),
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error searching files: {e}",
                is_error=True,
            )


class GrepTool:
    """
    Search file contents using regex patterns.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep",
            summary="Search file contents for patterns",
            tier=ToolTier.HOT,
            description="""Search file contents for a pattern.

Uses regular expressions for powerful pattern matching.
Returns matching lines with file paths and line numbers.

Examples:
- "def main" - find function definitions
- "TODO|FIXME" - find todo comments
- "import.*requests" - find requests imports""",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in (defaults to current directory)",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob pattern to filter files (e.g., '*.py'). Default: all files",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive search. Default: true",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of context lines before/after match. Default: 0",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default: 50",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute grep search asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        pattern = arguments.get("pattern", "")
        base_path = arguments.get("path", ".")
        file_pattern = arguments.get("file_pattern", "*")
        case_sensitive = arguments.get("case_sensitive", True)
        context_lines = arguments.get("context_lines", 0)
        max_results = arguments.get("max_results", 50)

        if not pattern:
            return ToolResult(
                call_id=call_id,
                content="Error: pattern is required",
                is_error=True,
            )

        path = Path(base_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: Path not found: {path}",
                is_error=True,
            )

        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: Invalid regex pattern: {e}",
                is_error=True,
            )

        try:
            results = []
            files_searched = 0
            files_with_matches = 0
            stopped_early: str | None = None

            deadline = time.monotonic() + WALK_DEADLINE_SEC

            # Streamed, not materialized — see the traversal-bounds notes at
            # the top of this module.
            if path.is_file():
                candidates: Iterator[Path] = iter([path])
            else:
                candidates = iter_files(path, deadline=deadline)

            # Skip binary files and common non-text files
            skip_extensions = {
                ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
                ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp",
                ".pdf", ".zip", ".tar", ".gz", ".bz2",
                ".mp3", ".mp4", ".avi", ".mov",
                ".woff", ".woff2", ".ttf", ".eot",
            }

            for file_path in candidates:
                if time.monotonic() > deadline:
                    stopped_early = f"time limit ({WALK_DEADLINE_SEC:.0f}s)"
                    break
                if files_searched >= MAX_FILES_SCANNED:
                    stopped_early = f"file limit ({MAX_FILES_SCANNED:,} files)"
                    break

                if file_path.suffix.lower() in skip_extensions:
                    continue

                # `file_pattern` used to select the rglob; with a streamed
                # walk it becomes an explicit filter on the name.
                if file_pattern not in ("*", "**/*") and not fnmatch.fnmatch(
                    file_path.name, file_pattern.rsplit("/", 1)[-1]
                ):
                    continue

                try:
                    # stat before open: skips huge files without reading them,
                    # and doubles as the is_file()/broken-symlink check.
                    st = file_path.stat()
                    if not os.path.isfile(file_path) or st.st_size > MAX_FILE_BYTES:
                        continue
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    files_searched += 1
                except Exception:
                    continue

                file_has_match = False
                for i, line in enumerate(lines):
                    if regex.search(line):
                        if not file_has_match:
                            file_has_match = True
                            files_with_matches += 1

                        if len(results) >= max_results:
                            break

                        # Get context lines
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)

                        try:
                            rel_path = file_path.relative_to(path)
                        except ValueError:
                            rel_path = file_path

                        match_info = {
                            "file": str(rel_path),
                            "line_num": i + 1,
                            "line": line.rstrip("\n\r"),
                            "context_before": [
                                (start + j + 1, lines[start + j].rstrip("\n\r"))
                                for j in range(i - start)
                            ] if context_lines > 0 else [],
                            "context_after": [
                                (i + 2 + j, lines[i + 1 + j].rstrip("\n\r"))
                                for j in range(end - i - 1)
                            ] if context_lines > 0 else [],
                        }
                        results.append(match_info)

                if len(results) >= max_results:
                    break

            # A cut-short search that found nothing is NOT "no matches" —
            # saying so would hand the model a false negative it has no way
            # to detect. Name the limit and how to narrow the search.
            incomplete_note = (
                f"\n⚠️ INCOMPLETE: stopped at the {stopped_early} after "
                f"{files_searched} file(s). Results below are partial — narrow "
                f"`path`, or set `file_pattern`, and search again."
                if stopped_early else ""
            )

            if not results:
                verdict = (
                    f"No matches found in the {files_searched} file(s) searched "
                    f"before the {stopped_early} — this is NOT a confirmed absence."
                    if stopped_early
                    else f"No matches found for pattern: {pattern}"
                )
                return ToolResult(
                    call_id=call_id,
                    content=f"{verdict}\nSearched {files_searched} file(s) in {path}{incomplete_note}",
                    is_error=False,
                )

            # Format output
            output_lines = [
                f"Found {len(results)} match(es) in {files_with_matches} file(s)",
                f"Pattern: {pattern}",
                f"Searched: {files_searched} file(s)",
            ]
            if incomplete_note:
                output_lines.append(incomplete_note.strip())
            if len(results) >= max_results:
                output_lines.append(f"(showing first {max_results} results)")
            output_lines.append("-" * 60)

            current_file = None
            for match in results:
                if match["file"] != current_file:
                    current_file = match["file"]
                    output_lines.append(f"\n{current_file}:")

                # Context before
                for line_num, content in match["context_before"]:
                    output_lines.append(f"  {line_num:4d}  {content}")

                # The matching line
                output_lines.append(f"  {match['line_num']:4d}> {match['line']}")

                # Context after
                for line_num, content in match["context_after"]:
                    output_lines.append(f"  {line_num:4d}  {content}")

            return ToolResult(
                call_id=call_id,
                content="\n".join(output_lines),
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error searching files: {e}",
                is_error=True,
            )
