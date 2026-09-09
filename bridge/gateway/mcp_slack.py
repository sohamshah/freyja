"""Slack surface for ``/mcp`` (card_019): full parity with the desktop
``mcp_command`` set, OAuth hand-off via chat, and elicitation via a
threaded reply.

Everything platform-specific about MCP management in the gateway lives
here so ``bridge/gateway/run.py`` only needs a two-line dispatch.

* :func:`handle_mcp_slash` — parse ``/mcp <action> ...`` text with the
  shared grammar (``bridge.mcp.commands.parse_mcp_command``), lazily build
  the process McpManager with the non-interactive OAuth auth factory and a
  :class:`SlackElicitationBridge`, run the shared handler with
  ``surface="gateway"`` and post the result. ``login`` / ``reauth`` run as
  background tasks: the authorize URL is posted (ephemeral to the caller)
  when the flow produces it and the outcome when it lands.

* :class:`SlackElicitationBridge` — ``ElicitationBridge`` whose emit posts
  the server's question (with the requested fields) into the Slack thread
  of the gateway session whose turn is running the tool call (fallback:
  the conversation that last used ``/mcp``). The operator answers with
  ``/mcp answer <requestId> key=value ... | decline | cancel`` — as a slash
  command, or as a plain threaded reply (Slack slash commands do not fire
  inside threads; :func:`promote_inline_mcp_answer` upgrades such a reply
  to the slash router).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from bridge.mcp.elicitation import ElicitationBridge, describe_schema_fields

logger = logging.getLogger(__name__)

MCP_HELP = (
    "*MCP servers* — `/mcp <action> ...`\n"
    "• `/mcp status [server]` — state, tools, auth, token expiry\n"
    "• `/mcp enable|disable <server>` — connect / disconnect (flips mcp.json)\n"
    "• `/mcp reload` — re-read `~/.freyja/mcp.json`\n"
    "• `/mcp login <server>` — OAuth sign-in (link is posted here)\n"
    "• `/mcp logout <server>` · `/mcp reauth <server>|--all`\n"
    "• `/mcp add <url|command ...> [--name N] [--header K=${VAR}] [--enable]`\n"
    "• `/mcp remove <server> [--purge]` · `/mcp test <server>`\n"
    "• `/mcp tools [server]` — registered tools + quarantine\n"
    "• `/mcp approve <server> <tool>` — register a quarantined tool after review\n"
    "• `/mcp catalog list|search <q>|info <name>|install <name> [--enable]`\n"
    "• `/mcp call <server> <tool> [json]`\n"
    "• `/mcp answer <requestId> key=value ... | decline` — reply to a server prompt"
)

_INLINE_ANSWER_RE = re.compile(r"^\s*/?mcp\s+answer\b", re.IGNORECASE)
_BACKGROUND_CHAT_ACTIONS = frozenset({"login", "reauth"})


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_result_for_slack(result: dict[str, Any]) -> str:
    """mcp_command_result -> Slack mrkdwn. The shared ``message`` is the
    body; fixed-width tables (status/tools/catalog/test) are fenced so
    columns line up."""
    message = str(result.get("message") or "").rstrip()
    action = str(result.get("action") or "")
    if not message:
        return "Done." if result.get("ok") else "Failed."
    if action == "status":
        if not result.get("servers"):
            return (
                "No MCP servers configured. `/mcp add <url>` or "
                "`/mcp catalog list`, then `/mcp reload`."
            )
        data = result.get("data") or {}
        table = str(data.get("table") or message)
        details = str(data.get("details") or "")
        out = "```\n" + table + "\n```"
        if details:
            out += "\n" + details
        return out
    if action in ("tools", "catalog") and "\n" in message:
        head, _, rest = message.partition("\n")
        return head + "\n```\n" + rest + "\n```"
    if action in ("test", "reauth"):
        return "```\n" + message + "\n```"
    prefix = "" if result.get("ok") else ":warning: "
    return prefix + message


def format_oauth_url_for_slack(event: dict[str, Any]) -> str:
    expires = event.get("expiresInS")
    minutes = f" (link valid ~{int(float(expires) // 60)} min)" if expires else ""
    return (
        f":key: Authorize *{event.get('server')}*: <{event.get('url')}|open the sign-in page>"
        f"{minutes}\nAfter approving, the browser redirects to `{event.get('redirectUri')}` "
        "on the gateway host; I'll confirm here when the token lands."
    )


def format_oauth_result_for_slack(event: dict[str, Any]) -> str:
    server = event.get("server")
    if event.get("ok"):
        scopes = event.get("scopes") or []
        return (
            f":white_check_mark: *{server}* authenticated"
            + (f" (scopes: {' '.join(scopes)})" if scopes else "")
        )
    return f":x: *{server}* login failed: {event.get('error')}"


def format_elicitation_for_slack(event: dict[str, Any]) -> str:
    server = event.get("server")
    request_id = event.get("requestId")
    timeout_s = int(float(event.get("timeoutS") or 0))
    lines = [f":question: MCP server *{server}* is asking:", f"> {event.get('message')}"]
    if event.get("mode") == "url":
        lines.append(f"It wants you to visit: <{event.get('url')}>")
        lines.append(
            f"When done reply `/mcp answer {request_id} accept` — or "
            f"`/mcp answer {request_id} decline`."
        )
    else:
        fields = describe_schema_fields(event.get("requestedSchema"))
        if fields:
            lines.append("Fields:")
            lines.extend(f"• `{f}`" for f in fields)
            example = " ".join(f"{f.split(':', 1)[0]}=..." for f in fields[:3])
            lines.append(f"Reply `/mcp answer {request_id} {example}` (yes/no for booleans)")
        else:
            lines.append(f"Reply `/mcp answer {request_id} accept`")
        lines.append(f"or `/mcp answer {request_id} decline`.")
    if timeout_s:
        lines.append(f"_No answer within {timeout_s}s cancels the request._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inline threaded replies
# ---------------------------------------------------------------------------

def promote_inline_mcp_answer(message: Any) -> bool:
    """A plain thread reply ``/mcp answer <id> ...`` (or ``mcp answer ...``)
    becomes a slash command so the /mcp router handles it. Returns True
    when promoted. Only ``answer`` is promoted — nothing else in a thread
    should be hijacked from the agent."""
    if getattr(message, "is_slash_command", False):
        return False
    text = str(getattr(message, "text", "") or "")
    if not _INLINE_ANSWER_RE.match(text):
        return False
    body = text.strip()
    body = body[1:] if body.startswith("/") else body
    body = body[len("mcp"):].strip()
    message.is_slash_command = True
    message.slash_command_name = "mcp"
    message.slash_command_args = body
    return True


# ---------------------------------------------------------------------------
# Elicitation bridge
# ---------------------------------------------------------------------------

class SlackElicitationBridge(ElicitationBridge):
    """Posts elicitations into the Slack conversation running the tool call.

    Target selection (the MCP elicitation callback runs on the SDK's
    receive loop, so no session context is available): every gateway
    session with an in-flight turn (``pending_task`` not done) and a
    ``gateway_source`` gets the post — that is the session whose tool call
    triggered it (normally exactly one). With none in flight, the
    conversation that last issued ``/mcp`` is used; with neither, the
    request is declined (logged) rather than left hanging.
    """

    def __init__(self, daemon: Any, *, timeout_s: float) -> None:
        super().__init__(self._post, timeout_s=timeout_s)
        self._daemon = daemon
        self.last_source: Any = None
        self.posted: list[dict[str, Any]] = []

    def note_source(self, source: Any) -> None:
        self.last_source = source

    def _targets(self) -> list[Any]:
        state = getattr(self._daemon, "state", None)
        sessions = getattr(state, "sessions", None) or {}
        targets: list[Any] = []
        for sess in list(sessions.values()):
            source = getattr(sess, "gateway_source", None)
            pending = getattr(sess, "pending_task", None)
            if source is None or pending is None or pending.done():
                continue
            targets.append(source)
        if not targets and self.last_source is not None:
            targets.append(self.last_source)
        return targets

    async def _post(self, event: dict[str, Any]) -> None:
        targets = self._targets()
        if not targets:
            raise RuntimeError("no gateway conversation to route the elicitation to")
        text = format_elicitation_for_slack(event)
        sent = 0
        for source in targets:
            adapter = self._daemon._adapter_for_platform(source.platform)  # noqa: SLF001
            if adapter is None:
                continue
            try:
                await adapter.send(
                    source.chat_id, text, thread_id=getattr(source, "thread_id", None),
                )
                sent += 1
                self.posted.append({"requestId": event.get("requestId"), "chat_id": source.chat_id})
            except Exception:  # noqa: BLE001
                logger.exception("mcp elicitation: Slack post failed")
        if sent == 0:
            raise RuntimeError("no adapter accepted the elicitation post")


# ---------------------------------------------------------------------------
# Manager + router
# ---------------------------------------------------------------------------

def ensure_manager(daemon: Any) -> Any:
    """The gateway process's McpManager (lazily built on first /mcp with
    the v2 hooks). When it is created here with servers configured, the
    connections are started in the background so sessions get the tools."""
    from bridge.mcp import McpManager
    from bridge.mcp.commands import non_interactive_auth_factory, spawn_background
    from bridge.mcp.connection import DEFAULT_ELICITATION_TIMEOUT_S

    state = daemon.state
    manager = getattr(state, "mcp_manager", None)
    if manager is not None:
        if not isinstance(getattr(manager, "approval_handler", None), ElicitationBridge):
            # Manager built elsewhere without a chat-capable handler: attach
            # ours (applies to connections created from now on).
            manager.approval_handler = SlackElicitationBridge(
                daemon, timeout_s=getattr(manager, "elicitation_timeout_s", None)
                or DEFAULT_ELICITATION_TIMEOUT_S,
            )
        return manager
    bridge = SlackElicitationBridge(daemon, timeout_s=DEFAULT_ELICITATION_TIMEOUT_S)
    manager = McpManager.load(
        Path.home() / ".freyja" / "mcp.json",
        run_dir=Path.home() / ".freyja" / "mcp-run",
        auth_factory=non_interactive_auth_factory,
        approval_handler=bridge,
        elicitation_timeout_s=DEFAULT_ELICITATION_TIMEOUT_S,
    )
    state.mcp_manager = manager
    if manager.server_count > 0:
        spawn_background(manager.start(), name="mcp-gateway-start")
    return manager


def _slack_event_sink(adapter: Any, source: Any) -> Any:
    """Event sink for the shared handler: OAuth hand-off events become
    Slack posts (ephemeral to the caller — the authorize URL carries
    PKCE state); mcp_status snapshots are not chat-worthy."""

    async def _sink(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "mcp_oauth_url":
            text = format_oauth_url_for_slack(event)
        elif etype == "mcp_oauth_result":
            text = format_oauth_result_for_slack(event)
        else:
            return
        await adapter.send(
            source.chat_id, text,
            thread_id=getattr(source, "thread_id", None),
            ephemeral_user_id=getattr(source, "user_id", None),
        )

    return _sink


async def handle_mcp_slash(daemon: Any, message: Any, adapter: Any) -> bool:
    """Full ``/mcp`` router for chat surfaces. Always replies; returns True."""
    from bridge.mcp.commands import (
        handle_mcp_command,
        parse_mcp_command,
        spawn_background,
    )

    source = message.source
    text_args = (message.slash_command_args or "").strip()

    async def _reply(text: str) -> None:
        await adapter.send(
            source.chat_id, text,
            thread_id=getattr(source, "thread_id", None),
            ephemeral_user_id=getattr(source, "user_id", None),
            raw_hint=getattr(message, "raw", None),
        )

    if text_args.lower() in ("help", "-h", "--help"):
        await _reply(MCP_HELP)
        return True
    if daemon.state is None:
        await _reply("Gateway state not ready yet — try again in a moment.")
        return True
    try:
        manager = ensure_manager(daemon)
    except Exception as exc:  # noqa: BLE001
        logger.exception("/mcp: manager init failed")
        await _reply(f":warning: MCP unavailable: {exc}")
        return True
    bridge = getattr(manager, "approval_handler", None)
    if isinstance(bridge, SlackElicitationBridge):
        bridge.note_source(source)

    cmd = parse_mcp_command(text_args)
    action = cmd["action"]
    sink = _slack_event_sink(adapter, source)

    async def _run() -> dict[str, Any]:
        try:
            return await handle_mcp_command(
                manager, cmd, surface="gateway", emit=sink,
                elicitation=bridge if isinstance(bridge, ElicitationBridge) else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("/mcp %s failed", action)
            return {"ok": False, "action": action, "message": f"/mcp {action} errored: {exc}"}

    if action in _BACKGROUND_CHAT_ACTIONS:
        do_all = "--all" in cmd.get("args", [])
        target = cmd.get("server") or ("all OAuth servers" if do_all else "")
        if target:
            await _reply(
                f":hourglass_flowing_sand: Starting `/mcp {action} {target}` — "
                "I'll post the sign-in link here."
            )

        async def _later() -> None:
            started = time.monotonic()
            result = await _run()
            logger.info(
                "/mcp %s finished in %.1fs ok=%s",
                action, time.monotonic() - started, result.get("ok"),
            )
            await _reply(format_result_for_slack(result))

        spawn_background(_later(), name=f"mcp-slack-{action}")
        return True

    result = await _run()
    await _reply(format_result_for_slack(result))
    return True


__all__ = [
    "MCP_HELP",
    "SlackElicitationBridge",
    "ensure_manager",
    "format_elicitation_for_slack",
    "format_oauth_result_for_slack",
    "format_oauth_url_for_slack",
    "format_result_for_slack",
    "handle_mcp_slash",
    "promote_inline_mcp_answer",
]
