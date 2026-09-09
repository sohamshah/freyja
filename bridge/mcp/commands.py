"""mcp_command handling shared by the bridge stdin IPC and gateway /mcp.

One async handler over an McpManager serves both entry points
(freyja_bridge._handle_command's ``mcp_command`` branch and the gateway's
``/mcp`` slash command) so unit tests can drive it directly with fake
state. Wire contract (fixed; the desktop renderer builds against it):

    UI -> bridge : {type:'mcp_command', requestId?, action, server?, args?: [..]}
    bridge -> UI : {type:'mcp_command_result', requestId?, ok, action, message,
                    servers?, rows?, data?}
    bridge -> UI : {type:'mcp_status', servers:[...]}          (after every change)
    bridge -> UI : {type:'mcp_oauth_url', server, url, redirectUri, expiresInS, opened}
    bridge -> UI : {type:'mcp_oauth_result', server, ok, error?, expiresAt?, scopes?}

Actions:
  · status [server]        — per-server rows (v1 keys stable) + v2 details:
                             transport_in_use, needs-auth reason, token
                             expiry, quarantined count, rapid_drops, last_error
  · enable / disable       — flip mcp.json + connect/disconnect live
  · reload                 — re-read mcp.json, apply the diff
  · login <server>         — OAuth 2.1 login. desktop: BrowserFlow (bridge
                             opens the URL) + mcp_oauth_url(opened=true);
                             gateway: HandoffFlow, URL posted to the chat.
                             One login per server at a time.
  · logout <server>        — wipe stored OAuth state, reconnect -> needs-auth
  · reauth <server>|--all  — logout+login, sequentially (hermes reauth --all)
  · add <url|cmd ...> [--name N] [--transport http|sse|stdio]
        [--header K=V]* [--env K=${VAR}]* [--enable] [--trust T] [-- args]
                           — discovery-first add (hermes mcp add): URLs are
                             probed with an unauthenticated JSON-RPC
                             initialize; 401/403 + resource_metadata ->
                             auth: oauth, 404/405 -> SSE, unreachable ->
                             added disabled with a warning
  · remove <server> [--purge]  — delete from mcp.json (+ tokens), unregister
  · test <server>          — one-shot isolated diagnostics (never touches
                             the live registry): initialize -> tools/list ->
                             ping with per-step timings
  · tools [server]         — registered proxy tools + pending quarantine
  · catalog list [--tag T] | search <q> | info <name>
            | install <name> [--enable] [--as NAME]
  · call <server> <tool> [json]  — invoke a registered proxy tool (debug/E2E)
  · answer <requestId> key=value ... | decline | cancel — settle a pending
                             elicitation (Slack surface; desktop uses the
                             typed mcp_elicitation_response event)

`message` is human-readable multi-line text usable verbatim by both
surfaces; `rows`/`data` carry the structured payload. No message ever
contains a token value.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from bridge.mcp.config import (
    McpConfigError,
    McpServerSpec,
    expand_env_refs,
    is_secret_key,
    load_catalog,
    save_catalog,
)
from bridge.mcp.connection import (
    DEFAULT_ELICITATION_TIMEOUT_S,
    McpConnection,
    State,
    describe_error,
    sdk_httpx,
)
from bridge.mcp.elicitation import ElicitationBridge, coerce_answer, parse_answer_tokens
from bridge.mcp.manager import McpManager, _tool_selected, scan_description_for_injection

logger = logging.getLogger(__name__)

VALID_ACTIONS = (
    "status", "enable", "disable", "reload", "login", "logout", "reauth",
    "add", "remove", "test", "tools", "catalog", "call", "answer", "approve",
)
SURFACES = ("desktop", "gateway", "test")
BACKGROUND_ACTIONS = frozenset({
    "enable", "disable", "reload", "login", "logout", "reauth",
    "add", "remove", "test", "catalog", "call", "approve",
})
"""Actions the desktop bridge runs as a background task (they may wait on
a connect, a network probe or a browser); the rest reply inline."""

DEFAULT_LOGIN_TIMEOUT_S = 300.0
DEFAULT_PROBE_TIMEOUT_S = 5.0
RESERVED_NAME_PREFIX = "mcp__"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_URL_RE = re.compile(r"^https?://[^/\s]+", re.IGNORECASE)
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata\s*=\s*"?([^",\s]+)"?', re.IGNORECASE)
_GENERIC_SLD = frozenset({"co", "com", "org", "net", "gov", "edu", "ac"})
_LAUNCHERS = frozenset({
    "npx", "uvx", "uv", "node", "python", "python3", "docker", "bunx", "pnpx", "deno", "npm",
})

EventSink = Callable[[dict[str, Any]], Any]
"""Receives bridge->UI events (``mcp_status`` / ``mcp_oauth_*``); sync or async."""

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

async def _emit(sink: EventSink | None, event: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        result = sink(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        logger.exception("mcp: event sink failed for %s", event.get("type"))


def spawn_background(coro: Awaitable[Any], *, name: str = "mcp-command") -> asyncio.Task[Any]:
    """Run *coro* as a task the caller does not await (long actions such
    as login/reauth must not block the bridge's stdin loop). Strong refs
    are kept until completion so tasks are never garbage-collected."""
    task = asyncio.ensure_future(coro)
    try:
        task.set_name(name)
    except Exception:  # noqa: BLE001
        pass
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def wait_background_tasks(timeout: float | None = None) -> None:
    """Await every outstanding background command task (tests/shutdown)."""
    pending = [t for t in _BACKGROUND_TASKS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


def tokenize(text: str) -> list[str]:
    """shlex split (quotes respected) with a plain-split fallback."""
    text = text or ""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


_SERVER_FIRST = frozenset({
    "enable", "disable", "login", "logout", "reauth", "remove", "test", "tools", "call",
})


def parse_mcp_command(text: str) -> dict[str, Any]:
    """Parse ``/mcp <action> [server] [args...]`` chat text into the
    ``mcp_command`` shape: ``{action, server?, tool?, args}``. Empty ->
    status. Actions that take a server name pull it from the first
    positional; ``add`` / ``catalog`` / ``answer`` keep everything in
    ``args`` (contract: args = remaining tokens after subcommand/server)."""
    text = text or ""
    head = text.split(None, 3)
    if head and head[0].lower() == "call" and len(head) == 4 and head[3].lstrip().startswith("{"):
        # ``call <server> <tool> {json}`` — keep the JSON object verbatim
        # (shlex would strip its quotes).
        return {"action": "call", "server": head[1], "tool": head[2], "args": [head[3].strip()]}
    tokens = tokenize(text)
    if not tokens:
        return {"action": "status", "args": []}
    action = tokens[0].lower()
    rest = tokens[1:]
    cmd: dict[str, Any] = {"action": action}
    if action in _SERVER_FIRST and rest and not rest[0].startswith("--"):
        cmd["server"] = rest[0]
        rest = rest[1:]
    if action == "status" and rest and not rest[0].startswith("--"):
        cmd["server"] = rest[0]
        rest = rest[1:]
    if action == "call" and rest:
        cmd["tool"] = rest[0]
        rest = rest[1:]
    cmd["args"] = rest
    return cmd


def parse_mcp_args(text: str) -> tuple[str, str]:
    """Split ``/mcp [action] [server]`` chat args. Empty -> status.
    (v1 helper; ``parse_mcp_command`` carries the full grammar.)"""
    cmd = parse_mcp_command(text)
    return cmd["action"], str(cmd.get("server") or "")


def missing_env_refs(
    spec: McpServerSpec, environ: dict[str, str] | None = None
) -> list[str]:
    """All ``${VAR}`` references in the spec (command/args/env/headers/url)
    that are unset in *environ* (default os.environ) and have no default."""
    env = os.environ if environ is None else environ
    values = [
        spec.command or "",
        *(spec.args or []),
        *(spec.env or {}).values(),
        *(spec.headers or {}).values(),
        spec.url or "",
    ]
    missing: set[str] = set()
    for value in values:
        missing.update(expand_env_refs(value, dict(env)).missing)
    return sorted(missing)


def _is_oauth_spec(spec: McpServerSpec) -> bool:
    auth = spec.auth
    if isinstance(auth, str):
        return auth.strip().lower() == "oauth"
    if isinstance(auth, dict):
        return str(auth.get("type", "")).strip().lower() == "oauth"
    return False


def auth_kind(spec: McpServerSpec) -> str:
    """``oauth`` | ``header`` | ``env`` | ``none`` — how the server authenticates."""
    if _is_oauth_spec(spec):
        return "oauth"
    for key, value in spec.headers.items():
        if key.lower() in ("authorization", "x-api-key") or is_secret_key(key) or "${" in value:
            return "header"
    for key, value in spec.env.items():
        if is_secret_key(key) or "${" in value:
            return "env"
    return "none"


def login_hint(server: str) -> str:
    return f"/mcp login {server}"


def _fmt_expiry(expires_at: float | None) -> str:
    if expires_at is None:
        return "unknown"
    remaining = expires_at - time.time()
    if remaining <= 0:
        return "expired"
    if remaining < 3600:
        return f"in {int(remaining // 60)}m"
    if remaining < 86400:
        return f"in {remaining / 3600:.1f}h"
    return f"in {remaining / 86400:.1f}d"


def _unknown_server(action: str, server: str, manager: McpManager) -> dict[str, Any]:
    known = ", ".join(sorted(manager.specs)) or "none configured"
    return {
        "ok": False,
        "action": action,
        "server": server,
        "message": f"unknown MCP server '{server}' (known: {known})",
    }


def _err(action: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "action": action, "message": message, **extra}


def _usage(action: str, text: str) -> dict[str, Any]:
    return {"ok": False, "action": action, "message": f"usage: {text}"}


# ---------------------------------------------------------------------------
# Status rows (v1 keys stable + v2 details)
# ---------------------------------------------------------------------------

def extended_status_rows(
    manager: McpManager, *, server: str | None = None
) -> list[dict[str, Any]]:
    """``manager.status_snapshot()`` rows plus: ``auth``, ``needs_auth``,
    ``needs_auth_reason``, ``login_hint``, ``has_tokens``,
    ``token_expires_at`` (oauth only), ``last_error``, and defaults for
    ``transport_in_use`` / ``rapid_drops`` / ``quarantined`` when no
    connection exists. Existing keys are never renamed."""
    rows: list[dict[str, Any]] = []
    specs = manager.specs
    for row in manager.status_snapshot():
        name = str(row.get("server"))
        if server and name != server:
            continue
        spec = specs.get(name)
        state = str(row.get("state") or "")
        row.setdefault("transport_in_use", row.get("transport"))
        row.setdefault("rapid_drops", 0)
        row.setdefault("quarantined", 0)
        row["auth"] = auth_kind(spec) if spec is not None else "none"
        needs_auth = state == "needs-auth"
        row["needs_auth"] = needs_auth
        row["needs_auth_reason"] = str(row.get("reason") or "") if needs_auth else None
        row["login_hint"] = (
            login_hint(name) if needs_auth and row["auth"] == "oauth" else None
        )
        if row["auth"] == "oauth":
            row["has_tokens"] = manager.has_tokens(name)
            row["token_expires_at"] = manager.token_expires_at(name)
        else:
            row["has_tokens"] = None
            row["token_expires_at"] = None
        row["last_error"] = (
            str(row.get("reason") or "")
            if state in ("failed", "backoff", "parked", "needs-auth") and row.get("reason")
            else None
        )
        rows.append(row)
    return rows


async def emit_status(emit: EventSink | None, manager: McpManager) -> None:
    """Full-snapshot ``mcp_status`` (contract shape: ``servers: [...]``)."""
    if emit is None:
        return
    await _emit(emit, {"type": "mcp_status", "servers": extended_status_rows(manager)})


def format_mcp_table(servers: list[dict[str, Any]]) -> str:
    """Compact fixed-width table: name, transport, state, tools, reason
    (design 4.4 — the /mcp chat surface and renderer settings panel)."""
    if not servers:
        return "no MCP servers configured"
    headers = ("name", "transport", "state", "tools", "reason")
    rows: list[tuple[str, ...]] = []
    for snap in servers:
        reason = str(snap.get("reason") or "-")
        if len(reason) > 48:
            reason = reason[:47] + "…"
        transport = str(snap.get("transport", "?"))
        in_use = snap.get("transport_in_use")
        if in_use and in_use != transport:
            transport = f"{transport}->{in_use}"
        rows.append((
            str(snap.get("server", "?")),
            transport,
            str(snap.get("state", "?")),
            str(snap.get("tool_count", 0)),
            reason,
        ))
    return _table(headers, rows)


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def _fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    lines = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    lines.extend(_fmt(row) for row in rows)
    return "\n".join(lines)


def format_status_details(rows: list[dict[str, Any]]) -> str:
    """One line per server that has something beyond the table worth
    saying: auth kind + token expiry, needs-auth hint, quarantined count,
    rapid drops, last error, protocol mode."""
    lines: list[str] = []
    for row in rows:
        name = row.get("server")
        bits: list[str] = []
        auth = row.get("auth")
        if auth and auth != "none":
            bit = f"auth={auth}"
            if auth == "oauth":
                if row.get("has_tokens"):
                    bit += f" (token expires {_fmt_expiry(row.get('token_expires_at'))})"
                else:
                    bit += " (no stored token)"
            bits.append(bit)
        if row.get("needs_auth"):
            hint = row.get("login_hint") or "check ~/.freyja/.env"
            bits.append(f"needs-auth → {hint}")
        if row.get("protocol_mode"):
            bits.append(f"protocol={row['protocol_mode']}")
        if row.get("quarantined"):
            bits.append(f"quarantined={row['quarantined']} (/mcp tools {name})")
        if row.get("rapid_drops"):
            bits.append(f"rapid_drops={row['rapid_drops']}")
        if row.get("last_error") and not row.get("needs_auth"):
            bits.append(f"last_error={row['last_error']}")
        if bits:
            lines.append(f"{name}: " + "; ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Login flows per surface
# ---------------------------------------------------------------------------

class SurfaceLoginFlow:
    """LoginFlow adapter for the bridge surfaces.

    desktop (``open_browser=True``): wraps ``BrowserFlow`` — the bridge runs
    on the same Mac as the user, so it launches the URL itself — and emits
    ``mcp_oauth_url`` with ``opened`` reflecting whether that worked, so the
    UI can show a waiting toast with a fallback link.

    gateway/test (``open_browser=False``): pure hand-off — the URL is only
    emitted (``opened=false``); the gateway posts it into the chat.
    """

    def __init__(
        self,
        *,
        emit: EventSink | None,
        timeout_s: float,
        open_browser: bool,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._emit_sink = emit
        self.timeout_s = timeout_s
        self.paste_source = None
        self.redirect_uri: str | None = None
        self.last_url: str | None = None
        self.opened = False
        self.urls: list[str] = []
        self._inner: Any = None
        if open_browser:
            from bridge.mcp.oauth.flow import BrowserFlow

            self._inner = BrowserFlow(opener=opener)

    async def on_authorize_url(self, url: str, *, server_name: str, redirect_uri: str) -> None:
        self.last_url = url
        self.urls.append(url)
        if self._inner is not None:
            await self._inner.on_authorize_url(
                url, server_name=server_name, redirect_uri=redirect_uri
            )
            self.opened = bool(self._inner.opened)
        await _emit(self._emit_sink, {
            "type": "mcp_oauth_url",
            "server": server_name,
            "url": url,
            "redirectUri": redirect_uri,
            "expiresInS": self.timeout_s,
            "opened": self.opened,
        })


def make_login_flow(
    surface: str,
    *,
    emit: EventSink | None,
    timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
    opener: Callable[[str], bool] | None = None,
) -> SurfaceLoginFlow:
    return SurfaceLoginFlow(
        emit=emit, timeout_s=timeout_s, open_browser=(surface == "desktop"), opener=opener,
    )


# ---------------------------------------------------------------------------
# Manager hooks for the bridge (auth factory + elicitation bridge)
# ---------------------------------------------------------------------------

def make_auth_factory(token_root: Path | str | None = None) -> Callable[[McpServerSpec, bool], Any]:
    """``AuthFactory`` for McpManager: OAuth servers get the SDK provider
    built NON-interactively regardless of the *interactive* flag — a
    missing token at connect time means needs-auth, never a surprise
    browser (``/mcp login`` is the only interactive path). Non-OAuth specs
    -> None. *token_root* must match the manager's ``token_root`` (None =
    ``$FREYJA_HOME/mcp-tokens``). The OAuth package (mcp SDK + httpx2) is
    imported on first use only so bridge startup stays cheap."""

    def _factory(spec: McpServerSpec, interactive: bool = False) -> Any:
        if not _is_oauth_spec(spec) or not spec.url:
            return None
        from bridge.mcp.oauth import build_httpx_auth

        return build_httpx_auth(spec, interactive=False, storage_root=token_root)

    _factory.token_root = token_root  # type: ignore[attr-defined]
    return _factory


non_interactive_auth_factory = make_auth_factory(None)
"""Default auth factory (tokens under ``$FREYJA_HOME/mcp-tokens``)."""


def manager_hooks(
    emit: EventSink,
    *,
    elicitation_timeout_s: float = DEFAULT_ELICITATION_TIMEOUT_S,
    token_root: Path | str | None = None,
) -> dict[str, Any]:
    """kwargs for ``McpManager.load``: the non-interactive auth factory and
    an ``ElicitationBridge`` that emits ``mcp_elicitation`` through *emit*
    and is settled by ``resolve_elicitation_response``."""
    hooks: dict[str, Any] = {
        "auth_factory": make_auth_factory(token_root),
        "approval_handler": ElicitationBridge(emit, timeout_s=elicitation_timeout_s),
        "elicitation_timeout_s": elicitation_timeout_s,
    }
    if token_root is not None:
        hooks["token_root"] = token_root
    return hooks


def elicitation_bridge_of(manager: McpManager | None) -> ElicitationBridge | None:
    handler = getattr(manager, "approval_handler", None)
    return handler if isinstance(handler, ElicitationBridge) else None


def resolve_elicitation_response(manager: McpManager | None, cmd: dict[str, Any]) -> bool:
    """Handle a UI ``mcp_elicitation_response`` event. False when the
    request id is unknown/stale or no bridge is wired."""
    bridge = elicitation_bridge_of(manager)
    if bridge is None:
        return False
    return bridge.resolve(
        str(cmd.get("requestId") or ""), str(cmd.get("action") or ""), cmd.get("content"),
    )


# ---------------------------------------------------------------------------
# add: argument parsing + discovery probe
# ---------------------------------------------------------------------------

@dataclass
class AddRequest:
    positionals: list[str] = field(default_factory=list)
    name: str | None = None
    transport: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    enable: bool = False
    trust: str | None = None
    tier: str | None = None


ADD_USAGE = (
    "add <url | command ...> [--name N] [--transport http|sse|stdio] "
    "[--header K=V]* [--env K=${VAR}]* [--enable] [--trust trusted|standard|untrusted] "
    "[-- <args passed to the command verbatim>]"
)


def _kv(flag: str, raw: str) -> tuple[str, str]:
    sep = "=" if "=" in raw else (":" if ":" in raw else None)
    if sep is None:
        raise ValueError(f"{flag} expects KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition(sep)
    key = key.strip()
    if not key:
        raise ValueError(f"{flag} expects KEY=VALUE, got {raw!r}")
    return key, value.strip()


def parse_add_args(args: list[str]) -> AddRequest:
    """Hand-rolled (argparse exits the process on error). Recognized flags
    may appear anywhere before a literal ``--``; everything after ``--``
    is a verbatim command argument (so ``add npx -- -y pkg --enable``
    hands ``--enable`` to npx)."""
    req = AddRequest()
    i = 0
    passthrough = False
    while i < len(args):
        tok = args[i]
        if passthrough:
            req.positionals.append(tok)
            i += 1
            continue
        if tok == "--":
            passthrough = True
            i += 1
            continue
        flag, eq, inline = tok.partition("=")
        if flag in ("--name", "--transport", "--header", "--env", "--trust", "--tier"):
            if eq:
                value = inline
            else:
                if i + 1 >= len(args):
                    raise ValueError(f"{flag} requires a value")
                i += 1
                value = args[i]
            if flag == "--name":
                req.name = value.strip()
            elif flag == "--transport":
                req.transport = value.strip().lower()
            elif flag == "--header":
                key, val = _kv(flag, value)
                req.headers[key] = val
            elif flag == "--env":
                key, val = _kv(flag, value)
                req.env[key] = val
            elif flag == "--trust":
                req.trust = value.strip().lower()
            else:
                req.tier = value.strip().lower()
        elif tok == "--enable":
            req.enable = True
        elif tok in ("--disable", "--disabled"):
            req.enable = False
        elif tok.startswith("--") and not req.positionals:
            raise ValueError(f"unknown option {tok!r} — {ADD_USAGE}")
        else:
            req.positionals.append(tok)
        i += 1
    return req


def sanitize_server_name(label: str) -> str:
    out = re.sub(r"[^a-z0-9_-]+", "-", label.strip().lower()).strip("-_")
    return out or "server"


def default_server_name_for_url(url: str) -> str:
    """``https://mcp.atlassian.com/v2/mcp`` -> ``atlassian`` (the host's
    registrable label; generic second-level labels such as ``co`` in
    ``example.co.uk`` are skipped). IPs / localhost sanitize verbatim."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "server"
    if host == "localhost" or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return sanitize_server_name(host)
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in _GENERIC_SLD:
        return sanitize_server_name(parts[-3])
    if len(parts) >= 2:
        return sanitize_server_name(parts[-2])
    return sanitize_server_name(parts[0])


