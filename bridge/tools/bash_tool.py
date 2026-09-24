"""
Bash command execution tool for the CLI agent.

Executes shell commands with permission gating for dangerous operations.
Single async-native implementation using asyncio subprocess.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from bridge.process_env import child_env
from bridge.tools.background_shell import (
    CURRENT_TOOL_CONTEXT,
    BackgroundCommand,
    StreamingProcess,
    ToolContext,
    new_background_id,
    tail,
)
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

    # When the operator cuts in, the runner gives this tool a moment to
    # move its command to the background (and return a normal result)
    # before cancelling it — see AsyncAgentRunner._race_interrupt.
    yields_on_interrupt = True

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

For long-running commands, consider using & for background execution.

If the operator messages you while a command is running (after ~15s, or at
once if they cut in), the command moves to the background instead of being
stopped: the result says so and shows the output so far, the command keeps
running, and a memo arrives when it exits.""",
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
        # A session that can take a memo back lets a long command step aside
        # for the operator (see bridge/tools/background_shell.py); without
        # one (sub-agents, tests) the command just runs to completion.
        tool_ctx = CURRENT_TOOL_CONTEXT.get()
        try:
            # Own process group, so stopping the command stops everything
            # it started — killing just the /bin/sh wrapper orphans the
            # real work (`sleep 45 && …` kept running after a timeout).
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(),
                start_new_session=True,
            )
            running = StreamingProcess(proc)
            started = time.monotonic()
            # Cancellation (the turn was stopped) stops the process group
            # inside wait() and propagates.
            outcome = await running.wait(
                started=started,
                timeout=timeout,
                should_yield=tool_ctx.tool_yield.reason_to_yield if tool_ctx else None,
            )
            if outcome == "timeout":
                await running.stop()
                partial = _format_output(bytes(running.stdout), bytes(running.stderr), None)
                return ToolResult(
                    call_id=call_id,
                    content=(
                        f"Error: Command timed out after {timeout} seconds and was "
                        f"stopped\nSummary: {summary}\n\nOutput before the timeout:\n{partial}"
                    ),
                    is_error=True,
                )
            if outcome != "exited" and tool_ctx is not None:
                return self._move_to_background(
                    call_id, command, summary, running, tool_ctx,
                    reason=outcome, ran_s=time.monotonic() - started,
                    timeout_s=float(timeout) if timeout else None,
                )

            return ToolResult(
                call_id=call_id,
                content=_format_output(
                    bytes(running.stdout), bytes(running.stderr), proc.returncode,
                ),
                is_error=proc.returncode != 0,
            )

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error executing command: {e}\nSummary: {summary}",
                is_error=True,
            )

    def _move_to_background(
        self,
        call_id: str,
        command: str,
        summary: str,
        running: StreamingProcess,
        tool_ctx: ToolContext,
        *,
        reason: str,
        ran_s: float,
        timeout_s: float | None,
    ) -> ToolResult:
        """Detach a running command so the operator's message can land:
        it keeps running with output going to a log, the call returns now,
        and the session gets a memo when it exits."""
        bg = BackgroundCommand(
            id=new_background_id(),
            command=command,
            summary=summary,
            pid=running.proc.pid,
            output_path="",
            started_at=time.time() - ran_s,
            backgrounded_at=time.time(),
            reason=reason,
            timeout_s=timeout_s,
            _process=running,
        )
        log_path = Path(tool_ctx.output_dir) / f"{bg.id}.log"
        bg.output_path = str(log_path)
        so_far_out = tail(bytes(running.stdout))
        so_far_err = tail(bytes(running.stderr))
        running.detach(log_path)
        tool_ctx.on_background_start(bg)

        async def _watch() -> None:
            finished = asyncio.ensure_future(running.finished())
            remaining = None if timeout_s is None else max(0.0, timeout_s - ran_s)
            done, _ = await asyncio.wait({finished}, timeout=remaining)
            if not done:
                # Past the time limit it had in the foreground: stop it
                # the same way a foreground timeout would have.
                if bg.state == "running":
                    bg.state = "timed_out"
                await running.stop()
            code = await finished
            bg.exit_code = code
            bg.ended_at = time.time()
            if bg.state == "running":
                bg.state = "exited"
            result = tool_ctx.on_background_exit(bg)
            if asyncio.iscoroutine(result):
                await result

        # Kept on the record: a task nobody references can be collected
        # mid-run, and then the exit memo would never come.
        bg._watch_task = asyncio.get_running_loop().create_task(
            _watch(), name=f"bg-watch-{bg.id}",
        )

        why = (
            "the operator cut in"
            if reason == "cut_in"
            else "the operator sent a message and this had been running a while"
        )
        parts = [
            f"Moved to the background after {ran_s:.0f}s because {why}. The "
            f"command is STILL RUNNING (id {bg.id}, pid {bg.pid}) — it was not "
            "stopped, so its effects continue.",
        ]
        if so_far_out:
            parts += ["", "STDOUT so far:", so_far_out]
        if so_far_err:
            parts += ["", "STDERR so far:", so_far_err]
        if not so_far_out and not so_far_err:
            parts += ["", "(no output yet)"]
        parts += [
            "",
            f"The rest of its output streams to {bg.output_path}. You'll get a "
            "memo when it exits — don't poll or wait for it, and don't assume "
            "how it ends."
            + (
                f" Its {int(timeout_s)}s time limit still applies."
                if timeout_s
                else ""
            )
            + " To stop it early: bash `kill -TERM -- -"
            f"{bg.pid}` (its whole process group).",
        ]
        return ToolResult(call_id=call_id, content="\n".join(parts), is_error=False)


def _format_output(stdout: bytes, stderr: bytes, exit_code: int | None) -> str:
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

    if exit_code is not None:
        output_parts.append("")
        output_parts.append(f"Exit code: {exit_code}")
    return "\n".join(output_parts)


# Backwards compatibility alias
AsyncBashTool = BashTool
