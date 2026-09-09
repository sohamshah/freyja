"""Tool-protocol wrapper around one remote MCP tool (design doc 1.4/1.5).

Naming: ``mcp__<server>__<tool>`` with each component sanitized to
``[a-z0-9_]`` and the whole name capped at 64 chars (provider limit) via
truncation + a stable 6-char hash suffix. The ORIGINAL remote tool name
is kept on the proxy for dispatch, so sanitization never breaks calls.

Permissions: requires_permission=True with the per-server trust mapping
(trusted -> no prompt, standard -> MEDIUM, untrusted -> HIGH) and
per-tool overrides via ``tools.permissions.<remote_name>``.

Result cap: text content is hard-capped at ``limits.max_result_chars``
(default 100k) BEFORE any budget layer sees it, with a truncation notice
appended (design: pathological results must never reach the context).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from bridge.tools.base import (
    ImageBlock,
    PermissionLevel,
    PermissionRequest,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolTier,
)

if TYPE_CHECKING:  # pragma: no cover
    from bridge.mcp.config import McpServerSpec
    from bridge.mcp.manager import McpManager

logger = logging.getLogger(__name__)

MAX_TOOL_NAME_LEN = 64
SUMMARY_MAX_LEN = 140

# trust -> PermissionLevel; None means "no prompt" (design doc 1.5 table).
TRUST_TO_LEVEL: dict[str, PermissionLevel | None] = {
    "trusted": None,
    "standard": PermissionLevel.MEDIUM,
    "untrusted": PermissionLevel.HIGH,
}

# per-tool override strings (tools.permissions.<name>) -> level
_OVERRIDE_TO_LEVEL: dict[str, PermissionLevel | None] = {
    "none": None,
    "low": PermissionLevel.LOW,
    "medium": PermissionLevel.MEDIUM,
    "high": PermissionLevel.HIGH,
}

_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def sanitize_component(component: str) -> str:
    """Lowercase and squash anything outside [a-z0-9_] to underscores."""
    out = _SANITIZE_RE.sub("_", component.lower()).strip("_")
    return out or "x"


def proxy_tool_name(server: str, remote_name: str) -> str:
    """``mcp__<server>__<tool>``, sanitized, 64-char capped with a stable
    6-char hash suffix on truncation (hash of the UNtruncated name so the
    result is deterministic across restarts)."""
    full = f"mcp__{sanitize_component(server)}__{sanitize_component(remote_name)}"
    if len(full) <= MAX_TOOL_NAME_LEN:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:6]
    return full[: MAX_TOOL_NAME_LEN - 7] + "_" + digest


def first_sentence(text: str) -> str:
    """First sentence of a description: up to the first period followed by
    whitespace/EOL, or the first newline, whichever comes first."""
    text = (text or "").strip()
    if not text:
        return ""
    newline_idx = text.find("\n")
    if newline_idx != -1:
        text = text[:newline_idx].strip()
    match = re.search(r"\.(?:\s|$)", text)
    if match:
        text = text[: match.start() + 1]
    return text.strip()


def build_summary(server: str, description: str) -> str:
    """``[server] `` + first sentence, hard-capped at 140 chars. Callers
    must run the injection scan BEFORE this (manager.py does)."""
    summary = f"[{server}] {first_sentence(description)}".strip()
    if len(summary) > SUMMARY_MAX_LEN:
        summary = summary[: SUMMARY_MAX_LEN - 1].rstrip() + "…"
    return summary


def resolve_permission_level(
    spec: "McpServerSpec", remote_name: str
) -> PermissionLevel | None:
    """Per-tool override wins over the server trust mapping."""
    override = spec.tool_permissions.get(remote_name)
    if override is not None:
        key = override.strip().lower()
        if key in _OVERRIDE_TO_LEVEL:
            return _OVERRIDE_TO_LEVEL[key]
        logger.warning(
            "mcp '%s': unknown tools.permissions.%s value %r; using trust default",
            spec.name, remote_name, override,
        )
    return TRUST_TO_LEVEL.get(spec.trust, PermissionLevel.MEDIUM)


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fingerprint secret-shaped argument values in permission prompts."""
    from bridge.mcp.config import is_secret_key

    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and is_secret_key(key) and len(value) > 10:
            out[key] = f"{value[:4]}…{value[-4:]}"
        else:
            out[key] = value
    return out