def default_server_name_for_command(command: str, args: list[str]) -> str:
    """``npx -y @modelcontextprotocol/server-filesystem@2026.7.10`` ->
    ``server-filesystem``; ``uvx mcp-server-fetch==2026.8.18`` ->
    ``mcp-server-fetch``; otherwise the command's basename."""
    base = Path(command).name.lower()
    if base in _LAUNCHERS:
        for arg in args:
            if arg.startswith("-") or arg in ("run", "exec", "install"):
                continue
            pkg = arg.split("/")[-1] if arg.startswith("@") else arg
            pkg = re.split(r"(?<=.)[@=]", pkg)[0]
            pkg = Path(pkg).name
            if pkg:
                return sanitize_server_name(pkg)
    return sanitize_server_name(base or command)


def name_hint(header: str) -> str:
    """``Authorization`` -> ``MCP_AUTHORIZATION_TOKEN``-style env var suggestion."""
    return "MCP_" + re.sub(r"[^A-Z0-9]+", "_", header.upper()).strip("_") + "_TOKEN"


def validate_server_name(name: str, existing: set[str]) -> str | None:
    if not name:
        return "server name must not be empty"
    if name.lower().startswith(RESERVED_NAME_PREFIX):
        return f"server names starting with '{RESERVED_NAME_PREFIX}' are reserved"
    if not _NAME_RE.match(name):
        return (
            f"invalid server name {name!r}: use letters, digits, '.', '_' or '-' "
            "(max 64 chars, must start alphanumeric)"
        )
    if name in existing:
        return f"server '{name}' already exists — /mcp remove {name} first"
    return None


