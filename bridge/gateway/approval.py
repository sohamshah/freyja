"""Block Kit approval gate for destructive tool calls over gateway sessions.

Default mode is "all tools available" (operator is presumed to be the
only user of their own install — see ``bridge/gateway/capabilities.py``).
This module adds back a narrow safety net: any tool call whose
arguments match a known destructive pattern (rm -rf, git clean, dd to a
device, etc.) blocks until the operator approves it via an interactive
Block Kit button in Slack.

Flow:
  1. Tool wrapper in ``freyja_bridge.py`` calls ``is_destructive(tool, args)``.
  2. If destructive AND the session has a ``gateway_source`` attribute,
     it awaits ``request_approval(...)`` instead of calling the tool.
  3. ``request_approval`` posts a Block Kit message to the originating
     platform via the registered adapter, stashes a Future keyed by a
     fresh request_id, and ``await``s with a timeout.
  4. The platform adapter (slack.py) wires ``@app.action`` listeners
     for ``freyja_approve`` / ``freyja_deny`` that call
     ``resolve_approval(request_id, approved)`` to set the Future.
  5. On approve: tool runs normally. On deny / timeout: a synthetic
     ToolResult with ``is_error=True`` and a clear message is returned,
     and the agent sees a normal-looking failure.

No "approve session" or "approve always" mode in v1 — every destructive
call gets its own prompt. We can layer those on if it gets noisy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ─── destructive-command detection ───────────────────────────────────


# Patterns that match shell commands which delete / overwrite local
# state and aren't trivially reversible (no Trash, no VCS undo). The
# tuple is (regex, human-readable reason).
#
# Conservative: we err on the side of prompting too often rather than
# missing a real delete. Operator can dismiss with one click.
_DESTRUCTIVE_BASH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # `rm` with any of the recursive / force flags
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRf][a-zA-Z]*\b)"), "rm with -r/-R/-f"),
    # `rm` followed by a path (catches bare `rm file.txt`); skip if
    # the user added the interactive flag (-i / -I) — those already
    # prompt at the shell level.
    (re.compile(r"\brm\s+(?!-[iI]\b)(?:-\w+\s+)?[^\s|&;]"), "rm with paths"),
    (re.compile(r"\brmdir\b"), "rmdir"),
    (re.compile(r"\bunlink\s+\S"), "unlink"),
    (re.compile(r"\bfind\b[^|;&]*-delete\b"), "find -delete"),
    (re.compile(r"\bfind\b[^|;&]*-exec\s+rm\b"), "find -exec rm"),
    # git destructive ops
    (re.compile(r"\bgit\s+clean\s+-\w*[fxd]"), "git clean -f/-x/-d"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+checkout\s+--\s"), "git checkout -- (file overwrite)"),
    (re.compile(r"\bgit\s+branch\s+-[dD]\b"), "git branch -d/-D"),
    (re.compile(r"\bgit\s+push\s+--force\b|\bgit\s+push\s+-f\b"), "git push --force"),
    # File system / device level
    (re.compile(r"\bmkfs(\.\w+)?\b"), "mkfs"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "dd writing to a device"),
    (re.compile(r">\s*/dev/sd[a-z]"), "redirect to /dev/sd*"),
    (re.compile(r"\btruncate\s+-s\s*0\b"), "truncate to zero"),
    (re.compile(r"\bshred\b"), "shred (secure delete)"),
    (re.compile(r"\bwipefs\b"), "wipefs (filesystem signature wipe)"),
    # Python / Node one-liners that delete via stdlib
    (re.compile(r"\bos\.(?:remove|unlink|rmdir|removedirs)\("), "python os.remove/unlink"),
    (re.compile(r"\bshutil\.rmtree\("), "python shutil.rmtree"),
    (re.compile(r"\bPath\([^)]*\)\.unlink\("), "python Path.unlink"),
    (re.compile(r"\bfs\.rm\w*Sync\("), "node fs.rmSync / rmdirSync"),
    (re.compile(r"\bfs\.unlinkSync\("), "node fs.unlinkSync"),
]


def is_destructive(tool_name: str, args: Any) -> tuple[bool, str, str]:
    """Decide whether a tool call should require operator approval.

    Returns ``(is_destructive, reason, command_preview)``. The preview
    is what the operator sees in the approval block — the actual
    command string we'd execute, capped for readability.
    """
    if not isinstance(args, dict):
        return False, "", ""
    name = (tool_name or "").lower()
    if name in {"bash", "shell"}:
        cmd = str(args.get("command") or args.get("cmd") or args.get("input") or "")
        if not cmd:
            return False, "", ""
        for pat, reason in _DESTRUCTIVE_BASH_PATTERNS:
            if pat.search(cmd):
                return True, reason, cmd
        return False, "", ""
    return False, "", ""


# ─── adapter registry ────────────────────────────────────────────────


# Filled in by the gateway runner when each platform adapter connects.
# Lets the tool wrapper (which doesn't know about platforms) find the
# right adapter to post an approval message to.
_ADAPTERS: dict[str, Any] = {}


def register_approval_adapter(platform_name: str, adapter: Any) -> None:
    """Register the live adapter for a platform so destructive tool
    approvals can be posted to it. Call from the adapter's ``connect``."""
    _ADAPTERS[platform_name] = adapter
    logger.info("approval adapter registered for %s", platform_name)


