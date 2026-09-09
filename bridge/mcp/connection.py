"""One MCP server connection (stdio / streamable HTTP / SSE) with the
v1 lifecycle state machine plus the v2 hardening.

Adapted from hermes-agent (Copyright (c) 2025 Nous Research, MIT) — the
ported portions are: the SDK httpx-module resolution, streamable-HTTP tuple-shape shim, the
initialize-first / discover-fallback protocol negotiation, the session-
proven rapid-drop budget, in-flight fast-fail, the ping -> tools/list
keepalive latch, the listChanged message handler + diff refresh, and the
hard result cap.

Lifecycle design (design doc 1.3 / 4.1-4.2): the SDK's transport clients
and ``ClientSession`` are async context managers built on anyio task
groups, whose cancel scopes are BOUND TO THE TASK that entered them. So
each connection runs a single dedicated supervisor task (``_run``) that
owns enter AND exit of every context manager; no other task ever touches
the contexts. Other tasks interact only through ``call_tool()`` (safe:
the session dispatches over in-memory streams) and the ``stop()`` signal.

State machine (design 4.1)::

    connecting -> active | needs-auth | failed | backoff
    active -> degraded -> active | backoff
    backoff -(budget exhausted)-> parked
    active -(3 unproven drops)-> parked          # rapid-drop budget (v2)
    parked -(probe ok)-> connecting -> active
    any -> disabled (operator stop)
    auth-shaped failures from anywhere -> needs-auth (no ladder)

Transient failures walk an exponential-backoff ladder with FULL jitter
(default 5 retries, 1s base, 60s cap) and then park, probing every 300s.
Permanent failures (missing secret env var, binary not found, HTTP
401/403, OAuth/NeedsAuth-shaped exceptions, invalid config) skip the
ladder entirely.

Session-proven gate (v2): reaching ACTIVE (initialize + tools/list) is
NOT proof of health — a flapping transport handshakes fine and drops a
moment later. A session is *proven* by the first successful exchange
after activation (keepalive probe, tool call, or listChanged refresh).
Only a proven session clears the retry budget; an unproven drop charges
the rapid-drop budget (default 3) and exhausting it parks the server so
a flapping transport cannot hot-loop respawns.

Transports:
  * ``stdio`` — allowlist-only spawn env (design 1.3 step 1 / section 8):
    the child gets the keys declared in the spec's ``env`` block over a
    minimal base (PATH, HOME, LANG), never the full ``os.environ``.
  * ``http`` — streamable HTTP via ``streamable_http_client`` over an
    httpx client WE own (built with the SDK's own httpx module so the
    request/response objects match), headers from ``spec.headers`` with
    ${VAR} expansion at connect time, optional ``httpx.Auth`` from the
    injected ``auth_factory``. A missing ``Mcp-Session-Id`` is valid
    (stateless server); the GET stream is never required. If the server
    rejects the legacy ``initialize`` as modern-only (-32022 / -32601)
    the session continues via ``server/discover`` (2026-07-28 stateless
    mode). If the endpoint answers ``initialize`` with HTTP 404/405 the
    connection falls back to SSE once and remembers the working
    transport for later reconnects.
  * ``sse`` — legacy HTTP+SSE via ``sse_client``.

All timers are constructor-injectable so tests run in fractions of a
second. The 5s keepalive floor from the design applies to CONFIG-sourced
values (see ``normalize_keepalive_interval``), not to programmatic
constructor arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from bridge.mcp.config import (
    DEFAULT_MAX_RESULT_CHARS,
    McpServerSpec,
    expand_env_refs,
    is_secret_key,
)
from bridge.process_env import child_env

logger = logging.getLogger(__name__)

# Minimal base env every stdio child receives (values from os.environ).
SPAWN_ENV_BASE = ("PATH", "HOME", "LANG")

# Design 4.2 defaults.
DEFAULT_KEEPALIVE_INTERVAL_S = 180.0
KEEPALIVE_FLOOR_S = 5.0
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 60.0
DEFAULT_BACKOFF_MAX_RETRIES = 5
DEFAULT_PARK_PROBE_INTERVAL_S = 300.0
DEFAULT_WATCHDOG_POLL_INTERVAL_S = 2.0
# v2 hardening defaults.
DEFAULT_RAPID_DROP_BUDGET = 3
DEFAULT_SSE_READ_TIMEOUT_S = 300.0
DEFAULT_HTTP_READ_TIMEOUT_S = 300.0
DEFAULT_ELICITATION_TIMEOUT_S = 300.0
LIST_TOOLS_MAX_PAGES = 50

# Structural JSON-RPC codes (mirrors mcp.types; kept local so the module
# stays importable without the SDK for config/unit tests).
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022
JSONRPC_CONNECTION_CLOSED = -32000
JSONRPC_INVALID_REQUEST = -32600

AUTH_STATUSES = (401, 403)
SSE_FALLBACK_STATUSES = (404, 405)

BACKOFF_JITTER_MODES = ("full", "equal")


def needs_auth_reason(server: str) -> str:
    """The single actionable needs-auth message (design 3.3)."""
    return f"authentication required — run /mcp login {server}"


def normalize_keepalive_interval(value: Any) -> float:
    """Clamp a CONFIG-sourced keepalive interval to the design floor
    (5s). Programmatic constructor args bypass this so tests can run
    sub-second timers."""
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEPALIVE_INTERVAL_S
    return max(KEEPALIVE_FLOOR_S, interval)


class State(Enum):
    """Connection lifecycle states (design doc 4.1)."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    BACKOFF = "backoff"
    PARKED = "parked"
    FAILED = "failed"
    NEEDS_AUTH = "needs-auth"


# States in which the server's tools remain registered/servable.
ACTIVE_LIKE = (State.ACTIVE, State.DEGRADED)


class McpUnavailableError(RuntimeError):
    """Raised by call_tool when the connection is not active, or when the
    transport died while the call was in flight (fast-fail)."""


class _NeedsAuth(Exception):
    """Internal: auth-shaped permanent failure (missing secret ${VAR},
    HTTP 401/403, OAuth/NeedsAuth-shaped exception). Never burns the
    retry ladder."""


class _PermanentFailure(Exception):
    """Internal: non-auth permanent failure (binary not found, invalid
    config). Skips the retry ladder, lands in FAILED."""


# ---------------------------------------------------------------------------
# Elicitation / sampling / auth injection types
# ---------------------------------------------------------------------------

@dataclass
class ElicitRequest:
    """A server's ``elicitation/create`` request, transport-agnostic, as
    handed to the injected approval handler."""

    server: str
    message: str
    mode: str = "form"
    """``form`` (structured input) or ``url`` (out-of-band browser flow)."""
    requested_schema: dict[str, Any] | None = None
    url: str | None = None
    elicitation_id: str | None = None
    raw: Any = None
    """The SDK params object, for handlers that want every field."""


ApprovalHandler = Callable[[ElicitRequest], Awaitable[Any]]
"""Returns a dict of form content to ACCEPT, ``None`` to DECLINE, or an
explicit ``{"action": "accept"|"decline"|"cancel", "content": {...}}``."""

AuthFactory = Callable[[McpServerSpec, bool], Any]
"""``(spec, interactive) -> httpx.Auth | None``. The Auth object must come
from the httpx module the installed MCP SDK uses (``httpx2`` on mcp>=2.0;
see ``sdk_httpx()``) — the SDK's own ``OAuthClientProvider`` qualifies."""

