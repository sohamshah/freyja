"""Session-owned shell monitors with bounded, rate-limited stdout events."""

from __future__ import annotations

import asyncio
import codecs
import inspect
import math
import os
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from bridge.process_env import child_env
from bridge.tools.background_shell import (
    CURRENT_TOOL_CONTEXT,
    TERM_GRACE_S,
    BackgroundCommand,
    ToolContext,
    stop_process_group,
)
from bridge.tools.base import PermissionRequest, ToolDefinition, ToolResult, ToolTier
from bridge.tools.bash_tool import classify_command_risk, sanitize_bash_summary

DEFAULT_TIMEOUT_MS = 300_000
MAX_TIMEOUT_MS = 1_800_000
MAX_CONCURRENT_MONITORS = 4
MAX_RECENT_MONITORS = 20
MAX_LINE_CHARS = 500
MAX_BATCH_CHARS = 3_000
BATCH_INTERVAL_S = 0.2
MAX_LOG_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 4096
TOKEN_CAPACITY = 10
TOKEN_REFILL_S = 2.0
NOISE_LIMIT_S = 30.0
NOISE_QUIET_S = 2.0


@dataclass
class MonitorRecord(BackgroundCommand):
    stderr_path: str = ""
    _exit_notified: bool = False


class _BoundedLog:
    def __init__(self, sink: BinaryIO) -> None:
        self.sink = sink
        self.written = 0

    def write(self, chunk: bytes) -> None:
        remaining = MAX_LOG_BYTES - self.written
        if remaining > 0:
            data = chunk[:remaining]
            self.sink.write(data)
            self.sink.flush()
            self.written += len(data)

    def close(self) -> None:
        self.sink.close()


