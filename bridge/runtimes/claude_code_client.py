"""Claude Code stream-json transport.

Speaks Anthropic's documented automation protocol for the `claude` CLI:

    claude --print --input-format=stream-json --output-format=stream-json \\
           --verbose --include-partial-messages [--session-id <uuid>|--resume <uuid>]

The protocol is newline-delimited JSON on stdin (user messages) and stdout
(system / stream_event / assistant / result events). The subprocess STAYS
ALIVE for the lifetime of the Freyja session: we feed each user turn into
stdin and drain stdout until we see the `result` event for that turn.
This is how Claude Code's own context window survives across the user's
turns inside one Freyja session — closer to "real" Claude Code semantics
than respawning per turn.

Resume across Freyja restart: we assign the conversation a stable UUID at
spawn time via `--session-id`. On the next Freyja launch, we respawn with
`--resume <uuid>` and Claude Code reloads its prior transcript from
`~/.claude/projects/<project>/<session-id>.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid as _uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TURN_TIMEOUT_S = 900.0


@dataclass
class ClaudeCodeTurnResult:
    """Outcome of one user turn, captured from the stream-json stream."""

    text: str = ""
    reasoning: str = ""
    tool_use_count: int = 0
    stop_reason: str = "end_turn"
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    is_error: bool = False
    error: Optional[str] = None
    # Raw events captured for end-of-turn audit / replay; bus delivery
    # happens via the on_event callback during the turn.
    events: list[dict] = field(default_factory=list)
    should_retire: bool = False


EventHandler = Callable[[dict], Awaitable[None]]


class ClaudeCodeClientError(RuntimeError):
    pass


class ClaudeCodeClient:
    """One `claude --print` subprocess, one Freyja session.

    Not thread-safe — drive from a single asyncio task. Stdout reader
    runs as a background task; we drive turns by writing to stdin and
    awaiting the next `result` event keyed on session_id."""

    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...],
        cwd: str | Path,
        env_overrides: dict[str, str] | None = None,
        resume_session_id: Optional[str] = None,
        stderr_tail_lines: int = 80,
    ) -> None:
        self._command = command
        self._cwd = str(Path(cwd).expanduser().resolve())
        self._env_overrides = dict(env_overrides or {})
        # Generate (or reuse) a stable UUID up-front. We pass it as
        # --session-id on first spawn so Claude Code's session file on
        # disk is deterministic and we can --resume against it later.
        if resume_session_id:
            self._session_id: Optional[str] = resume_session_id
            self._is_resume = True
        else:
            self._session_id = str(_uuid.uuid4())
            self._is_resume = False

        # Build the full argv: base args (from registry) + session
        # control. We strip any pre-existing --session-id / --resume
        # from the configured args so env-var overrides don't clash.
        cleaned_args = [
            a
            for a in args
            if not (
                a.startswith("--session-id")
                or a.startswith("--resume")
                or a == "-r"
            )
        ]
        if self._is_resume:
            cleaned_args.extend(["--resume", self._session_id])  # type: ignore[list-item]
        else:
            cleaned_args.extend(["--session-id", self._session_id])  # type: ignore[list-item]
        self._args = cleaned_args

        self._stderr_tail: deque[str] = deque(maxlen=stderr_tail_lines)

        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stdin_lock = asyncio.Lock()
        # Per-turn result Future, set by the stdout reader when we see
        # the `result` event for the in-flight turn.
        self._turn_future: asyncio.Future[dict] | None = None
        self._on_event: EventHandler | None = None
        # Accumulators populated by the stdout reader, drained when a
        # turn completes.
        self._current_turn_text: list[str] = []
        self._current_turn_reasoning: list[str] = []
        self._current_turn_events: list[dict] = []
        self._current_turn_tool_uses = 0
        self._closed = False

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Spawn `claude --print ...`. Note: Claude Code holds the
        system/init event until the FIRST user message arrives over
        stdin, so we don't wait for init here — prompt() does. If
        the subprocess dies before the first prompt completes, the
        turn future picks up the error from the stdout reader."""
        if self._proc is not None:
            return
        # PATH defense (mirrors codex_client) — macOS GUI-launched apps
        # have a minimal PATH; walk standard install locations as a
        # fallback before giving up.
        resolved = shutil.which(self._command)
        if resolved is None and "/" not in self._command:
            home = os.path.expanduser("~")
            for prefix in (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                f"{home}/.nvm/versions/node",  # nvm — special-cased below
                f"{home}/.bun/bin",
                f"{home}/.local/bin",
                f"{home}/bin",
            ):
                if prefix.endswith("/versions/node"):
                    # Pick the highest installed node version that has
                    # the binary — nvm doesn't symlink "current" to
                    # /usr/local for global installs.
                    if not os.path.isdir(prefix):
                        continue
                    try:
                        versions = sorted(os.listdir(prefix), reverse=True)
                    except OSError:
                        continue
                    for v in versions:
                        candidate = os.path.join(prefix, v, "bin", self._command)
                        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                            resolved = candidate
                            logger.info(
                                "claude found at %s via nvm fallback", candidate
                            )
                            break
                    if resolved:
                        break
                else:
                    candidate = os.path.join(prefix, self._command)
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        resolved = candidate
                        logger.info(
                            "claude found at %s via fallback PATH walk", candidate
                        )
                        break
        if resolved is None:
            raise ClaudeCodeClientError(
                f"`{self._command}` not found on PATH or in standard install "
                f"locations. Install Claude Code (see https://docs.claude.com/"
                f"en/docs/claude-code) or set FREYJA_CLAUDE_CODE_COMMAND to "
                f"override."
            )
        self._command = resolved
        logger.info("claude spawn: %s %s", self._command, " ".join(self._args))

        # `child_env()` strips Freyja's PYTHONHOME/PYTHONPATH/VIRTUAL_ENV
        # so any python the Claude Code subprocess spawns (MCP servers,
        # hooks, user scripts) doesn't crash with "No module named
        # 'encodings'". The harness binary itself is Node, but it can
        # and does spawn python children. See bridge/process_env.py.
        from bridge.process_env import child_env

        env = child_env()
        env.update(self._env_overrides)
        # Claude Code refuses to emit ANSI without a TTY by default, but
        # explicit env vars make this deterministic across shells.
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                # Default StreamReader limit is 64KB; Claude Code's
                # stream-json result events carry the full assistant
                # message + tool input/output and can easily exceed
                # that on a turn with large file contents. Match the
                # codex client's bump so readline() doesn't crash.
                limit=32 * 1024 * 1024,
            )
        except FileNotFoundError as e:
            raise ClaudeCodeClientError(
                f"failed to spawn `{self._command}`: {e}"
            ) from e

        logger.info(
            "Claude Code subprocess spawned (pid=%s session=%s)",
            self._proc.pid,
            (self._session_id or "new")[:8],
        )
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"claude-stdout-{(self._session_id or 'new')[:8]}"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"claude-stderr-{(self._session_id or 'new')[:8]}"
        )

    async def prompt(
        self,
        user_text: str,
        *,
        on_event: EventHandler | None = None,
        timeout_s: float = _DEFAULT_TURN_TIMEOUT_S,
    ) -> ClaudeCodeTurnResult:
        """Write one user message to stdin, drain stdout until the `result`
        event arrives, return the projected turn result."""
        if not self.is_alive:
            await self.start()
        assert self._proc is not None and self._proc.stdin is not None

        if self._turn_future is not None and not self._turn_future.done():
            raise ClaudeCodeClientError("a turn is already in flight")

        # Reset per-turn accumulators.
        self._current_turn_text = []
        self._current_turn_reasoning = []
        self._current_turn_events = []
        self._current_turn_tool_uses = 0
        self._on_event = on_event
        loop = asyncio.get_event_loop()
        self._turn_future = loop.create_future()

        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            },
        }
        try:
            await self._send_line(payload)
        except Exception as exc:
            self._turn_future = None
            raise ClaudeCodeClientError(
                f"failed to write user message: {exc}"
            ) from exc

        try:
            result_event = await asyncio.wait_for(
                self._turn_future, timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return ClaudeCodeTurnResult(
                text="".join(self._current_turn_text),
                reasoning="".join(self._current_turn_reasoning),
                events=list(self._current_turn_events),
                tool_use_count=self._current_turn_tool_uses,
                stop_reason="timeout",
                is_error=True,
                error=f"turn timed out after {timeout_s:.0f}s",
                should_retire=True,
            )
        finally:
            self._on_event = None
            self._turn_future = None

        usage = (result_event.get("usage") or {})
        return ClaudeCodeTurnResult(
            text=(
                result_event.get("result")
                or "".join(self._current_turn_text)
            ),
            reasoning="".join(self._current_turn_reasoning),
            events=list(self._current_turn_events),
            tool_use_count=self._current_turn_tool_uses,
            stop_reason=str(result_event.get("stop_reason") or "end_turn"),
            duration_ms=int(result_event.get("duration_ms") or 0),
            total_cost_usd=float(result_event.get("total_cost_usd") or 0.0),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            is_error=bool(result_event.get("is_error") or False),
        )

    async def cancel(self) -> None:
        """Best-effort: close stdin so the child stops generating.

        Claude Code's stream-json mode doesn't have a documented mid-turn
        cancel RPC; closing stdin terminates the conversation cleanly
        between turns. For mid-turn cancel we'd need SIGINT — keeping it
        light here, the next prompt() will spawn a fresh process."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._terminate()

    # ────────────────────────────────────────────────────────────────────
    # Internals — wire protocol
    # ────────────────────────────────────────────────────────────────────

    async def _send_line(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ClaudeCodeClientError("stdin not available")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._stdin_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise ClaudeCodeClientError(
                    f"Claude Code stdin closed: {e}"
                ) from e

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.debug("Claude Code non-JSON stdout: %r", line[:200])
                    continue
                await self._handle_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Claude Code stdout reader crashed")
        finally:
            # Fail any in-flight turn so callers don't hang.
            if self._turn_future is not None and not self._turn_future.done():
                self._turn_future.set_exception(
                    ClaudeCodeClientError("Claude Code subprocess exited")
                )

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("Claude Code stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Claude Code stderr reader crashed")

    async def _handle_event(self, msg: dict) -> None:
        """Route one stream-json line.

        We materialize three things:
          · text deltas (for live UI rendering via on_event)
          · the eventual turn-final `result` event (resolves turn_future)
          · system/init's session_id (verified once at start)
        Everything else accumulates into the per-turn buffer for the
        end-of-turn audit and is forwarded raw to on_event."""
        msg_type = msg.get("type")

        if msg_type == "system":
            subtype = msg.get("subtype")
            if subtype == "init":
                sid = msg.get("session_id")
                if isinstance(sid, str) and sid:
                    if self._session_id is None:
                        self._session_id = sid
                    elif sid != self._session_id:
                        logger.warning(
                            "Claude Code init session_id %s != requested %s",
                            sid[:8],
                            (self._session_id or "?")[:8],
                        )
                        self._session_id = sid
                return

        # Accumulate raw for end-of-turn audit
        self._current_turn_events.append(msg)
        # Forward raw to live handler if registered (for streaming UI)
        if self._on_event is not None:
            try:
                await self._on_event(msg)
            except Exception:
                logger.exception("Claude Code on_event handler raised")

        if msg_type == "stream_event":
            ev = msg.get("event") or {}
            ev_type = ev.get("type")
            if ev_type == "content_block_delta":
                delta = ev.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        self._current_turn_text.append(text)
                elif dt == "thinking_delta":
                    text = str(delta.get("thinking") or "")
                    if text:
                        self._current_turn_reasoning.append(text)
            elif ev_type == "content_block_start":
                cb = ev.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    self._current_turn_tool_uses += 1
            return

        if msg_type == "result":
            # Terminal event for this turn.
            if self._turn_future is not None and not self._turn_future.done():
                self._turn_future.set_result(msg)
            return

    # ────────────────────────────────────────────────────────────────────
    # Internals — termination
    # ────────────────────────────────────────────────────────────────────

    async def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                if proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
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
