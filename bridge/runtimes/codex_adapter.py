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
        self._mcp_config = mcp_config
        # The thread to pick up when the next Codex process starts: the
        # one the session last ran on (saved by the app), then whichever
        # thread this adapter itself last held. Every spawn used to open a
        # new thread, so a retired process (the silence watchdog) or an app
        # restart left the agent with no memory of the session.
        self._resume_thread_id: Optional[str] = resume_harness_session_id or None
        # The model and effort the session asks for (prefer_model). Applied
        # when Codex offers that model; otherwise Codex keeps its own.
        self._wanted: tuple[Optional[str], Optional[str]] = (None, None)
        # The (model, effort) last reported to the app.
        self._reported: tuple[Optional[str], Optional[str]] = (None, None)

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def harness_session_id(self) -> Optional[str]:
        return self._client.thread_id if self._client is not None else None

    @property
    def model(self) -> Optional[str]:
        return self._client.model if self._client is not None else None

    @property
    def reasoning_effort(self) -> Optional[str]:
        return self._client.reasoning_effort if self._client is not None else None

    def prefer_model(self, model: Optional[str], reasoning_level: Optional[str]) -> None:
        """The session's model and reasoning level. Codex runs only its own
        models: one it lists is passed on the next turn, with the level when
        that model takes it; any other leaves Codex on what it runs now."""
        self._wanted = (model or None, reasoning_level or None)

    def _report_model(self, *, resumed: bool = False) -> None:
        """Tell the app what Codex runs, when that changed. The app showed
        the session's own model, which Codex never got: a session set to
        Claude Opus 5.5 ran GPT-6 Astra for ten turns."""
        client = self._client
        if client is None or not client.model:
            return
        now = (client.model, client.reasoning_effort)
        if now == self._reported:
            return
        self._reported = now
        effort = f" ({client.reasoning_effort})" if client.reasoning_effort else ""
        self._emit(
            {
                "type": "system_event",
                "sessionId": self._session_id,
                "subtype": "harness_model",
                "message": f"{self._spec.label} runs {client.model}{effort}",
                "details": {
                    "runtime": self._runtime_id,
                    "model": client.model,
                    "reasoningEffort": client.reasoning_effort,
                    "harnessSessionId": client.thread_id,
                    "resumed": resumed,
                },
            }
        )

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
            # Pick the session's thread up again; a new one only when
            # there is none or Codex cannot resume it (the bridge then
            # says so with harness_session_recreated).
            await self._client.ensure_thread(resume_thread_id=self._resume_thread_id)
            self._resume_thread_id = self._client.thread_id
            self._report_model(resumed=self._client.resumed)
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
        override_model, override_effort = await self._overrides()

        async def on_event(msg: dict) -> None:
            try:
                method = str(msg.get("method") or "")
                params = msg.get("params") or {}
                item = params.get("item") or {}
                if method == "model/rerouted":
                    # The client takes the new model after this handler.
                    to_model = params.get("toModel")
                    if isinstance(to_model, str) and to_model and self._client is not None:
                        self._client.model = to_model
                        self._report_model()
                    return
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
                        tid = str(item.get("id") or "")
                        # Build the arguments the renderer renders inside
                        # the tool-call card so the operator sees the
                        # actual command instead of just "exec_command".
                        args: dict = {}
                        if t == "commandExecution":
                            args = {
                                "command": item.get("command") or "",
                                "cwd": item.get("cwd") or "",
                            }
                        elif t == "fileChange":
                            args = {
                                "changes": [
                                    {
                                        "kind": (c.get("kind") or {}).get("type") or "update",
                                        "path": c.get("path") or "",
                                    }
                                    for c in (item.get("changes") or [])
                                ],
                            }
                        elif t == "mcpToolCall":
                            args = {
                                "server": item.get("server") or "",
                                "tool": item.get("tool") or "",
                                "arguments": item.get("arguments") or {},
                            }
                        elif t == "dynamicToolCall":
                            args = item.get("arguments") or {}
                        self._emit(
                            {
                                "type": "tool_use_start",
                                "sessionId": self._session_id,
                                "id": tid,
                                "name": tool_name,
                            }
                        )
                        if args:
                            self._emit(
                                {
                                    "type": "tool_input_end",
                                    "sessionId": self._session_id,
                                    "id": tid,
                                    "arguments": args,
                                }
                            )
                    return
                # Item completions: codex sends one terminal item/completed
                # per item carrying the final payload. We project four
                # shapes — agentMessage / reasoning are text events for
                # the streaming card; commandExecution / fileChange /
                # mcpToolCall / dynamicToolCall close out the tool cards
                # with the actual output (preview + exit status).
                elif method == "item/completed":
                    t = str(item.get("type") or "")
                    if t == "agentMessage":
                        text = str(item.get("text") or "")
                        if text:
                            self._emit(
                                {
                                    "type": "text_delta",
                                    "sessionId": self._session_id,
                                    "text": text,
                                }
                            )
                    elif t == "reasoning":
                        bits: list[str] = []
                        for k in ("summary", "content"):
                            arr = item.get(k) or []
                            if isinstance(arr, list):
                                for entry in arr:
                                    if isinstance(entry, str):
                                        bits.append(entry)
                                    elif isinstance(entry, dict):
                                        tt = entry.get("text") or entry.get("content")
                                        if isinstance(tt, str):
                                            bits.append(tt)
                        if bits:
                            self._emit(
                                {
                                    "type": "thinking_delta",
                                    "sessionId": self._session_id,
                                    "thinking": "\n".join(bits),
                                }
                            )
                    elif t in {
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "dynamicToolCall",
                    }:
                        tid = str(item.get("id") or "")
                        preview = ""
                        is_error = False
                        if t == "commandExecution":
                            preview = str(item.get("aggregatedOutput") or "")
                            exit_code = item.get("exitCode")
                            if exit_code is not None and exit_code != 0:
                                preview = f"[exit {exit_code}]\n{preview}"
                                is_error = True
                        elif t == "fileChange":
                            status = item.get("status") or "unknown"
                            n_changes = len(item.get("changes") or [])
                            preview = f"apply_patch status={status}, {n_changes} change(s)"
                            is_error = status not in {"applied", "completed", "success"}
                        elif t in {"mcpToolCall", "dynamicToolCall"}:
                            result = item.get("result")
                            err = item.get("error")
                            if err:
                                preview = f"[error] {err}"
                                is_error = True
                            elif result is not None:
                                try:
                                    import json as _json
                                    preview = _json.dumps(result, ensure_ascii=False)[:4000]
                                except Exception:
                                    preview = str(result)[:4000]
                            else:
                                preview = ""
                        self._emit(
                            {
                                "type": "tool_result",
                                "sessionId": self._session_id,
                                "id": tid,
                                "preview": preview[:4000],
                                "isError": is_error,
                            }
                        )
            except Exception:
                logger.exception("Codex on_event projection failed")

        try:
            result = await self._client.prompt(
                user_text,
                model=override_model,
                effort=override_effort,
                on_event=on_event,
            )
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

        self._report_model()
        if result.should_retire:
            await self.close()
        return result

    async def _overrides(self) -> tuple[Optional[str], Optional[str]]:
        """The model and effort to send with the next turn, if they differ
        from what the thread runs now."""
        client = self._client
        want_model, want_level = self._wanted
        if client is None or not want_model:
            return None, None
        models = await client.list_models()
        if want_model not in models:
            return None, None
        model = want_model if want_model != client.model else None
        effort = (
            want_level
            if want_level and want_level in models[want_model] and want_level != client.reasoning_effort
            else None
        )
        return model, effort

    async def cancel(self) -> None:
        if self._client is None:
            return
        await self._client.cancel()

    async def close(self) -> None:
        if self._client is None:
            return
        # The next process picks this thread up again.
        if self._client.thread_id:
            self._resume_thread_id = self._client.thread_id
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