@dataclass
class ProbeResult:
    """Outcome of the unauthenticated ``initialize`` probe against a URL."""

    url: str
    reachable: bool = False
    status: int | None = None
    transport: str | None = None
    """``http`` (streamable) or ``sse`` when the endpoint answered like an MCP server."""
    auth: str = "unknown"
    """``none`` | ``oauth`` | ``bearer`` (401 without resource metadata) | ``unknown``."""
    resource_metadata_url: str | None = None
    www_authenticate: str | None = None
    server_info: dict[str, Any] | None = None
    protocol_version: str | None = None
    error: str | None = None
    elapsed_ms: float = 0.0
    sse_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def summary(self) -> str:
        if not self.reachable:
            return f"unreachable ({self.error or 'no response'})"
        bits = [f"HTTP {self.status}"]
        if self.transport:
            bits.append(f"transport={self.transport}")
        bits.append(f"auth={self.auth}")
        if self.server_info and self.server_info.get("name"):
            ver = self.server_info.get("version")
            bits.append(f"server={self.server_info['name']}" + (f" {ver}" if ver else ""))
        if self.protocol_version:
            bits.append(f"protocol={self.protocol_version}")
        if self.error:
            bits.append(self.error)
        return ", ".join(bits) + f" in {self.elapsed_ms:.0f}ms"


def _initialize_body() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "freyja", "version": "mcp-add-probe"},
        },
    }


def parse_resource_metadata(www_authenticate: str | None) -> str | None:
    if not www_authenticate:
        return None
    match = _RESOURCE_METADATA_RE.search(www_authenticate)
    return match.group(1) if match else None


def _classify_auth(probe: ProbeResult, headers: Any) -> None:
    www = headers.get("www-authenticate") if headers is not None else None
    probe.www_authenticate = www
    probe.resource_metadata_url = parse_resource_metadata(www)
    probe.auth = "oauth" if probe.resource_metadata_url else "bearer"


def _parse_initialize_response(probe: ProbeResult, response: Any) -> None:
    ctype = str(response.headers.get("content-type") or "").lower()
    doc: Any = None
    try:
        text = response.text
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:"):
                    doc = json.loads(line[5:].strip())
                    break
        elif text.strip():
            doc = json.loads(text)
    except Exception:  # noqa: BLE001
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("result"), dict):
        result = doc["result"]
        info = result.get("serverInfo")
        probe.server_info = info if isinstance(info, dict) else None
        version = result.get("protocolVersion")
        probe.protocol_version = str(version) if version else None


async def probe_http_server(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Discovery probe (hermes ``mcp add`` connect-first, without a
    session): POST an unauthenticated JSON-RPC ``initialize``.
    200 -> reachable, auth none; 401/403 -> auth required (``oauth`` when
    WWW-Authenticate carries ``resource_metadata``, else ``bearer``);
    404/405 -> retry as legacy SSE (GET, Accept: text/event-stream);
    connection errors -> unreachable. Uses the SDK's httpx module."""
    httpx_mod = sdk_httpx()
    probe = ProbeResult(url=url)
    started = time.monotonic()
    base_headers = dict(headers or {})
    post_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **base_headers,
    }
    try:
        async with httpx_mod.AsyncClient(
            timeout=httpx_mod.Timeout(timeout_s), follow_redirects=True
        ) as client:
            try:
                response = await client.post(url, json=_initialize_body(), headers=post_headers)
            except Exception as exc:  # noqa: BLE001
                probe.error = f"{type(exc).__name__}: {exc}".strip(": ")
                return probe
            probe.reachable = True
            probe.status = int(response.status_code)
            status = probe.status
            if status in (401, 403):
                probe.transport = "http"
                _classify_auth(probe, response.headers)
                return probe
            if 200 <= status < 300:
                probe.transport = "http"
                probe.auth = "none"
                _parse_initialize_response(probe, response)
                return probe
            if status in (404, 405):
                get_headers = {"Accept": "text/event-stream", **base_headers}
                try:
                    async with client.stream("GET", url, headers=get_headers) as sse:
                        probe.sse_status = int(sse.status_code)
                        ctype = str(sse.headers.get("content-type") or "").lower()
                        if sse.status_code == 200 and "text/event-stream" in ctype:
                            probe.transport = "sse"
                            probe.auth = "none"
                        elif sse.status_code in (401, 403):
                            probe.transport = "sse"
                            _classify_auth(probe, sse.headers)
                        else:
                            probe.error = (
                                f"initialize answered HTTP {status} and the SSE GET answered "
                                f"HTTP {sse.status_code} — not an MCP endpoint?"
                            )
                except Exception as exc:  # noqa: BLE001
                    probe.error = f"SSE probe failed: {type(exc).__name__}: {exc}".strip(": ")
                return probe
            probe.error = f"unexpected HTTP {status} from initialize"
            return probe
    finally:
        probe.elapsed_ms = (time.monotonic() - started) * 1000.0