ToolsChangedHook = Callable[["McpConnection", set[str], set[str], set[str]], None]
"""``(conn, added, removed, changed)`` remote tool names after a diff
refresh (listChanged notification or schema TTL)."""


async def decline_elicitation(request: ElicitRequest) -> None:
    """Default approval handler: decline everything, loudly, so a server
    waiting on user input never blocks the bridge."""
    logger.warning(
        "mcp '%s': declining %s-mode elicitation (no approval handler wired): %.200s",
        request.server, request.mode, request.message,
    )
    return None


# ---------------------------------------------------------------------------
# SDK compat shims (adapted from hermes-agent, MIT)
# ---------------------------------------------------------------------------

_SDK_HTTPX_MOD: Any = None


def sdk_httpx() -> Any:
    """Return the httpx module the *installed* MCP SDK is built against.

    mcp 2.0 moved its HTTP transports to ``httpx2`` — a separate
    distribution with the same API, importable side by side with the
    bridge's own ``httpx``. Every object that crosses the SDK boundary
    (the ``AsyncClient`` handed to ``streamable_http_client``, the
    ``Auth`` object, the exception classes) has to come from the module
    the SDK itself imports, so resolve it from the SDK's transport module
    rather than guessing from a version number.
    """
    global _SDK_HTTPX_MOD
    if _SDK_HTTPX_MOD is not None:
        return _SDK_HTTPX_MOD
    try:
        from mcp.client import streamable_http as _transport

        _SDK_HTTPX_MOD = getattr(_transport, "httpx2", None) or getattr(_transport, "httpx", None)
    except ImportError:
        _SDK_HTTPX_MOD = None
    if _SDK_HTTPX_MOD is None:
        try:
            import httpx2 as _fallback  # type: ignore[import-not-found]
        except ImportError:
            import httpx as _fallback  # type: ignore[no-redef]
        _SDK_HTTPX_MOD = _fallback
    return _SDK_HTTPX_MOD


def unpack_transport_streams(streams: Any) -> tuple[Any, Any, Callable[[], str | None] | None]:
    """Normalize the streamable-HTTP transport yield across SDK versions:
    mcp 1.x yields ``(read, write, get_session_id)``, mcp 2.x yields
    ``(read, write)``. Returns ``(read, write, get_session_id_or_None)``."""
    items = tuple(streams)
    if len(items) < 2:
        raise RuntimeError(f"unexpected transport shape: {len(items)} items")
    get_session_id = items[2] if len(items) >= 3 and callable(items[2]) else None
    return items[0], items[1], get_session_id


class _HttpStatusRecorder:
    """httpx response event hook: remembers the latest HTTP status and
    session id so a failed handshake can be classified (401/403 ->
    needs-auth, 404/405 -> SSE fallback) even though the SDK folds every
    non-2xx into a generic JSON-RPC error."""

    def __init__(self) -> None:
        self.last_status: int | None = None
        self.statuses: list[int] = []
        self.session_id: str | None = None

    async def __call__(self, response: Any) -> None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            self.last_status = status
            self.statuses.append(status)
        try:
            sid = response.headers.get("mcp-session-id")
        except Exception:  # noqa: BLE001
            sid = None
        if sid:
            self.session_id = sid

    def last_in(self, codes: tuple[int, ...]) -> bool:
        return self.last_status in codes


# ---------------------------------------------------------------------------
# Failure classification (design 3.3 / hermes _classify_mcp_failure).
# ---------------------------------------------------------------------------

_AUTH_ERROR_RE = re.compile(
    r"(?:\b401\b|\b403\b|unauthorized|forbidden|invalid_grant|"
    r"insufficient_scope|authentication\s+(?:failed|required|error)|"
    r"invalid\s+(?:api\s+key|token|credentials)|"
    r"(?:token|credential)s?\s+(?:expired|revoked)|expired\s+token|"
    r"not\s+authenticated|missing\s+(?:api\s+key|bearer|credentials))",
    re.IGNORECASE,
)


def looks_like_auth_error(text: str) -> bool:
    """True if an error string is 401/403/credential-shaped (design 3.3):
    these are permanent and map to needs-auth with no retry ladder."""
    return bool(_AUTH_ERROR_RE.search(text or ""))


_DEAD_STREAM_TYPES = (
    "ClosedResourceError", "BrokenResourceError", "EndOfStream",
    "BrokenPipeError", "ConnectionResetError", "ConnectionError",
    "EOFError", "ProcessLookupError", "RemoteProtocolError", "ReadError",
    "ConnectError", "WriteError",
)
_DEAD_STREAM_TEXT_RE = re.compile(
    r"(closed|broken\s+pipe|\beof\b|connection\s+(?:reset|lost|closed)|"
    r"peer\s+closed|incomplete\s+chunked\s+read|stream\s+ended|"
    r"all\s+connection\s+attempts\s+failed)",
    re.IGNORECASE,
)


