"""
File system tools for the CLI agent.

Provides tools for reading, writing, and editing files.
All tools are async-native, using executor for I/O operations.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
from pathlib import Path
from typing import Any

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


class ReadFileTool:
    """
    Read the contents of a file.

    Supports reading text files with optional line range specification.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            summary="Read file contents",
            tier=ToolTier.HOT,
            description="""Read the contents of a file from the filesystem.

Use this tool to read text files. You can optionally specify a line range
to read only a portion of large files.

Returns the file contents with line numbers for easy reference.""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to read (absolute or relative to cwd)",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed). Defaults to 1.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Ending line number (inclusive). Defaults to end of file.",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute file read asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        path_str = arguments.get("path", "")
        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line")

        if not path_str:
            return ToolResult(
                call_id=call_id,
                content="Error: path is required",
                is_error=True,
            )

        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: File not found: {path}",
                is_error=True,
            )

        if not path.is_file():
            return ToolResult(
                call_id=call_id,
                content=f"Error: Not a file: {path}",
                is_error=True,
            )

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = end_line if end_line else total_lines

            selected_lines = lines[start_idx:end_idx]

            # Format with line numbers
            output_lines = []
            for i, line in enumerate(selected_lines, start=start_idx + 1):
                # Remove trailing newline for consistent formatting
                line_content = line.rstrip("\n\r")
                output_lines.append(f"{i:6d}| {line_content}")

            content = "\n".join(output_lines)

            # Add file info header
            header = f"File: {path}\n"
            if start_line > 1 or end_line:
                header += f"Lines: {start_idx + 1}-{min(end_idx, total_lines)} of {total_lines}\n"
            else:
                header += f"Total lines: {total_lines}\n"
            header += "-" * 60 + "\n"

            return ToolResult(
                call_id=call_id,
                content=header + content,
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error reading file: {e}",
                is_error=True,
            )


class WriteFileTool:
    """
    Write content to a file (creates or overwrites).

    Use with caution - this will overwrite existing files.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            summary="Create or overwrite files",
            tier=ToolTier.HOT,
            description="""Write content to a file, creating it if it doesn't exist or overwriting if it does.

WARNING: This will completely replace the file contents. For targeted edits,
use the edit_file tool instead.

Creates parent directories if they don't exist.

IMPORTANT: Both 'path' and 'content' parameters are required. The content
parameter must contain the actual file content - do not omit it.""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to write (absolute or relative to cwd)",
                    },
                    "content": {
                        "type": "string",
                        "description": "REQUIRED: The full content to write to the file. This parameter must be provided and cannot be empty for intentional writes.",
                    },
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute file write asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        path_str = arguments.get("path", "")
        content = arguments.get("content")

        if not path_str:
            return ToolResult(
                call_id=call_id,
                content="Error: path is required",
                is_error=True,
            )

        if content is None:
            return ToolResult(
                call_id=call_id,
                content="Error: content is required (was not provided in tool call)",
                is_error=True,
            )

        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        try:
            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            # Check if file exists (for reporting)
            existed = path.exists()

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            action = "Overwrote" if existed else "Created"

            return ToolResult(
                call_id=call_id,
                content=f"{action} file: {path}\nWrote {len(content)} characters ({lines} lines)",
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error writing file: {e}",
                is_error=True,
            )


class EditFileTool:
    """
    Make targeted edits to an existing file.

    Supports multiple editing modes for token efficiency:
    - Line-based: Replace lines by number (most efficient when you know line numbers)
    - Anchor-based: Replace content between patterns (good for structured docs)
    - Insert: Add content at a specific location
    - str_replace: Classic exact string replacement (fallback)
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_file",
            summary="Make precise edits to files",
            tier=ToolTier.HOT,
            description="""Make targeted edits to an existing file. Multiple modes available:

**LINE-BASED (most token-efficient when you just read the file):**
  Use start_line + end_line + content. No need to repeat old content.
  Example: {"path": "...", "start_line": 10, "end_line": 20, "content": "new content"}

**ANCHOR-BASED (best for markdown/structured docs):**
  Use start_anchor + end_anchor + content. Finds text by pattern.
  Example: {"path": "...", "start_anchor": "## Section 1", "end_anchor": "## Section 2", "content": "## Section 1\\n..."}
  - start_anchor: Pattern marking the START of text to replace (inclusive)
  - end_anchor: Pattern marking the END (exclusive). If omitted, replaces to end of file.

**INSERT MODE (for additions):**
  Use insert_after_line + content OR insert_after_pattern + content.
  Example: {"path": "...", "insert_after_line": 50, "content": "new line"}

**STR_REPLACE (fallback):**
  Use old_string + new_string for exact text replacement.
  Example: {"path": "...", "old_string": "old", "new_string": "new"}

Choose the mode that minimizes tokens - prefer line-based or anchor-based over str_replace.""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to edit",
                    },
                    # Line-based mode (Phase 1)
                    "start_line": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed) for line-based replacement",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Ending line number (inclusive) for line-based replacement. Defaults to start_line if not specified.",
                    },
                    # Anchor-based mode (Phase 2)
                    "start_anchor": {
                        "type": "string",
                        "description": "Pattern marking the START of text to replace (inclusive). Used with anchor-based mode.",
                    },
                    "end_anchor": {
                        "type": "string",
                        "description": "Pattern marking the END of text to replace (exclusive). If omitted, replaces to end of file.",
                    },
                    # Insert mode
                    "insert_after_line": {
                        "type": "integer",
                        "description": "Line number after which to insert content (use 0 to insert at beginning)",
                    },
                    "insert_after_pattern": {
                        "type": "string",
                        "description": "Pattern after which to insert content",
                    },
                    # Content for line-based, anchor-based, or insert modes
                    "content": {
                        "type": "string",
                        "description": "The new content for line-based, anchor-based, or insert operations",
                    },
                    # Legacy str_replace mode
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find and replace (str_replace mode)",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to replace it with (str_replace mode)",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "If true, replace all occurrences. Default: false (str_replace mode only)",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute file edit asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        path_str = arguments.get("path", "")

        if not path_str:
            return ToolResult(
                call_id=call_id,
                content="Error: path is required",
                is_error=True,
            )

        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: File not found: {path}",
                is_error=True,
            )

        # Determine which mode to use based on provided arguments
        has_line_args = "start_line" in arguments
        has_anchor_args = "start_anchor" in arguments
        has_insert_line = "insert_after_line" in arguments
        has_insert_pattern = "insert_after_pattern" in arguments
        has_str_replace = "old_string" in arguments

        try:
            if has_line_args:
                return self._edit_by_lines(call_id, path, arguments)
            elif has_anchor_args:
                return self._edit_by_anchors(call_id, path, arguments)
            elif has_insert_line:
                return self._insert_after_line(call_id, path, arguments)
            elif has_insert_pattern:
                return self._insert_after_pattern(call_id, path, arguments)
            elif has_str_replace:
                return self._str_replace(call_id, path, arguments)
            else:
                return ToolResult(
                    call_id=call_id,
                    content="Error: Must specify one of: start_line, start_anchor, insert_after_line, insert_after_pattern, or old_string",
                    is_error=True,
                )
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error editing file: {e}",
                is_error=True,
            )

    def _edit_by_lines(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        """Phase 1: Line-based replacement - most token-efficient."""
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line", start_line)
        content = arguments.get("content")

        if content is None:
            return ToolResult(
                call_id=call_id,
                content="Error: content is required for line-based editing",
                is_error=True,
            )

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_lines = len(lines)

        # Validate line numbers
        if start_line < 1 or start_line > total_lines:
            return ToolResult(
                call_id=call_id,
                content=f"Error: start_line {start_line} out of range (file has {total_lines} lines)",
                is_error=True,
            )

        if end_line < start_line or end_line > total_lines:
            return ToolResult(
                call_id=call_id,
                content=f"Error: end_line {end_line} invalid (must be >= start_line and <= {total_lines})",
                is_error=True,
            )

        # Convert to 0-indexed
        start_idx = start_line - 1
        end_idx = end_line  # end_line is inclusive, so we use it directly for slicing

        # Ensure content ends with newline for consistency
        if content and not content.endswith("\n"):
            content += "\n"

        # Replace the lines
        new_lines = lines[:start_idx] + [content] + lines[end_idx:]

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        replaced_count = end_line - start_line + 1
        new_line_count = content.count("\n")

        return ToolResult(
            call_id=call_id,
            content=f"Edited file: {path}\nReplaced lines {start_line}-{end_line} ({replaced_count} lines) with {new_line_count} new lines",
            is_error=False,
        )

    def _edit_by_anchors(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        """Phase 2: Anchor-based replacement - good for structured documents."""
        start_anchor = arguments.get("start_anchor", "")
        end_anchor = arguments.get("end_anchor")  # Optional - if None, replace to end of file
        content = arguments.get("content")

        if not start_anchor:
            return ToolResult(
                call_id=call_id,
                content="Error: start_anchor is required for anchor-based editing",
                is_error=True,
            )

        if content is None:
            return ToolResult(
                call_id=call_id,
                content="Error: content is required for anchor-based editing",
                is_error=True,
            )

        with open(path, "r", encoding="utf-8") as f:
            file_content = f.read()

        # Find start anchor
        start_pos = file_content.find(start_anchor)
        if start_pos == -1:
            return ToolResult(
                call_id=call_id,
                content=f"Error: start_anchor not found in file.\n\nSearched for: {start_anchor[:100]}{'...' if len(start_anchor) > 100 else ''}",
                is_error=True,
            )

        # Check for multiple start anchor matches
        second_start = file_content.find(start_anchor, start_pos + 1)
        if second_start != -1:
            return ToolResult(
                call_id=call_id,
                content=f"Error: start_anchor found multiple times. Provide a more specific anchor.\n\nFirst match at position {start_pos}, second at {second_start}",
                is_error=True,
            )

        # Find end anchor (if specified)
        if end_anchor:
            # Search for end anchor AFTER start anchor
            end_pos = file_content.find(end_anchor, start_pos + len(start_anchor))
            if end_pos == -1:
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: end_anchor not found after start_anchor.\n\nSearched for: {end_anchor[:100]}{'...' if len(end_anchor) > 100 else ''}",
                    is_error=True,
                )
        else:
            # No end anchor - replace to end of file
            end_pos = len(file_content)

        # Calculate what we're replacing
        old_content = file_content[start_pos:end_pos]
        old_lines = old_content.count("\n")

        # Build new file content
        new_file_content = file_content[:start_pos] + content + file_content[end_pos:]

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_file_content)

        new_lines = content.count("\n")

        return ToolResult(
            call_id=call_id,
            content=f"Edited file: {path}\nReplaced {len(old_content)} chars ({old_lines} lines) between anchors with {len(content)} chars ({new_lines} lines)",
            is_error=False,
        )

    def _insert_after_line(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        """Insert content after a specific line number."""
        insert_line = arguments.get("insert_after_line")
        content = arguments.get("content")

        if content is None:
            return ToolResult(
                call_id=call_id,
                content="Error: content is required for insert operation",
                is_error=True,
            )

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_lines = len(lines)

        # Validate line number (0 = insert at beginning)
        if insert_line < 0 or insert_line > total_lines:
            return ToolResult(
                call_id=call_id,
                content=f"Error: insert_after_line {insert_line} out of range (use 0-{total_lines})",
                is_error=True,
            )

        # Ensure content ends with newline
        if content and not content.endswith("\n"):
            content += "\n"

        # Insert the content
        new_lines = lines[:insert_line] + [content] + lines[insert_line:]

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        inserted_lines = content.count("\n")

        return ToolResult(
            call_id=call_id,
            content=f"Edited file: {path}\nInserted {inserted_lines} lines after line {insert_line}",
            is_error=False,
        )

    def _insert_after_pattern(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        """Insert content after a pattern match."""
        pattern = arguments.get("insert_after_pattern", "")
        content = arguments.get("content")

        if not pattern:
            return ToolResult(
                call_id=call_id,
                content="Error: insert_after_pattern is required",
                is_error=True,
            )

        if content is None:
            return ToolResult(
                call_id=call_id,
                content="Error: content is required for insert operation",
                is_error=True,
            )

        with open(path, "r", encoding="utf-8") as f:
            file_content = f.read()

        # Find pattern
        pos = file_content.find(pattern)
        if pos == -1:
            return ToolResult(
                call_id=call_id,
                content=f"Error: pattern not found.\n\nSearched for: {pattern[:100]}{'...' if len(pattern) > 100 else ''}",
                is_error=True,
            )

        # Check for multiple matches
        second_pos = file_content.find(pattern, pos + 1)
        if second_pos != -1:
            return ToolResult(
                call_id=call_id,
                content=f"Error: pattern found multiple times. Provide a more specific pattern.\n\nFirst match at position {pos}, second at {second_pos}",
                is_error=True,
            )

        # Insert after the pattern (and its trailing newline if present)
        insert_pos = pos + len(pattern)
        # If pattern ends at a newline, insert after it
        if insert_pos < len(file_content) and file_content[insert_pos] == "\n":
            insert_pos += 1

        # Ensure content ends with newline
        if content and not content.endswith("\n"):
            content += "\n"

        new_content = file_content[:insert_pos] + content + file_content[insert_pos:]

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        inserted_lines = content.count("\n")

        return ToolResult(
            call_id=call_id,
            content=f"Edited file: {path}\nInserted {inserted_lines} lines after pattern",
            is_error=False,
        )

    def _str_replace(self, call_id: str, path: Path, arguments: dict[str, Any]) -> ToolResult:
        """Legacy str_replace mode - exact string replacement."""
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        replace_all = arguments.get("replace_all", False)

        if not old_string:
            return ToolResult(
                call_id=call_id,
                content="Error: old_string is required for str_replace mode",
                is_error=True,
            )

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Count occurrences
        count = content.count(old_string)

        if count == 0:
            return ToolResult(
                call_id=call_id,
                content=f"Error: old_string not found in file.\n\nSearched for:\n{old_string[:200]}{'...' if len(old_string) > 200 else ''}",
                is_error=True,
            )

        if count > 1 and not replace_all:
            return ToolResult(
                call_id=call_id,
                content=f"Error: old_string found {count} times. Set replace_all=true to replace all, or provide more context to make the match unique.",
                is_error=True,
            )

        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult(
            call_id=call_id,
            content=f"Edited file: {path}\nReplaced {replaced} occurrence(s)",
            is_error=False,
        )


class ListDirectoryTool:
    """
    List contents of a directory.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_directory",
            summary="List directory contents",
            tier=ToolTier.HOT,
            description="""List the contents of a directory.

Returns files and subdirectories with basic metadata (size, type).
Use glob tool for pattern-based file finding.""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The directory path to list (defaults to current directory)",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "Include hidden files (starting with '.') Default: false",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List recursively (up to 3 levels). Default: false",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute directory listing asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        path_str = arguments.get("path", ".")
        show_hidden = arguments.get("show_hidden", False)
        recursive = arguments.get("recursive", False)

        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: Directory not found: {path}",
                is_error=True,
            )

        if not path.is_dir():
            return ToolResult(
                call_id=call_id,
                content=f"Error: Not a directory: {path}",
                is_error=True,
            )

        try:
            output_lines = [f"Directory: {path}", "-" * 60]

            def format_entry(entry: Path, indent: int = 0) -> str:
                prefix = "  " * indent
                if entry.is_dir():
                    return f"{prefix}[DIR]  {entry.name}/"
                else:
                    try:
                        size = entry.stat().st_size
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size / 1024:.1f}KB"
                        else:
                            size_str = f"{size / (1024 * 1024):.1f}MB"
                        return f"{prefix}[FILE] {entry.name} ({size_str})"
                    except Exception:
                        return f"{prefix}[FILE] {entry.name}"

            def list_dir(dir_path: Path, indent: int = 0, max_depth: int = 3) -> list[str]:
                lines = []
                try:
                    entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                    for entry in entries:
                        if not show_hidden and entry.name.startswith("."):
                            continue
                        lines.append(format_entry(entry, indent))
                        if recursive and entry.is_dir() and indent < max_depth:
                            lines.extend(list_dir(entry, indent + 1, max_depth))
                except PermissionError:
                    lines.append(f"{'  ' * indent}[Permission denied]")
                return lines

            entries = list_dir(path, 0, 3 if recursive else 0)
            output_lines.extend(entries)

            if not entries:
                output_lines.append("(empty directory)")

            return ToolResult(
                call_id=call_id,
                content="\n".join(output_lines),
                is_error=False,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error listing directory: {e}",
                is_error=True,
            )


class EditJsonTool:
    """
    JSON-aware file editor that operates on the parsed structure.

    Eliminates common JSON editing errors (missing commas, duplicate braces)
    by parsing, modifying, and re-serializing the JSON.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_json",
            summary="Edit JSON files by path — REQUIRED for workflow JSON",
            tier=ToolTier.HOT,
            description="""Edit JSON files using a path-based approach that understands JSON structure.

**IMPORTANT: Always use this tool (not edit_file) for workflow JSON files.**
Line-based text editing breaks JSON syntax. This tool guarantees valid output.

Unlike text-based editing, this tool:
- Parses the JSON first, so syntax is always valid
- Navigates to the target location using a path expression
- Updates the value and re-serializes with proper formatting
- Eliminates comma, brace, and quote errors

**Path syntax:**
- Use dot notation for object keys: `namedResults.answer`
- Use brackets for arrays: `actions[0]` or `items[*]` (all items)
- Combine them: `actions[0].inputs.extraction_columns`
- Use `$` for root: `$.namedResults` (optional, same as `namedResults`)

**Operations:**
- `set`: Replace value at path (default)
- `delete`: Remove key/element at path
- `merge`: Deep merge value into object at path
- `append`: Append value to array at path

**Examples:**
```json
{"path": "workflow.json", "json_path": "actions[0].inputs.prompt", "value": "New prompt"}
{"path": "workflow.json", "json_path": "enumTypes[0].options", "value": [{"name": "A"}, {"name": "B"}]}
{"path": "file.json", "json_path": "config.timeout", "value": 30}
{"path": "file.json", "json_path": "items", "value": {"new": "item"}, "operation": "append"}
{"path": "file.json", "json_path": "oldKey", "operation": "delete"}
```""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the JSON file to edit",
                    },
                    "json_path": {
                        "type": "string",
                        "description": "Path to the value to edit (e.g., 'config.timeout' or 'items[0].name')",
                    },
                    "value": {
                        "description": "The new value to set (can be any JSON type: object, array, string, number, boolean, null). Not required for 'delete' operation.",
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["set", "delete", "merge", "append"],
                        "description": "Operation to perform. Default: 'set'",
                    },
                    "indent": {
                        "type": "integer",
                        "description": "Indentation spaces for output. Default: 2",
                    },
                },
                "required": ["path", "json_path"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute JSON edit asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        path_str = arguments.get("path", "")
        json_path = arguments.get("json_path", "")
        value = arguments.get("value")
        operation = arguments.get("operation", "set")
        indent = arguments.get("indent", 2)

        if not path_str:
            return ToolResult(
                call_id=call_id,
                content="Error: path is required",
                is_error=True,
            )

        if not json_path:
            return ToolResult(
                call_id=call_id,
                content="Error: json_path is required",
                is_error=True,
            )

        if operation not in ("set", "delete", "merge", "append"):
            return ToolResult(
                call_id=call_id,
                content=f"Error: invalid operation '{operation}'. Use: set, delete, merge, append",
                is_error=True,
            )

        if operation != "delete" and value is None and "value" not in arguments:
            return ToolResult(
                call_id=call_id,
                content=f"Error: value is required for '{operation}' operation",
                is_error=True,
            )

        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if not path.exists():
            return ToolResult(
                call_id=call_id,
                content=f"Error: File not found: {path}",
                is_error=True,
            )

        try:
            # Parse the JSON file
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error parsing JSON: {e}",
                is_error=True,
            )

        # Parse the json_path and navigate
        try:
            path_parts = self._parse_json_path(json_path)
        except ValueError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error parsing json_path: {e}",
                is_error=True,
            )

        # Perform the operation
        try:
            if operation == "set":
                self._set_value(data, path_parts, value)
                op_desc = f"Set '{json_path}'"
            elif operation == "delete":
                self._delete_value(data, path_parts)
                op_desc = f"Deleted '{json_path}'"
            elif operation == "merge":
                self._merge_value(data, path_parts, value)
                op_desc = f"Merged into '{json_path}'"
            elif operation == "append":
                self._append_value(data, path_parts, value)
                op_desc = f"Appended to '{json_path}'"
        except (KeyError, IndexError, TypeError) as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error at path '{json_path}': {e}",
                is_error=True,
            )

        # Write back
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
                f.write("\n")  # Trailing newline
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error writing JSON: {e}",
                is_error=True,
            )

        return ToolResult(
            call_id=call_id,
            content=f"Edited JSON file: {path}\n{op_desc}",
            is_error=False,
        )

    def _parse_json_path(self, json_path: str) -> list[str | int]:
        """Parse a JSON path into a list of keys/indices.

        Examples:
            "foo.bar" -> ["foo", "bar"]
            "items[0].name" -> ["items", 0, "name"]
            "$.config" -> ["config"]
            "arr[*]" -> ["arr", "*"]  (wildcard for all elements)
        """
        # Strip leading $ if present
        if json_path.startswith("$."):
            json_path = json_path[2:]
        elif json_path == "$":
            return []

        parts: list[str | int] = []
        # Split on dots and brackets
        # Pattern matches: word, [number], [*]
        pattern = r'([^.\[\]]+)|\[(\d+|\*)\]'

        for match in re.finditer(pattern, json_path):
            if match.group(1):  # Key name
                parts.append(match.group(1))
            elif match.group(2):  # Array index or wildcard
                idx = match.group(2)
                if idx == "*":
                    parts.append("*")
                else:
                    parts.append(int(idx))

        return parts

    def _navigate_to_parent(self, data: Any, path_parts: list[str | int]) -> tuple[Any, str | int]:
        """Navigate to the parent of the target and return (parent, final_key)."""
        if not path_parts:
            raise ValueError("Cannot navigate to parent of root")

        current = data
        for part in path_parts[:-1]:
            if part == "*":
                raise ValueError("Wildcard (*) not supported in intermediate path")
            if isinstance(current, dict):
                if part not in current:
                    raise KeyError(f"Key '{part}' not found")
                current = current[part]
            elif isinstance(current, list):
                if not isinstance(part, int):
                    raise TypeError(f"Expected integer index for array, got '{part}'")
                if part < 0 or part >= len(current):
                    raise IndexError(f"Index {part} out of range (array has {len(current)} items)")
                current = current[part]
            else:
                raise TypeError(f"Cannot navigate into {type(current).__name__}")

        return current, path_parts[-1]

    def _set_value(self, data: Any, path_parts: list[str | int], value: Any) -> None:
        """Set a value at the given path."""
        if not path_parts:
            raise ValueError("Cannot set root value (use write_file instead)")

        parent, key = self._navigate_to_parent(data, path_parts)

        if key == "*":
            # Set all elements in array
            if not isinstance(parent, list):
                raise TypeError("Wildcard (*) requires parent to be an array")
            for i in range(len(parent)):
                parent[i] = value
        elif isinstance(parent, dict):
            parent[key] = value
        elif isinstance(parent, list):
            if not isinstance(key, int):
                raise TypeError(f"Expected integer index for array, got '{key}'")
            if key < 0 or key >= len(parent):
                raise IndexError(f"Index {key} out of range")
            parent[key] = value
        else:
            raise TypeError(f"Cannot set value in {type(parent).__name__}")

    def _delete_value(self, data: Any, path_parts: list[str | int]) -> None:
        """Delete a value at the given path."""
        if not path_parts:
            raise ValueError("Cannot delete root")

        parent, key = self._navigate_to_parent(data, path_parts)

        if key == "*":
            if not isinstance(parent, list):
                raise TypeError("Wildcard (*) requires parent to be an array")
            parent.clear()
        elif isinstance(parent, dict):
            if key not in parent:
                raise KeyError(f"Key '{key}' not found")
            del parent[key]
        elif isinstance(parent, list):
            if not isinstance(key, int):
                raise TypeError(f"Expected integer index for array, got '{key}'")
            if key < 0 or key >= len(parent):
                raise IndexError(f"Index {key} out of range")
            del parent[key]
        else:
            raise TypeError(f"Cannot delete from {type(parent).__name__}")

    def _merge_value(self, data: Any, path_parts: list[str | int], value: Any) -> None:
        """Deep merge a value into the object at the given path."""
        if not path_parts:
            # Merge into root
            if not isinstance(data, dict) or not isinstance(value, dict):
                raise TypeError("Merge requires both target and value to be objects")
            self._deep_merge(data, value)
            return

        parent, key = self._navigate_to_parent(data, path_parts)

        if isinstance(parent, dict):
            if key not in parent:
                parent[key] = value
            elif isinstance(parent[key], dict) and isinstance(value, dict):
                self._deep_merge(parent[key], value)
            else:
                parent[key] = value
        elif isinstance(parent, list):
            if not isinstance(key, int):
                raise TypeError(f"Expected integer index for array, got '{key}'")
            if isinstance(parent[key], dict) and isinstance(value, dict):
                self._deep_merge(parent[key], value)
            else:
                parent[key] = value
        else:
            raise TypeError(f"Cannot merge into {type(parent).__name__}")

    def _deep_merge(self, target: dict, source: dict) -> None:
        """Deep merge source dict into target dict."""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value

    def _append_value(self, data: Any, path_parts: list[str | int], value: Any) -> None:
        """Append a value to the array at the given path."""
        if not path_parts:
            raise ValueError("Cannot append to root (root must be an array)")

        parent, key = self._navigate_to_parent(data, path_parts)

        if isinstance(parent, dict):
            target = parent.get(key)
            if target is None:
                parent[key] = [value]
            elif isinstance(target, list):
                target.append(value)
            else:
                raise TypeError(f"Cannot append to {type(target).__name__}, expected array")
        elif isinstance(parent, list):
            if not isinstance(key, int):
                raise TypeError(f"Expected integer index for array, got '{key}'")
            target = parent[key]
            if isinstance(target, list):
                target.append(value)
            else:
                raise TypeError(f"Cannot append to {type(target).__name__}, expected array")
        else:
            raise TypeError(f"Cannot access {type(parent).__name__}")
