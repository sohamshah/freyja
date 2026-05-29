"""Bridges CodexClient to Freyja's per-session event bus.

One adapter per Freyja session. Projects Codex item/completed events
and item/<type>/delta notifications into Freyja's standard event types
so the activity dashboard renders Codex turns identically to native.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from bridge.runtimes.codex_client import (
    CodexClient,
    CodexClientError,
    CodexTurnResult,
)
from bridge.runtimes.registry import RuntimeSpec, get_runtime

logger = logging.getLogger(__name__)

EmitFn = Callable[[dict], None]


class CodexAdapter:
    """One `codex app-server` subprocess + thread per Freyja session."""

    def __init__(
        self,
        *,
        runtime_id: str,
        session_id: str,
        workspace: str,
        emit: EmitFn,
        resume_harness_session_id: Optional[str] = None,
        mcp_config: Optional[dict] = None,
    ) -> None:
        self._runtime_id = runtime_id
        self._spec: RuntimeSpec = get_runtime(runtime_id)
        self._session_id = session_id
        self._workspace = workspace
        self._emit = emit
        self._client: Optional[CodexClient] = None
        self._resume_harness_session_id = resume_harness_session_id
        self._mcp_config = mcp_config

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def harness_session_id(self) -> Optional[str]:
        return self._client.thread_id if self._client is not None else None

    async def ensure_started(
        self,
        *,
        mcp_servers: Optional[list[dict]] = None,  # Codex picks up from ~/.codex/config.toml — Stage 2 wires the bridge
    ) -> None:
        if self._client is not None and self._client.is_alive:
            return
        if self._client is not None:
            await self._client.close()
            self._client = None
        # Codex's `app-server` subcommand accepts `-c key=value` overrides
        # for ~/.codex/config.toml. We use this to register the Freyja
        # MCP server at spawn-time so the global config file stays
        # untouched (no cross-session clobbering, idempotent across runs).
        extra_args: list[str] = []
        if self._mcp_config is not None:
            cmd = self._mcp_config.get("command") or ""
            args_list = list(self._mcp_config.get("args") or [])
            env_dict = dict(self._mcp_config.get("env") or {})
            extra_args.extend(
                [
                    "-c",
                    f'mcp_servers.freyja.command="{cmd}"',
                    "-c",
                    f"mcp_servers.freyja.args={json.dumps(args_list)}",
                ]
            )
            if env_dict:
                env_pairs = [
                    f'"{k}"="{v}"'
                    for k, v in env_dict.items()
                ]
                extra_args.extend(
                    [
                        "-c",
                        f"mcp_servers.freyja.env={{ {', '.join(env_pairs)} }}",
                    ]
                )
        self._client = CodexClient(
            command=self._spec.resolved_command(),
            args=tuple(self._spec.args) + tuple(extra_args),
            cwd=self._workspace,
            server_request_handler=self._handle_server_request,
        )
        try:
            await self._client.start()
            # Try to resume the prior thread; fall back to new.
            if self._resume_harness_session_id:
                resumed = await self._client.resume_thread(
                    self._resume_harness_session_id
                )
                if not resumed:
                    await self._client.ensure_thread()
            else:
                await self._client.ensure_thread()
        except CodexClientError as exc:
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
    ) -> CodexTurnResult:
        await self.ensure_started(mcp_servers=mcp_servers)
        assert self._client is not None

        async def on_event(msg: dict) -> None:
            try:
                method = str(msg.get("method") or "")
                params = msg.get("params") or {}
                item = params.get("item") or {}
                # Stream agent text + reasoning via delta events for live UI
                if method.endswith("/delta"):
                    delta_text = (
                        item.get("text")
                        or item.get("textDelta")
                        or (item.get("delta") or {}).get("text")
                    )
                    if "/agentMessage/" in method and isinstance(delta_text, str):
                        self._emit(
                            {
                                "type": "text_delta",
                                "sessionId": self._session_id,
                                "text": delta_text,
                            }
                        )
                    elif "/reasoning/" in method and isinstance(delta_text, str):
                        self._emit(
                            {
                                "type": "thinking_delta",
                                "sessionId": self._session_id,
                                "thinking": delta_text,
                            }
                        )
                # Tool starts: emit on item/started for tool-shaped items
                elif method == "item/started":
                    t = str(item.get("type") or "")
                    if t in {
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "dynamicToolCall",
                    }:
                        tool_name = {
                            "commandExecution": "exec_command",
                            "fileChange": "apply_patch",
                            "mcpToolCall": (
                                f"mcp.{item.get('server') or 'mcp'}."
                                f"{item.get('tool') or 'unknown'}"
                            ),
                            "dynamicToolCall": str(item.get("tool") or "tool"),
                        }[t]
                        self._emit(
                            {
                                "type": "tool_use_start",
                                "sessionId": self._session_id,
                                "id": str(item.get("id") or ""),
                                "name": tool_name,
                            }
                        )
            except Exception:
                logger.exception("Codex on_event projection failed")

        try:
            result = await self._client.prompt(user_text, on_event=on_event)
        except CodexClientError as exc:
            self._emit(
                {
                    "type": "error",
                    "sessionId": self._session_id,
                    "message": f"{self._spec.label} turn failed: {exc}",
                    "recoverable": True,
                }
            )
            await self.close()
            return CodexTurnResult(
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

    # ────────────────────────────────────────────────────────────────────
    # Server-initiated requests from Codex (approvals + MCP elicitations)
    # ────────────────────────────────────────────────────────────────────

    async def _handle_server_request(
        self, method: str, params: dict
    ) -> dict:
        """Stage 1: auto-accept exec + file approvals (the operator
        already picked Codex as the runtime for this session, so the
        approval modal would be friction). Permission-mode changes and
        unknown MCP elicitations decline cleanly. Real Freyja approval
        modal bridging lands in Stage 2 alongside the MCP server."""
        if method == "item/commandExecution/requestApproval":
            return {"decision": "acceptForSession"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "acceptForSession"}
        if method == "item/permissions/requestApproval":
            return {"decision": "decline"}
        if method == "mcpServer/elicitation/request":
            server_name = str(params.get("serverName") or "")
            if server_name == "freyja":  # our future MCP bridge
                return {"action": "accept", "content": None, "_meta": None}
            return {"action": "decline", "content": None, "_meta": None}
        raise RuntimeError(f"unhandled Codex server request: {method}")
