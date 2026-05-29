"""Bridge between Freyja's per-session run_turn and the ACPClient.

The adapter owns one ACPClient per Freyja session. It translates
`session/update` notifications into Freyja's existing event stream
(text_delta, thinking_delta) so the activity dashboard renders harness
turns the same way it renders native turns.

A session/prompt call is one logical Freyja turn. Inside it, the harness
runs its OWN agent loop — multiple model calls, multiple tool calls — and
we project the final text + reasoning back. Tool calls the harness makes
via MCP (when we expose Freyja tools via the MCP bridge in Stage 2) come
out as session/update notifications too; for now we just stream text.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bridge.runtimes.acp_client import ACPClient, ACPClientError, ACPTurnResult
from bridge.runtimes.registry import RuntimeSpec, get_runtime

logger = logging.getLogger(__name__)


EmitFn = Callable[[dict], None]


class ACPHarnessAdapter:
    """One-runtime-per-Freyja-session bridge to the ACP transport.

    Lazy: the ACPClient is built on first run_turn so a brand-new
    `claude_code_acp` session doesn't spawn `claude` until the user
    actually sends something. Lets the operator browse / configure
    without paying the subprocess cost."""

    def __init__(
        self,
        *,
        runtime_id: str,
        session_id: str,
        workspace: str,
        emit: EmitFn,
        resume_harness_session_id: Optional[str] = None,
    ) -> None:
        self._runtime_id = runtime_id
        self._spec: RuntimeSpec = get_runtime(runtime_id)
        self._session_id = session_id
        self._workspace = workspace
        self._emit = emit
        self._client: Optional[ACPClient] = None
        # Resume id from a prior incarnation; tried on first ensure_session.
        self._resume_harness_session_id = resume_harness_session_id
        # Latest harness sessionId we've observed — updated whenever the
        # client allocates a new one. Persistence layer reads this.
        self._current_harness_session_id: Optional[str] = resume_harness_session_id

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def harness_session_id(self) -> Optional[str]:
        return self._current_harness_session_id

    async def ensure_started(
        self,
        *,
        mcp_servers: Optional[list[dict]] = None,
    ) -> None:
        """Spawn the subprocess + initialize + session/new on first use."""
        if self._client is None:
            self._client = ACPClient(
                command=self._spec.resolved_command(),
                args=tuple(self._spec.args),
                cwd=self._workspace,
                server_request_handler=self._handle_server_request,
                client_info={
                    "name": "freyja",
                    "title": "Freyja",
                    "version": "0.0.0",
                },
            )
        if self._client.session_id is not None:
            return
        await self._client.start()

        loaded = False
        if self._resume_harness_session_id:
            loaded = await self._client.load_session(
                self._resume_harness_session_id
            )
            if loaded:
                self._current_harness_session_id = self._client.session_id
                logger.info(
                    "ACP session/load resumed %s for runtime=%s",
                    (self._client.session_id or "")[:8],
                    self._runtime_id,
                )

        if not loaded:
            sid = await self._client.new_session(mcp_servers=mcp_servers)
            self._current_harness_session_id = sid

    async def run_turn(
        self,
        user_text: str,
        *,
        mcp_servers: Optional[list[dict]] = None,
        turn_id: str = "",
    ) -> ACPTurnResult:
        """Run a single user turn through the harness. Streams text_delta
        events to the renderer as `agent_message_chunk` updates arrive."""
        await self.ensure_started(mcp_servers=mcp_servers)
        assert self._client is not None

        async def on_update(notification: dict) -> None:
            try:
                update = (notification.get("params") or {}).get("update") or {}
                kind = str(update.get("sessionUpdate") or "")
                content = update.get("content") or {}
                chunk_text = ""
                if isinstance(content, dict):
                    chunk_text = str(content.get("text") or "")
                if kind == "agent_message_chunk" and chunk_text:
                    self._emit(
                        {
                            "type": "text_delta",
                            "sessionId": self._session_id,
                            "text": chunk_text,
                        }
                    )
                elif kind == "agent_thought_chunk" and chunk_text:
                    self._emit(
                        {
                            "type": "thinking_delta",
                            "sessionId": self._session_id,
                            "thinking": chunk_text,
                        }
                    )
                # Other update kinds (tool_call, plan, etc.) are
                # captured in the turn result and surfaced via the
                # bridge's normal event channel in Stage 2.
            except Exception:
                logger.exception("ACP on_update projection failed")

        try:
            result = await self._client.prompt(user_text, on_update=on_update)
        except ACPClientError as exc:
            # Surface as a non-fatal turn error and mark for respawn.
            self._emit(
                {
                    "type": "error",
                    "sessionId": self._session_id,
                    "message": f"{self._spec.label} turn failed: {exc}",
                    "recoverable": True,
                }
            )
            await self.close()
            return ACPTurnResult(
                error=str(exc), should_retire=True, stop_reason="error"
            )

        # Harness sessionId may have been allocated lazily inside prompt
        # if new_session deferred — refresh.
        self._current_harness_session_id = self._client.session_id

        if result.should_retire:
            await self.close()
        return result

    async def cancel(self) -> None:
        if self._client is None:
            return
        await self._client.cancel_active_prompt()

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        finally:
            self._client = None

    # ────────────────────────────────────────────────────────────────────
    # Server-initiated requests from the harness (fs/* + permissions)
    # ────────────────────────────────────────────────────────────────────

    async def _handle_server_request(
        self, method: str, params: dict
    ) -> dict:
        """Translate ACP server-initiated requests into Freyja's
        permission/file flow.

        Stage 1: implement fs/read_text_file + fs/write_text_file with
        workspace-scoped path validation; decline everything else (the
        harness will fall back to its own internal handling). Permission
        bridging into Freyja's modal lands in Stage 2 alongside the MCP
        tool callback surface."""
        if method == "fs/read_text_file":
            return await self._fs_read(params)
        if method == "fs/write_text_file":
            return await self._fs_write(params)
        # session/request_permission / fs/* extensions land in Stage 2.
        raise RuntimeError(f"unhandled ACP server request: {method}")

    def _resolve_workspace_path(self, raw: str) -> Optional[Path]:
        """Reject paths outside the session workspace. Symlinks are
        resolved so a `../../etc/passwd` attempt is caught here, not
        after the read."""
        if not raw:
            return None
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path(self._workspace) / p
            resolved = p.resolve(strict=False)
            workspace = Path(self._workspace).expanduser().resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError:
                return None
            return resolved
        except Exception:
            return None

    async def _fs_read(self, params: dict) -> dict:
        raw_path = str(params.get("path") or "")
        path = self._resolve_workspace_path(raw_path)
        if path is None:
            raise RuntimeError(f"path outside workspace: {raw_path}")
        if not path.exists():
            raise RuntimeError(f"file not found: {raw_path}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise RuntimeError(f"file is not UTF-8 text: {raw_path}") from e
        return {"content": text}

    async def _fs_write(self, params: dict) -> dict:
        raw_path = str(params.get("path") or "")
        content = str(params.get("content") or "")
        path = self._resolve_workspace_path(raw_path)
        if path is None:
            raise RuntimeError(f"path outside workspace: {raw_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {}
