"""McpManager: owns the server catalog + connections, feeds registries.

Design doc 1.2/1.4/4: one manager per bridge process, running on the main
asyncio loop. Each enabled server gets a supervised McpConnection task.
Registries attach via weakref so later connects/disconnects register()/
unregister() live without the manager keeping sessions alive.

v1 additions (design 4.1-4.5):
  · full lifecycle plumbing: degraded connections keep their tools;
    backoff/parked/failed transitions unregister them; a revival
    re-registers with a DIFF against the previous tool list (stale
    names unregistered, new ones registered);
  · ``status_listener`` callback — the bridge wires it to ``emit()`` as
    ``mcp_status`` events (server, state, reason, since, tool_count,
    schema_chars, restart_count, last_latency_ms);
  · ``status_snapshot()`` for the ``mcp_command status`` IPC reply;
  · ``enable()`` / ``disable()`` — flip mcp.json via save_catalog and
    connect/disconnect + register/unregister live, no restart;
  · ``reload()`` — re-read mcp.json and apply the diff;
  · pid-file sweep on ``start()`` (see bridge/mcp/pidfiles.py) plus
    watchdog-wrapped spawns when ``run_dir``/``watchdog`` are set.

v2 additions (card_017):
  · HTTP/SSE transports with an injected ``auth_factory`` (never imports
    bridge.mcp.oauth; ``None`` = unauthenticated);
  · ``approval_handler`` routes server elicitations through Freyja's
    approval surface (default: decline);
  · diff-based tool refresh on ``tools/list_changed`` / schema TTL:
    added tools register, removed tools unregister, changed tools update
    in place — never a nuke-and-repave — subject to the same injection
    scan (a poisoned re-description quarantines the tool);
  · ``pending_quarantine()`` lists tools skipped by the injection scan
    so an operator can review them.

v2 additions (card_019):
  · ``reconnect()`` rebuilds one server's connection without touching
    mcp.json (post-login / post-logout);
  · token-file watch: a server parked in needs-auth whose OAuth
    ``tokens.json`` changes on disk (login completed from ANOTHER process
    — the gateway daemon and the desktop bridge share
    ``~/.freyja/mcp-tokens``) is reconnected automatically on the next
    watch tick (``token_watch_interval_s``, default = keepalive interval);
  · ``logins_in_progress`` — the per-server login concurrency guard used
    by commands.py.

Tool descriptions are scanned for prompt-injection phrasing BEFORE
registration (design doc section 8); a hit skips the tool and logs.
The pattern set is the union of Freyja's and hermes-agent's
(``_MCP_INJECTION_PATTERNS``, Copyright (c) 2025 Nous Research, MIT).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import re
import time
import weakref
from pathlib import Path
from typing import Any, Callable

from bridge.mcp.config import McpCatalog, McpServerSpec, load_catalog, save_catalog
from bridge.mcp.connection import (
    ACTIVE_LIKE,
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_BACKOFF_CAP_S,
    DEFAULT_BACKOFF_MAX_RETRIES,
    DEFAULT_ELICITATION_TIMEOUT_S,
    DEFAULT_KEEPALIVE_INTERVAL_S,
    DEFAULT_PARK_PROBE_INTERVAL_S,
    DEFAULT_RAPID_DROP_BUDGET,
    DEFAULT_WATCHDOG_POLL_INTERVAL_S,
    ApprovalHandler,
    AuthFactory,
    McpConnection,
    State,
    normalize_keepalive_interval,
)
from bridge.mcp.pidfiles import pid_file_path, remove_pid_file, sweep_stale_pidfiles
from bridge.mcp.proxy_tool import McpProxyTool, build_summary, proxy_tool_name

logger = logging.getLogger(__name__)

# Instruction-override phrasing in third-party tool metadata. Case-
# insensitive. Deliberately conservative: these strings have no business
# in an honest tool description. Union of Freyja's original set and
# hermes-agent's ``_MCP_INJECTION_PATTERNS`` (Nous Research, MIT).
INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # -- Freyja --
        r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions",
        r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions",
        r"forget\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions",
        r"system\s+prompt",
        r"<\s*/?\s*system\s*>",
        r"\bIMPORTANT\s*:",
        r"you\s+must\s+(?:now\s+)?obey",
        r"new\s+instructions\s*:",
        r"do\s+not\s+(?:tell|inform|alert)\s+the\s+user",
        r"override\s+(?:your|the)\s+(?:instructions|rules|guidelines)",
        # -- hermes-agent (identity/task override, role tags, concealment) --
        r"you\s+are\s+now\s+a\b",
        r"your\s+new\s+(?:task|role|instructions?)\s+(?:is|are)\b",
        r"(?:^|\n)\s*system\s*:\s*",
        r"<\s*/?\s*(?:human|assistant)\s*>",
        r"do\s+not\s+(?:tell|inform|mention|reveal)\s+(?:the\s+)?(?:user|human|operator)",
        # -- hermes-agent (payload smuggling in descriptions) --
        r"\b(?:curl|wget)\s+https?://",
        r"base64\.(?:b64decode|decodebytes)",
        r"\b(?:exec|eval)\s*\(\s*(?:base64|compile|open|__import__|request|input)",
        r"\bimport\s+(?:subprocess|socket)\b",
    )
)


def scan_description_for_injection(text: str) -> str | None:
    """Return the offending pattern string on a hit, else None."""
    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return None


class McpManager:
    """Catalog + connections + live registry integration + status surface."""

    def __init__(
        self,
        catalog: McpCatalog | None = None,
        *,
        ping_interval_s: float = DEFAULT_KEEPALIVE_INTERVAL_S,
        keepalive_interval_s: float | None = None,
        keepalive_timeout_s: float | None = None,
        max_reconnects: int | None = None,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
        backoff_max_retries: int = DEFAULT_BACKOFF_MAX_RETRIES,
        park_probe_interval_s: float = DEFAULT_PARK_PROBE_INTERVAL_S,
        watchdog: bool = True,
        watchdog_poll_interval_s: float = DEFAULT_WATCHDOG_POLL_INTERVAL_S,
        run_dir: Path | str | None = None,
        catalog_path: Path | str | None = None,
        status_listener: Callable[[dict[str, Any]], None] | None = None,
        auth_factory: AuthFactory | None = None,
        approval_handler: ApprovalHandler | None = None,
        elicitation_timeout_s: float = DEFAULT_ELICITATION_TIMEOUT_S,
        rapid_drop_budget: int = DEFAULT_RAPID_DROP_BUDGET,
        backoff_jitter: str = "full",
        schema_ttl_s: float | None = None,
        token_root: Path | str | None = None,
        token_watch_interval_s: float | None = None,
    ) -> None:
        self._catalog = catalog or McpCatalog()
        self._keepalive_interval_s = (
            keepalive_interval_s if keepalive_interval_s is not None else ping_interval_s
        )
        self._keepalive_timeout_s = keepalive_timeout_s
        self._max_reconnects = max_reconnects
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._backoff_max_retries = backoff_max_retries
        self._park_probe_interval_s = park_probe_interval_s
        self._watchdog = watchdog
        self._watchdog_poll_interval_s = watchdog_poll_interval_s
        self._run_dir = Path(run_dir) if run_dir is not None else None
        self._catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.status_listener = status_listener
        """Called with an mcp_status-shaped dict on every state transition.
        The bridge wires this to emit({'type': 'mcp_status', ...})."""
        self.auth_factory = auth_factory
        """``(spec, interactive) -> httpx.Auth | None`` for HTTP servers.
        Injected so this module never imports bridge.mcp.oauth."""
        self.approval_handler = approval_handler
        """Elicitation router (``ElicitRequest -> dict | None``)."""
        self._elicitation_timeout_s = elicitation_timeout_s
        self._rapid_drop_budget = rapid_drop_budget
        self._backoff_jitter = backoff_jitter
        self._schema_ttl_s = schema_ttl_s
        self._quarantine: dict[str, list[dict[str, Any]]] = {}
        """server name -> tools skipped by the injection scan."""
        self._token_root = Path(token_root) if token_root is not None else None
        self._token_watch_interval_s = (
            float(token_watch_interval_s)
            if token_watch_interval_s is not None
            else self._keepalive_interval_s
        )
        self._token_mtimes: dict[str, float | None] = {}
        """server name -> tokens.json mtime last observed while needs-auth."""
        self._token_watch_task: asyncio.Task[None] | None = None
        self.token_reconnects: int = 0
        """Reconnects triggered by the token-file watch (diagnostics/tests)."""
        self.logins_in_progress: set[str] = set()
        """Servers with an interactive /mcp login running right now."""

        self._connections: dict[str, McpConnection] = {}
        self._proxies: dict[str, list[McpProxyTool]] = {}
        """server name -> currently registered proxy tools."""
        self._last_tool_names: dict[str, set[str]] = {}
        """server name -> proxy names from the previous activation, kept
        across deaths so a revival can diff (design 4.6 reconnect rule)."""
        self._registries: list[weakref.ref[Any]] = []
        self._started = False

    # -- construction ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: Path | str,
        workspace: Path | str | None = None,
        *,
        include_project: bool = False,
        ping_interval_s: float | None = None,
        max_reconnects: int | None = None,
        run_dir: Path | str | None = None,
        watchdog: bool = True,
        status_listener: Callable[[dict[str, Any]], None] | None = None,
        auth_factory: AuthFactory | None = None,
        approval_handler: ApprovalHandler | None = None,
        **kwargs: Any,
    ) -> "McpManager":
        """Build a manager from mcp.json. A missing file yields a manager
        with zero servers — start() is then a no-op and the bridge behaves
        exactly as it did before MCP support existed.

        Config-sourced keepalive (top-level ``settings.keepalive_interval_s``
        in mcp.json) is clamped to the 5s floor; an explicit
        ``ping_interval_s`` argument is taken verbatim (test seam).
        """
        catalog = load_catalog(path, workspace, include_project=include_project)
        if ping_interval_s is None:
            settings = catalog.extra.get("settings") or {}
            configured = (
                settings.get("keepalive_interval_s") if isinstance(settings, dict) else None
            )
            ping_interval_s = (
                normalize_keepalive_interval(configured)
                if configured is not None
                else DEFAULT_KEEPALIVE_INTERVAL_S
            )
        return cls(
            catalog,
            ping_interval_s=ping_interval_s,
            max_reconnects=max_reconnects,
            run_dir=run_dir,
            watchdog=watchdog,
            catalog_path=path,
            status_listener=status_listener,
            auth_factory=auth_factory,
            approval_handler=approval_handler,
            **kwargs,
        )

    @property
    def specs(self) -> dict[str, McpServerSpec]:
        return dict(self._catalog.specs)

    @property
    def server_count(self) -> int:
        return len(self._catalog.specs)

    @property
    def catalog_path(self) -> Path | None:
        """mcp.json this manager was loaded from (None for in-memory catalogs)."""
        return self._catalog_path

    @property
    def token_root(self) -> Path:
        """OAuth token directory (``$FREYJA_HOME/mcp-tokens`` by default).
        Resolved lazily so tests can point FREYJA_HOME at a tmp dir."""
        if self._token_root is not None:
            return self._token_root
        home = Path(os.environ.get("FREYJA_HOME") or (Path.home() / ".freyja"))
        return home / "mcp-tokens"

    def connection(self, name: str) -> McpConnection | None:
        return self._connections.get(name)

    # -- lifecycle -------------------------------------------------------------

    def _make_connection(self, spec: McpServerSpec) -> McpConnection:
        pid_file = (
            pid_file_path(self._run_dir, spec.name) if self._run_dir is not None else None
        )
        return McpConnection(
            spec,
            on_state_change=self._on_state_change,
            on_tools_changed=self._on_tools_changed,
            keepalive_interval_s=self._keepalive_interval_s,
            keepalive_timeout_s=self._keepalive_timeout_s,
            max_reconnects=self._max_reconnects,
            backoff_base_s=self._backoff_base_s,
            backoff_cap_s=self._backoff_cap_s,
            backoff_max_retries=self._backoff_max_retries,
            park_probe_interval_s=self._park_probe_interval_s,
            watchdog=self._watchdog,
            watchdog_poll_interval_s=self._watchdog_poll_interval_s,
            pid_file=pid_file,
            auth_factory=self.auth_factory,
            approval_handler=self.approval_handler,
            elicitation_timeout_s=self._elicitation_timeout_s,
            rapid_drop_budget=self._rapid_drop_budget,
            backoff_jitter=self._backoff_jitter,
            schema_ttl_s=self._schema_ttl_s,
        )

    async def start(self) -> None:
        """Sweep stale pids, then connect all enabled servers concurrently.
        Each connection is a supervised asyncio task owned by the
        McpConnection itself; start() returns once every first connect
        attempt has resolved."""
        if self._started:
            return
        self._started = True
        if self._run_dir is not None:
            try:
                sweep_stale_pidfiles(self._run_dir)
            except Exception:  # noqa: BLE001
                logger.exception("mcp: pid-file sweep failed (continuing)")
        enabled = [s for s in self._catalog.specs.values() if s.enabled]
        if not enabled:
            return
        self._ensure_token_watch()
        for spec in enabled:
            self._connections[spec.name] = self._make_connection(spec)
        await asyncio.gather(
            *(conn.start() for conn in self._connections.values()),
            return_exceptions=True,
        )

    async def stop(self) -> None:
        """Clean shutdown: stop every connection (terminating children),
        pull all proxy tools out of attached registries, and remove any
        pid files this manager owned."""
        watch, self._token_watch_task = self._token_watch_task, None
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(BaseException):
                await watch
        conns = list(self._connections.values())
        if conns:
            await asyncio.gather(
                *(conn.stop() for conn in conns), return_exceptions=True
            )
        for name in list(self._proxies):
            self._unregister_server_tools(name)
        if self._run_dir is not None:
            for name in list(self._connections):
                remove_pid_file(pid_file_path(self._run_dir, name))
        self._connections.clear()
        self._started = False

    # -- management (design 4.5) -----------------------------------------------

    def _save_catalog(self) -> None:
        if self._catalog_path is not None:
            save_catalog(self._catalog, self._catalog_path)

    async def enable(self, name: str) -> dict[str, Any]:
        """Flip ``enabled`` in mcp.json and connect + register live."""
        spec = self._catalog.specs.get(name)
        if spec is None:
            raise KeyError(f"unknown MCP server '{name}'")
        spec.enabled = True
        self._save_catalog()
        old = self._connections.pop(name, None)
        if old is not None:
            await old.stop()
        conn = self._make_connection(spec)
        self._connections[name] = conn
        self._started = True
        self._ensure_token_watch()
        self._token_mtimes.pop(name, None)
        await conn.start()
        return self._server_snapshot(name)

    async def reconnect(self, name: str, reason: str = "reconnect requested") -> dict[str, Any]:
        """Rebuild one ENABLED server's connection without touching mcp.json
        (after ``/mcp login`` minted tokens, ``/mcp logout`` cleared them,
        or another process changed the token file). A dead supervisor
        (needs-auth / failed / parked) cannot be poked, so the connection
        is stopped and recreated; the first connect attempt is awaited so
        callers can report the resulting state."""
        spec = self._catalog.specs.get(name)
        if spec is None:
            raise KeyError(f"unknown MCP server '{name}'")
        if not spec.enabled:
            return self._server_snapshot(name)
        old = self._connections.pop(name, None)
        if old is not None:
            await old.stop()
        logger.info("mcp '%s': reconnecting (%s)", name, reason)
        conn = self._make_connection(spec)
        self._connections[name] = conn
        self._started = True
        self._ensure_token_watch()
        self._token_mtimes.pop(name, None)
        await conn.start()
        return self._server_snapshot(name)

    async def disable(self, name: str) -> dict[str, Any]:
        """Flip ``enabled`` off in mcp.json, disconnect + unregister live."""
        spec = self._catalog.specs.get(name)
        if spec is None:
            raise KeyError(f"unknown MCP server '{name}'")
        spec.enabled = False
        self._save_catalog()
        conn = self._connections.pop(name, None)
        if conn is not None:
            await conn.stop()
        self._unregister_server_tools(name)
        if self._run_dir is not None:
            remove_pid_file(pid_file_path(self._run_dir, name))
        snapshot = self._server_snapshot(name)
        self._emit_status_dict(snapshot | {"reason": "disabled by operator"})
        return snapshot

    async def reload(self) -> dict[str, Any]:
        """Re-read mcp.json and apply the diff: removed/changed servers are
        stopped, newly-enabled servers are connected, disabled servers are
        disconnected. Registered tools follow via the state hooks."""
        if self._catalog_path is None:
            return {
                "added": [], "removed": [], "changed": [],
                "message": "no catalog path configured; reload unavailable",
            }
        new_catalog = load_catalog(self._catalog_path)
        old_specs = self._catalog.specs
        new_specs = new_catalog.specs
        added = sorted(n for n in new_specs if n not in old_specs)
        removed = sorted(n for n in old_specs if n not in new_specs)
        changed = sorted(
            n for n in new_specs
            if n in old_specs and new_specs[n].to_dict() != old_specs[n].to_dict()
        )
        self._catalog = new_catalog
        self._started = True

        # Tear down removed/changed/now-disabled connections.
        teardown = set(removed) | set(changed)
        teardown |= {
            n for n, spec in new_specs.items()
            if not spec.enabled and n in self._connections
        }
        for name in sorted(teardown):
            conn = self._connections.pop(name, None)
            if conn is not None:
                await conn.stop()
            self._unregister_server_tools(name)

        # Connect anything enabled that has no live connection.
        to_start = [
            spec for name, spec in new_specs.items()
            if spec.enabled and name not in self._connections
        ]
        for spec in to_start:
            self._connections[spec.name] = self._make_connection(spec)
            self._token_mtimes.pop(spec.name, None)
        if to_start:
            self._ensure_token_watch()
            await asyncio.gather(
                *(self._connections[s.name].start() for s in to_start),
                return_exceptions=True,
            )
        return {"added": added, "removed": removed, "changed": changed}

    # -- token-file watch (card_019 #7) -------------------------------------------

    def token_file_for(self, spec: McpServerSpec) -> Path | None:
        """``<token_root>/<safe-name>/tokens.json`` for an ``auth: oauth``
        server, else None. Layout mirrors bridge.mcp.oauth.storage without
        importing it (the manager never depends on the OAuth package)."""
        if spec.auth != "oauth":
            return None
        safe = re.sub(r"[^\w\-]", "_", spec.name).strip("_")[:128] or "default"
        return self.token_root / safe / "tokens.json"

    def token_expires_at(self, name: str) -> float | None:
        """Absolute expiry recorded in the stored token file (None when
        no token / unknown). Only the ``expires_at`` number is read; the
        token values are never returned or logged."""
        spec = self._catalog.specs.get(name)
        path = self.token_file_for(spec) if spec is not None else None
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("expires_at") if isinstance(data, dict) else None
            return float(value) if value is not None else None
        except (OSError, ValueError, TypeError):
            return None

    def has_tokens(self, name: str) -> bool:
        spec = self._catalog.specs.get(name)
        path = self.token_file_for(spec) if spec is not None else None
        return bool(path is not None and path.is_file())

    def _ensure_token_watch(self) -> None:
        if self._token_watch_interval_s <= 0:
            return
        task = self._token_watch_task
        if task is not None and not task.done():
            return
        try:
            self._token_watch_task = asyncio.create_task(
                self._token_watch_loop(), name="mcp-token-watch"
            )
        except RuntimeError:
            self._token_watch_task = None  # no running loop (sync construction)

    async def _token_watch_loop(self) -> None:
        interval = max(0.01, self._token_watch_interval_s)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.token_watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("mcp: token-file watch tick failed (continuing)")

    async def token_watch_tick(self) -> list[str]:
        """One pass: every needs-auth OAuth server whose tokens.json mtime
        differs from the last observation is reconnected. The first
        observation after entering needs-auth only records the baseline.
        Returns the names reconnected (tests call this directly)."""
        reconnected: list[str] = []
        for name, conn in list(self._connections.items()):
            if conn.state is not State.NEEDS_AUTH:
                self._token_mtimes.pop(name, None)
                continue
            if name in self.logins_in_progress:
                continue  # the in-process login will reconnect itself
            path = self.token_file_for(conn.spec)
            if path is None:
                continue
            try:
                mtime: float | None = path.stat().st_mtime
            except OSError:
                mtime = None
            if name not in self._token_mtimes:
                self._token_mtimes[name] = mtime
                continue
            previous = self._token_mtimes[name]
            if mtime is None or mtime == previous:
                self._token_mtimes[name] = mtime
                continue
            logger.info(
                "mcp '%s': token file changed while needs-auth (another process "
                "logged in?) — reconnecting", name,
            )
            self.token_reconnects += 1
            self._token_mtimes[name] = mtime
            try:
                await self.reconnect(name, "token file changed on disk")
            except Exception:  # noqa: BLE001
                logger.exception("mcp '%s': token-watch reconnect failed", name)
                continue
            reconnected.append(name)
        return reconnected

    # -- status surface (design 4.4) ---------------------------------------------

    def _schema_chars(self, name: str) -> int:
        total = 0
        for proxy in self._proxies.get(name, []):
            try:
                total += len(json.dumps(proxy.definition.parameters, default=str))
            except Exception:  # noqa: BLE001
                pass
        return total

    def _server_snapshot(self, name: str) -> dict[str, Any]:
        spec = self._catalog.specs.get(name)
        conn = self._connections.get(name)
        if conn is not None:
            snap = conn.snapshot()
        else:
            snap = {
                "server": name,
                "transport": spec.transport if spec else "stdio",
                "state": "disabled" if (spec is None or not spec.enabled) else "not-connected",
                "reason": "disabled in config" if (spec and not spec.enabled) else "",
                "since": None,
                "tool_count": 0,
                "restart_count": 0,
                "last_latency_ms": None,
            }
        snap["enabled"] = bool(spec.enabled) if spec else False
        snap["schema_chars"] = self._schema_chars(name)
        if conn is not None:
            snap.update(conn.details())
        snap["quarantined"] = len(self._quarantine.get(name, []))
        return snap

    def status_snapshot(self) -> list[dict[str, Any]]:
        """One row per configured server (design 4.4)."""
        return [self._server_snapshot(name) for name in self._catalog.specs]

    def pending_quarantine(self) -> list[dict[str, Any]]:
        """Tools the injection scan refused to register (or update), for
        operator review: ``{server, tool, pattern, reason, since}``."""
        out: list[dict[str, Any]] = []
        for name in self._catalog.specs:
            out.extend(dict(entry) for entry in self._quarantine.get(name, []))
        return out

    def _quarantine_tool(self, server: str, remote_name: str, pattern: str, reason: str) -> None:
        entries = self._quarantine.setdefault(server, [])
        for entry in entries:
            if entry["tool"] == remote_name:
                entry.update({"pattern": pattern, "reason": reason, "since": time.time()})
                return
        entries.append({
            "server": server, "tool": remote_name, "pattern": pattern,
            "reason": reason, "since": time.time(),
        })

    def _emit_status_dict(self, event: dict[str, Any]) -> None:
        listener = self.status_listener
        if listener is None:
            return
        try:
            listener(dict(event))
        except Exception:  # noqa: BLE001
            logger.exception("mcp status listener failed (continuing)")

    def _emit_status(self, conn: McpConnection) -> None:
        self._emit_status_dict(self._server_snapshot(conn.spec.name))

    # -- registry integration --------------------------------------------------

    def attach(self, registry: Any) -> None:
        """Keep a weakref so future connects/disconnects mutate this
        registry live. Does NOT register current proxies (the caller —
        build_desktop_registry — already ran them through its loop)."""
        self._registries = [r for r in self._registries if r() is not None]
        if any(r() is registry for r in self._registries):
            return
        self._registries.append(weakref.ref(registry))

    def register_into(self, registry: Any) -> None:
        """attach() + immediately register all current proxy tools."""
        self.attach(registry)
        for proxies in self._proxies.values():
            for proxy in proxies:
                self._register_proxy(registry, proxy)

    def proxy_tools(self) -> list[McpProxyTool]:
        """Wrappers for every ACTIVE/DEGRADED server's surviving tools
        (degraded = still serving, last keepalive failed)."""
        out: list[McpProxyTool] = []
        for name, proxies in self._proxies.items():
            conn = self._connections.get(name)
            if conn is not None and conn.state in ACTIVE_LIKE:
                out.extend(proxies)
        return out

    # -- internals ---------------------------------------------------------------

    def _live_registries(self) -> list[Any]:
        alive: list[Any] = []
        kept: list[weakref.ref[Any]] = []
        for ref in self._registries:
            registry = ref()
            if registry is not None:
                alive.append(registry)
                kept.append(ref)
        self._registries = kept
        return alive

    def _on_state_change(self, conn: McpConnection, old: State, new: State) -> None:
        if new is State.ACTIVE and old not in ACTIVE_LIKE:
            self._activate_server(conn)
        elif old in ACTIVE_LIKE and new not in ACTIVE_LIKE:
            self._unregister_server_tools(conn.spec.name)
        # DEGRADED <-> ACTIVE keeps the tools registered (design 4.1).
        self._emit_status(conn)

    def _activate_server(self, conn: McpConnection) -> None:
        """(Re)activation: rebuild proxies from the fresh tools/list and
        diff against the PREVIOUS activation — unregister stale names,
        register new ones (design 4.2 revive / 4.6 reconnect rule)."""
        name = conn.spec.name
        new_proxies = self._build_proxies(conn)
        new_names = {p.definition.name for p in new_proxies}
        prev_names = self._last_tool_names.get(name, set())
        stale = prev_names - new_names
        registries = self._live_registries()
        for registry in registries:
            for tool_name in sorted(stale):
                try:
                    registry.unregister(tool_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "mcp '%s': failed to unregister stale '%s': %s",
                        name, tool_name, exc,
                    )
        self._proxies[name] = new_proxies
        for registry in registries:
            for proxy in new_proxies:
                self._register_proxy(registry, proxy)
        if prev_names and prev_names != new_names:
            logger.info(
                "mcp '%s': tool set changed on reconnect (+%d new, -%d stale)",
                name, len(new_names - prev_names), len(stale),
            )
        self._last_tool_names[name] = new_names

    def _on_tools_changed(
        self, conn: McpConnection, added: set[str], removed: set[str], changed: set[str]
    ) -> None:
        """listChanged / schema-TTL diff refresh (hermes _refresh_tools):
        unregister only the tools that vanished, register only the new
        ones, and update changed ones IN PLACE so live tool-call ids keep
        pointing at working handlers. Every added/changed description
        re-runs the injection scan; a poisoned re-description quarantines
        the tool (unregistering it if it was live)."""
        if not (added or removed or changed):
            return
        name = conn.spec.name
        if conn.state not in ACTIVE_LIKE or name not in self._proxies:
            return
        current = {p.remote_name: p for p in self._proxies.get(name, [])}
        fresh = {p.remote_name: p for p in self._build_proxies(conn)}
        registries = self._live_registries()
        kept: list[McpProxyTool] = []
        to_register: list[McpProxyTool] = []
        to_unregister: list[str] = []
        for remote_name, proxy in current.items():
            replacement = fresh.get(remote_name)
            if replacement is None:
                # Removed by the server, filtered out, or newly quarantined.
                to_unregister.append(proxy.definition.name)
                continue
            if remote_name in changed:
                proxy.update_remote(
                    description=replacement._description,  # noqa: SLF001
                    input_schema=replacement._input_schema,  # noqa: SLF001
                    summary=replacement._summary,  # noqa: SLF001
                )
            kept.append(proxy)
        for remote_name, proxy in fresh.items():
            if remote_name not in current:
                to_register.append(proxy)
        for registry in registries:
            for tool_name in to_unregister:
                try:
                    registry.unregister(tool_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mcp '%s': failed to unregister '%s': %s", name, tool_name, exc)
            for proxy in to_register:
                self._register_proxy(registry, proxy)
        self._proxies[name] = kept + to_register
        self._last_tool_names[name] = {p.definition.name for p in self._proxies[name]}
        logger.info(
            "mcp '%s': tool refresh applied — +%d registered, -%d unregistered, "
            "%d updated in place",
            name, len(to_register), len(to_unregister),
            len([p for p in kept if p.remote_name in changed]),
        )
        self._emit_status(conn)

    def _register_proxy(self, registry: Any, proxy: McpProxyTool) -> None:
        name = proxy.definition.name
        try:
            existing = registry.get(name)
            if existing is not None and not isinstance(existing, McpProxyTool):
                # Name-squatting guard: never shadow a built-in tool.
                logger.warning(
                    "mcp '%s': refusing to register '%s' — collides with a built-in",
                    proxy.server, name,
                )
                return
            registry.register(proxy)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp '%s': failed to register '%s': %s", proxy.server, name, exc)

    def _unregister_server_tools(self, server: str) -> None:
        proxies = self._proxies.pop(server, [])
        for registry in self._live_registries():
            for proxy in proxies:
                try:
                    registry.unregister(proxy.definition.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "mcp '%s': failed to unregister '%s': %s",
                        server, proxy.definition.name, exc,
                    )

    def _build_proxies(self, conn: McpConnection) -> list[McpProxyTool]:
        """Filter include/exclude, injection-scan descriptions, build proxies."""
        spec = conn.spec
        proxies: list[McpProxyTool] = []
        seen_names: set[str] = set()
        self._quarantine[spec.name] = []
        for remote in conn.tools:
            remote_name = getattr(remote, "name", "") or ""
            if not remote_name:
                continue
            if not _tool_selected(remote_name, spec):
                logger.debug(
                    "mcp '%s': tool '%s' filtered by include/exclude",
                    spec.name,
                    remote_name,
                )
                continue
            description = getattr(remote, "description", None) or ""
            title = getattr(remote, "title", None) or ""
            hit = scan_description_for_injection(f"{title}\n{description}")
            if hit is not None and remote_name in spec.tools_allow_quarantined:
                logger.info(
                    "mcp '%s': tool '%s' matches injection pattern %r but is approved "
                    "via tools.allow_quarantined", spec.name, remote_name, hit,
                )
                hit = None
            if hit is not None:
                logger.warning(
                    "mcp '%s': SKIPPING tool '%s' — description matches injection "
                    "pattern %r (quarantined; see pending_quarantine())",
                    spec.name, remote_name, hit,
                )
                self._quarantine_tool(
                    spec.name, remote_name, hit,
                    "description matches prompt-injection pattern",
                )
                continue
            proxy_name = proxy_tool_name(spec.name, remote_name)
            if proxy_name in seen_names:
                logger.warning(
                    "mcp '%s': sanitized name collision for '%s' (%s); skipped",
                    spec.name, remote_name, proxy_name,
                )
                continue
            seen_names.add(proxy_name)
            schema = getattr(remote, "input_schema", None)
            if schema is not None and not isinstance(schema, dict):
                try:
                    schema = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
                except Exception:  # noqa: BLE001
                    schema = None
            proxies.append(
                McpProxyTool(
                    manager=self,
                    spec=spec,
                    remote_name=remote_name,
                    description=description,
                    input_schema=schema,
                    summary=build_summary(spec.name, description),
                )
            )
        return proxies


def _tool_selected(remote_name: str, spec: McpServerSpec) -> bool:
    included = any(fnmatch.fnmatchcase(remote_name, p) for p in spec.tools_include)
    excluded = any(fnmatch.fnmatchcase(remote_name, p) for p in spec.tools_exclude)
    return included and not excluded
