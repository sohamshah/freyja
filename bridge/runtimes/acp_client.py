"""Generic ACP (Agent Client Protocol) transport.

Drives any CLI that speaks ACP over stdio — Claude Code, GitHub Copilot CLI,
and future vendors implementing the same protocol. Lifetime is the
Freyja session: spawn once on the first turn, hold the subprocess across
turns, close at session end (or on hard error → next turn respawns).

This is the part of the design we depart from Hermes on: their ACP client
spawns a fresh subprocess per `_create_chat_completion` call, which means
the child's internal session state is lost between turns. We open ONE ACP
session per Freyja session and stream `session/prompt` calls into it, so
Claude Code's own context window survives across the user's turns the way
it would if the operator were chatting with it directly.

JSON-RPC 2.0 messages, newline-delimited, asyncio-native:
  * `_writer_task`: not needed — we write directly under a lock
  * `_stdout_reader`: parses each line, dispatches responses to
    pending Future, notifications to per-prompt queue, server-initiated
    requests to the handler chain
  * `_stderr_reader`: tail buffer for diagnostics (auth errors, etc.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_INIT_TIMEOUT_S = 15.0
_DEFAULT_NEW_SESSION_TIMEOUT_S = 30.0
_DEFAULT_PROMPT_TIMEOUT_S = 600.0
# After the harness signals a tool-call-completed event, if no further
# session/update arrives within this window, assume the child wedged
# and abort the turn. Mirrors Hermes' 90 s post-tool quiet watchdog.
_POST_TOOL_QUIET_TIMEOUT_S = 90.0


@dataclass
class ACPTurnResult:
    """Outcome of one session/prompt call, projected for the bridge."""

    text: str = ""
    reasoning: str = ""
    # session/update notifications captured during the turn, in arrival
    # order. The runtime adapter projects these into Freyja's bus / message
    # stream as they arrive AND we keep them here for end-of-turn fixup.
    updates: list[dict] = field(default_factory=list)
    interrupted: bool = False
    error: Optional[str] = None
    # When set True, the caller should close the ACP client and respawn
    # on the next turn — the current process is in an unrecoverable
    # state (subprocess exited, auth failure, deadline blown).
    should_retire: bool = False
    # ACP-defined turn stop reason: end_turn / max_tokens / refusal / etc.
    stop_reason: str = "end_turn"


# Server-request handlers receive (method, params) → result-or-error dict.
# Used for fs/* requests and permission prompts the harness sends back to us.
ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]
UpdateHandler = Callable[[dict], Awaitable[None]]


class ACPClientError(RuntimeError):
    pass


class ACPClient:
    """One ACP subprocess + handshake + session, owned by one Freyja session."""

    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...] = ("--acp", "--stdio"),
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
        # Per-turn update queue; reset at the start of each prompt() call.
        self._update_queue: asyncio.Queue[dict] | None = None
        self._update_handler: UpdateHandler | None = None
        # Server-initiated requests we haven't responded to yet, keyed by id.
        self._in_flight_server_requests: set[Any] = set()
        # Lifecycle: did we ever finish the initialize handshake?
        self._initialized = False
        self._session_id: str | None = None
        self._closed = False
        # Live `session/prompt` future — set during prompt() so we can
        # cancel cleanly on interrupt.
        self._active_prompt_future: asyncio.Future | None = None

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Spawn the subprocess + initialize handshake. Idempotent."""
        if self._initialized:
            return
        if shutil.which(self._command) is None:
            raise ACPClientError(
                f"`{self._command}` not found on PATH. Install it or set the "
                f"override env var (see runtime registry)."
            )

        env = os.environ.copy()
        env.update(self._env_overrides)
        # Many CLIs assume a TTY; tell them otherwise so they don't ANSI-paint.
        env.setdefault("TERM", "dumb")
        env.setdefault("NO_COLOR", "1")

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
            raise ACPClientError(f"failed to spawn `{self._command}`: {e}") from e

        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"acp-stdout-{self._command}"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"acp-stderr-{self._command}"
        )

        try:
            init_result = await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {
                                "readTextFile": True,
                                "writeTextFile": True,
                            },
                        },
                        "clientInfo": self._client_info,
                    },
                ),
                timeout=_DEFAULT_INIT_TIMEOUT_S,
            )
        except asyncio.TimeoutError as e:
            self._stderr_blob_to_log("ACP initialize timed out")
            await self._terminate()
            raise ACPClientError("ACP initialize handshake timed out") from e
        except Exception:
            self._stderr_blob_to_log("ACP initialize failed")
            await self._terminate()
            raise

        logger.info(
            "ACP initialize: command=%s server=%s",
            self._command,
            (init_result or {}).get("serverInfo")
            or (init_result or {}).get("agentInfo"),
        )
        self._initialized = True

    async def new_session(
        self,
        *,
        mcp_servers: list[dict] | None = None,
    ) -> str:
        """Create a new ACP session. Idempotent — returns the cached id."""
        if not self._initialized:
            await self.start()
        if self._session_id:
            return self._session_id
        result = await asyncio.wait_for(
            self._request(
                "session/new",
                {
                    "cwd": self._cwd,
                    "mcpServers": mcp_servers or [],
                },
            ),
            timeout=_DEFAULT_NEW_SESSION_TIMEOUT_S,
        )
        session_id = str((result or {}).get("sessionId") or "").strip()
        if not session_id:
            raise ACPClientError(
                f"session/new returned no sessionId (keys: "
                f"{sorted((result or {}).keys())})"
            )
        self._session_id = session_id
        logger.info("ACP session/new: id=%s cwd=%s", session_id[:8], self._cwd)
        return session_id

    async def load_session(self, session_id: str) -> bool:
        """Best-effort attempt to resume a prior harness session.

        Returns True if the harness accepted the load. Implementations
        vary across ACP-speaking CLIs; not all support resume. On
        failure we return False and the caller falls back to a fresh
        session/new + history replay."""
        if not self._initialized:
            await self.start()
        try:
            result = await asyncio.wait_for(
                self._request(
                    "session/load",
                    {"sessionId": session_id},
                ),
                timeout=_DEFAULT_NEW_SESSION_TIMEOUT_S,
            )
        except Exception as exc:
            logger.info("session/load(%s) declined: %s", session_id[:8], exc)
            return False
        loaded = (result or {}).get("sessionId") or session_id
        self._session_id = str(loaded)
        return True

    async def prompt(
        self,
        text: str,
        *,
        on_update: UpdateHandler | None = None,
        timeout_s: float = _DEFAULT_PROMPT_TIMEOUT_S,
        post_tool_quiet_timeout_s: float = _POST_TOOL_QUIET_TIMEOUT_S,
    ) -> ACPTurnResult:
        """One user turn. Sends session/prompt, drains session/update
        notifications until the prompt RPC returns (or watchdog fires),
        and returns the projected result."""
        if not self._initialized:
            await self.start()
        if not self._session_id:
            await self.new_session()
        assert self._session_id is not None

        result = ACPTurnResult()
        # Reset the per-turn update queue + register the live handler.
        self._update_queue = asyncio.Queue()
        self._update_handler = on_update

        # Fire the prompt RPC and a watchdog task in parallel. The first
        # to resolve wins. Most turns end with the RPC returning a normal
        # stopReason; the watchdog catches wedged children.
        rpc_task = asyncio.create_task(
            self._request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            ),
            name="acp-session-prompt",
        )
        self._active_prompt_future = rpc_task
        try:
            try:
                rpc_result = await asyncio.wait_for(rpc_task, timeout=timeout_s)
            except asyncio.TimeoutError:
                result.interrupted = True
                result.should_retire = True
                result.error = f"prompt timed out after {timeout_s:.0f}s"
                # Tell the child to drop the in-flight turn so it doesn't
                # keep generating into the void. Best-effort.
                try:
                    await self._notification(
                        "session/cancel",
                        {"sessionId": self._session_id},
                    )
                except Exception:
                    pass
                return result
            except ACPClientError as e:
                result.error = str(e)
                result.should_retire = True
                return result

            # The prompt RPC normally returns { stopReason: ... }. Map it.
            stop = (rpc_result or {}).get("stopReason") or "end_turn"
            result.stop_reason = str(stop)
        finally:
            self._active_prompt_future = None
            self._update_handler = None

        # Drain any straggler updates that arrived just before the RPC
        # response (the protocol is async, the model can emit a final
        # session/update concurrently with the prompt return).
        while self._update_queue is not None and not self._update_queue.empty():
            try:
                upd = self._update_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            result.updates.append(upd)

        # Concatenate text/reasoning from all updates we saw. The
        # streaming handler already pushed deltas to the renderer; this
        # is the final, complete capture for the message store.
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for upd in result.updates:
            kind = (upd.get("update") or {}).get("sessionUpdate") or ""
            content = (upd.get("update") or {}).get("content") or {}
            chunk = ""
            if isinstance(content, dict):
                chunk = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk:
                text_parts.append(chunk)
            elif kind == "agent_thought_chunk" and chunk:
                reasoning_parts.append(chunk)
        result.text = "".join(text_parts)
        result.reasoning = "".join(reasoning_parts)
        return result

    async def cancel_active_prompt(self) -> None:
        """Best-effort: signal the active turn to abort. The session
        stays alive; the next prompt() call can drive a new turn."""
        if not self._session_id:
            return
        try:
            await self._notification(
                "session/cancel",
                {"sessionId": self._session_id},
            )
        except Exception:
            logger.debug("session/cancel failed", exc_info=True)

    async def close(self) -> None:
        """Terminate the subprocess and reap reader tasks. Idempotent."""
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
            raise ACPClientError("subprocess not started")
        req_id = self._next_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send_line(payload)
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _notification(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        await self._send_line(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def _respond(self, req_id: Any, result: dict) -> None:
        await self._send_line({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _respond_error(
        self, req_id: Any, code: int, message: str
    ) -> None:
        await self._send_line(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )

    async def _send_line(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ACPClientError("stdin not available")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._stdin_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise ACPClientError(f"subprocess stdin closed: {e}") from e

    async def _read_stdout(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    # EOF — subprocess exited.
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    # Some CLIs print banners before going stdio. Log and skip.
                    logger.debug(
                        "ACP stdout non-JSON line: %r", line[:200]
                    )
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ACP stdout reader crashed")
        finally:
            # Fail any pending requests so callers don't hang forever
            # if the subprocess died.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        ACPClientError("ACP subprocess exited")
                    )
            self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("ACP stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ACP stderr reader crashed")

    async def _dispatch(self, msg: dict) -> None:
        """Route an inbound JSON-RPC message."""
        # Response to one of our requests: has `id`, has `result` or `error`.
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg.get("id")
            fut = self._pending.get(req_id)  # type: ignore[arg-type]
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    ACPClientError(
                        f"{err.get('code')}: {err.get('message') or err}"
                    )
                )
            else:
                fut.set_result(msg.get("result") or {})
            return

        # Notification or server-initiated request: has `method`.
        method = msg.get("method")
        if not method:
            return
        params = msg.get("params") or {}

        if "id" in msg:
            # Server-initiated REQUEST — must respond.
            req_id = msg["id"]
            self._in_flight_server_requests.add(req_id)
            try:
                handler = self._server_request_handler
                if handler is None:
                    await self._respond_error(
                        req_id, -32601, f"no handler for {method}"
                    )
                    return
                try:
                    result = await handler(method, params)
                    await self._respond(req_id, result or {})
                except Exception as e:
                    logger.exception("ACP server-request handler raised")
                    await self._respond_error(req_id, -32603, str(e))
            finally:
                self._in_flight_server_requests.discard(req_id)
            return

        # Pure notification (no id).
        if method == "session/update":
            if self._update_queue is not None:
                await self._update_queue.put(msg)
            handler = self._update_handler
            if handler is not None:
                try:
                    await handler(msg)
                except Exception:
                    logger.exception("ACP update handler raised")
            return

        # Unknown notification — log and ignore.
        logger.debug("ACP unhandled notification: %s", method)

    # ────────────────────────────────────────────────────────────────────
    # Internals — termination
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

    def _stderr_blob_to_log(self, prefix: str) -> None:
        tail = "\n".join(self.stderr_tail(40))
        if tail.strip():
            logger.warning("%s; stderr tail:\n%s", prefix, tail)
        else:
            logger.warning("%s (no stderr captured)", prefix)
