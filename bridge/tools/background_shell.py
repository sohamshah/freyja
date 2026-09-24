"""Shell commands that step aside for the operator.

A follow-up the operator sends mid-turn lands at the agent's next step
boundary — which, behind a ten-minute build, used to mean ten minutes. And
cutting in (⌃↵) used to SIGKILL the build. Neither is acceptable, so a
running shell command can move to the background instead:

  * the command keeps running, its output streams to a log file;
  * the tool call returns now, with the output so far and where the rest
    will be, so the turn reaches its boundary and the message lands;
  * when the command exits, the session gets a memo (like a finished
    sub-agent) and an idle session is woken to look at it.

The pieces:

  ``ToolYield``        per-session "please step aside" signal. A queued
                       follow-up asks softly (yield once the command has run
                       ``SOFT_YIELD_AFTER_S``); a cut-in asks hard (yield now).
                       The bridge clears it at every step boundary.
  ``ToolContext``      what a tool call needs from the session it runs for
                       (the signal, where logs go, lifecycle callbacks). Set
                       per call by the bridge's tracing wrapper through a
                       ContextVar, so the one shared ``BashTool`` object serves
                       every session correctly.
  ``BackgroundCommand``  one detached command.

A command stopped outright (⌘Esc, stop agents, emergency stop, timeout) gets
SIGTERM to its whole process group, a short grace, then SIGKILL — so it can
drop its locks and flush instead of leaving ``.git/index.lock`` behind.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# A queued (non-forced) follow-up waits at most this long behind a running
# shell command before the command moves to the background.
SOFT_YIELD_AFTER_S = 15.0
# How long a stopped command's process group gets between SIGTERM and SIGKILL.
TERM_GRACE_S = 1.0
# The log a tool result shows inline when a command moves to the background.
TAIL_LINES = 40


@dataclass
class ToolYield:
    """Per-session request for long-running tools to step aside."""

    soft: bool = False
    hard: bool = False

    def request(self, *, hard: bool) -> None:
        self.soft = True
        if hard:
            self.hard = True

    def clear(self) -> None:
        self.soft = False
        self.hard = False

    def reason_to_yield(self, ran_s: float) -> str | None:
        """Why a tool that has run ``ran_s`` seconds should yield now, if it
        should: ``"cut_in"`` or ``"followup"``."""
        if self.hard:
            return "cut_in"
        if self.soft and ran_s >= SOFT_YIELD_AFTER_S:
            return "followup"
        return None


@dataclass
class BackgroundCommand:
    id: str
    command: str
    summary: str
    pid: int
    output_path: str
    started_at: float
    backgrounded_at: float
    reason: str  # "cut_in" | "followup"
    state: str = "running"  # running | exited | stopped | timed_out
    # The command's own time limit still applies in the background — a
    # hung or never-ending command (a dev server) would otherwise run, and
    # hold every quiescence wait, forever.
    timeout_s: float | None = None
    exit_code: int | None = None
    ended_at: float | None = None
    # "operator" when the operator's stop ended it (no wake, memo batched).
    cancel_origin: str = ""
    _process: Any = field(default=None, repr=False)
    _watch_task: Any = field(default=None, repr=False)

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    @property
    def is_running(self) -> bool:
        return self.state == "running"


@dataclass
class ToolContext:
    """Session services a tool call can use. Present only for sessions that
    can take a memo back (root bridge sessions); None elsewhere, where the
    bash tool behaves as it always did."""

    session_id: str
    tool_yield: ToolYield
    output_dir: Path
    on_background_start: Callable[[BackgroundCommand], None]
    on_background_exit: Callable[[BackgroundCommand], Awaitable[None] | None]


CURRENT_TOOL_CONTEXT: ContextVar[ToolContext | None] = ContextVar(
    "freyja_tool_context", default=None,
)


def new_background_id() -> str:
    return f"bg_{uuid.uuid4().hex[:10]}"


def _signal_group(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_process_group(proc: Any, *, grace_s: float = TERM_GRACE_S) -> None:
    """SIGTERM the command's process group now, SIGKILL it after ``grace_s``
    if it is still there. Never blocks: safe from a CancelledError handler,
    where the caller must re-raise promptly. The command runs in its own
    session, so its pid is its process-group id."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    if not _signal_group(pid, signal.SIGTERM):
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    def _escalate() -> None:
        # The whole group, whether or not /bin/sh (the leader) already
        # died — members that ignore or outlive SIGTERM are the point.
        if not _signal_group(pid, signal.SIGKILL) and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    try:
        asyncio.get_running_loop().call_later(grace_s, _escalate)
    except RuntimeError:
        _escalate()


