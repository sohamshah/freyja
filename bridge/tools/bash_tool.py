"""
Bash command execution tool for the CLI agent.

Executes shell commands with permission gating for dangerous operations.
Single async-native implementation using asyncio subprocess.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from bridge.process_env import child_env
from bridge.tools.base import (
    PermissionLevel,
    PermissionRequest,
    ToolDefinition,
    ToolResult,
    ToolTier,
)

DEFAULT_BASH_TIMEOUT = 120.0
MAX_BASH_SUMMARY_LENGTH = 280

MISSING_BASH_SUMMARY_ERROR = (
    "Error: `summary` is required for bash commands. Provide a short user-facing "
    "description of what will be executed, and do not repeat the full command."
)


# Patterns that indicate dangerous commands
DANGEROUS_PATTERNS = [
    # Destructive file operations
    "rm -rf",
    "rm -fr",
    "rm --recursive --force",
    "rmdir",
    "> /dev/",
    "dd if=",
    "mkfs",
    "format",
    # System modification
    "chmod -R",
    "chown -R",
    "sudo",
    "su ",
    "su\n",
    # Network/security risks
    "curl.*|.*sh",
    "wget.*|.*sh",
    "curl.*|.*bash",
    "wget.*|.*bash",
    # Process control
    "kill -9",
    "killall",
    "pkill",
    # Package management (can break system)
    "apt remove",
    "apt purge",
    "yum remove",
    "brew uninstall",
    "pip uninstall",
    "npm uninstall -g",
    # Git destructive operations
    "git reset --hard",
    "git clean -f",
    "git push --force",
    "git push -f",
    # Database operations
    "DROP TABLE",
    "DROP DATABASE",
    "TRUNCATE ",
    "TRUNCATE;",
    "DELETE FROM",
]

# Patterns that indicate high-risk but not necessarily dangerous
HIGH_RISK_PATTERNS = [
    # File modifications
    "mv ",
    "cp -r",
    "rsync",
    # Git operations
    "git push",
    "git merge",
    "git rebase",
    "git checkout",
    "git branch -d",
    "git branch -D",
    # Package installation
    "pip install",
    "npm install",
    "brew install",
    "apt install",
    # Network operations
    "curl",
    "wget",
    "ssh",
    "scp",
]

# Commands that are generally safe (read-only or low impact)
SAFE_PATTERNS = [
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "wc",
    "which",
    "whereis",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "env",
    "printenv",
    "file",
    "stat",
    "du",
    "df",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "pip list",
    "pip show",
    "npm list",
    "npm show",
    "python --version",
    "node --version",
    "pip --version",
]


def classify_command_risk(command: str) -> PermissionLevel:
    """
    Classify the risk level of a command.

    Returns:
        PermissionLevel indicating the risk level
    """
    cmd_lower = command.lower().strip()

    # Check for dangerous patterns first
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            return PermissionLevel.DANGEROUS

    # Check for high-risk patterns
    for pattern in HIGH_RISK_PATTERNS:
        if pattern.lower() in cmd_lower:
            return PermissionLevel.HIGH

    # Check for safe patterns
    for pattern in SAFE_PATTERNS:
        if cmd_lower.startswith(pattern.lower()):
            return PermissionLevel.LOW

    # Default to medium risk for unknown commands
    return PermissionLevel.MEDIUM


def sanitize_bash_summary(summary: str | None) -> str:
    """Normalize an agent-provided bash summary for user-facing display."""
    if not summary:
        return ""

    normalized = " ".join(summary.split())
    if not normalized:
        return ""

    if len(normalized) > MAX_BASH_SUMMARY_LENGTH:
        normalized = normalized[: MAX_BASH_SUMMARY_LENGTH - 3].rstrip() + "..."
    return normalized


def default_bash_summary(command: str) -> str:
    """Fallback summary when the agent does not provide one."""
    stripped = command.strip()
    if not stripped:
        return "run a shell command"
    if "\n" in stripped or "<<" in stripped:
        return "run a multiline shell command"
    return "run a shell command"


def build_bash_display_summary(command: str, summary: str | None = None) -> str:
    """Build the user-facing summary for bash execution prompts and logs."""
    return sanitize_bash_summary(summary) or default_bash_summary(command)


class BashTool:
    """
    Execute bash commands with permission gating.

    Async-native implementation using asyncio subprocess.
    Dangerous commands require explicit user confirmation.
    """

    def __init__(
        self,
        working_dir: str | None = None,
        timeout: float = DEFAULT_BASH_TIMEOUT,
    ) -> None:
        """Initialize the bash tool.

        Parameters
        ----------
        working_dir : str or None, optional
            Default working directory for commands. Defaults to cwd.
        timeout : float, optional
            Default timeout in seconds.
        """
        self._working_dir = working_dir or os.getcwd()
        self._timeout = timeout

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            summary="Run shell commands",
            tier=ToolTier.HOT,
            description="""Execute a bash command.