# ---------------------------------------------------------------------------
# test <server>: isolated one-shot diagnostics
# ---------------------------------------------------------------------------

def _is_method_not_found(exc: BaseException) -> bool:
    code = getattr(getattr(exc, "error", None), "code", None)
    return code == -32601 or "method not found" in str(exc).lower()


async def run_server_test(
    manager: McpManager,
    spec: McpServerSpec,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Connect an ISOLATED McpConnection (same spec, manager's auth factory,
    non-interactive, no registry hooks, single attempt), run initialize ->
    tools/list -> ping, and always tear it down. Returns the report dict
    (also rendered by :func:`format_test_report`)."""
    probe_spec = dataclasses.replace(spec, enabled=True)
    conn = McpConnection(
        probe_spec,
        auth_factory=manager.auth_factory,
        auth_interactive=False,
        max_reconnects=0,
        watchdog=False,
        keepalive_interval_s=3600.0,
        keepalive_timeout_s=min(10.0, float(spec.connect_timeout_s)),
    )
    budget = timeout_s if timeout_s is not None else float(spec.connect_timeout_s) + 10.0
    report: dict[str, Any] = {
        "server": spec.name,
        "transport": spec.transport,
        "transport_in_use": spec.transport,
        "protocol_mode": None,
        "protocol_version": None,
        "server_info": None,
        "state": None,
        "reason": "",
        "ok": False,
        "steps": [],
        "tool_count": 0,
        "tools": [],
        "filtered_out": [],
        "quarantined": [],
        "auth": {
            "kind": auth_kind(spec),
            "needs_auth": False,
            "login_hint": None,
            "has_tokens": manager.has_tokens(spec.name) if _is_oauth_spec(spec) else None,
            "token_expires_at": (
                manager.token_expires_at(spec.name) if _is_oauth_spec(spec) else None
            ),
        },
        "elapsed_ms": 0.0,
    }
    started = time.monotonic()

    def _step(name: str, ok: bool, elapsed_ms: float, detail: str = "") -> None:
        report["steps"].append({
            "name": name, "ok": ok, "elapsed_ms": round(elapsed_ms, 1), "detail": detail,
        })

    try:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(conn.start(), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            _step(
                "connect", False, (time.monotonic() - t0) * 1000.0,
                f"timed out after {budget:g}s",
            )
            report["state"] = "timeout"
            report["reason"] = f"no answer within {budget:g}s"
            return report
        connect_ms = (time.monotonic() - t0) * 1000.0
        report["state"] = conn.state.value
        report["reason"] = conn.reason
        report["transport_in_use"] = conn.transport_in_use
        report["protocol_mode"] = conn.protocol_mode or None
        if conn.state is State.NEEDS_AUTH:
            report["auth"]["needs_auth"] = True
            report["auth"]["login_hint"] = (
                login_hint(spec.name) if _is_oauth_spec(spec)
                else "set the missing ${VAR} in ~/.freyja/.env"
            )
            _step("connect", False, connect_ms, conn.reason)
            return report
        if conn.state is not State.ACTIVE:
            _step("connect", False, connect_ms, conn.reason)
            return report
        handshake_ms = conn.last_latency_ms or connect_ms
        session = conn._session  # noqa: SLF001 — diagnostics need the negotiated version
        version = getattr(session, "_negotiated_version", None)
        init_result = getattr(session, "_initialize_result", None)
        info = getattr(init_result, "server_info", None) or getattr(init_result, "serverInfo", None)
        if info is not None:
            report["server_info"] = {
                "name": getattr(info, "name", None),
                "version": getattr(info, "version", None),
            }
        report["protocol_version"] = str(version) if version else None
        _step(
            "initialize", True, handshake_ms,
            f"{conn.protocol_mode}" + (f", protocol {version}" if version else ""),
        )

        t1 = time.monotonic()
        try:
            await asyncio.wait_for(conn.refresh_tools("test"), timeout=budget)
            _step(
                "tools/list", True, (time.monotonic() - t1) * 1000.0,
                f"{len(conn.tools)} tool(s)",
            )
        except Exception as exc:  # noqa: BLE001
            _step("tools/list", False, (time.monotonic() - t1) * 1000.0, describe_error(exc))

        t2 = time.monotonic()
        try:
            if session is None:
                raise RuntimeError("session vanished")
            await asyncio.wait_for(session.send_ping(), timeout=min(10.0, budget))
            _step("ping", True, (time.monotonic() - t2) * 1000.0, "")
        except Exception as exc:  # noqa: BLE001
            if _is_method_not_found(exc):
                _step(
                    "ping", True, (time.monotonic() - t2) * 1000.0,
                    "unsupported (-32601); keepalive uses tools/list",
                )
            else:
                _step("ping", False, (time.monotonic() - t2) * 1000.0, describe_error(exc))

        names: list[str] = []
        for tool in conn.tools:
            remote = getattr(tool, "name", "") or ""
            if not remote:
                continue
            if not _tool_selected(remote, spec):
                report["filtered_out"].append(remote)
                continue
            text = f"{getattr(tool, 'title', '') or ''}\n{getattr(tool, 'description', '') or ''}"
            hit = scan_description_for_injection(text)
            if hit is not None:
                report["quarantined"].append({"tool": remote, "pattern": hit})
                continue
            names.append(remote)
        report["tool_count"] = len(names)
        report["tools"] = names[:10]
        report["ok"] = all(s["ok"] for s in report["steps"])
        return report
    finally:
        try:
            await asyncio.wait_for(conn.stop(), timeout=15.0)
        except Exception:  # noqa: BLE001
            logger.exception("mcp test '%s': teardown failed", spec.name)
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        report["state"] = report["state"] or conn.state.value


def format_test_report(report: dict[str, Any]) -> str:
    name = report.get("server")
    verdict = "OK" if report.get("ok") else "FAILED"
    lines = [f"test '{name}': {verdict} ({report.get('elapsed_ms', 0):.0f}ms total)"]
    transport = report.get("transport")
    in_use = report.get("transport_in_use")
    tline = f"transport: {transport}"
    if in_use and in_use != transport:
        tline += f" (in use: {in_use})"
    if report.get("protocol_mode"):
        tline += f", mode={report['protocol_mode']}"
    if report.get("protocol_version"):
        tline += f", protocol={report['protocol_version']}"
    lines.append(tline)
    info = report.get("server_info") or {}
    if info.get("name"):
        version = f" {info['version']}" if info.get("version") else ""
        lines.append(f"server: {info['name']}{version}")
    auth = report.get("auth") or {}
    aline = f"auth: {auth.get('kind')}"
    if auth.get("kind") == "oauth":
        aline += (
            f" (token expires {_fmt_expiry(auth.get('token_expires_at'))})"
            if auth.get("has_tokens") else " (no stored token)"
        )
    if auth.get("needs_auth"):
        aline += f" — needs-auth → {auth.get('login_hint')}"
    lines.append(aline)
    for step in report.get("steps") or []:
        mark = "ok" if step["ok"] else "FAIL"
        detail = f" — {step['detail']}" if step.get("detail") else ""
        lines.append(f"  {step['name']:<11} {mark:<4} {step['elapsed_ms']:>7.1f}ms{detail}")
    if report.get("state") and not report.get("ok") and not report.get("steps"):
        lines.append(f"state: {report['state']} — {report.get('reason')}")
    if report.get("tool_count") or report.get("tools"):
        shown = ", ".join(report.get("tools") or [])
        more = report["tool_count"] - len(report.get("tools") or [])
        lines.append(
            f"tools: {report['tool_count']}" + (f" — {shown}" if shown else "")
            + (f" (+{more} more)" if more > 0 else "")
        )
    if report.get("filtered_out"):
        lines.append(f"filtered by include/exclude: {', '.join(report['filtered_out'])}")
    if report.get("quarantined"):
        lines.append(
            "quarantined (injection scan): "
            + ", ".join(f"{q['tool']} [{q['pattern']}]" for q in report["quarantined"])
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tools / catalog formatting
# ---------------------------------------------------------------------------

def tools_rows(manager: McpManager, server: str | None = None) -> list[dict[str, Any]]:
    from bridge.mcp.proxy_tool import resolve_permission_level

    rows: list[dict[str, Any]] = []
    for proxy in manager.proxy_tools():
        if server and proxy.server != server:
            continue
        level = resolve_permission_level(proxy.spec, proxy.remote_name)
        definition = proxy.definition
        rows.append({
            "server": proxy.server,
            "tool": definition.name,
            "remote": proxy.remote_name,
            "summary": definition.summary,
            "tier": str(getattr(definition.tier, "value", definition.tier)),
            "permission": getattr(level, "value", str(level)) if level is not None else "none",
        })
    rows.sort(key=lambda r: (r["server"], r["tool"]))
    return rows


def format_tools_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no registered MCP tools"
    table_rows = []
    for row in rows:
        summary = str(row.get("summary") or "")
        if len(summary) > 60:
            summary = summary[:59] + "…"
        table_rows.append((
            str(row["tool"]), str(row["tier"]), str(row["permission"]), summary,
        ))
    return _table(("tool", "tier", "permission", "summary"), table_rows)


def format_quarantine(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = ["pending quarantine (injection scan refused to register):"]
    for entry in entries:
        lines.append(
            f"  {entry.get('server')}/{entry.get('tool')} — pattern {entry.get('pattern')!r}"
        )
    first = entries[0]
    lines.append(
        f"review the tool's description, then /mcp approve {first.get('server')} "
        f"{first.get('tool')} to register it"
    )
    return "\n".join(lines)


def format_catalog_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no catalog entries"
    table_rows = []
    for row in rows:
        desc = str(row.get("description") or "")
        if len(desc) > 56:
            desc = desc[:55] + "…"
        table_rows.append((
            str(row.get("name")), str(row.get("transport")), str(row.get("auth")), desc,
        ))
    return _table(("name", "transport", "auth", "description"), table_rows)


def format_catalog_info(info: dict[str, Any]) -> str:
    lines = [f"{info['name']} — {info.get('description', '')}".rstrip(" —")]
    lines.append(
        f"transport: {info.get('transport')}  auth: {info.get('auth')}  "
        f"verified: {info.get('verified')}"
    )
    if info.get("homepage"):
        lines.append(f"homepage: {info['homepage']}")
    detail = info.get("transport_detail") or {}
    if detail.get("url"):
        lines.append(f"url: {detail['url']}")
    if detail.get("command"):
        lines.append(f"command: {detail['command']} {' '.join(detail.get('args') or [])}".rstrip())
    if info.get("required_secrets"):
        secrets = ", ".join(info["required_secrets"])
        lines.append(f"required secrets (set in ~/.freyja/.env): {secrets}")
    if info.get("required_env"):
        extra = [e for e in info["required_env"] if e not in (info.get("required_secrets") or [])]
        if extra:
            lines.append(f"required env: {', '.join(extra)}")
    tools = info.get("tools") or {}
    if tools.get("default_excluded"):
        lines.append(f"excluded by default: {', '.join(tools['default_excluded'])}")
    if info.get("tags"):
        lines.append(f"tags: {', '.join(info['tags'])}")
    if info.get("setup_notes"):
        lines.append(f"notes: {str(info['setup_notes']).strip()}")
    lines.append(f"install: /mcp catalog install {info['name']} [--enable] [--as NAME]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def handle_mcp_command(
    manager: McpManager,
    cmd: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    surface: str = "test",
    flow: Any = None,
    emit: EventSink | None = None,
    elicitation: ElicitationBridge | None = None,
    token_root: Path | str | None = None,
    login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    opener: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Dispatch one mcp_command. Always returns a dict with at least
    ``ok`` and ``action``; ``servers`` carries status rows where it makes
    sense, ``rows``/``data`` the action-specific structured payload and
    ``message`` the human-readable text.

    *surface* selects the login flow (``desktop`` opens the browser,
    ``gateway``/``test`` hand the URL off); *flow* overrides it entirely
    (tests). *emit* receives ``mcp_status`` / ``mcp_oauth_url`` /
    ``mcp_oauth_result`` events. *token_root* overrides the OAuth token
    directory (default: the manager's ``token_root``).
    """
    action = str(cmd.get("action") or "status").strip().lower()
    server = str(cmd.get("server") or "").strip()
    raw_args = cmd.get("args")
    args: list[str] = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
    if surface not in SURFACES:
        surface = "test"
    ctx = _Ctx(
        manager=manager, environ=environ, surface=surface, flow=flow, emit=emit,
        elicitation=elicitation or elicitation_bridge_of(manager),
        token_root=Path(token_root) if token_root is not None else manager.token_root,
        login_timeout_s=login_timeout_s, probe_timeout_s=probe_timeout_s, opener=opener,
    )
    try:
        if action == "status":
            return _status(ctx, server or None)
        if action == "reload":
            return await _reload(ctx)
        if action in ("enable", "disable", "login", "logout", "test", "tools", "remove"):
            if action == "tools" and not server:
                return _tools(ctx, None)
            if not server:
                extra = " [--purge]" if action == "remove" else ""
                return _usage(action, f"{action} <server>{extra}")
            spec = manager.specs.get(server)
            if spec is None:
                return _unknown_server(action, server, manager)
            if action == "enable":
                return await _enable(ctx, server)
            if action == "disable":
                return await _disable(ctx, server)
            if action == "login":
                return await _login(ctx, spec, action="login")
            if action == "logout":
                return await _logout(ctx, spec)
            if action == "test":
                return await _test(ctx, spec)
            if action == "tools":
                return _tools(ctx, server)
            return await _remove(ctx, server, purge="--purge" in args)
        if action == "reauth":
            return await _reauth(ctx, server, args)
        if action == "add":
            return await _add(ctx, args)
        if action == "catalog":
            return await _catalog(ctx, args)
        if action == "call":
            return await _call(ctx, server, cmd, args)
        if action == "answer":
            return _answer(ctx, args)
        if action == "approve":
            return await _approve(ctx, server, args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("mcp_command %r failed", action)
        return {
            "ok": False,
            "action": action,
            "server": server or None,
            "message": f"{action} failed: {describe_error(exc)}",
        }
    return {
        "ok": False,
        "action": action,
        "message": (
            f"unknown mcp_command action '{action}' "
            f"(expected one of {', '.join(VALID_ACTIONS)})"
        ),
    }


@dataclass
class _Ctx:
    manager: McpManager
    environ: dict[str, str] | None
    surface: str
    flow: Any
    emit: EventSink | None
    elicitation: ElicitationBridge | None
    token_root: Path
    login_timeout_s: float
    probe_timeout_s: float
    opener: Callable[[str], bool] | None


# -- status / enable / disable / reload ----------------------------------------

def _status(ctx: _Ctx, server: str | None) -> dict[str, Any]:
    rows = extended_status_rows(ctx.manager, server=server)
    if server and not rows:
        return _unknown_server("status", server, ctx.manager)
    message = format_mcp_table(rows)
    details = format_status_details(rows)
    if details:
        message += "\n" + details
    quarantine = ctx.manager.pending_quarantine()
    if quarantine and not server:
        note = f"{len(quarantine)} quarantined tool(s) — /mcp tools"
        details = (details + "\n" if details else "") + note
        message = format_mcp_table(rows) + "\n" + details
    return {
        "ok": True,
        "action": "status",
        "servers": rows,
        "message": message,
        "data": {"table": format_mcp_table(rows), "details": details},
    }


async def _reload(ctx: _Ctx) -> dict[str, Any]:
    summary = await ctx.manager.reload()
    parts = []
    for key in ("added", "removed", "changed"):
        names = summary.get(key) or []
        if names:
            parts.append(f"{key}: {', '.join(names)}")
    message = summary.get("message") or ("; ".join(parts) if parts else "no catalog changes")
    await emit_status(ctx.emit, ctx.manager)
    return {
        "ok": True,
        "action": "reload",
        "added": summary.get("added", []),
        "removed": summary.get("removed", []),
        "changed": summary.get("changed", []),
        "message": message,
        "servers": extended_status_rows(ctx.manager),
    }


async def _enable(ctx: _Ctx, server: str) -> dict[str, Any]:
    snapshot = await ctx.manager.enable(server)
    await emit_status(ctx.emit, ctx.manager)
    state = snapshot.get("state", "?")
    tools = snapshot.get("tool_count", 0)
    message = f"'{server}' enabled — state: {state} ({tools} tool(s))"
    if state == "needs-auth":
        message += f" — {snapshot.get('reason')}"
    return {
        "ok": True,
        "action": "enable",
        "server": server,
        "message": message,
        "servers": extended_status_rows(ctx.manager, server=server),
    }


async def _disable(ctx: _Ctx, server: str) -> dict[str, Any]:
    await ctx.manager.disable(server)
    await emit_status(ctx.emit, ctx.manager)
    return {
        "ok": True,
        "action": "disable",
        "server": server,
        "message": f"'{server}' disabled — tools unregistered",
        "servers": extended_status_rows(ctx.manager, server=server),
    }


# -- login / logout / reauth ---------------------------------------------------

def _not_oauth_login(ctx: _Ctx, spec: McpServerSpec, action: str) -> dict[str, Any]:
    server = spec.name
    if spec.oauth is not None or spec.auth is not None:
        return {
            "ok": False,
            "action": action,
            "server": server,
            "message": (
                f"'{server}' is not configured for OAuth (auth={spec.auth!r}); set "
                f"\"auth\": \"oauth\" on the server in mcp.json (keeping its \"oauth\" "
                f"block) and run /mcp reload, then {login_hint(server)}"
            ),
        }
    missing = missing_env_refs(spec, ctx.environ)
    if missing:
        keys = ", ".join(missing)
        return {
            "ok": False,
            "action": action,
            "server": server,
            "missing_env": missing,
            "message": (
                f"'{server}' is not an OAuth server; it is missing env var(s): {keys} — "
                f"add to ~/.freyja/.env, then run: /mcp reload (or /mcp enable {server})"
            ),
        }
    return {
        "ok": True,
        "action": action,
        "server": server,
        "missing_env": [],
        "message": (
            f"'{server}' is not an OAuth server and has no missing env vars; if it "
            f"still fails, check the values in ~/.freyja/.env and run: /mcp reload"
        ),
    }


async def _login(ctx: _Ctx, spec: McpServerSpec, *, action: str) -> dict[str, Any]:
    manager = ctx.manager
    server = spec.name
    if not _is_oauth_spec(spec):
        return _not_oauth_login(ctx, spec, action)
    if not spec.url:
        return {
            "ok": False, "action": action, "server": server,
            "message": f"'{server}' has no url — OAuth needs an http/sse server",
        }
    if server in manager.logins_in_progress:
        return {
            "ok": False,
            "action": action,
            "server": server,
            "message": f"login already in progress for '{server}' — finish it in the browser first",
        }
    from bridge.mcp.oauth.flow import run_login

    manager.logins_in_progress.add(server)
    try:
        flow = ctx.flow or make_login_flow(
            ctx.surface, emit=ctx.emit, timeout_s=ctx.login_timeout_s, opener=ctx.opener,
        )
        result = await run_login(
            spec, flow=flow, storage_root=ctx.token_root, timeout=ctx.login_timeout_s,
            environ=ctx.environ,
        )
        await _emit(ctx.emit, {
            "type": "mcp_oauth_result",
            "server": server,
            "ok": bool(result.ok),
            "error": result.error,
            "expiresAt": result.expires_at,
            "scopes": list(result.scopes or []),
        })
        if not result.ok:
            return {
                "ok": False,
                "action": action,
                "server": server,
                "message": f"login failed for '{server}': {result.error}",
                "data": {"skipped": result.skipped, "error": result.error},
            }
        if spec.enabled:
            snapshot = await manager.reconnect(server, f"{action} completed")
        else:
            snapshot = await manager.enable(server)
    finally:
        manager.logins_in_progress.discard(server)
    await emit_status(ctx.emit, manager)
    state = str(snapshot.get("state", "?"))
    tools = snapshot.get("tool_count", 0)
    ok = state not in ("needs-auth", "failed")
    message = (
        f"'{server}' authenticated (token expires {_fmt_expiry(result.expires_at)}"
        + (f", scopes: {' '.join(result.scopes)}" if result.scopes else "")
        + f") — state: {state} ({tools} tool(s))"
    )
    if not ok:
        message += f" — {snapshot.get('reason')}"
    return {
        "ok": ok,
        "action": action,
        "server": server,
        "message": message,
        "servers": extended_status_rows(manager, server=server),
        "data": {
            "expires_at": result.expires_at,
            "scopes": list(result.scopes or []),
            "client_id": result.client_id,
            "state": state,
        },
    }


async def _logout(ctx: _Ctx, spec: McpServerSpec) -> dict[str, Any]:
    server = spec.name
    if not _is_oauth_spec(spec):
        return {
            "ok": False, "action": "logout", "server": server,
            "message": (
                f"'{server}' is not an OAuth server (auth={spec.auth!r}); nothing to log out"
            ),
        }
    from bridge.mcp.oauth.storage import FreyjaTokenStorage

    storage = FreyjaTokenStorage(server, root=ctx.token_root)
    had_tokens = storage.has_cached_tokens()
    storage.remove()
    logger.info("mcp oauth '%s': stored OAuth state removed by /mcp logout", server)
    snapshot = None
    if spec.enabled and ctx.manager.connection(server) is not None:
        snapshot = await ctx.manager.reconnect(server, "logout")
    await emit_status(ctx.emit, ctx.manager)
    removed = "removed" if had_tokens else "was already empty"
    message = f"'{server}' logged out — stored OAuth state {removed}"
    if snapshot is not None:
        message += f"; state: {snapshot.get('state')}"
    message += f". To sign in again: {login_hint(server)}"
    return {
        "ok": True,
        "action": "logout",
        "server": server,
        "message": message,
        "servers": extended_status_rows(ctx.manager, server=server),
    }


async def _reauth(ctx: _Ctx, server: str, args: list[str]) -> dict[str, Any]:
    manager = ctx.manager
    do_all = "--all" in args or server == "--all"
    if not do_all:
        if not server:
            return _usage("reauth", "reauth <server> | reauth --all")
        spec = manager.specs.get(server)
        if spec is None:
            return _unknown_server("reauth", server, manager)
        return await _login(ctx, spec, action="reauth")
    targets = [s for s in manager.specs.values() if _is_oauth_spec(s) and s.url and s.enabled]
    if not targets:
        return {
            "ok": True, "action": "reauth",
            "message": "no enabled OAuth servers to re-authenticate",
            "rows": [],
        }
    rows: list[dict[str, Any]] = []
    lines = [f"re-authenticating {len(targets)} OAuth server(s) one at a time…"]
    succeeded = 0
    for spec in targets:  # serial by design: one browser flow at a time
        result = await _login(ctx, spec, action="reauth")
        ok = bool(result.get("ok"))
        succeeded += int(ok)
        rows.append({
            "server": spec.name, "ok": ok, "message": result.get("message"),
            "state": (result.get("data") or {}).get("state"),
        })
        lines.append(f"{'ok  ' if ok else 'FAIL'} {spec.name}: {result.get('message')}")
    lines.append(f"re-authenticated {succeeded}/{len(targets)} server(s)")
    return {
        "ok": succeeded == len(targets),
        "action": "reauth",
        "message": "\n".join(lines),
        "rows": rows,
        "servers": extended_status_rows(manager),
    }


# -- add / remove --------------------------------------------------------------

async def _add(ctx: _Ctx, args: list[str]) -> dict[str, Any]:
    manager = ctx.manager
    path = manager.catalog_path
    if path is None:
        return _err("add", "no catalog path configured; add unavailable")
    try:
        req = parse_add_args(args)
    except ValueError as exc:
        return {"ok": False, "action": "add", "message": f"{exc}\nusage: {ADD_USAGE}"}
    if not req.positionals:
        return _usage("add", ADD_USAGE)
    first = req.positionals[0]
    is_url = bool(_URL_RE.match(first))
    transport = req.transport or ("http" if is_url else "stdio")
    if transport not in ("http", "sse", "stdio"):
        return _err("add", f"unknown transport {transport!r} (http|sse|stdio)")
    if transport in ("http", "sse") and not is_url:
        return _err("add", f"{transport} transport needs an http(s):// URL, got {first!r}")
    if transport == "stdio" and is_url:
        return _err("add", "stdio transport takes a command, not a URL (or pass --transport http)")
    if transport in ("http", "sse") and len(req.positionals) > 1:
        extra = " ".join(req.positionals[1:])
        return _err("add", f"unexpected extra arguments after the URL: {extra}")
    if transport == "stdio" and req.headers:
        return _err("add", "--header only applies to http/sse servers")
    for key, value in req.headers.items():
        # Credentials never go into mcp.json: an Authorization/API-key header
        # must reference ${VAR} (value in ~/.freyja/.env). config.validate()
        # covers TOKEN/KEY/SECRET-shaped names; 'Authorization' is added here.
        credential_name = key.lower() in ("authorization", "proxy-authorization", "x-api-key")
        if (credential_name or is_secret_key(key)) and value and "${" not in value:
            return _err(
                "add",
                f"header {key!r} holds an inline secret; write it as "
                f"'{key}=Bearer ${{{name_hint(key)}}}' and put the value in ~/.freyja/.env",
            )

    catalog = load_catalog(path)
    existing = set(catalog.specs) | set(manager.specs)
    if transport == "stdio":
        name = req.name or default_server_name_for_command(first, req.positionals[1:])
    else:
        name = req.name or default_server_name_for_url(first)
    problem = validate_server_name(name, existing)
    if problem:
        return {"ok": False, "action": "add", "server": name, "message": problem}

    raw: dict[str, Any] = {"transport": transport, "enabled": bool(req.enable)}
    if req.trust:
        raw["trust"] = req.trust
    if req.tier:
        raw["tier"] = req.tier
    warnings: list[str] = []
    next_steps: list[str] = []
    probe: ProbeResult | None = None
    if transport == "stdio":
        raw["command"] = first
        if len(req.positionals) > 1:
            raw["args"] = list(req.positionals[1:])
        if req.env:
            raw["env"] = dict(req.env)
        env = os.environ if ctx.environ is None else ctx.environ
        resolved = expand_env_refs(first, dict(env)).text
        if resolved and shutil.which(resolved) is None:
            warnings.append(
                f"command {resolved!r} not found on PATH — the server will fail to start "
                "until it is installed"
            )
    else:
        raw["url"] = first
        if req.headers:
            raw["headers"] = dict(req.headers)
        env = os.environ if ctx.environ is None else ctx.environ
        probe_headers = {k: expand_env_refs(v, dict(env)).text for k, v in req.headers.items()}
        probe = await probe_http_server(first, probe_headers, timeout_s=ctx.probe_timeout_s)
        if not probe.reachable:
            raw["enabled"] = False
            warnings.append(
                f"{first} is unreachable ({probe.error}); added DISABLED — fix the URL or network, "
                f"then /mcp test {name} and /mcp enable {name}"
            )
        else:
            if probe.transport == "sse" and transport == "http":
                raw["transport"] = "sse"
                warnings.append(
                    f"endpoint answered HTTP {probe.status} to streamable HTTP; "
                    "using legacy SSE transport"
                )
            if probe.auth == "oauth":
                raw["auth"] = "oauth"
                oauth_block: dict[str, Any] = {}
                if probe.resource_metadata_url:
                    oauth_block["resource_metadata_url"] = probe.resource_metadata_url
                raw["oauth"] = oauth_block
            elif probe.auth == "bearer":
                if not req.headers:
                    warnings.append(
                        f"server answered HTTP {probe.status} without OAuth resource metadata — it "
                        f"probably wants an API key: re-add with --header "
                        f"'Authorization=Bearer ${{{name.upper().replace('-', '_')}_TOKEN}}' "
                        f"(value in ~/.freyja/.env), or try {login_hint(name)}"
                    )
            elif probe.transport is None:
                raw["enabled"] = False
                warnings.append(f"{probe.error}; added DISABLED — /mcp test {name} once fixed")

    try:
        spec = McpServerSpec.from_dict(name, raw)
        spec.validate()
    except McpConfigError as exc:
        return {"ok": False, "action": "add", "server": name, "message": str(exc)}
    catalog.specs[name] = spec
    save_catalog(catalog, path)
    await manager.reload()
    await emit_status(ctx.emit, manager)
    snapshot_rows = extended_status_rows(manager, server=name)
    snapshot = snapshot_rows[0] if snapshot_rows else {}

    missing = missing_env_refs(spec, ctx.environ)
    if missing:
        next_steps.append(f"set {', '.join(missing)} in {path.parent / '.env'}")
    if spec.auth == "oauth":
        next_steps.append(login_hint(name))
        if not spec.enabled:
            next_steps.append(f"/mcp enable {name}")
    elif not spec.enabled and (probe is None or probe.reachable):
        next_steps.append(f"/mcp enable {name}")
    next_steps.append(f"/mcp test {name}")

    kind = spec.transport + (", auth: oauth" if spec.auth == "oauth" else "")
    lines = [
        f"added '{name}' ({kind}, {'enabled' if spec.enabled else 'disabled'}) to {path}"
    ]
    if probe is not None:
        lines.append(f"probe: {probe.summary()}")
    if spec.enabled and snapshot:
        reason = f" — {snapshot.get('reason')}" if snapshot.get("reason") else ""
        lines.append(
            f"state: {snapshot.get('state')} ({snapshot.get('tool_count', 0)} tool(s)){reason}"
        )
    for warning in warnings:
        lines.append(f"warning: {warning}")
    if next_steps:
        lines.append("next: " + " → ".join(next_steps))
    return {
        "ok": True,
        "action": "add",
        "server": name,
        "message": "\n".join(lines),
        "servers": snapshot_rows,
        "data": {
            "spec": spec.to_dict(),
            "probe": probe.to_dict() if probe is not None else None,
            "warnings": warnings,
            "next_steps": next_steps,
        },
    }


async def _remove(ctx: _Ctx, server: str, *, purge: bool) -> dict[str, Any]:
    manager = ctx.manager
    path = manager.catalog_path
    if path is None:
        return _err("remove", "no catalog path configured; remove unavailable")
    catalog = load_catalog(path)
    spec = catalog.specs.pop(server, None)
    if spec is None and server not in manager.specs:
        return _unknown_server("remove", server, manager)
    save_catalog(catalog, path)
    # Stop the connection BEFORE purging: a live OAuth provider may still
    # persist AS metadata during teardown and resurrect the directory.
    summary = await manager.reload()
    purged = False
    if purge:
        from bridge.mcp.oauth.storage import FreyjaTokenStorage

        storage = FreyjaTokenStorage(server, root=ctx.token_root)
        purged = storage.server_dir.exists()
        storage.remove()
        logger.info("mcp oauth '%s': stored OAuth state purged by /mcp remove", server)
    await emit_status(ctx.emit, manager)
    message = f"removed '{server}' from {path}"
    if server in (summary.get("removed") or []):
        message += " — connection stopped, tools unregistered"
    if purge:
        message += "; stored OAuth state " + ("purged" if purged else "was empty")
    elif spec is not None and _is_oauth_spec(spec):
        message += f" (OAuth tokens kept; use /mcp remove {server} --purge to delete them)"
    return {
        "ok": True,
        "action": "remove",
        "server": server,
        "message": message,
        "servers": extended_status_rows(manager),
        "data": {"purged": purged},
    }


async def _approve(ctx: _Ctx, server: str | None, args: list[str]) -> dict[str, Any]:
    """Persist ``tools.allow_quarantined`` for a tool the injection scan
    refused, then reload so the server re-registers it. Only tools currently
    quarantined for that server (or explicitly known remote names) qualify."""
    tool = args[0] if args else None
    if not server or not tool:
        return _usage("approve", "approve <server> <tool>")
    manager = ctx.manager
    if server not in manager.specs:
        return _unknown_server("approve", server, manager)
    path = manager.catalog_path
    if path is None:
        return _err("approve", "no catalog path configured; approve unavailable")
    quarantined = {
        q.get("tool") for q in manager.pending_quarantine() if q.get("server") == server
    }
    if tool not in quarantined:
        return _err(
            "approve",
            f"'{tool}' is not quarantined on '{server}'"
            + (f" (quarantined: {', '.join(sorted(quarantined))})" if quarantined else ""),
        )
    catalog = load_catalog(path)
    spec = catalog.specs.get(server)
    if spec is None:
        return _unknown_server("approve", server, manager)
    if tool not in spec.tools_allow_quarantined:
        spec.tools_allow_quarantined.append(tool)
        save_catalog(catalog, path)
    await manager.reload()
    await emit_status(ctx.emit, manager)
    registered = any(r.get("remote") == tool for r in tools_rows(manager, server))
    message = (
        f"approved '{tool}' on '{server}' (tools.allow_quarantined in {path})"
        + (" — registered" if registered else " — reconnecting; it registers on the next tool list")
    )
    return {
        "ok": True,
        "action": "approve",
        "server": server,
        "message": message,
        "servers": extended_status_rows(manager),
        "data": {"tool": tool, "registered": registered},
    }


# -- test / tools ----------------------------------------------------------------

async def _test(ctx: _Ctx, spec: McpServerSpec) -> dict[str, Any]:
    report = await run_server_test(ctx.manager, spec)
    return {
        "ok": bool(report.get("ok")),
        "action": "test",
        "server": spec.name,
        "message": format_test_report(report),
        "data": report,
        "rows": list(report.get("steps") or []),
    }


def _tools(ctx: _Ctx, server: str | None) -> dict[str, Any]:
    manager = ctx.manager
    rows = tools_rows(manager, server)
    quarantine = [
        q for q in manager.pending_quarantine() if not server or q.get("server") == server
    ]
    lines: list[str] = []
    if server:
        row = next(iter(extended_status_rows(manager, server=server)), {})
        state = row.get("state")
        reason = (
            f" ({row.get('reason')})"
            if row.get("reason") and state not in ("active", "degraded") else ""
        )
        lines.append(f"'{server}': {state} — {len(rows)} registered tool(s){reason}")
    else:
        servers = len({r["server"] for r in rows})
        lines.append(f"{len(rows)} registered MCP tool(s) across {servers} server(s)")
    lines.append(format_tools_table(rows))
    quarantine_text = format_quarantine(quarantine)
    if quarantine_text:
        lines.append(quarantine_text)
    return {
        "ok": True,
        "action": "tools",
        "server": server,
        "message": "\n".join(lines),
        "rows": rows,
        "data": {"quarantine": quarantine},
    }


# -- catalog -----------------------------------------------------------------------

CATALOG_USAGE = (
    "catalog list [--tag T]* | search <query> | info <name> | "
    "install <name> [--enable] [--as NAME]"
)


async def _catalog(ctx: _Ctx, args: list[str]) -> dict[str, Any]:
    from bridge.mcp import catalog as cat

    sub = (args[0].lower() if args else "list")
    rest = args[1:]
    if sub in ("list", "ls"):
        tags: list[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "--tag" and i + 1 < len(rest):
                tags.append(rest[i + 1])
                i += 2
            elif rest[i].startswith("--tag="):
                tags.append(rest[i].split("=", 1)[1])
                i += 1
            else:
                tags.append(rest[i])
                i += 1
        rows = cat.catalog_list(tags=tags or None)
        noun = "entry" if len(rows) == 1 else "entries"
        head = f"{len(rows)} catalog {noun}" + (f" tagged {', '.join(tags)}" if tags else "")
        return {
            "ok": True, "action": "catalog", "sub": "list",
            "message": head + "\n" + format_catalog_table(rows), "rows": rows,
        }
    if sub == "search":
        query = " ".join(rest).strip()
        if not query:
            return _usage("catalog", CATALOG_USAGE)
        rows = cat.catalog_search(query)
        return {
            "ok": True, "action": "catalog", "sub": "search",
            "message": f"{len(rows)} match(es) for {query!r}\n" + format_catalog_table(rows),
            "rows": rows,
        }
    if sub == "info":
        if not rest:
            return _usage("catalog", CATALOG_USAGE)
        info = cat.catalog_info(rest[0])
        if info is None:
            return {
                "ok": False, "action": "catalog", "sub": "info",
                "message": f"unknown catalog entry {rest[0]!r} — /mcp catalog search <q>",
            }
        return {
            "ok": True, "action": "catalog", "sub": "info",
            "message": format_catalog_info(info), "data": info,
        }
    if sub == "install":
        if not rest:
            return _usage("catalog", CATALOG_USAGE)
        entry = rest[0]
        enable = "--enable" in rest
        alias: str | None = None
        for i, tok in enumerate(rest):
            if tok == "--as" and i + 1 < len(rest):
                alias = rest[i + 1]
            elif tok.startswith("--as="):
                alias = tok.split("=", 1)[1]
        path = ctx.manager.catalog_path
        if path is None:
            return {
                "ok": False, "action": "catalog", "sub": "install",
                "message": "no catalog path configured; install unavailable",
            }
        if alias:
            problem = validate_server_name(alias, set())
            if problem:
                return {"ok": False, "action": "catalog", "sub": "install", "message": problem}
        try:
            report = cat.catalog_install(
                entry, mcp_json_path=path, server_name=alias, enable=enable, environ=ctx.environ,
            )
        except cat.CatalogError as exc:
            return {"ok": False, "action": "catalog", "sub": "install", "message": str(exc)}
        await ctx.manager.reload()
        await emit_status(ctx.emit, ctx.manager)
        rows = extended_status_rows(ctx.manager, server=report.server_name)
        snapshot = rows[0] if rows else {}
        verb = (
            "installed" if report.created
            else ("updated" if report.changed else "already installed")
        )
        lines = [
            f"{verb} "
            f"'{report.server_name}' from catalog entry '{report.name}' "
            f"({report.auth}, {'enabled' if report.enabled else 'disabled'})"
        ]
        if report.enabled and snapshot:
            lines.append(
                f"state: {snapshot.get('state')} ({snapshot.get('tool_count', 0)} tool(s))"
            )
        if report.missing_env:
            lines.append(f"missing env: {', '.join(report.missing_env)}")
        if report.next_steps:
            lines.append("next: " + " → ".join(report.next_steps))
        if report.post_install:
            lines.append(f"notes: {report.post_install.strip()}")
        return {
            "ok": True, "action": "catalog", "sub": "install", "server": report.server_name,
            "message": "\n".join(lines), "servers": rows, "data": report.to_dict(),
        }
    return _usage("catalog", CATALOG_USAGE)


# -- call --------------------------------------------------------------------------

def _parse_call_arguments(cmd: dict[str, Any], args: list[str]) -> dict[str, Any] | str:
    """``arguments`` dict wins; legacy ``args`` dict accepted; a token list
    is one JSON object or ``key=value`` pairs. Returns an error string on
    malformed input."""
    arguments = cmd.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    raw = cmd.get("args")
    if isinstance(raw, dict):
        return raw
    if raw is not None and not isinstance(raw, list):
        return "arguments must be a JSON object"
    if not args:
        return {}
    joined = " ".join(args).strip()
    if joined.startswith("{"):
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError as exc:
            return f"arguments must be a JSON object: {exc}"
        return parsed if isinstance(parsed, dict) else "arguments must be a JSON object"
    out: dict[str, Any] = {}
    for tok in args:
        if "=" not in tok:
            return f"expected key=value or a JSON object, got {tok!r}"
        key, _, value = tok.partition("=")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


async def _call(ctx: _Ctx, server: str, cmd: dict[str, Any], args: list[str]) -> dict[str, Any]:
    manager = ctx.manager
    tool = str(cmd.get("tool") or "").strip()
    if not server or not tool:
        return _usage("call", "call <server> <tool> [json-object | key=value ...]")
    arguments = _parse_call_arguments(cmd, args)
    if isinstance(arguments, str):
        return {"ok": False, "action": "call", "server": server, "tool": tool, "message": arguments}
    proxy = next(
        (
            p for p in manager.proxy_tools()
            if p.server == server and tool in (p.remote_name, p.proxy_name)
        ),
        None,
    )
    if proxy is None:
        available = sorted(p.remote_name for p in manager.proxy_tools() if p.server == server)
        return {
            "ok": False,
            "action": "call",
            "server": server,
            "tool": tool,
            "message": (
                f"no registered tool '{tool}' on server '{server}' "
                f"(available: {', '.join(available) or 'none'})"
            ),
        }
    result = await proxy.execute(f"mcp-call-{server}-{tool}", arguments)
    content = result.content
    if not isinstance(content, str):
        try:
            content = json.dumps(content, default=str)
        except Exception:  # noqa: BLE001
            content = str(content)
    return {
        "ok": not result.is_error,
        "action": "call",
        "server": server,
        "tool": tool,
        "tool_name": proxy.proxy_name,
        "is_error": bool(result.is_error),
        "content": content,
        "message": content if isinstance(content, str) else str(content),
    }


# -- answer (elicitation) -----------------------------------------------------------

def _answer(ctx: _Ctx, args: list[str]) -> dict[str, Any]:
    bridge = ctx.elicitation
    if bridge is None:
        return _err("answer", "no elicitation bridge is wired on this surface")
    if not args:
        pending = bridge.pending()
        if not pending:
            return {
                "ok": True, "action": "answer", "message": "no pending elicitations", "rows": [],
            }
        lines = [f"{len(pending)} pending elicitation(s):"]
        for entry in pending:
            prompt = str(entry.get("message") or "")[:80]
            lines.append(f"  {entry['requestId']} — {entry['server']}: {prompt}")
        lines.append("answer with: /mcp answer <requestId> key=value ... | decline | cancel")
        return {"ok": True, "action": "answer", "message": "\n".join(lines), "rows": pending}
    request_id = args[0]
    entry = bridge.get(request_id)
    if entry is None:
        return _err(
            "answer", f"no pending elicitation with id {request_id!r} (it may have timed out)"
        )
    try:
        action, fields = parse_answer_tokens(args[1:])
    except ValueError as exc:
        return {"ok": False, "action": "answer", "message": str(exc)}
    content: dict[str, Any] = {}
    if action == "accept":
        content, problems = coerce_answer(entry.get("requestedSchema"), fields)
        if problems:
            return _err("answer", "cannot accept: " + "; ".join(problems))
    resolved = bridge.resolve(request_id, action, content)
    if not resolved:
        return _err("answer", f"elicitation {request_id!r} is no longer pending")
    past = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}[action]
    return {
        "ok": True,
        "action": "answer",
        "message": f"{past} elicitation {request_id} for '{entry['server']}'",
        "data": {"requestId": request_id, "action": action, "content": content},
    }


__all__ = [
    "ADD_USAGE",
    "BACKGROUND_ACTIONS",
    "CATALOG_USAGE",
    "DEFAULT_LOGIN_TIMEOUT_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "SURFACES",
    "VALID_ACTIONS",
    "AddRequest",
    "ProbeResult",
    "SurfaceLoginFlow",
    "auth_kind",
    "default_server_name_for_command",
    "default_server_name_for_url",
    "elicitation_bridge_of",
    "emit_status",
    "extended_status_rows",
    "format_catalog_info",
    "format_catalog_table",
    "format_mcp_table",
    "format_quarantine",
    "format_status_details",
    "format_test_report",
    "format_tools_table",
    "handle_mcp_command",
    "make_auth_factory",
    "make_login_flow",
    "manager_hooks",
    "missing_env_refs",
    "non_interactive_auth_factory",
    "parse_add_args",
    "parse_mcp_args",
    "parse_mcp_command",
    "parse_resource_metadata",
    "probe_http_server",
    "resolve_elicitation_response",
    "run_server_test",
    "spawn_background",
    "tokenize",
    "tools_rows",
    "validate_server_name",
    "wait_background_tasks",
]