class StreamingProcess:
    """A subprocess whose stdout/stderr are pumped as they arrive, so its
    output so far is always available and it can be detached mid-run."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.stdout = bytearray()
        self.stderr = bytearray()
        self._sink: Any = None
        self._readers = [
            asyncio.ensure_future(self._pump(proc.stdout, self.stdout)),
            asyncio.ensure_future(self._pump(proc.stderr, self.stderr)),
        ]
        self._done = asyncio.ensure_future(self._finish())

    async def _pump(self, stream: Any, buf: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            if self._sink is not None:
                self._sink.write(chunk)
                self._sink.flush()
            else:
                buf.extend(chunk)

    async def _finish(self) -> int:
        code = await self.proc.wait()
        await asyncio.gather(*self._readers, return_exceptions=True)
        return code

    async def wait(
        self,
        *,
        started: float,
        timeout: float | None,
        should_yield: Callable[[float], str | None] | None,
        poll_s: float = 0.25,
    ) -> str:
        """``"exited"``, ``"timeout"``, or a yield reason. Cancellation stops
        the process group and propagates."""
        try:
            while True:
                done, _ = await asyncio.wait({self._done}, timeout=poll_s)
                if done:
                    return "exited"
                ran = time.monotonic() - started
                if timeout is not None and ran >= timeout:
                    return "timeout"
                if should_yield is not None:
                    reason = should_yield(ran)
                    if reason:
                        return reason
        except asyncio.CancelledError:
            stop_process_group(self.proc)
            raise

    async def stop(self) -> None:
        stop_process_group(self.proc)
        await asyncio.wait({self._done}, timeout=TERM_GRACE_S + 2)

    def detach(self, path: Path) -> None:
        """From now on, output goes to ``path`` (which starts with what was
        captured so far)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        sink = open(path, "wb")  # noqa: SIM115 — lives until the process exits
        if self.stdout:
            sink.write(b"--- stdout so far ---\n")
            sink.write(bytes(self.stdout))
            if not self.stdout.endswith(b"\n"):
                sink.write(b"\n")
        if self.stderr:
            sink.write(b"--- stderr so far ---\n")
            sink.write(bytes(self.stderr))
            if not self.stderr.endswith(b"\n"):
                sink.write(b"\n")
        sink.write(b"--- live output (stdout and stderr) ---\n")
        sink.flush()
        self._sink = sink

    async def finished(self) -> int:
        code = await self._done
        if self._sink is not None:
            try:
                self._sink.write(f"--- exited with code {code} ---\n".encode())
                self._sink.close()
            except Exception:  # noqa: BLE001
                pass
        return code


def read_tail_bytes(path: Path, limit: int = 64 * 1024) -> bytes:
    """The last ``limit`` bytes of a file — a chatty build's log can be
    gigabytes, and a memo needs only its end."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
        if size > limit:
            data = data.split(b"\n", 1)[-1]  # drop the cut-off first line
        return data
    except OSError:
        return b""


def tail(data: bytes, lines: int = TAIL_LINES) -> str:
    text = data.decode("utf-8", errors="replace")
    parts = text.rstrip("\n").split("\n")
    if len(parts) <= lines:
        return text.rstrip("\n")
    return f"[… {len(parts) - lines} earlier lines in the log]\n" + "\n".join(parts[-lines:])


def build_background_command_memo(bg: BackgroundCommand) -> Any:
    """The inbox memo a session gets when a backgrounded command exits."""
    from bridge.inbox import KIND_MEMO, InboxMessage, neutralize_control_tags, new_message_id

    elapsed = int(bg.elapsed)
    took = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
    if bg.state == "stopped":
        outcome, state = "was stopped by the operator", "cancelled"
    elif bg.state == "timed_out":
        outcome, state = (
            f"ran past its {int(bg.timeout_s or 0)}s time limit and was stopped",
            "failed",
        )
    elif bg.exit_code == 0:
        outcome, state = "finished (exit code 0)", "done"
    else:
        outcome, state = f"FAILED (exit code {bg.exit_code})", "failed"
    why = "the operator cut in" if bg.reason == "cut_in" else "the operator sent a message"
    command = " ".join(bg.command.split())
    if len(command) > 300:
        command = command[:300] + "…"
    lines = [
        f"[background command memo · {bg.summary or 'shell command'} · id {bg.id} · {state} · {took}]",
        f"A shell command you ran — moved to the background because {why} — "
        f"{outcome}. (Automatic notice — no human input has occurred.)",
        f"Command: {neutralize_control_tags(command)}",
    ]
    log_tail = tail(read_tail_bytes(Path(bg.output_path)))
    if log_tail:
        lines += [
            "",
            f'<command-output id="{bg.id}">',
            neutralize_control_tags(log_tail),
            "</command-output>",
        ]
    lines += ["", f"Full log: {bg.output_path}"]
    return InboxMessage(
        id=new_message_id(),
        from_session=bg.id,
        from_label=bg.summary or "shell command",
        from_role="agent",
        content="\n".join(lines),
        kind=KIND_MEMO,
        meta={
            "subagentId": bg.id,
            "label": bg.summary or "shell command",
            "agentType": "bash",
            "state": state,
            "elapsedMs": int(bg.elapsed * 1000),
            "artifactPath": bg.output_path,
            "exitCode": bg.exit_code,
        },
    )