Commands are classified by risk level:
- LOW: Read-only commands (ls, cat, git status) - auto-approved
- MEDIUM: Most commands - quick confirmation needed
- HIGH: Commands that modify state (git push, pip install) - explicit approval
- DANGEROUS: Destructive commands (rm -rf, git push --force) - requires explicit y/n confirmation

The command runs with a timeout (default 120s) and captures both stdout and stderr.

If `command` is long, multiline, or contains an inline script, provide `summary`
with a short user-facing description. Never repeat the full command in `summary`.

For long-running commands, consider using & for background execution.""",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Brief user-facing summary of what the command does, with enough "
                            "detail for an approval prompt. "
                            "Required; use this instead of repeating a long command or script."
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Working directory for the command (defaults to current directory)",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 120)",
                    },
                },
                "required": ["command", "summary"],
            },
        )

    @property
    def requires_permission(self) -> bool:
        """Whether this tool requires user permission before execution."""
        return True

    async def permission_prompt(self, arguments: dict[str, Any]) -> PermissionRequest | None:
        """Build a permission request based on command risk classification.

        Parameters
        ----------
        arguments : dict[str, Any]
            Tool arguments from the model, expecting ``command`` key.

        Returns
        -------
        PermissionRequest or None
            Request with risk level from ``classify_command_risk``,
            or None if no command provided.
        """
        command = arguments.get("command", "")
        if not command:
            return None
        summary = sanitize_bash_summary(arguments.get("summary"))
        working_dir = arguments.get("working_dir", self._working_dir)
        timeout = arguments.get("timeout", self._timeout)
        risk_level = classify_command_risk(command)
        # Use summary for the user-facing prompt when available,
        # fall back to the raw command otherwise
        display_text = summary or command
        return PermissionRequest(
            prompt=f"Execute command: {display_text}",
            level=risk_level,
            details=f"Working directory: {working_dir}\nTimeout: {timeout}s",
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute bash command asynchronously."""
        command = arguments.get("command", "")
        summary = sanitize_bash_summary(arguments.get("summary"))
        working_dir = arguments.get("working_dir", self._working_dir)
        timeout = arguments.get("timeout", self._timeout)

        if not command:
            return ToolResult(
                call_id=call_id,
                content="Error: command is required",
                is_error=True,
            )

        if not summary:
            return ToolResult(
                call_id=call_id,
                content=MISSING_BASH_SUMMARY_ERROR,
                is_error=True,
            )

        # Execute async using asyncio subprocess. `env=child_env()`
        # strips Freyja's PYTHONHOME/PYTHONPATH/VIRTUAL_ENV so any
        # python the agent runs from bash (system python3, a uv-managed
        # venv, etc.) doesn't crash with "No module named 'encodings'"
        # because it inherited a PYTHONHOME pointed at Freyja's bundle.
        # See bridge/process_env.py for the full incident note.
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()  # Clean up the process
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: Command timed out after {timeout} seconds\nSummary: {summary}",
                    is_error=True,
                )

            output_parts = []
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            if stdout_text:
                output_parts.append("STDOUT:")
                output_parts.append(stdout_text)

            if stderr_text:
                if output_parts:
                    output_parts.append("")
                output_parts.append("STDERR:")
                output_parts.append(stderr_text)

            if not output_parts:
                output_parts.append("(no output)")

            output_parts.append("")
            output_parts.append(f"Exit code: {proc.returncode}")

            return ToolResult(
                call_id=call_id,
                content="\n".join(output_parts),
                is_error=proc.returncode != 0,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error executing command: {e}\nSummary: {summary}",
                is_error=True,
            )


# Backwards compatibility alias
AsyncBashTool = BashTool