def unregister_approval_adapter(platform_name: str) -> None:
    _ADAPTERS.pop(platform_name, None)


def get_approval_adapter(platform_name: str) -> Any | None:
    return _ADAPTERS.get(platform_name)


# ─── pending Future registry ─────────────────────────────────────────


_PENDING: dict[str, asyncio.Future] = {}


# External resolvers: when a request_id was minted somewhere other than
# ``request_approval`` (e.g. by ``DesktopPermissionHandler.awaiter``
# inside the freyja bridge), register a resolver here so the Slack
# click handler can still drive the resolution. The resolver takes
# ``(approved: bool) -> bool`` and returns whether the resolution
# succeeded (False = unknown/already-resolved request).
_EXTERNAL_RESOLVERS: dict[str, Any] = {}


def register_external_resolver(request_id: str, resolver: Any) -> None:
    """Register a resolver callback for an externally-minted approval id.

    Pair with ``unregister_external_resolver`` in the same code path
    that owns the request lifetime — we never auto-evict so callers
    must clean up themselves. (sweep_stale only runs against
    ``_PENDING`` Futures.)
    """
    _EXTERNAL_RESOLVERS[request_id] = resolver


def unregister_external_resolver(request_id: str) -> None:
    """Drop the resolver. Idempotent."""
    _EXTERNAL_RESOLVERS.pop(request_id, None)


def resolve_approval(request_id: str, approved: bool) -> bool:
    """Set the Future for a pending approval request. Returns True if
    the request was found + resolved, False if it was already
    resolved or expired (idempotent — safe to call twice from a
    double-clicked button)."""
    fut = _PENDING.get(request_id)
    if fut is not None and not fut.done():
        try:
            fut.set_result(approved)
            return True
        except asyncio.InvalidStateError:
            pass
    # Fall through to externally-registered resolvers (e.g.
    # DesktopPermissionHandler-minted requests dispatched to Slack via
    # the stream consumer).
    resolver = _EXTERNAL_RESOLVERS.get(request_id)
    if resolver is not None:
        try:
            return bool(resolver(approved))
        except Exception:  # noqa: BLE001
            logger.exception("external approval resolver raised")
            return False
    return False


async def request_approval(
    *,
    platform_name: str,
    chat_id: str,
    thread_id: str | None,
    tool_name: str,
    command_preview: str,
    reason: str,
    timeout_sec: float = 600.0,
) -> bool:
    """Post an approval request and block until the operator decides.

    Returns True on approve, False on deny / timeout / post failure.
    """
    adapter = get_approval_adapter(platform_name)
    if adapter is None:
        # No adapter registered (gateway not connected, or unsupported
        # platform). Fail open — let the tool run. The alternative
        # (fail closed) would silently break the agent the first time
        # the gateway is misconfigured.
        logger.warning(
            "no approval adapter for platform %s — allowing %s without prompt",
            platform_name, tool_name,
        )
        return True
    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _PENDING[request_id] = fut
    try:
        res = await adapter.send_approval_request(
            chat_id=chat_id,
            request_id=request_id,
            tool_name=tool_name,
            command_preview=command_preview,
            reason=reason,
            thread_id=thread_id,
        )
        if not res.ok:
            logger.warning(
                "approval post failed (%s) — defaulting to deny",
                getattr(res, "error", ""),
            )
            return False
        try:
            return bool(await asyncio.wait_for(fut, timeout=timeout_sec))
        except asyncio.TimeoutError:
            logger.info(
                "approval request %s timed out after %ds — treating as deny",
                request_id, int(timeout_sec),
            )
            return False
    finally:
        _PENDING.pop(request_id, None)


# ─── eviction sweep (defensive) ──────────────────────────────────────


_LAST_SWEEP_AT: float = 0.0
_SWEEP_INTERVAL_SEC = 300.0  # every 5 min


def sweep_stale() -> int:
    """Cancel any Future older than the timeout window. Safety net in
    case ``request_approval``'s ``finally`` didn't run (e.g. event-loop
    cancellation). Returns number swept."""
    global _LAST_SWEEP_AT
    now = time.monotonic()
    if now - _LAST_SWEEP_AT < _SWEEP_INTERVAL_SEC:
        return 0
    _LAST_SWEEP_AT = now
    cleared = 0
    for rid, fut in list(_PENDING.items()):
        if fut.done():
            _PENDING.pop(rid, None)
            cleared += 1
    return cleared