def _iter_leaf_exceptions(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _iter_leaf_exceptions(inner)
    else:
        yield exc


def _find_leaf(exc: BaseException, types: tuple[type, ...]) -> BaseException | None:
    for leaf in _iter_leaf_exceptions(exc):
        if isinstance(leaf, types):
            return leaf
    return None


def describe_error(exc: BaseException) -> str:
    """Flatten (possibly nested anyio) exception groups to a readable
    reason string."""
    parts: list[str] = []
    for leaf in _iter_leaf_exceptions(exc):
        text = str(leaf) or type(leaf).__name__
        if text not in parts:
            parts.append(text)
    return "; ".join(parts) or type(exc).__name__


def looks_like_dead_stream(exc: BaseException) -> bool:
    """True when the exception means the server/pipe is definitively gone
    (confirmed death → straight to backoff, no degraded grace ping)."""
    for leaf in _iter_leaf_exceptions(exc):
        for klass in type(leaf).__mro__:
            if klass.__name__ in _DEAD_STREAM_TYPES:
                return True
        if _jsonrpc_code(leaf) == JSONRPC_CONNECTION_CLOSED:
            return True
        if _DEAD_STREAM_TEXT_RE.search(str(leaf)):
            return True
    return False


def _jsonrpc_code(exc: BaseException) -> int | None:
    code = getattr(getattr(exc, "error", None), "code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _is_method_not_found(exc: BaseException) -> bool:
    for leaf in _iter_leaf_exceptions(exc):
        if _jsonrpc_code(leaf) == JSONRPC_METHOD_NOT_FOUND:
            return True
        if "method not found" in str(leaf).lower():
            return True
    return False


def handshake_rejected_as_modern(exc: BaseException) -> bool:
    """True when a failed ``initialize`` signals a 2026-07-28-only
    (stateless) server: UnsupportedProtocolVersion (-32022) or the
    handshake methods missing entirely (-32601). Structural code first,
    substring fallback so detection survives ExceptionGroup wrapping."""
    for leaf in _iter_leaf_exceptions(exc):
        code = _jsonrpc_code(leaf)
        if code in (JSONRPC_UNSUPPORTED_PROTOCOL_VERSION, JSONRPC_METHOD_NOT_FOUND):
            return True
        msg = str(leaf).lower()
        if (
            "unsupported protocol version" in msg
            or str(JSONRPC_UNSUPPORTED_PROTOCOL_VERSION) in msg
        ):
            return True
        if "method not found" in msg:
            return True
    return False


def is_auth_exception(exc: BaseException) -> bool:
    """NeedsAuthError-compatible classification: any httpx 401/403, any
    exception whose class name contains ``OAuth`` or ``NeedsAuth``, or
    401/403/credential-shaped error text (checked on every leaf of an
    exception group)."""
    for leaf in _iter_leaf_exceptions(exc):
        for klass in type(leaf).__mro__:
            name = klass.__name__
            if "OAuth" in name or "NeedsAuth" in name:
                return True
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        if status in AUTH_STATUSES:
            return True
        if getattr(leaf, "status_code", None) in AUTH_STATUSES:
            return True
        if looks_like_auth_error(str(leaf)):
            return True
    return False


# ---------------------------------------------------------------------------
# Result cap (adapted from hermes _truncate_mcp_text_result, MIT)
# ---------------------------------------------------------------------------

def truncate_result_text(text: str, max_chars: int = DEFAULT_MAX_RESULT_CHARS) -> str:
    """Hard-cap a tool result BEFORE any budget layer sees it. Results at
    or under ``max_chars`` pass through unchanged; oversized text keeps
    the head and appends a truncation notice naming the omitted count and
    the knob (``limits.max_result_chars``)."""
    if max_chars < 1 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return (
        text[:max_chars]
        + f"\n\n[MCP RESULT TRUNCATED — {omitted:,} of {len(text):,} chars omitted; "
        f"raise limits.max_result_chars in mcp.json to see more]"
    )


# ---------------------------------------------------------------------------
# Protocol negotiation (adapted from hermes _negotiate_session, MIT)
# ---------------------------------------------------------------------------

async def negotiate_session(session: Any, timeout: float, *, name: str = "") -> tuple[Any, str]:
    """Initialize-first, ``server/discover`` fallback.

    Nearly every deployed server speaks the handshake era, so trying the
    legacy ``initialize`` first costs them zero extra round-trips. When
    the server rejects it as modern-only (2026-07-28 stateless: -32022
    UnsupportedProtocolVersion or -32601 method-not-found) and the SDK
    exposes ``discover()``, negotiate via ``server/discover`` instead.
    Returns ``(result, mode)`` with mode ``"handshake"`` or ``"stateless"``.
    """
    try:
        result = await asyncio.wait_for(session.initialize(), timeout=timeout)
        return result, "handshake"
    except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:
        if not handshake_rejected_as_modern(exc) or not hasattr(session, "discover"):
            raise
        logger.info(
            "mcp '%s': legacy handshake rejected (%s) — retrying via server/discover "
            "(2026-07-28 stateless server)",
            name, describe_error(exc),
        )
        result = await asyncio.wait_for(session.discover(), timeout=timeout)
        return result, "stateless"


def build_spawn_env(declared: dict[str, str]) -> dict[str, str]:
    """Allowlist-only spawn env: PATH/HOME/LANG base + declared keys.

    Goes through ``child_env(extra=...)`` so leaked Freyja python vars are
    stripped even if a declared key names one, then filters to the
    allowlist so nothing else from the parent environ leaks to
    third-party server code.
    """
    full = child_env(extra=declared)
    allowed = set(SPAWN_ENV_BASE) | set(declared)
    return {k: v for k, v in full.items() if k in allowed}


def tool_fingerprint(tool: Any) -> str:
    """Stable string for change detection across refreshes (name +
    description + schema + annotations)."""
    try:
        dumped = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps(dumped, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps(
            {
                "name": getattr(tool, "name", None),
                "description": getattr(tool, "description", None),
                "schema": str(getattr(tool, "input_schema", getattr(tool, "inputSchema", None))),
            },
            sort_keys=True,
            default=str,
        )


class McpConnection:
    """Supervises one MCP server: connect (spawn / HTTP), handshake,
    tools, health, backoff/park revival, listChanged, elicitation."""

    def __init__(
        self,
        spec: McpServerSpec,
        *,
        on_state_change: Callable[["McpConnection", State, State], None] | None = None,
        on_tools_changed: ToolsChangedHook | None = None,
        # Keepalive. ``ping_interval_s`` is the pre-v1 name, kept as an
        # alias; ``keepalive_interval_s`` wins when both are given.
        ping_interval_s: float = DEFAULT_KEEPALIVE_INTERVAL_S,
        keepalive_interval_s: float | None = None,
        keepalive_timeout_s: float | None = None,
        # Retry ladder. Legacy ``max_reconnects`` (v0) still works: when
        # given, it becomes the retry budget and parking is DISABLED
        # (exhaustion -> FAILED), preserving v0 test semantics. When
        # None (default), the v1 ladder applies: ``backoff_max_retries``
        # tries then PARKED with slow probes.
        max_reconnects: int | None = None,
        reconnect_delay_s: float | None = None,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
        backoff_max_retries: int = DEFAULT_BACKOFF_MAX_RETRIES,
        backoff_jitter: str = "full",
        park_probe_interval_s: float = DEFAULT_PARK_PROBE_INTERVAL_S,
        rapid_drop_budget: int = DEFAULT_RAPID_DROP_BUDGET,
        rng: random.Random | None = None,
        # Watchdog wrapper (design 4.3 layer 1).
        watchdog: bool = False,
        watchdog_poll_interval_s: float = DEFAULT_WATCHDOG_POLL_INTERVAL_S,
        pid_file: Path | str | None = None,
        # HTTP transports.
        auth_factory: AuthFactory | None = None,
        auth_interactive: bool = False,
        sse_read_timeout_s: float = DEFAULT_SSE_READ_TIMEOUT_S,
        http_read_timeout_s: float = DEFAULT_HTTP_READ_TIMEOUT_S,
        # Elicitation.
        approval_handler: ApprovalHandler | None = None,
        elicitation_timeout_s: float = DEFAULT_ELICITATION_TIMEOUT_S,
        # Schema cache TTL (None -> spec.schema_ttl_s; 0 = listChanged only).
        schema_ttl_s: float | None = None,
    ) -> None:
        self.spec = spec
        self.state: State = State.DISABLED
        self.since: float = time.time()
        self.reason: str = ""
        self.tool_count: int = 0
        self.restart_count: int = 0
        """Successful re-activations after the first connect."""
        self.last_latency_ms: float | None = None
        """Round-trip of the last keepalive probe (or initial handshake)."""
        self.tools: list[Any] = []
        """mcp.types.Tool list from the last successful tools/list."""
        self.transport_in_use: str = spec.transport
        """Transport actually serving this connection; ``http`` becomes
        ``sse`` after a successful 404/405 fallback and is remembered."""
        self.protocol_mode: str = ""
        """``handshake`` (legacy initialize) or ``stateless`` (discover)."""
        self.sse_fallbacks: int = 0
        """Times streamable HTTP was rejected (404/405) and SSE took over."""
        self.session_id: str | None = None
        """Mcp-Session-Id issued by an HTTP server; None = stateless."""
        self.session_proven: bool = False
        self.rapid_drops: int = 0
        """Consecutive drops of ACTIVE-but-unproven sessions."""
        self.ping_unsupported: bool = False
        """Latched per transport connection on a -32601 for ``ping``."""
        self.suspect_reason: str | None = None
        """Set when in-flight calls were aborted; the next call probes first."""
        self.sampling_declined: int = 0
        self.elicitations: dict[str, int] = {
            "requests": 0, "accepted": 0, "declined": 0, "cancelled": 0,
        }

        self._on_state_change = on_state_change
        self._on_tools_changed = on_tools_changed
        self._keepalive_interval_s = (
            keepalive_interval_s if keepalive_interval_s is not None else ping_interval_s
        )
        self._keepalive_timeout_s = (
            keepalive_timeout_s
            if keepalive_timeout_s is not None
            else max(2.0, min(10.0, self._keepalive_interval_s))
        )
        if max_reconnects is not None:
            self._retry_budget = max(0, int(max_reconnects))
            self._park_on_exhaustion = False
        else:
            self._retry_budget = max(0, int(backoff_max_retries))
            self._park_on_exhaustion = True
        self._backoff_base_s = (
            reconnect_delay_s if reconnect_delay_s is not None else backoff_base_s
        )
        self._backoff_cap_s = backoff_cap_s
        if backoff_jitter not in BACKOFF_JITTER_MODES:
            raise ValueError(f"backoff_jitter must be one of {BACKOFF_JITTER_MODES}")
        self._backoff_jitter = backoff_jitter
        self._park_probe_interval_s = park_probe_interval_s
        self._rapid_drop_budget = max(1, int(rapid_drop_budget))
        self._rng = rng or random.Random()
        self._watchdog_enabled = watchdog
        self._watchdog_poll_interval_s = watchdog_poll_interval_s
        self._pid_file = Path(pid_file) if pid_file is not None else None
        self._auth_factory = auth_factory
        self._auth_interactive = auth_interactive
        self._sse_read_timeout_s = sse_read_timeout_s
        self._http_read_timeout_s = http_read_timeout_s
        self._approval_handler = approval_handler
        self._elicitation_timeout_s = elicitation_timeout_s
        self._schema_ttl_s = float(schema_ttl_s if schema_ttl_s is not None else spec.schema_ttl_s)

        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._reconnect_reason = ""
        self._death_event = asyncio.Event()
        self._death_reason = ""
        self._inflight: set[asyncio.Future[Any]] = set()
        self._refresh_lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._http_recorder: _HttpStatusRecorder | None = None
        self._ever_active = False
        self._went_active = False

    # -- state ---------------------------------------------------------------

    def _set_state(self, new: State, *, reason: str = "", tool_count: int | None = None) -> None:
        old = self.state
        self.state = new
        if old is not new:
            self.since = time.time()
        self.reason = reason
        if tool_count is not None:
            self.tool_count = tool_count
        if old is not new:
            logger.info(
                "mcp '%s': %s -> %s%s",
                self.spec.name, old.value, new.value,
                f" ({reason})" if reason else "",
            )
            if self._on_state_change is not None:
                try:
                    self._on_state_change(self, old, new)
                except Exception:  # noqa: BLE001
                    logger.exception("mcp '%s': state-change callback failed", self.spec.name)

    def snapshot(self) -> dict[str, Any]:
        """Status-model fields for this connection (design 4.1). Shape is
        frozen; v2 diagnostics live in ``details()``."""
        return {
            "server": self.spec.name,
            "transport": self.spec.transport,
            "state": self.state.value,
            "reason": self.reason,
            "since": self.since,
            "tool_count": self.tool_count,
            "restart_count": self.restart_count,
            "last_latency_ms": (
                round(self.last_latency_ms, 1) if self.last_latency_ms is not None else None
            ),
        }

    def details(self) -> dict[str, Any]:
        """v2 diagnostics: transport actually in use, protocol mode,
        session id presence, proven flag, rapid drops, keepalive latch."""
        return {
            "transport_in_use": self.transport_in_use,
            "protocol_mode": self.protocol_mode,
            "sse_fallbacks": self.sse_fallbacks,
            "session_id": self.session_id,
            "session_proven": self.session_proven,
            "rapid_drops": self.rapid_drops,
            "ping_unsupported": self.ping_unsupported,
            "suspect": self.suspect_reason,
            "sampling_declined": self.sampling_declined,
            "elicitations": dict(self.elicitations),
        }

    # -- public API ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the supervisor task and wait for the first connect attempt
        to resolve (active, backoff, parked, failed, or needs-auth)."""
        if self._task is not None:
            return
        if not self.spec.enabled:
            self._set_state(State.DISABLED, reason="disabled in config")
            self._ready_event.set()
            return
        self._task = asyncio.create_task(
            self._run(), name=f"mcp-conn-{self.spec.name}"
        )
        await self._ready_event.wait()

    async def stop(self) -> None:
        """Request clean shutdown and wait for the supervisor to unwind
        (which exits the SDK contexts in their owning task, terminating
        the child process / HTTP session)."""
        self._stop_event.set()
        self._fail_inflight_calls("connection stopped")
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (asyncio.TimeoutError, TimeoutError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except Exception:  # noqa: BLE001
            pass
        self._task = None
        for refresh in list(self._refresh_tasks):
            refresh.cancel()
        self._remove_pid_file()
        if self.state not in (State.FAILED, State.NEEDS_AUTH):
            self._set_state(State.DISABLED, reason="stopped")

    def request_reconnect(self, reason: str = "reconnect requested") -> None:
        """Ask the supervisor to tear down the current transport and walk
        the normal reconnect path (e.g. after ``/mcp login`` refreshed
        credentials). No-op when not connected."""
        self._reconnect_reason = reason
        self._reconnect_event.set()

    def mark_suspect(self, reason: str) -> None:
        """Latch a suspicion; the NEXT ``call_tool`` probes before reusing
        the session (hermes SuspectableBackend)."""
        if reason and self.suspect_reason is None:
            logger.warning(
                "mcp '%s': connection marked suspect (%s); next call will health-check it",
                self.spec.name, reason,
            )
        self.suspect_reason = reason or None

    async def call_tool(
        self,
        remote_name: str,
        arguments: dict[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a remote tool. Raises McpUnavailableError when the
        connection is not active/degraded or when the transport dies while
        the call is in flight (fast-fail instead of waiting out the
        timeout); SDK McpError (incl. per-call timeout via
        ``read_timeout_seconds``) propagates to the caller."""
        session = self._session
        if session is None or self.state not in ACTIVE_LIKE:
            raise McpUnavailableError(
                f"MCP server '{self.spec.name}' is {self.state.value}"
                + (f": {self.reason}" if self.reason else "")
            )
        if self.suspect_reason is not None:
            if not await self._ensure_healthy():
                raise McpUnavailableError(
                    f"MCP server '{self.spec.name}': connection failed its health probe "
                    f"after '{self._death_reason or 'transport failure'}'; reconnecting"
                )
            session = self._session
            if session is None or self.state not in ACTIVE_LIKE:
                raise McpUnavailableError(f"MCP server '{self.spec.name}' is {self.state.value}")

        death = self._death_event
        call: asyncio.Future[Any] = asyncio.ensure_future(
            session.call_tool(remote_name, arguments or {}, read_timeout_seconds=timeout)
        )
        waiter: asyncio.Future[Any] = asyncio.ensure_future(death.wait())
        self._inflight.add(call)
        try:
            done, _pending = await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            call.cancel()
            raise
        finally:
            waiter.cancel()
            self._inflight.discard(call)
        if call in done:
            try:
                result = call.result()
            except Exception as exc:
                if looks_like_dead_stream(exc):
                    # The SDK noticed the peer vanished before the supervisor
                    # did; make the supervisor tear down now instead of at
                    # the next keepalive tick.
                    self.request_reconnect(f"tool call saw dead stream: {describe_error(exc)}")
                raise
            self._mark_proven("tool call")
            return result
        call.cancel()
        with contextlib.suppress(BaseException):
            await call
        raise McpUnavailableError(
            f"MCP server '{self.spec.name}': transport died while '{remote_name}' was in "
            f"flight ({self._death_reason or 'connection closed'}); the call was aborted "
            f"instead of waiting out its timeout"
        )

    # -- supervisor ----------------------------------------------------------

    async def _run(self) -> None:
        """Full lifecycle loop: connect attempts, backoff ladder, rapid-drop
        budget, parking with slow probes, permanent-failure short-circuit."""
        failures = 0
        while not self._stop_event.is_set():
            self._went_active = False
            self.session_proven = False
            error = ""
            try:
                await self._run_once()
            except _NeedsAuth as exc:
                self._set_state(State.NEEDS_AUTH, reason=str(exc))
                self._ready_event.set()
                return
            except _PermanentFailure as exc:
                self._set_state(State.FAILED, reason=str(exc))
                self._ready_event.set()
                return
            except Exception as exc:  # noqa: BLE001
                leaf = _find_leaf(exc, (_NeedsAuth, _PermanentFailure))
                if isinstance(leaf, _NeedsAuth):
                    self._set_state(State.NEEDS_AUTH, reason=str(leaf))
                    self._ready_event.set()
                    return
                if isinstance(leaf, _PermanentFailure):
                    self._set_state(State.FAILED, reason=str(leaf))
                    self._ready_event.set()
                    return
                error = describe_error(exc)
                if self._is_auth_failure(exc):
                    # 401/403/OAuth-shaped: permanent, no ladder (design 3.3).
                    self._set_state(
                        State.NEEDS_AUTH,
                        reason=f"{needs_auth_reason(self.spec.name)} ({error})",
                    )
                    self._ready_event.set()
                    return
            finally:
                self._ready_event.set()
            if self._stop_event.is_set():
                return
            # Abnormal end of a connect/serve cycle.
            reason = error or self.reason or "connection closed"
            if self._went_active and self.session_proven:
                failures = 0
                self.rapid_drops = 0
            elif self._went_active:
                # Handshake completed but the session never proved healthy:
                # charge the rapid-drop budget (hermes #62212).
                self.rapid_drops += 1
                logger.warning(
                    "mcp '%s': session dropped before proving healthy (%d/%d rapid drops): %s",
                    self.spec.name, self.rapid_drops, self._rapid_drop_budget, reason,
                )
            failures += 1
            rapid_exhausted = self.rapid_drops >= self._rapid_drop_budget
            if failures > self._retry_budget or rapid_exhausted:
                if not self._park_on_exhaustion:
                    if self.state is not State.FAILED:
                        self._set_state(State.FAILED, reason=reason)
                    return
                why = (
                    f"{self.rapid_drops} rapid drops without a healthy session"
                    if rapid_exhausted
                    else "retry budget exhausted"
                )
                self._set_state(
                    State.PARKED,
                    reason=(
                        f"{why}; probing every "
                        f"{self._park_probe_interval_s:g}s ({reason})"
                    ),
                )
                if await self._sleep_unless_stopped(self._park_probe_interval_s):
                    return
                # One probe per wake: keep the budgets pinned at the edge so
                # a failed probe re-parks immediately instead of re-walking
                # the ladder.
                failures = max(failures, self._retry_budget)
                if rapid_exhausted:
                    self.rapid_drops = self._rapid_drop_budget - 1
                continue  # parked probe = one more connect attempt
            delay = self._backoff_delay(failures)
            self._set_state(
                State.BACKOFF,
                reason=(
                    f"retry {failures}/{self._retry_budget} in "
                    f"{delay:.1f}s ({reason})"
                ),
            )
            if await self._sleep_unless_stopped(delay):
                return

    def _is_auth_failure(self, exc: BaseException) -> bool:
        if is_auth_exception(exc):
            return True
        recorder = self._http_recorder
        return recorder is not None and recorder.last_in(AUTH_STATUSES)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter. ``full`` (default, AWS "full
        jitter"): U(0, min(cap, base * 2^(n-1))) with a small floor so a
        retry never fires instantly; ``equal``: U(0.5d, d)."""
        delay = min(self._backoff_cap_s, self._backoff_base_s * (2 ** max(0, attempt - 1)))
        if self._backoff_jitter == "equal":
            return delay * self._rng.uniform(0.5, 1.0)
        floor = min(delay, self._backoff_base_s * 0.1)
        return max(floor, delay * self._rng.uniform(0.0, 1.0))

    async def _sleep_unless_stopped(self, seconds: float) -> bool:
        """Sleep up to *seconds*; True if stop was requested meanwhile."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    def _new_cycle(self) -> None:
        self._death_event = asyncio.Event()
        self._death_reason = ""
        self._reconnect_event = asyncio.Event()
        self._reconnect_reason = ""
        self.ping_unsupported = False
        self.session_id = None
        self.protocol_mode = ""
        self._http_recorder = None

    async def _run_once(self) -> None:
        """One connect->serve cycle. Owns the SDK context managers for its
        entire duration; raises on abnormal termination."""
        self._set_state(State.CONNECTING)
        self._new_cycle()
        try:
            if self.spec.transport in ("http", "sse"):
                await self._run_http()
            else:
                await self._run_stdio()
        finally:
            self._fail_inflight_calls(self._death_reason or "transport closed")
            self._session = None
            self._remove_pid_file()
            if self.state in ACTIVE_LIKE and self._stop_event.is_set():
                self._set_state(State.DISABLED, reason="stopped")
            # Leaving ACTIVE/DEGRADED abnormally: _run() decides the next
            # state (backoff/parked/failed); that transition fires the
            # manager's unregister hook.

    # -- stdio ---------------------------------------------------------------

    async def _run_stdio(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self.transport_in_use = "stdio"
        command, args, env = self._resolve_spawn()
        if self._watchdog_enabled:
            from bridge.mcp.stdio_watchdog import wrap_with_watchdog

            command, args = wrap_with_watchdog(
                command,
                args,
                parent_pid=os.getpid(),
                poll_interval_s=self._watchdog_poll_interval_s,
                pid_file=self._pid_file,
            )
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, **self._session_kwargs()
            ) as session:
                await self._handshake(session)
                await self._activate_and_watch(session)

    # -- http / sse ----------------------------------------------------------

    async def _run_http(self) -> None:
        url, headers = self._resolve_http()
        auth = self._build_auth()
        if self.transport_in_use == "sse":
            await self._run_sse(url, headers, auth)
            return
        fallback = await self._run_streamable(url, headers, auth)
        if fallback:
            status = self._http_recorder.last_status if self._http_recorder else None
            logger.warning(
                "mcp '%s': streamable HTTP endpoint answered initialize with HTTP %s — "
                "falling back to legacy SSE transport (remembered for reconnects)",
                self.spec.name, status,
            )
            self.transport_in_use = "sse"
            self.sse_fallbacks += 1
            await self._run_sse(url, headers, auth)

    def _build_http_client(self, headers: dict[str, str], auth: Any) -> Any:
        """httpx AsyncClient from the SDK's own httpx module with the MCP
        defaults (follow redirects, long read timeout for SSE bodies) plus
        the status-recording hook."""
        from mcp.shared._httpx_utils import create_mcp_http_client

        httpx_mod = sdk_httpx()
        recorder = _HttpStatusRecorder()
        self._http_recorder = recorder
        timeout = httpx_mod.Timeout(
            float(self.spec.connect_timeout_s), read=self._http_read_timeout_s
        )
        client = create_mcp_http_client(headers=headers or None, timeout=timeout, auth=auth)
        client.event_hooks["response"].append(recorder)
        return client

    async def _run_streamable(self, url: str, headers: dict[str, str], auth: Any) -> bool:
        """Serve over streamable HTTP. Returns True when the handshake was
        rejected with HTTP 404/405 and the caller should try SSE."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        client = self._build_http_client(headers, auth)
        recorder = self._http_recorder
        assert recorder is not None
        pending: BaseException | None = None
        fallback = False
        try:
            async with client:
                async with streamable_http_client(url, http_client=client) as streams:
                    read_stream, write_stream, get_session_id = unpack_transport_streams(streams)
                    async with ClientSession(
                        read_stream, write_stream, **self._session_kwargs()
                    ) as session:
                        try:
                            await self._handshake(session)
                        except (asyncio.CancelledError, _NeedsAuth, _PermanentFailure):
                            raise
                        except Exception as exc:  # noqa: BLE001
                            if recorder.last_in(AUTH_STATUSES) or is_auth_exception(exc):
                                pending = _NeedsAuth(
                                    f"{needs_auth_reason(self.spec.name)} "
                                    f"(HTTP {recorder.last_status}: {describe_error(exc)})"
                                )
                            elif (
                                self.spec.transport == "http"
                                and recorder.last_in(SSE_FALLBACK_STATUSES)
                            ):
                                fallback = True
                            else:
                                pending = exc
                        else:
                            sid = None
                            if get_session_id is not None:
                                with contextlib.suppress(Exception):
                                    sid = get_session_id()
                            self.session_id = sid or recorder.session_id
                            if self.session_id is None:
                                logger.info(
                                    "mcp '%s': server issued no Mcp-Session-Id (stateless "
                                    "streamable HTTP); continuing without a GET stream",
                                    self.spec.name,
                                )
                            await self._activate_and_watch(session)
        except BaseExceptionGroup as group:
            if pending is None and not fallback:
                if recorder.last_in(AUTH_STATUSES):
                    raise _NeedsAuth(
                        f"{needs_auth_reason(self.spec.name)} (HTTP {recorder.last_status})"
                    ) from group
                raise
            logger.debug(
                "mcp '%s': transport unwound with %s while handling a classified handshake "
                "failure", self.spec.name, describe_error(group),
            )
        if pending is not None:
            raise pending
        return fallback

    async def _run_sse(self, url: str, headers: dict[str, str], auth: Any) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        httpx_mod = sdk_httpx()
        recorder = _HttpStatusRecorder()
        self._http_recorder = recorder
        read_timeout = self._http_read_timeout_s

        def _factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
            kwargs: dict[str, Any] = {
                "follow_redirects": True,
                "timeout": (
                    timeout if timeout is not None
                    else httpx_mod.Timeout(30.0, read=read_timeout)
                ),
                "event_hooks": {"response": [recorder]},
            }
            if headers is not None:
                kwargs["headers"] = headers
            if auth is not None:
                kwargs["auth"] = auth
            return httpx_mod.AsyncClient(**kwargs)

        kwargs: dict[str, Any] = {
            "headers": headers or None,
            "timeout": float(self.spec.connect_timeout_s),
            "sse_read_timeout": self._sse_read_timeout_s,
            "httpx_client_factory": _factory,
        }
        if auth is not None:
            kwargs["auth"] = auth
        try:
            async with sse_client(url, **kwargs) as streams:
                read_stream, write_stream, _get_sid = unpack_transport_streams(streams)
                async with ClientSession(
                    read_stream, write_stream, **self._session_kwargs()
                ) as session:
                    await self._handshake(session)
                    self.session_id = recorder.session_id
                    await self._activate_and_watch(session)
        except (_NeedsAuth, _PermanentFailure):
            raise
        except Exception as exc:  # noqa: BLE001 — includes anyio ExceptionGroups
            if _find_leaf(exc, (_NeedsAuth, _PermanentFailure)) is not None:
                raise
            if recorder.last_in(AUTH_STATUSES) or is_auth_exception(exc):
                raise _NeedsAuth(
                    f"{needs_auth_reason(self.spec.name)} "
                    f"(HTTP {recorder.last_status}: {describe_error(exc)})"
                ) from exc
            raise

    def _build_auth(self) -> Any:
        """Ask the injected factory for an httpx Auth (None = no auth).
        Auth-shaped factory errors -> needs-auth. An Auth object from the
        WRONG httpx module (``httpx`` vs the SDK's ``httpx2``) is a config
        bug, not a transient failure -> permanent with a clear message."""
        if self._auth_factory is None:
            return None
        try:
            auth = self._auth_factory(self.spec, self._auth_interactive)
        except Exception as exc:  # noqa: BLE001
            if is_auth_exception(exc):
                raise _NeedsAuth(
                    f"{needs_auth_reason(self.spec.name)} ({describe_error(exc)})"
                ) from exc
            raise
        if auth is None or callable(auth):
            return auth
        httpx_mod = sdk_httpx()
        if not isinstance(auth, httpx_mod.Auth):
            raise _PermanentFailure(
                f"auth_factory returned {type(auth).__module__}.{type(auth).__name__}; the MCP "
                f"SDK needs an instance of {httpx_mod.__name__}.Auth (the SDK's own httpx module)"
            )
        return auth

    def _resolve_http(self) -> tuple[str, dict[str, str]]:
        """Expand ${VAR} refs in url + headers at connect time. Missing
        secret-shaped vars -> needs-auth; other missing vars expand empty
        with a warning. Invalid/missing URL -> permanent failure."""
        missing_secret: list[str] = []

        def _expand(value: str) -> str:
            result = expand_env_refs(value)
            for var in result.missing:
                if is_secret_key(var):
                    missing_secret.append(var)
                else:
                    logger.warning(
                        "mcp '%s': env var %s unset; expanded empty", self.spec.name, var,
                    )
            return result.text

        url = _expand(self.spec.url or "").strip()
        headers = {key: _expand(value) for key, value in self.spec.headers.items()}
        if missing_secret:
            unique = sorted(set(missing_secret))
            raise _NeedsAuth(
                "missing secret env var(s): " + ", ".join(unique) + " — add to ~/.freyja/.env"
            )
        if not url:
            raise _PermanentFailure(f"{self.spec.transport} server has no url configured")
        if not re.match(r"^https?://[^/\s]+", url):
            raise _PermanentFailure(f"invalid MCP url {url!r}: expected http(s)://host[/path]")
        return url, headers

    # -- session callbacks ---------------------------------------------------

    def _session_kwargs(self) -> dict[str, Any]:
        return {
            "message_handler": self._make_message_handler(),
            "elicitation_callback": self._elicitation_callback,
            "sampling_callback": self._sampling_callback,
        }

    def _make_message_handler(self) -> Callable[[Any], Awaitable[None]]:
        """Dispatch server notifications. ``tools/list_changed`` schedules
        a diff refresh in its OWN task — never inline, because refreshing
        synchronously inside the SDK's notification handler can race a
        concurrent request and wedge the JSON-RPC stream (hermes 2740).
        prompts/resources list_changed are logged no-ops."""

        async def _handler(message: Any) -> None:
            try:
                if isinstance(message, Exception):
                    logger.debug(
                        "mcp '%s': transport exception surfaced: %s", self.spec.name, message,
                    )
                    return
                import mcp.types as mcp_types

                # mcp 1.x wraps notifications in a RootModel (.root); 2.x
                # hands over the concrete type directly.
                payload = getattr(message, "root", message)
                if isinstance(payload, mcp_types.ToolListChangedNotification):
                    logger.info("mcp '%s': tools/list_changed received", self.spec.name)
                    self._schedule_tools_refresh("tools/list_changed")
                    await asyncio.sleep(0)
                elif isinstance(payload, mcp_types.PromptListChangedNotification):
                    logger.debug("mcp '%s': prompts/list_changed (ignored)", self.spec.name)
                elif isinstance(payload, mcp_types.ResourceListChangedNotification):
                    logger.debug("mcp '%s': resources/list_changed (ignored)", self.spec.name)
            except Exception:  # noqa: BLE001
                logger.exception("mcp '%s': message handler failed", self.spec.name)

        return _handler

    async def _elicitation_callback(self, context: Any, params: Any) -> Any:
        """Route ``elicitation/create`` through the injected approval
        handler (default: decline). Fail-closed: timeout -> cancel, any
        error -> decline."""
        import mcp.types as mcp_types

        self.elicitations["requests"] += 1
        schema = (
            getattr(params, "requested_schema", None)
            or getattr(params, "requestedSchema", None)
        )
        if schema is not None and not isinstance(schema, dict):
            with contextlib.suppress(Exception):
                schema = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
        request = ElicitRequest(
            server=self.spec.name,
            message=str(getattr(params, "message", "") or ""),
            mode=str(getattr(params, "mode", "form") or "form"),
            requested_schema=schema if isinstance(schema, dict) else None,
            url=getattr(params, "url", None),
            elicitation_id=(
                getattr(params, "elicitation_id", None)
                or getattr(params, "elicitationId", None)
            ),
            raw=params,
        )
        handler = self._approval_handler or decline_elicitation
        try:
            answer = await asyncio.wait_for(handler(request), timeout=self._elicitation_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "mcp '%s': elicitation timed out after %gs; cancelling",
                self.spec.name, self._elicitation_timeout_s,
            )
            self.elicitations["cancelled"] += 1
            return mcp_types.ElicitResult(action="cancel")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "mcp '%s': approval handler failed; declining elicitation", self.spec.name,
            )
            self.elicitations["declined"] += 1
            return mcp_types.ElicitResult(action="decline")

        if answer is None:
            self.elicitations["declined"] += 1
            return mcp_types.ElicitResult(action="decline")
        if hasattr(answer, "action") and not isinstance(answer, dict):
            action = str(getattr(answer, "action", "decline"))
            bucket = {"accept": "accepted", "cancel": "cancelled"}.get(action, "declined")
            self.elicitations[bucket] += 1
            return answer
        if isinstance(answer, dict) and "action" in answer and set(answer) <= {"action", "content"}:
            action = str(answer["action"])
            if action == "accept":
                self.elicitations["accepted"] += 1
                return mcp_types.ElicitResult(action="accept", content=answer.get("content") or {})
            self.elicitations["cancelled" if action == "cancel" else "declined"] += 1
            return mcp_types.ElicitResult(action="cancel" if action == "cancel" else "decline")
        if isinstance(answer, dict):
            self.elicitations["accepted"] += 1
            return mcp_types.ElicitResult(action="accept", content=answer)
        logger.warning(
            "mcp '%s': approval handler returned %s; declining",
            self.spec.name, type(answer).__name__,
        )
        self.elicitations["declined"] += 1
        return mcp_types.ElicitResult(action="decline")

    async def _sampling_callback(self, context: Any, params: Any) -> Any:
        """Sampling is deprecated by the 2026-07-28 spec; decline cleanly
        with a JSON-RPC error rather than proxying model calls."""
        import mcp.types as mcp_types

        self.sampling_declined += 1
        logger.info(
            "mcp '%s': declining sampling/createMessage request (deprecated feature; "
            "Freyja does not proxy model sampling for MCP servers)",
            self.spec.name,
        )
        return mcp_types.ErrorData(
            code=JSONRPC_INVALID_REQUEST,
            message=(
                "Sampling is not supported by this client (deprecated as of MCP "
                "2026-07-28); the request was declined"
            ),
        )

    # -- handshake / serve ---------------------------------------------------

    async def _handshake(self, session: Any) -> None:
        """Negotiate protocol era + first tools/list. Raises on failure
        (caller classifies)."""
        started = time.monotonic()
        _result, mode = await negotiate_session(
            session, self.spec.connect_timeout_s, name=self.spec.name
        )
        self.protocol_mode = mode
        tools = await self._list_all_tools(session)
        self.last_latency_ms = (time.monotonic() - started) * 1000.0
        self.tools = tools

    async def _activate_and_watch(self, session: Any) -> None:
        self._session = session
        if self._ever_active:
            self.restart_count += 1
        self._ever_active = True
        self._went_active = True
        self.session_proven = False
        self._set_state(State.ACTIVE, tool_count=len(self.tools))
        self._ready_event.set()
        await self._watch(session)

    async def _wait_for_tick(self, seconds: float) -> str:
        """Wait up to *seconds* for stop or reconnect; returns
        ``"stop"`` / ``"reconnect"`` / ``"timeout"``."""
        if self._stop_event.is_set():
            return "stop"
        if self._reconnect_event.is_set():
            return "reconnect"
        stop_w = asyncio.ensure_future(self._stop_event.wait())
        reconn_w = asyncio.ensure_future(self._reconnect_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_w, reconn_w}, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_w.cancel()
            reconn_w.cancel()
        if stop_w in done:
            return "stop"
        if reconn_w in done:
            return "reconnect"
        return "timeout"

    async def _watch(self, session: Any) -> None:
        """Active phase: keepalive every keepalive_interval_s (skipped
        while calls are in flight — they prove liveness themselves and a
        concurrent probe could wedge a stdio stream), optional schema-TTL
        diff refresh, reconnect requests. First failed probe -> DEGRADED
        (tools stay registered); a second consecutive failure, or a
        definitively dead stream, raises -> backoff ladder. A successful
        probe from DEGRADED -> ACTIVE and proves the session."""
        interval = self._keepalive_interval_s
        ttl = self._schema_ttl_s if self._schema_ttl_s and self._schema_ttl_s > 0 else None
        now = time.monotonic()
        next_keepalive = now + interval
        next_refresh = now + ttl if ttl is not None else None
        while not self._stop_event.is_set():
            now = time.monotonic()
            deadline = next_keepalive if next_refresh is None else min(next_keepalive, next_refresh)
            tick = await self._wait_for_tick(deadline - now)
            if tick == "stop":
                return  # clean stop
            if tick == "reconnect":
                raise ConnectionError(self._reconnect_reason or "reconnect requested")
            now = time.monotonic()
            if next_refresh is not None and now >= next_refresh:
                next_refresh = now + ttl  # type: ignore[operator]
                self._schedule_tools_refresh("schema_ttl")
            if now < next_keepalive:
                continue
            next_keepalive = now + interval
            if self._inflight:
                logger.debug(
                    "mcp '%s': keepalive skipped (%d call(s) in flight)",
                    self.spec.name, len(self._inflight),
                )
                continue
            try:
                latency_ms = await self._keepalive(session)
            except Exception as exc:  # noqa: BLE001
                desc = describe_error(exc)
                if self.state is State.DEGRADED or looks_like_dead_stream(exc):
                    raise ConnectionError(f"keepalive failed: {desc}") from exc
                self._set_state(State.DEGRADED, reason=f"keepalive failed: {desc}")
                continue
            self.last_latency_ms = latency_ms
            self._mark_proven("keepalive")
            if self.state is State.DEGRADED:
                self._set_state(State.ACTIVE, reason="keepalive recovered")

    async def _keepalive(self, session: Any) -> float:
        """One health probe: ``ping`` first; on -32601 latch
        ``ping_unsupported`` for this transport connection and use a cheap
        ``tools/list`` from then on (design 4.2 / hermes 2846). Returns
        latency in ms."""
        started = time.monotonic()
        async with self._rpc_lock:
            if not self.ping_unsupported:
                try:
                    await asyncio.wait_for(session.send_ping(), timeout=self._keepalive_timeout_s)
                    return (time.monotonic() - started) * 1000.0
                except (asyncio.TimeoutError, TimeoutError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    if not _is_method_not_found(exc):
                        raise
                    self.ping_unsupported = True
                    logger.info(
                        "mcp '%s': server lacks the optional 'ping' utility (-32601); "
                        "using tools/list for keepalive on this connection",
                        self.spec.name,
                    )
            await asyncio.wait_for(session.list_tools(), timeout=self._keepalive_timeout_s)
        return (time.monotonic() - started) * 1000.0

    def _mark_proven(self, how: str) -> None:
        """Record that the current session demonstrated real health; only
        then does the reconnect budget clear (hermes _mark_session_proven)."""
        if not self.session_proven and self.state in ACTIVE_LIKE:
            self.session_proven = True
            if self.rapid_drops:
                logger.info(
                    "mcp '%s': session proven healthy via %s after %d rapid drop(s)",
                    self.spec.name, how, self.rapid_drops,
                )
            self.rapid_drops = 0

    async def _ensure_healthy(self) -> bool:
        """Probe a suspect connection before reuse; on failure request a
        reconnect and return False (never raises)."""
        reason = self.suspect_reason
        session = self._session
        if session is None:
            self.suspect_reason = None
            return False
        try:
            latency_ms = await asyncio.wait_for(
                self._keepalive(session), timeout=self._keepalive_timeout_s * 2
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp '%s': suspect connection (%s) failed its health probe (%s); reconnecting",
                self.spec.name, reason, describe_error(exc),
            )
            self.suspect_reason = None
            self.request_reconnect(f"health probe failed after: {reason}")
            return False
        self.last_latency_ms = latency_ms
        self.suspect_reason = None
        self._mark_proven("health probe")
        logger.info(
            "mcp '%s': suspect connection passed its health probe (%s)", self.spec.name, reason,
        )
        return True

    def _fail_inflight_calls(self, reason: str) -> None:
        """Fail every pending ``call_tool`` immediately (they were awaiting
        a dying transport) and latch suspicion so the next call on the
        replacement session probes first (hermes _fail_inflight_calls)."""
        victims = [c for c in self._inflight if not c.done()]
        self._death_reason = reason
        self._death_event.set()
        if victims:
            self.mark_suspect(f"{reason} aborted {len(victims)} in-flight call(s)")
            logger.warning(
                "mcp '%s': failing %d in-flight call(s) fast (%s)",
                self.spec.name, len(victims), reason,
            )

    # -- listChanged / schema TTL -------------------------------------------

    def _schedule_tools_refresh(self, reason: str) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._refresh_tools_task(reason), name=f"mcp-refresh-{self.spec.name}"
        )
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return task

    async def _refresh_tools_task(self, reason: str) -> None:
        try:
            await self.refresh_tools(reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("mcp '%s': tool refresh (%s) failed", self.spec.name, reason)

    async def refresh_tools(self, reason: str = "manual") -> tuple[set[str], set[str], set[str]]:
        """Re-fetch tools/list and DIFF against the current set: returns
        ``(added, removed, changed)`` remote names and notifies the
        ``on_tools_changed`` hook so the manager can register/unregister/
        update proxies without a nuke-and-repave (live tool-call ids keep
        pointing at surviving handlers)."""
        async with self._refresh_lock:
            session = self._session
            if session is None or self.state not in ACTIVE_LIKE:
                return set(), set(), set()
            async with self._rpc_lock:
                new_tools = await self._list_all_tools(session)
            old_by_name = {getattr(t, "name", ""): t for t in self.tools}
            new_by_name = {getattr(t, "name", ""): t for t in new_tools}
            added = set(new_by_name) - set(old_by_name)
            removed = set(old_by_name) - set(new_by_name)
            changed = {
                n for n in set(old_by_name) & set(new_by_name)
                if tool_fingerprint(old_by_name[n]) != tool_fingerprint(new_by_name[n])
            }
            self.tools = new_tools
            self.tool_count = len(new_tools)
            self._mark_proven(f"refresh:{reason}")
            if added or removed or changed:
                logger.warning(
                    "mcp '%s': tool set changed (%s) — added %s, removed %s, changed %s. "
                    "Verify these changes are expected.",
                    self.spec.name, reason,
                    sorted(added) or "-", sorted(removed) or "-", sorted(changed) or "-",
                )
            else:
                logger.debug(
                    "mcp '%s': tools refreshed (%s), %d tool(s), no changes",
                    self.spec.name, reason, len(new_tools),
                )
            if self._on_tools_changed is not None:
                try:
                    self._on_tools_changed(self, added, removed, changed)
                except Exception:  # noqa: BLE001
                    logger.exception("mcp '%s': tools-changed hook failed", self.spec.name)
            return added, removed, changed

    # -- helpers -------------------------------------------------------------

    def _remove_pid_file(self) -> None:
        if self._pid_file is not None:
            with contextlib.suppress(OSError):
                self._pid_file.unlink()

    def _resolve_spawn(self) -> tuple[str, list[str], dict[str, str]]:
        """Expand ${VAR} refs at connect time and build the allowlist env.

        Raises _NeedsAuth when a referenced var with a secret-shaped name
        is unset (design doc section 2); non-secret unset vars expand to
        empty with a warning. Raises _PermanentFailure when the resolved
        binary does not exist (no retry ladder for a missing binary).
        """
        missing_secret: list[str] = []

        def _expand(value: str) -> str:
            result = expand_env_refs(value)
            for var in result.missing:
                if is_secret_key(var):
                    missing_secret.append(var)
                else:
                    logger.warning(
                        "mcp '%s': env var %s unset; expanded empty",
                        self.spec.name, var,
                    )
            return result.text

        command = _expand(self.spec.command or "")
        args = [_expand(a) for a in self.spec.args]
        declared = {key: _expand(value) for key, value in self.spec.env.items()}
        if missing_secret:
            unique = sorted(set(missing_secret))
            raise _NeedsAuth(
                "missing secret env var(s): " + ", ".join(unique)
                + " — add to ~/.freyja/.env"
            )
        if not command:
            raise _PermanentFailure("stdio server has no command configured")
        if shutil.which(command) is None:
            raise _PermanentFailure(
                f"command not found: {command!r} — check PATH or the mcp.json entry"
            )
        return command, args, build_spawn_env(declared)

    async def _list_all_tools(self, session: Any) -> list[Any]:
        """tools/list with nextCursor pagination (bounded)."""
        import mcp.types as mcp_types

        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(LIST_TOOLS_MAX_PAGES):
            params = (
                mcp_types.PaginatedRequestParams(cursor=cursor)
                if cursor is not None
                else None
            )
            result = await asyncio.wait_for(
                session.list_tools(params=params),
                timeout=self.spec.connect_timeout_s,
            )
            tools.extend(result.tools)
            cursor = getattr(result, "next_cursor", None) or getattr(result, "nextCursor", None)
            if not isinstance(cursor, str) or not cursor:
                break
        else:
            logger.warning(
                "mcp '%s': tools/list pagination exceeded %d pages; truncating at %d tools",
                self.spec.name, LIST_TOOLS_MAX_PAGES, len(tools),
            )
        return tools