class _MonitorProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        record: MonitorRecord,
        ctx: ToolContext,
        stdout_log: BinaryIO,
        stderr_log: BinaryIO,
    ) -> None:
        self.proc = proc
        self.record = record
        self.ctx = ctx
        self._logs = (_BoundedLog(stdout_log), _BoundedLog(stderr_log))
        self._pending: list[str] = []
        self._pending_chars = 0
        self._pending_suppressed = 0
        self._tokens = float(TOKEN_CAPACITY)
        self._refilled_at = time.monotonic()
        self._last_line_at: float | None = None
        self._noise_since: float | None = None
        self.rate_limited = asyncio.Event()
        self._stdout_done = asyncio.Event()
        self._stop_task: asyncio.Task[None] | None = None
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        self._sender = asyncio.create_task(self._deliver())
        self._waiter = asyncio.create_task(self.proc.wait())
        self._done = asyncio.create_task(self._finish())

    def _suppress(self, now: float, count: int = 1) -> None:
        self.record.suppressed_count += count
        self._pending_suppressed += count
        if self._noise_since is None:
            self._noise_since = now

    def _line(self, line: str) -> None:
        now = time.monotonic()
        if self._last_line_at is not None and now - self._last_line_at >= NOISE_QUIET_S:
            self._noise_since = None
        self._last_line_at = now
        if self._pending_chars + len(line) + 1 > MAX_BATCH_CHARS - 80:
            self._suppress(now)
        else:
            self._pending.append(line)
            self._pending_chars += len(line) + 1
        if self._noise_since is not None and now - self._noise_since >= NOISE_LIMIT_S:
            self.rate_limited.set()

    async def _read_stdout(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        partial = ""

        def consume(text: str, *, final: bool = False) -> None:
            nonlocal partial
            partial += text
            while partial:
                newline = partial.find("\n")
                if 0 <= newline <= MAX_LINE_CHARS:
                    self._line(partial[:newline].removesuffix("\r"))
                    partial = partial[newline + 1 :]
                elif len(partial) > MAX_LINE_CHARS:
                    self._line(partial[: MAX_LINE_CHARS - 1] + "…")
                    partial = partial[MAX_LINE_CHARS:]
                else:
                    break
            if final and partial:
                self._line(partial)
                partial = ""

        try:
            if self.proc.stdout is not None:
                while chunk := await self.proc.stdout.read(READ_CHUNK_BYTES):
                    self._logs[0].write(chunk)
                    consume(decoder.decode(chunk))
                    await asyncio.sleep(0)
            consume(decoder.decode(b"", final=True), final=True)
        finally:
            self._stdout_done.set()

    async def _read_stderr(self) -> None:
        if self.proc.stderr is not None:
            while chunk := await self.proc.stderr.read(READ_CHUNK_BYTES):
                self._logs[1].write(chunk)
                await asyncio.sleep(0)

    async def _deliver(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stdout_done.wait(), timeout=BATCH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            if self._pending:
                lines, self._pending = self._pending, []
                self._pending_chars = 0
                now = time.monotonic()
                self._tokens = min(
                    float(TOKEN_CAPACITY),
                    self._tokens + (now - self._refilled_at) / TOKEN_REFILL_S,
                )
                self._refilled_at = now
                if self._tokens < 1:
                    self._suppress(now, len(lines))
                else:
                    self._tokens -= 1
                    suppressed, self._pending_suppressed = self._pending_suppressed, 0
                    text = "\n".join(lines)
                    if suppressed:
                        text = f"[{suppressed} lines suppressed]\n{text}"
                    self.record.event_count += 1
                    result = self.ctx.on_monitor_event(self.record, text)
                    if inspect.isawaitable(result):
                        await result
            if self._stdout_done.is_set() and not self._pending:
                return

    async def _finish(self) -> int:
        try:
            results = await asyncio.gather(self._waiter, *self._readers, self._sender)
            return results[0]
        finally:
            for log in self._logs:
                log.close()

    def group_exists(self) -> bool:
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _stop(self) -> None:
        if self.proc.returncode is None or self.group_exists():
            stop_process_group(self.proc, grace_s=TERM_GRACE_S)
            await asyncio.sleep(TERM_GRACE_S)
        try:
            await asyncio.wait_for(asyncio.shield(self._done), timeout=2.0)
        except Exception:
            tasks = [*self._readers, self._sender, self._waiter, self._done]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for log in self._logs:
                log.close()

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await asyncio.shield(self._stop_task)


async def _finalize(record: MonitorRecord, ctx: ToolContext) -> None:
    if record._exit_notified:
        return
    record.exit_code = record._process.proc.returncode
    record.ended_at = time.time()
    if record.is_running:
        record.state = "exited"
    record._exit_notified = True
    result = ctx.on_background_exit(record)
    if inspect.isawaitable(result):
        await result


async def _cancelled_watch(record: MonitorRecord, ctx: ToolContext) -> None:
    if record.is_running:
        record.state = "stopped"
    await record._process.stop()
    await _finalize(record, ctx)


def _watch_done(task: asyncio.Task[None], record: MonitorRecord, ctx: ToolContext) -> None:
    if task.cancelled() and not record._exit_notified:
        record._watch_task = asyncio.create_task(_cancelled_watch(record, ctx))


async def _watch(record: MonitorRecord, ctx: ToolContext) -> None:
    running = record._process
    limited = asyncio.create_task(running.rate_limited.wait())
    cancelled = False
    try:
        done, _ = await asyncio.wait(
            {running._done, limited},
            timeout=record.timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if running._done in done:
            await running._done
            if running.group_exists():
                await running.stop()
        else:
            if record.is_running:
                record.state = "rate_limited" if limited in done else "timed_out"
            await running.stop()
    except asyncio.CancelledError:
        cancelled = True
        if record.is_running:
            record.state = "stopped"
        await running.stop()
    except Exception:
        if record.is_running:
            record.state = "failed"
        await running.stop()
    finally:
        limited.cancel()
        await asyncio.gather(limited, return_exceptions=True)
        await _finalize(record, ctx)
    if cancelled:
        raise asyncio.CancelledError


class MonitorTool:
    def __init__(self, working_dir: str | None = None) -> None:
        self._working_dir = working_dir or os.getcwd()
        self._starting: dict[int, int] = {}

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="monitor",
            summary="Monitor a shell command's stdout in the background",
            tier=ToolTier.WARM,
            description=(
                "Start, list, or stop a session-owned shell monitor. Shell commands only; "
                "WebSocket/WS monitoring is not supported. Start returns immediately; stdout "
                "arrives as batched events and a terminal memo reports completion. Stderr is "
                "logged without events. Provide command and a short description for start. "
                "Output is bounded and rate-limited; sustained noisy monitors are stopped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "list", "stop"],
                        "default": "start",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command (required for start)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short user-facing description (required for start)",
                    },
                    "working_dir": {"type": "string"},
                    "timeout_ms": {
                        "type": "number",
                        "default": DEFAULT_TIMEOUT_MS,
                        "maximum": MAX_TIMEOUT_MS,
                        "exclusiveMinimum": 0,
                    },
                    "id": {
                        "type": "string",
                        "description": "Session-owned monitor ID (required for stop)",
                    },
                },
                "additionalProperties": False,
            },
        )

    @property
    def requires_permission(self) -> bool:
        return True

    def _validate(self, arguments: dict[str, Any]) -> str | None:
        unknown = arguments.keys() - {
            "action",
            "command",
            "description",
            "working_dir",
            "timeout_ms",
            "id",
        }
        if unknown:
            return "Unsupported arguments; shell commands only (no WS/WebSocket)."
        action = arguments.get("action", "start")
        if action not in ("start", "list", "stop"):
            return "action must be start, list, or stop."
        if action == "stop":
            monitor_id = arguments.get("id")
            if not isinstance(monitor_id, str) or not monitor_id.strip():
                return "id is required for stop."
        if action == "start":
            for key in ("command", "description"):
                value = arguments.get(key)
                if not isinstance(value, str) or not value.strip():
                    return f"{key} is required for start."
            if "working_dir" in arguments:
                value = arguments["working_dir"]
                if not isinstance(value, str) or not value.strip():
                    return "working_dir must be a nonempty string."
            timeout = arguments.get("timeout_ms", DEFAULT_TIMEOUT_MS)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                return "timeout_ms must be a finite positive number."
            try:
                valid_timeout = math.isfinite(timeout) and 0 < timeout <= MAX_TIMEOUT_MS
            except OverflowError:
                valid_timeout = False
            if not valid_timeout:
                return f"timeout_ms must be finite and positive, at most {MAX_TIMEOUT_MS}."
        for key in ("command", "description", "working_dir", "id"):
            value = arguments.get(key)
            if isinstance(value, str) and any(
                unicodedata.category(char) in ("Cc", "Cf") and char not in "\n\t" for char in value
            ):
                return f"{key} contains unsupported control characters."
        return None

    async def permission_prompt(self, arguments: dict[str, Any]) -> PermissionRequest | None:
        if self._validate(arguments) or arguments.get("action", "start") != "start":
            return None
        return PermissionRequest(
            prompt=f"Start shell monitor: {sanitize_bash_summary(arguments['description'])}",
            level=classify_command_risk(arguments["command"]),
            details=(
                f"Working directory: {arguments.get('working_dir', self._working_dir)}\n"
                f"Timeout: {arguments.get('timeout_ms', DEFAULT_TIMEOUT_MS)}ms"
            ),
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        error = self._validate(arguments)
        if error:
            return ToolResult(call_id=call_id, content=f"Error: {error}", is_error=True)
        ctx = CURRENT_TOOL_CONTEXT.get()
        if (
            ctx is None
            or not isinstance(getattr(ctx, "background_commands", None), dict)
            or not callable(getattr(ctx, "on_monitor_event", None))
            or not callable(getattr(ctx, "is_stopping", None))
            or not callable(getattr(ctx, "on_background_start", None))
            or not callable(getattr(ctx, "on_background_exit", None))
            or getattr(ctx, "output_dir", None) is None
        ):
            return ToolResult(
                call_id=call_id,
                content="Error: monitor requires a session with monitor lifecycle support.",
                is_error=True,
            )
        action = arguments.get("action", "start")
        if action == "list":
            records = [bg for bg in ctx.background_commands.values() if bg.kind == "monitor"]
            records = sorted(records, key=lambda bg: bg.started_at, reverse=True)[
                :MAX_RECENT_MONITORS
            ]
            lines = [
                f"{bg.id} | {bg.state} | {sanitize_bash_summary(bg.summary)} | "
                f"events={bg.event_count} suppressed={bg.suppressed_count}"
                for bg in records
            ]
            return ToolResult(
                call_id=call_id, content="\n".join(lines) or "No monitors in this session."
            )
        if action == "stop":
            record = ctx.background_commands.get(arguments["id"])
            if record is None or record.kind != "monitor":
                return ToolResult(
                    call_id=call_id,
                    content="Error: monitor ID not found in this session.",
                    is_error=True,
                )
            if record.is_running:
                record.state = "stopped"
                await record._process.stop()
            if record._watch_task is not None:
                await asyncio.shield(record._watch_task)
            return ToolResult(call_id=call_id, content=f"Monitor {record.id}: {record.state}.")
        return await self._start(call_id, arguments, ctx)

    async def _start(self, call_id: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_stopping():
            return ToolResult(
                call_id=call_id,
                content="Error: cannot start a monitor while this session is stopping.",
                is_error=True,
            )
        registry_key = id(ctx.background_commands)
        active = sum(
            bg.kind == "monitor" and bg.is_running for bg in ctx.background_commands.values()
        )
        if active + self._starting.get(registry_key, 0) >= MAX_CONCURRENT_MONITORS:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: at most {MAX_CONCURRENT_MONITORS} concurrent monitors "
                    "are allowed per session."
                ),
                is_error=True,
            )
        self._starting[registry_key] = self._starting.get(registry_key, 0) + 1
        logs: list[BinaryIO] = []
        spawn: asyncio.Task[Any] | None = None
        running: _MonitorProcess | None = None
        record: MonitorRecord | None = None
        registered = False
        try:
            monitor_id = f"monitor_{uuid.uuid4().hex}"
            output_dir = Path(ctx.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = output_dir / f"{monitor_id}.stdout.log"
            stderr_path = output_dir / f"{monitor_id}.stderr.log"
            logs.append(stdout_path.open("wb"))
            logs.append(stderr_path.open("wb"))
            spawn = asyncio.create_task(
                asyncio.create_subprocess_shell(
                    arguments["command"],
                    cwd=arguments.get("working_dir", self._working_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_env(),
                    start_new_session=True,
                    limit=READ_CHUNK_BYTES * 2,
                )
            )
            proc = await asyncio.shield(spawn)
            if ctx.is_stopping():
                await self._stop_startup(spawn)
                return ToolResult(
                    call_id=call_id,
                    content="Error: session stopped while the monitor was starting.",
                    is_error=True,
                )
            now = time.time()
            record = MonitorRecord(
                id=monitor_id,
                command=arguments["command"],
                summary=sanitize_bash_summary(arguments["description"]),
                pid=proc.pid,
                output_path=str(stdout_path),
                stderr_path=str(stderr_path),
                started_at=now,
                backgrounded_at=now,
                reason="monitor",
                kind="monitor",
                timeout_s=arguments.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000,
            )
            running = _MonitorProcess(proc, record, ctx, *logs)
            record._process = running
            ctx.on_background_start(record)
            registered = True
            record._watch_task = asyncio.create_task(
                _watch(record, ctx), name=f"monitor-watch-{record.id}"
            )
            record._watch_task.add_done_callback(lambda task: _watch_done(task, record, ctx))
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Started shell monitor {record.id} (pid {record.pid}). "
                    f"Stdout events arrive automatically; do not poll.\n"
                    f"Stdout log: {record.output_path}\nStderr log: {record.stderr_path}"
                ),
            )
        except asyncio.CancelledError:
            if running is not None:
                await running.stop()
            elif spawn is not None:
                await self._stop_startup(spawn)
            raise
        except Exception as exc:
            if running is not None:
                await running.stop()
            elif spawn is not None:
                await self._stop_startup(spawn)
            if record is not None and (
                registered or ctx.background_commands.get(record.id) is record
            ):
                record.state = "failed"
                await _finalize(record, ctx)
            return ToolResult(
                call_id=call_id, content=f"Error starting monitor: {exc}", is_error=True
            )
        finally:
            self._starting[registry_key] -= 1
            if not self._starting[registry_key]:
                del self._starting[registry_key]
            if running is None:
                for log in logs:
                    log.close()

    @staticmethod
    async def _stop_startup(spawn: asyncio.Task[Any]) -> None:
        try:
            proc = await asyncio.shield(spawn)
        except Exception:
            return
        stop_process_group(proc, grace_s=TERM_GRACE_S)
        await asyncio.sleep(TERM_GRACE_S)
        await proc.communicate()
