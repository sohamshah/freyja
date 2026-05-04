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
from pathlib import Path
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


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
            # Use rglob for recursive patterns, glob for non-recursive
            if "**" in pattern:
                matches = list(path.rglob(pattern.replace("**/", "")))
            else:
                matches = list(path.glob(pattern))

            # Filter out directories if pattern doesn't end with /
            if not pattern.endswith("/"):
                matches = [m for m in matches if m.is_file()]

            # Sort by modification time (newest first)
            matches.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)

            # Limit results
            total_matches = len(matches)
            matches = matches[:max_results]

            if not matches:
                return ToolResult(
                    call_id=call_id,
                    content=f"No files found matching pattern: {pattern}\nSearch directory: {path}",
                    is_error=False,
                )

            output_lines = [
                f"Found {total_matches} file(s) matching '{pattern}'",
                f"Search directory: {path}",
            ]
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

            # Get files to search
            if path.is_file():
                files_to_search = [path]
            else:
                if "**" in file_pattern or file_pattern == "*":
                    files_to_search = list(path.rglob("*"))
                else:
                    files_to_search = list(path.rglob(file_pattern))
                files_to_search = [f for f in files_to_search if f.is_file()]

            # Skip binary files and common non-text files
            skip_extensions = {
                ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
                ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp",
                ".pdf", ".zip", ".tar", ".gz", ".bz2",
                ".mp3", ".mp4", ".avi", ".mov",
                ".woff", ".woff2", ".ttf", ".eot",
            }

            for file_path in files_to_search:
                if file_path.suffix.lower() in skip_extensions:
                    continue

                # Skip hidden directories
                if any(part.startswith(".") for part in file_path.parts):
                    if ".git" in file_path.parts or ".venv" in file_path.parts:
                        continue

                try:
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

            if not results:
                return ToolResult(
                    call_id=call_id,
                    content=f"No matches found for pattern: {pattern}\nSearched {files_searched} file(s) in {path}",
                    is_error=False,
                )

            # Format output
            output_lines = [
                f"Found {len(results)} match(es) in {files_with_matches} file(s)",
                f"Pattern: {pattern}",
                f"Searched: {files_searched} file(s)",
            ]
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
