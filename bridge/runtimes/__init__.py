"""Harness runtime adapters — drive external CLI agents from Freyja.

Two runtimes ship today:
  * `claude_code`       → real `claude` CLI via its stream-json protocol
  * `codex_app_server`  → real `codex` CLI via its app-server JSON-RPC

Each runtime gets its own *_client.py (wire protocol) + *_adapter.py
(bridges client to Freyja's per-session event bus). The dispatcher
below picks the right adapter from a runtime id string.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def build_adapter(
    *,
    runtime_id: str,
    session_id: str,
    workspace: str,
    emit: Callable[[dict], None],
    resume_harness_session_id: Optional[str] = None,
    mcp_config: Optional[dict] = None,
) -> Any:
    """Construct a harness adapter for the given runtime id.

    `mcp_config` (when set) tells the adapter to register a Freyja MCP
    server alongside the harness so the harness can call Freyja's tools.
    Shape: {command, args, env}.

    Imports are local so a missing optional dep doesn't break the whole
    bridge module on startup."""
    if runtime_id == "claude_code":
        from bridge.runtimes.claude_code_adapter import ClaudeCodeAdapter

        return ClaudeCodeAdapter(
            runtime_id=runtime_id,
            session_id=session_id,
            workspace=workspace,
            emit=emit,
            resume_harness_session_id=resume_harness_session_id,
            mcp_config=mcp_config,
        )
    if runtime_id == "codex_app_server":
        from bridge.runtimes.codex_adapter import CodexAdapter

        # `resume_harness_session_id` is intentionally dropped on this
        # path: Codex has no working thread-resume RPC, so the adapter
        # always opens a fresh thread on spawn. The bridge emits a
        # `harness_session_recreated` event when this loses continuity
        # mid-session so the operator can see what happened.
        return CodexAdapter(
            runtime_id=runtime_id,
            session_id=session_id,
            workspace=workspace,
            emit=emit,
            mcp_config=mcp_config,
        )
    raise ValueError(f"unknown harness runtime: {runtime_id!r}")