def to_tool_result(
    call_id: str, mcp_result: Any, *, max_chars: int | None = None
) -> ToolResult:
    """Map an MCP CallToolResult content array to a Freyja ToolResult
    (inverse of _tool_result_to_mcp_content in freyja_bridge.py).

    Text blocks concatenate; image blocks map to engine ImageBlock (the
    codebase's ToolResult natively supports list[TextBlock|ImageBlock]);
    isError maps to an error result. ``max_chars`` (None = no cap) hard-
    caps the text before anything downstream sees it.
    """
    from bridge.mcp.connection import truncate_result_text

    is_error = bool(getattr(mcp_result, "is_error", False))
    texts: list[str] = []
    blocks: list[Any] = []
    has_image = False
    for item in getattr(mcp_result, "content", None) or []:
        btype = getattr(item, "type", None)
        if btype == "text":
            text = getattr(item, "text", "") or ""
            texts.append(text)
            blocks.append(TextBlock(text=text))
        elif btype == "image":
            has_image = True
            blocks.append(
                ImageBlock(
                    data=str(getattr(item, "data", "") or ""),
                    media_type=str(getattr(item, "mime_type", None) or "image/png"),
                )
            )
        else:
            # Unknown content type — render as JSON text so nothing is
            # silently dropped.
            try:
                dumped = item.model_dump(mode="json")  # pydantic model
            except Exception:  # noqa: BLE001
                dumped = repr(item)
            text = json.dumps(dumped) if not isinstance(dumped, str) else dumped
            texts.append(text)
            blocks.append(TextBlock(text=text))
    if has_image and not is_error:
        if max_chars is not None:
            blocks = [
                TextBlock(text=truncate_result_text(b.text, max_chars))
                if isinstance(b, TextBlock) else b
                for b in blocks
            ]
        return ToolResult(call_id=call_id, content=blocks, is_error=False)
    text = "\n".join(texts) if texts else ("" if not is_error else "MCP tool reported an error")
    if max_chars is not None and len(text) > max_chars:
        logger.warning(
            "mcp: tool result of %d chars exceeds limits.max_result_chars=%d; truncated",
            len(text), max_chars,
        )
        text = truncate_result_text(text, max_chars)
    return ToolResult(call_id=call_id, content=text, is_error=is_error)


class McpProxyTool:
    """Registry-facing proxy for one remote MCP tool."""

    requires_permission = True

    def __init__(
        self,
        *,
        manager: "McpManager",
        spec: "McpServerSpec",
        remote_name: str,
        description: str,
        input_schema: dict[str, Any] | None,
        summary: str,
    ) -> None:
        self._manager = manager
        self.spec = spec
        self.server = spec.name
        self.remote_name = remote_name
        """Original remote tool name, used verbatim for dispatch."""
        self.proxy_name = proxy_tool_name(spec.name, remote_name)
        self._description = description
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._summary = summary

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.proxy_name,
            description=self._description,
            summary=self._summary,
            parameters=self._input_schema,
            tier=ToolTier(self.spec.tier),
            strict=False,
        )

    def update_remote(
        self,
        *,
        description: str,
        input_schema: dict[str, Any] | None,
        summary: str,
    ) -> None:
        """Refresh description/schema in place after a tools/list_changed
        diff so registry entries (and any live tool-call ids pointing at
        this proxy) keep working without unregister/register churn."""
        self._description = description
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._summary = summary

    async def permission_prompt(
        self, arguments: dict[str, Any]
    ) -> PermissionRequest | None:
        level = resolve_permission_level(self.spec, self.remote_name)
        if level is None:
            return None
        try:
            details = json.dumps(_redact_arguments(arguments or {}), default=str)[:2000]
        except Exception:  # noqa: BLE001
            details = ""
        return PermissionRequest(
            prompt=(
                f"Call MCP tool '{self.remote_name}' on server '{self.server}'"
            ),
            level=level,
            details=details,
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        from bridge.mcp.connection import McpUnavailableError, State

        conn = self._manager.connection(self.server)
        if conn is None:
            return ToolResult(
                call_id=call_id,
                content=f"Error: MCP server '{self.server}' is not configured",
                is_error=True,
            )
        if conn.state is not State.ACTIVE:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: MCP server '{self.server}' is {conn.state.value}"
                    + (f": {conn.reason}" if conn.reason else "")
                ),
                is_error=True,
            )
        try:
            result = await conn.call_tool(
                self.remote_name,
                arguments or {},
                timeout=self.spec.call_timeout_s,
            )
        except McpUnavailableError as exc:
            return ToolResult(call_id=call_id, content=f"Error: {exc}", is_error=True)
        except (asyncio.TimeoutError, TimeoutError):
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: MCP tool '{self.remote_name}' timed out after "
                    f"{self.spec.call_timeout_s}s"
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — includes SDK McpError timeouts
            text = str(exc) or type(exc).__name__
            if "timed out" in text.lower() or "timeout" in text.lower():
                text = (
                    f"MCP tool '{self.remote_name}' timed out after "
                    f"{self.spec.call_timeout_s}s ({text})"
                )
            return ToolResult(call_id=call_id, content=f"Error: {text}", is_error=True)
        return to_tool_result(call_id, result, max_chars=self.spec.max_result_chars)
