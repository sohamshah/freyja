"""Codex app-server JSON-RPC transport.

Speaks the protocol exposed by `codex app-server` (Codex CLI 0.125+).
Per-Freyja-session lifetime: one subprocess + one thread for all turns.

Protocol (verified against Hermes §27.2 / Codex CLI 0.135):

  Handshake
    → initialize { clientInfo, capabilities }
    ← initialize.result { userAgent, codexHome, ... }
    → initialized (notification)

  Thread
    → thread/start { cwd }
    ← thread/start.result { thread: { id } }

  Per turn
    → turn/start { threadId, input: [{type:"text", text}] }
    ← turn/start.result { turn: { id } }
    ← (notifications) item/started, item/<type>/delta, item/completed, turn/completed
    ← (server-initiated requests) item/commandExecution/requestApproval,
                                  item/fileChange/requestApproval,
                                  item/permissions/requestApproval,
                                  mcpServer/elicitation/request

Item types we materialize on item/completed:
  agentMessage, reasoning, commandExecution, fileChange,
  mcpToolCall, dynamicToolCall, userMessage
Delta items (item/<type>/delta) are display-only — surfaced to on_event
for streaming UI but not added to the durable message list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_INIT_TIMEOUT_S = 15.0
_DEFAULT_TURN_TIMEOUT_S = 600.0
_POST_TOOL_QUIET_TIMEOUT_S = 90.0


@dataclass
class CodexTurnResult:
    """Outcome of one Codex turn (turn/start → turn/completed)."""

    text: str = ""
    reasoning: str = ""
    tool_use_count: int = 0
    stop_reason: str = "completed"
    interrupted: bool = False
    is_error: bool = False
    error: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    items: list[dict] = field(default_factory=list)
    should_retire: bool = False


EventHandler = Callable[[dict], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]


class CodexClientError(RuntimeError):
    pass


class CodexClient:
    """One `codex app-server` subprocess + thread per Freyja session."""

    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...],
        cwd: str | Path,
        env_overrides: dict[str, str] | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        client_info: dict[str, str] | None = None,
        stderr_tail_lines: int = 60,
    ) -> None:
        self._command = command
        self._args = list(args)
        self._cwd = str(Path(cwd).expanduser().resolve())
        self._env_overrides = dict(env_overrides or {})
        self._server_request_handler = server_request_handler
        self._client_info = client_info or {
            "name": "freyja",
            "title": "Freyja",
            "version": "0.0.0",
        }
        self._stderr_tail: deque[str] = deque(maxlen=stderr_tail_lines)

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stdin_lock = asyncio.Lock()
        self._next_request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_id: Optional[str] = None
        self._initialized = False
        self._closed = False

        # Per-turn state, populated by stdout reader during a turn.
        self._turn_active = False
        self._turn_completed = asyncio.Event()
        self._turn_text_parts: list[str] = []
        self._turn_reasoning_parts: list[str] = []
        self._turn_items: list[dict] = []
        self._turn_tool_count = 0
        self._turn_on_event: EventHandler | None = None
        self._turn_last_tool_at: Optional[float] = None
        self._turn_error: Optional[str] = None

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────

    @property
    def thread_id(self) -> Optional[str]:
        return self._thread_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self._initialized:
            return
        if shutil.which(self._command) is None:
            raise CodexClientError(
                f"`{self._command}` not found on PATH. Install Codex CLI "
                f"(brew install codex; needs version 0.125+) or set "
                f"FREYJA_CODEX_COMMAND to override."
            )

        env = os.environ.copy()
        env.update(self._env_overrides)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        # Default Codex tracing low so stderr stays readable.
        env.setdefault("RUST_LOG", "warn")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
            )
        except FileNotFoundError as e:
            raise CodexClientError(
                f"failed to spawn `{self._command}`: {e}"
            ) from e

        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name="codex-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="codex-stderr"
        )

        try:
            await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "clientInfo": self._client_info,
                        "capabilities": {},
                    },
                ),
                timeout=_DEFAULT_INIT_TIMEOUT_S,
            )
        except asyncio.TimeoutError as e:
            tail = "\n".join(self.stderr_tail(40))
            await self._terminate()
            raise CodexClientError(
                f"Codex initialize timed out. Is `codex` authenticated "
                f"(`codex login`)? stderr:\n{tail}"
            ) from e
        except CodexClientError:
            await self._terminate()
            raise

        await self._notification("initialized", {})
        self._initialized = True

    async def ensure_thread(self) -> str:
        if not self._initialized:
            await self.start()
        if self._thread_id:
            return self._thread_id
        result = await asyncio.wait_for(
            self._request("thread/start", {"cwd": self._cwd}),
            timeout=30.0,
        )
        thread_obj = result.get("thread") or {}
        tid = (
            thread_obj.get("id")
            or thread_obj.get("sessionId")
            or result.get("sessionId")
            or result.get("threadId")
        )
        if not tid:
            raise CodexClientError(
                f"thread/start returned no thread id (keys: "
                f"{sorted(result.keys())})"
            )
        self._thread_id = str(tid)
        logger.info("Codex thread/start: id=%s cwd=%s", self._thread_id[:8], self._cwd)
        return self._thread_id

    async def resume_thread(self, thread_id: str) -> bool:
        """Best-effort thread resume. Codex doesn't currently expose a
        documented thread/resume — we just adopt the id and let it
        succeed/fail on the first turn. Returns False if the server
        rejected it."""
        if not self._initialized:
            await self.start()
        # Some Codex versions accept a "threadId" param to thread/start.
        try:
            result = await asyncio.wait_for(
                self._request(
                    "thread/start",
                    {"cwd": self._cwd, "threadId": thread_id},
                ),
                timeout=10.0,
            )
        except Exception as e:
            logger.info("Codex thread resume rejected: %s", e)
            return False
        thread_obj = result.get("thread") or {}
        tid = thread_obj.get("id") or result.get("sessionId")
        self._thread_id = str(tid or thread_id)
        return True

    async def prompt(
        self,
        text: str,
        *,
        on_event: EventHandler | None = None,
        timeout_s: float = _DEFAULT_TURN_TIMEOUT_S,
        post_tool_quiet_timeout_s: float = _POST_TOOL_QUIET_TIMEOUT_S,
    ) -> CodexTurnResult:
        if not self._initialized:
            await self.start()
        if not self._thread_id:
            await self.ensure_thread()
        assert self._thread_id is not None

        # Reset per-turn state.
        self._turn_active = True
        self._turn_completed = asyncio.Event()
        self._turn_text_parts = []
        self._turn_reasoning_parts = []
        self._turn_items = []
        self._turn_tool_count = 0
        self._turn_on_event = on_event
        self._turn_last_tool_at = None
        self._turn_error = None

        result = CodexTurnResult(thread_id=self._thread_id)

        try:
            ts = await asyncio.wait_for(
                self._request(
                    "turn/start",
                    {
                        "threadId": self._thread_id,
                        "input": [{"type": "text", "text": text}],
                    },
                ),
                timeout=10.0,
            )
        except Exception as exc:
            self._turn_active = False
            self._turn_on_event = None
            result.error = self._format_error("turn/start failed", exc)
            result.should_retire = True
            return result

        result.turn_id = (ts.get("turn") or {}).get("id")

        # Wait for turn/completed or watchdog.
        deadline = asyncio.get_event_loop().time() + timeout_s
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                # Post-tool watchdog: if we saw a tool completion and
                # nothing else for `post_tool_quiet_timeout_s`, abort.
                if self._turn_last_tool_at is not None:
                    quiet = (
                        asyncio.get_event_loop().time()
                        - self._turn_last_tool_at
                    )
                    if quiet > post_tool_quiet_timeout_s:
                        await self._issue_interrupt(result.turn_id)
                        result.interrupted = True
                        result.should_retire = True
                        result.error = (
                            f"Codex went silent for {post_tool_quiet_timeout_s:.0f}s "
                            f"after a tool result; retiring session"
                        )
                        break
                try:
                    await asyncio.wait_for(
                        self._turn_completed.wait(),
                        timeout=min(remaining, post_tool_quiet_timeout_s),
                    )
                    break
                except asyncio.TimeoutError:
                    # Either deadline or watchdog window — loop to recheck.
                    continue
        except asyncio.TimeoutError:
            await self._issue_interrupt(result.turn_id)
            result.interrupted = True
            result.should_retire = True
            result.error = f"turn timed out after {timeout_s:.0f}s"
        finally:
            self._turn_active = False
            self._turn_on_event = None

        result.text = "".join(self._turn_text_parts)
        result.reasoning = "".join(self._turn_reasoning_parts)
        result.items = list(self._turn_items)
        result.tool_use_count = self._turn_tool_count
        if self._turn_error and result.error is None:
            result.error = self._turn_error
            result.is_error = True
        return result

    async def cancel(self) -> None:
        if not self.is_alive or not self._thread_id or not self._turn_active:
            return
        # We don't know the turn id outside of prompt; if a turn is
        # active, the cancel will be issued by the prompt's watchdog
        # path. Best-effort no-op here.
        return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._terminate()

    # ────────────────────────────────────────────────────────────────────
    # Internals — wire protocol
    # ────────────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    async def _request(self, method: str, params: dict) -> dict:
        if self._proc is None or self._proc.stdin is None:
            raise CodexClientError("subprocess not started")
        req_id = self._next_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._send_line(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _notification(self, method: str, params: dict) -> None:
        await self._send_line(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def _respond(self, req_id: Any, result: dict) -> None:
        await self._send_line({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _respond_error(self, req_id: Any, code: int, msg: str) -> None:
        await self._send_line(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": msg},
            }
        )

    async def _send_line(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise CodexClientError("stdin not available")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._stdin_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise CodexClientError(f"Codex stdin closed: {e}") from e

    async def _issue_interrupt(self, turn_id: Optional[str]) -> None:
        if not (self._thread_id and turn_id):
            return
        try:
            await asyncio.wait_for(
                self._request(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": turn_id},
                ),
                timeout=5.0,
            )
        except Exception:
            # "no active turn to interrupt" is fine; subprocess crash too.
            pass

    async def _read_stdout(self) -> None:
        # Snapshot the stream so close() flipping self._proc to None
        # mid-iteration doesn't crash this task on the next readline.
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.debug("Codex non-JSON stdout: %r", line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex stdout reader crashed")
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CodexClientError("Codex subprocess exited"))
            self._pending.clear()
            # Unblock any active turn waiter
            if self._turn_active and not self._turn_completed.is_set():
                self._turn_error = "Codex subprocess exited mid-turn"
                self._turn_completed.set()

    async def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("Codex stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex stderr reader crashed")

    async def _dispatch(self, msg: dict) -> None:
        # Response to our request
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg.get("id")
            fut = self._pending.get(req_id)  # type: ignore[arg-type]
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    CodexClientError(
                        f"{err.get('code')}: {err.get('message') or err}"
                    )
                )
            else:
                fut.set_result(msg.get("result") or {})
            return

        method = msg.get("method")
        if not method:
            return
        params = msg.get("params") or {}

        # Server-initiated request — must respond
        if "id" in msg:
            await self._handle_server_request(msg["id"], method, params)
            return

        # Notification — accumulate / project
        await self._handle_notification(method, params)

    async def _handle_server_request(
        self, req_id: Any, method: str, params: dict
    ) -> None:
        handler = self._server_request_handler
        if handler is None:
            # No handler installed — decline cleanly so codex doesn't hang.
            await self._respond_error(req_id, -32601, f"no handler for {method}")
            return
        try:
            result = await handler(method, params)
            await self._respond(req_id, result or {})
        except Exception as e:
            logger.exception("Codex server-request handler raised")
            await self._respond_error(req_id, -32603, str(e))

    async def _handle_notification(self, method: str, params: dict) -> None:
        # Forward raw to live handler for streaming UI
        if self._turn_on_event is not None and self._turn_active:
            try:
                await self._turn_on_event({"method": method, "params": params})
            except Exception:
                logger.exception("Codex on_event handler raised")

        if method == "turn/completed":
            # turn payload carries failure info when status != "completed".
            # Always prefer a turn.error.message over earlier generic
            # markers (systemError, etc.) — it's the most specific.
            turn = params.get("turn") or {}
            status = (turn.get("status") or "").strip()
            err = turn.get("error") or {}
            if err:
                self._turn_error = str(err.get("message") or err)
            elif status and status not in {"completed", "interrupted"}:
                self._turn_error = self._turn_error or f"turn ended status={status}"
            self._turn_completed.set()
            return

        # Codex emits top-level `error` notifications for auth refresh
        # failures, network errors, etc. Surface immediately AND prefer
        # this over an earlier generic systemError marker.
        if method == "error":
            err = params.get("error") or {}
            msg = (
                err.get("message")
                or err.get("codexErrorInfo")
                or "Codex error"
            )
            # Common case: stale OAuth — tell the user to re-login.
            info = str(err.get("codexErrorInfo") or "").lower()
            lowered = str(msg).lower()
            if "unauthorized" in info or "refresh" in lowered or "token" in lowered:
                self._turn_error = (
                    f"Codex auth needs refresh — run `codex logout && codex login`. "
                    f"Original: {msg}"
                )
            else:
                self._turn_error = str(msg)
            self._turn_completed.set()
            return

        # Thread-level system error: record as a fallback marker but
        # don't signal turn completion yet — the more specific `error`
        # notification + `turn/completed` payload follow within ms and
        # carry the actual cause (auth refresh, network, etc.). If
        # those never arrive, the outer prompt timeout still catches us.
        if method == "thread/status/changed":
            status_obj = params.get("status") or {}
            if str(status_obj.get("type") or "") == "systemError":
                if self._turn_error is None:
                    self._turn_error = "Codex thread entered systemError"
            return

        # MCP startup status — log only.
        if method == "mcpServer/startupStatus/updated":
            return

        # `thread/started` and `turn/started` are informational acks of
        # our own RPCs; the result payload already gave us the ids.
        if method in {"thread/started", "turn/started", "remoteControl/status/changed"}:
            return

        if method == "item/completed":
            item = params.get("item") or {}
            item_type = str(item.get("type") or "")
            self._turn_items.append(params)
            if item_type == "agentMessage":
                text = str(item.get("text") or "")
                if text:
                    self._turn_text_parts.append(text)
            elif item_type == "reasoning":
                # reasoning items carry "summary" or "content" arrays
                for k in ("summary", "content"):
                    arr = item.get(k) or []
                    if isinstance(arr, list):
                        for entry in arr:
                            if isinstance(entry, str):
                                self._turn_reasoning_parts.append(entry)
                            elif isinstance(entry, dict):
                                t = entry.get("text") or entry.get("content")
                                if isinstance(t, str):
                                    self._turn_reasoning_parts.append(t)
            elif item_type in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "dynamicToolCall",
            }:
                self._turn_tool_count += 1
                self._turn_last_tool_at = asyncio.get_event_loop().time()
            return

        # Delta items — display only; forwarded above to on_event.
        # Unknown notifications: log + ignore.
        if not method.startswith("item/"):
            logger.debug("Codex unhandled notification: %s", method)

    # ────────────────────────────────────────────────────────────────────
    # Internals — termination + diagnostics
    # ────────────────────────────────────────────────────────────────────

    async def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except ProcessLookupError:
                pass
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._stdout_task = None
        self._stderr_task = None

    def stderr_tail(self, lines: int = 20) -> list[str]:
        return list(self._stderr_tail)[-lines:]

    def _format_error(self, prefix: str, exc: Any = "") -> str:
        body = f"{prefix}: {exc}" if str(exc) else prefix
        tail = "\n".join(self.stderr_tail(20))
        return f"{body}\nstderr:\n{tail}" if tail.strip() else body
