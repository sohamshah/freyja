"""Bridges ClaudeCodeClient to Freyja's per-session event bus.

One adapter per Freyja session. Projects Claude Code stream-json events
into Freyja's existing event types (text_delta, thinking_delta,
tool_use_start) so the activity dashboard renders Claude Code turns
identically to native turns.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from bridge.runtimes.claude_code_client import (
    ClaudeCodeClient,
    ClaudeCodeClientError,
    ClaudeCodeTurnResult,
)
from bridge.runtimes.registry import RuntimeSpec, get_runtime

logger = logging.getLogger(__name__)

EmitFn = Callable[[dict], None]


class ClaudeCodeAdapter:
    """One Claude Code subprocess per Freyja session, lifetime owned by
    the calling _BridgeSession."""

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
        self._client: Optional[ClaudeCodeClient] = None
        self._resume_harness_session_id = resume_harness_session_id

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def harness_session_id(self) -> Optional[str]:
        return self._client.session_id if self._client is not None else None

    async def ensure_started(
        self,
        *,
        mcp_servers: Optional[list[dict]] = None,  # ignored — Claude Code uses --mcp-config not session-level
    ) -> None:
        if self._client is not None and self._client.is_alive:
            return
        # Tear down a dead client first so we don't end up holding two.
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._client = ClaudeCodeClient(
            command=self._spec.resolved_command(),
            args=tuple(self._spec.args),
            cwd=self._workspace,
            resume_session_id=self._resume_harness_session_id,
        )
        try:
            await self._client.start()
        except ClaudeCodeClientError as exc:
            # Bubble up with a Freyja-friendly error event.
            self._emit(
                {
                    "type": "error",
                    "sessionId": self._session_id,
                    "message": f"{self._spec.label}: {exc}",
                    "recoverable": True,
                }
            )
            self._client = None
            raise

    async def run_turn(
        self,
        user_text: str,
        *,
        mcp_servers: Optional[list[dict]] = None,
        turn_id: str = "",
    ) -> ClaudeCodeTurnResult:
        await self.ensure_started(mcp_servers=mcp_servers)
        assert self._client is not None

        async def on_event(msg: dict) -> None:
            try:
                msg_type = msg.get("type")
                if msg_type == "stream_event":
                    ev = msg.get("event") or {}
                    et = ev.get("type")
                    if et == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                self._emit(
                                    {
                                        "type": "text_delta",
                                        "sessionId": self._session_id,
                                        "text": text,
                                    }
                                )
                        elif delta.get("type") == "thinking_delta":
                            think = str(delta.get("thinking") or "")
                            if think:
                                self._emit(
                                    {
                                        "type": "thinking_delta",
                                        "sessionId": self._session_id,
                                        "thinking": think,
                                    }
                                )
                    elif et == "content_block_start":
                        cb = ev.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            self._emit(
                                {
                                    "type": "tool_use_start",
                                    "sessionId": self._session_id,
                                    "id": cb.get("id") or "",
                                    "name": cb.get("name") or "",
                                }
                            )
            except Exception:
                logger.exception("Claude Code on_event projection failed")

        try:
            result = await self._client.prompt(user_text, on_event=on_event)
        except ClaudeCodeClientError as exc:
            self._emit(
                {
                    "type": "error",
                    "sessionId": self._session_id,
                    "message": f"{self._spec.label} turn failed: {exc}",
                    "recoverable": True,
                }
            )
            await self.close()
            return ClaudeCodeTurnResult(
                stop_reason="error",
                is_error=True,
                error=str(exc),
                should_retire=True,
            )

        if result.should_retire:
            await self.close()
        return result

    async def cancel(self) -> None:
        if self._client is None:
            return
        await self._client.cancel()

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        finally:
            self._client = None
