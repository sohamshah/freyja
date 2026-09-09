"""Slack capability card for gateway sessions.

Tells a Slack-originated session up front what Slack access it has —
which tokens exist in the process environment, who each one acts as,
and which endpoints to use for reading vs. writing — so the agent
doesn't spend a turn grepping ``~/.freyja/.env`` and ``mcp.json`` to
discover its own credentials (and then mis-describe what works).

Built from ENV VAR PRESENCE ONLY. Token values are read exactly once,
inside ``_auth_test``, to populate the ``Authorization`` header of a
single ``auth.test`` call per token per process. They are never
logged, never cached, and never rendered into the block.

Identity resolution is failure-tolerant: any error (network, timeout,
``ok: false``) degrades to "token present, identity unknown" and is
cached so later sessions don't re-pay the timeout. ``reset_identity_cache``
exists for tests.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
SLACK_USER_TOKEN = "SLACK_USER_TOKEN"
SLACK_APP_TOKEN = "SLACK_APP_TOKEN"

AUTH_TEST_URL = "https://slack.com/api/auth.test"
AUTH_TEST_TIMEOUT_S = 5.0

# Only these two are Web API credentials. SLACK_APP_TOKEN (xapp-) is
# Socket Mode plumbing and auth.test would just reject it.
_IDENTITY_VARS = (SLACK_BOT_TOKEN, SLACK_USER_TOKEN)

_DISABLED_MCP_LINE = (
    "The Slack MCP server in mcp.json is disabled (pending OAuth support); "
    "use the Web API path above."
)


@dataclass(frozen=True)
class SlackIdentity:
    """Who a token acts as, per ``auth.test``. Never holds the token."""

    var: str
    user: str | None = None      # handle, e.g. "freyja" / "soham"
    user_id: str | None = None   # U…
    team: str | None = None      # workspace display name
    team_id: str | None = None   # T…
    bot_id: str | None = None    # B… (bot tokens only)

    @property
    def is_bot(self) -> bool:
        return bool(self.bot_id)


# var name -> identity (None = present but lookup failed). Only ever
# keyed by env var NAME; token values do not enter this structure.
_identity_cache: dict[str, SlackIdentity | None] = {}


def reset_identity_cache() -> None:
    """Forget resolved identities so the next call re-runs auth.test."""
    _identity_cache.clear()


def _present(environ: Mapping[str, str], var: str) -> bool:
    return bool((environ.get(var) or "").strip())


def _first_token(raw: str) -> str:
    # The Slack adapter accepts comma-split multi-workspace tokens; the
    # identity card describes the primary workspace.
    return raw.split(",")[0].strip()


def _auth_test(token: str) -> dict[str, Any]:
    """POST ``auth.test`` with a Bearer header; return the decoded JSON.

    Raises on transport errors or non-JSON bodies. The caller treats
    any exception (and ``ok: false``) as "identity unknown".
    """
    req = urllib.request.Request(
        AUTH_TEST_URL,
        data=urllib.parse.urlencode({}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=AUTH_TEST_TIMEOUT_S) as resp:
        body = resp.read()
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("auth.test: response is not a JSON object")
    return payload


def _resolve_one(var: str, token: str) -> SlackIdentity | None:
    try:
        payload = _auth_test(token)
    except Exception as exc:  # noqa: BLE001
        # Log the class only — never the message, which for some
        # transport errors can echo request details.
        logger.info("slack capabilities: auth.test for %s failed (%s)", var, type(exc).__name__)
        return None
    if not payload.get("ok"):
        logger.info(
            "slack capabilities: auth.test for %s returned error %r",
            var, payload.get("error"),
        )
        return None
    return SlackIdentity(
        var=var,
        user=payload.get("user") or None,
        user_id=payload.get("user_id") or None,
        team=payload.get("team") or None,
        team_id=payload.get("team_id") or None,
        bot_id=payload.get("bot_id") or None,
    )


def resolve_slack_identities(
    environ: Mapping[str, str] | None = None,
) -> dict[str, SlackIdentity | None]:
    """Resolve who each present Web API token acts as, once per process.

    Returns ``{var: identity-or-None}`` for every PRESENT token among
    ``SLACK_BOT_TOKEN`` / ``SLACK_USER_TOKEN``. ``None`` means the token
    exists but ``auth.test`` failed; that outcome is cached too so a
    flaky network costs one 5s timeout per process, not one per turn.
    """
    env = os.environ if environ is None else environ
    out: dict[str, SlackIdentity | None] = {}
    for var in _IDENTITY_VARS:
        if not _present(env, var):
            continue
        if var not in _identity_cache:
            _identity_cache[var] = _resolve_one(var, _first_token(env[var]))
        out[var] = _identity_cache[var]
    return out


def _describe(identity: SlackIdentity | None, *, bot: bool) -> str:
    """Render 'acts as …' for one token, degrading when identity is unknown."""
    if identity is None:
        return "acts as the bot user" if bot else "acts as the operator"
    handle = f"@{identity.user}" if identity.user else ("the bot user" if bot else "the operator")
    ids = [identity.user_id] if identity.user_id else []
    if identity.bot_id:
        ids.append(f"bot {identity.bot_id}")
    who = f"acts as {'bot ' if bot else ''}{handle}"
    if ids:
        who += f" ({', '.join(ids)})"
    if identity.team:
        who += f" in workspace {identity.team}"
    return who


def _mcp_line(
    mcp_path: Path | str | None,
    tool_snapshot: Callable[[], Iterable[str]] | None,
) -> str | None:
    """One optional line about the Slack MCP server / tools.

    Live ``mcp__slack*`` tools (via the injected snapshot) win; otherwise
    a disabled slack-ish server in mcp.json yields the "disabled" note.
    Missing module, missing file, or any error → no line.
    """
    if tool_snapshot is not None:
        try:
            names = sorted(n for n in tool_snapshot() if str(n).startswith("mcp__slack"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("slack capabilities: tool snapshot failed (%s)", type(exc).__name__)
            names = []
        if names:
            return (
                "Active Slack MCP tools: " + ", ".join(f"`{n}`" for n in names)
                + " — prefer these over raw curl when they cover the request."
            )
    try:
        from bridge.mcp.config import load_catalog
    except Exception:  # noqa: BLE001
        return None
    if mcp_path is None:
        from bridge.gateway.pid import freyja_home
        mcp_path = freyja_home() / "mcp.json"
    try:
        catalog = load_catalog(mcp_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("slack capabilities: mcp.json unreadable (%s)", type(exc).__name__)
        return None
    slack_specs = [s for name, s in catalog.specs.items() if "slack" in name.lower()]
    if slack_specs and not any(s.enabled for s in slack_specs):
        return _DISABLED_MCP_LINE
    return None


def slack_capability_block(
    environ: Mapping[str, str] | None = None,
    *,
    mcp_path: Path | str | None = None,
    tool_snapshot: Callable[[], Iterable[str]] | None = None,
) -> str:
    """Render the Slack capability card (~10–14 lines) for a system prompt.

    ``environ`` defaults to ``os.environ``; tests pass a dict. ``mcp_path``
    overrides ``~/.freyja/mcp.json``. ``tool_snapshot`` optionally yields
    the live tool names so active ``mcp__slack*`` tools can be listed.
    """
    env = os.environ if environ is None else environ
    has_bot = _present(env, SLACK_BOT_TOKEN)
    has_user = _present(env, SLACK_USER_TOKEN)
    has_app = _present(env, SLACK_APP_TOKEN)

    lines: list[str] = [
        "SLACK ACCESS (this block is the source of truth — do NOT grep or cat "
        "~/.freyja/.env or mcp.json to discover tokens, and never print token values):"
    ]

    if not has_bot and not has_user:
        lines.append(
            "· No SLACK_BOT_TOKEN or SLACK_USER_TOKEN is set in this process — "
            "Slack Web API calls are unavailable. Ask the operator to run "
            "`freyja setup slack` / add tokens to ~/.freyja/.env."
        )
        if has_app:
            lines.append(
                "· SLACK_APP_TOKEN is set (Socket Mode plumbing only, not a Web API credential)."
            )
        return "\n".join(lines)

    identities = resolve_slack_identities(env)

    if has_bot:
        lines.append(
            f"· SLACK_BOT_TOKEN — present; {_describe(identities.get(SLACK_BOT_TOKEN), bot=True)} "
            "— sees only channels it has joined; cannot call search.*."
        )
    else:
        lines.append("· SLACK_BOT_TOKEN — not set; posting as Freyja is unavailable.")

    if has_user:
        lines.append(
            "· SLACK_USER_TOKEN — present; "
            f"{_describe(identities.get(SLACK_USER_TOKEN), bot=False)} "
            "— full visibility: private channels, DMs, standalone canvases, workspace search."
        )
    else:
        lines.append(
            "· SLACK_USER_TOKEN — not set; workspace search (search.messages / search.files / "
            "assistant.search.context), private channels and DMs the bot hasn't joined, and "
            "standalone canvases are UNAVAILABLE — the bot token cannot call search.* and only "
            "sees channels it is a member of. Say so instead of retrying; the operator can add "
            "a user token (xoxp-) to ~/.freyja/.env to enable them."
        )

    if has_app:
        lines.append(
            "· SLACK_APP_TOKEN — present; Socket Mode plumbing only, not a Web API credential."
        )

    if has_user:
        lines.append(
            "READ / SEARCH with the user token: search.messages, search.files, "
            "assistant.search.context, files.list, conversations.history, conversations.replies; "
            "canvas content via files.info then GET its url_private with the same Bearer."
        )
        lines.append(
            "Any WRITE with the user token (post, react, edit, upload) appears as the operator "
            "personally — confirm with the user before doing that."
        )
    else:
        lines.append(
            "READ with the bot token where it is a member: conversations.history, "
            "conversations.replies, files.list, files.info (+ GET url_private)."
        )
    if has_bot:
        lines.append(
            "POST / REACT with the bot token (chat.postMessage, reactions.add, files upload) so "
            "output is attributed to Freyja."
        )

    token_var = SLACK_USER_TOKEN if has_user else SLACK_BOT_TOKEN
    lines.append(
        "Token access from bash: `set -a; source ~/.freyja/.env; set +a` then "
        f'`curl -s -H "Authorization: Bearer ${token_var}" https://slack.com/api/<method>` '
        "— reference the variable, never echo it."
    )

    mcp_line = _mcp_line(mcp_path, tool_snapshot)
    if mcp_line:
        lines.append(mcp_line)

    return "\n".join(lines)
