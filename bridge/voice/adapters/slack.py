"""Slack reach verbs (slice 2): slack.read / slack.send.

The morning-session gap these close: the operator asked Freyja to send a
Slack message and got a shrug — the only path was a blind mission spawn.
These verbs talk to the Slack Web API directly through
``slack_sdk.web.async_client.AsyncWebClient`` so a read is instant and a
send is a first-class confirm-tier action with a receipt.

Multi-workspace limitation: the gateway convention is a comma-separated
``SLACK_BOT_TOKEN`` (one token per workspace). The voice verbs use only
the FIRST token — a spoken "read #general" has no channel to disambiguate
workspaces, so voice reach is single-workspace until the catalog grows a
workspace argument.

Caching: channel name→id (conversations_list) and the member directory
(users_list) are cached module-level for 5 minutes; per-user display
names (users_info) are cached for the process lifetime — people don't
rename mid-conversation. Tests monkeypatch ``AsyncWebClient`` on this module and
reset the caches; nothing here ever hits real Slack in the suite.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from slack_sdk.web.async_client import AsyncWebClient

from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_CACHE_TTL_SEC = 300.0  # 5 min — channels/members drift slowly
_LIST_PAGE_LIMIT = 200
_LIST_MAX_ITEMS = 1000  # ~5 pages; a spoken verb never needs more
_READ_DEFAULT_COUNT = 8
_READ_MAX_COUNT = 20
_TEXT_TRUNCATE = 300

_MISSING_TOKEN_SUMMARY = (
    "Slack isn't wired — run `freyja setup slack` or set SLACK_BOT_TOKEN"
)

# name(lower) → channel id, keyed to the token that fetched it so a
# token swap invalidates rather than serving another workspace's map.
_channel_cache: dict[str, Any] = {"token": None, "expires": 0.0, "by_name": {}}
# users_list snapshot for DM name resolution (same keying).
_member_cache: dict[str, Any] = {"token": None, "expires": 0.0, "members": []}
# user id → display name (users_info) — process-lifetime.
_name_cache: dict[str, str] = {}


def _reset_caches() -> None:
    """Test seam — drop every module-level cache."""
    _channel_cache.update({"token": None, "expires": 0.0, "by_name": {}})
    _member_cache.update({"token": None, "expires": 0.0, "members": []})
    _name_cache.clear()


def _token() -> Optional[str]:
    """First token from the comma-separated SLACK_BOT_TOKEN (see module
    docstring for the single-workspace limitation).

    Falls back to the setup wizard's ~/.freyja/.env: the gateway daemon
    merges that file into its environment at startup, but the DESKTOP
    bridge does not — observed live 2026-07-08 as "Slack isn't wired"
    while the gateway sat happily connected with the very same token."""
    raw = os.environ.get("SLACK_BOT_TOKEN", "")
    if not raw.strip():
        try:
            from bridge.gateway.setup.env_writer import read_env

            raw = str(read_env().get("SLACK_BOT_TOKEN") or "")
        except Exception:  # noqa: BLE001 — fallback path only, never fatal
            raw = ""
    first = raw.split(",")[0].strip()
    return first or None


def _other_workspace_hint() -> str:
    """Voice uses only the FIRST workspace token; when more are configured,
    say so instead of a bare not-found (the channel may exist next door)."""
    raw = os.environ.get("SLACK_BOT_TOKEN", "")
    if "," in raw:
        return " (note: voice searches only the first Slack workspace)"
    return ""


def _terse_error(exc: Exception) -> str:
    """One line for the receipt/HUD — the Slack error code when the SDK
    surfaced one, else the exception's first line. Never a traceback."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            code = str(resp.get("error") or "")
        except Exception:  # noqa: BLE001 — response shape is theirs, not ours
            code = ""
        if code:
            return code
    return str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__


async def _channel_map(client: AsyncWebClient, token: str) -> dict[str, str]:
    now = time.time()
    if _channel_cache["token"] == token and now < _channel_cache["expires"]:
        return _channel_cache["by_name"]
    by_name: dict[str, str] = {}
    cursor: Optional[str] = None
    fetched = 0
    while fetched < _LIST_MAX_ITEMS:
        resp = await client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=_LIST_PAGE_LIMIT,
            cursor=cursor,
        )
        channels = list(resp.get("channels") or [])
        for ch in channels:
            name = str(ch.get("name") or "")
            cid = str(ch.get("id") or "")
            if name and cid:
                by_name[name.lower()] = cid
        fetched += len(channels)
        cursor = str((resp.get("response_metadata") or {}).get("next_cursor") or "") or None
        if cursor is None:
            break
    _channel_cache.update({"token": token, "expires": now + _CACHE_TTL_SEC, "by_name": by_name})
    return by_name


async def _resolve_channel(
    client: AsyncWebClient, token: str, name: str
) -> Optional[str]:
    by_name = await _channel_map(client, token)
    return by_name.get(name.lower())


async def _display_name(client: AsyncWebClient, user_id: str) -> str:
    if not user_id:
        return ""
    cached = _name_cache.get(user_id)
    if cached is not None:
        return cached
    try:
        resp = await client.users_info(user=user_id)
        user = dict(resp.get("user") or {})
        profile = dict(user.get("profile") or {})
        name = str(
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception:  # noqa: BLE001 — a name lookup must not sink the read
        name = user_id
    _name_cache[user_id] = name
    return name


async def _members(client: AsyncWebClient, token: str) -> list[dict[str, Any]]:
    now = time.time()
    if _member_cache["token"] == token and now < _member_cache["expires"]:
        return _member_cache["members"]
    members: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    while len(members) < _LIST_MAX_ITEMS:
        resp = await client.users_list(limit=_LIST_PAGE_LIMIT, cursor=cursor)
        members.extend(dict(m) for m in (resp.get("members") or []))
        cursor = str((resp.get("response_metadata") or {}).get("next_cursor") or "") or None
        if cursor is None:
            break
    _member_cache.update({"token": token, "expires": now + _CACHE_TTL_SEC, "members": members})
    return members


async def _resolve_user(
    client: AsyncWebClient, token: str, query: str
) -> tuple[Optional[tuple[str, str]], list[str]]:
    """Match a spoken name to a member: ((user id, display name), candidates).

    A DM recipient is resolved AFTER the operator's spoken confirmation
    (the confirm prompt can only echo the query), so fuzzy matching here
    is how you message the wrong Alex with no undo. Rule: a UNIQUE match
    wins — exact match on display/real/handle name, or a unique
    prefix/substring match. Anything ambiguous returns (None, candidate
    labels) so the model reads the options back instead of guessing."""
    q = query.strip().lower()
    if not q:
        return None, []
    exact: list[tuple[str, str]] = []
    fuzzy: list[tuple[str, str]] = []
    for member in await _members(client, token):
        if member.get("deleted"):
            continue
        profile = dict(member.get("profile") or {})
        names = [
            str(profile.get("display_name") or ""),
            str(profile.get("real_name") or member.get("real_name") or ""),
            str(member.get("name") or ""),
        ]
        label = names[0] or names[1] or names[2]
        uid = str(member.get("id") or "")
        if not uid or not label:
            continue
        lows = [n.lower() for n in names if n]
        if q in lows:
            exact.append((uid, label))
        elif any(low.startswith(q) or q in low for low in lows):
            fuzzy.append((uid, label))
    if len(exact) == 1:
        return exact[0], []
    if not exact and len(fuzzy) == 1:
        return fuzzy[0], []
    pool = exact or fuzzy
    return None, sorted({label for _uid, label in pool})[:6]


def _coerce_count(raw: Any) -> int:
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return _READ_DEFAULT_COUNT
    return max(1, min(_READ_MAX_COUNT, count))


async def _read(args: dict[str, Any]) -> VerbResult:
    token = _token()
    if token is None:
        return VerbResult(ok=False, summary=_MISSING_TOKEN_SUMMARY, data={"setup": "slack"})
    channel = str(args.get("channel") or "").strip().lstrip("#")
    if not channel:
        return VerbResult(ok=False, summary="which channel?", error="missing_channel")
    count = _coerce_count(args.get("count", _READ_DEFAULT_COUNT))
    client = AsyncWebClient(token=token)
    try:
        channel_id = await _resolve_channel(client, token, channel)
        if channel_id is None:
            return VerbResult(
                ok=False, summary=f"no channel named #{channel}", error="unknown_channel" + _other_workspace_hint()
            )
        resp = await client.conversations_history(channel=channel_id, limit=count)
        raw_messages = list(resp.get("messages") or [])
        messages: list[dict[str, str]] = []
        # History arrives newest-first; flip so the digest reads in order.
        for message in reversed(raw_messages):
            who = await _display_name(client, str(message.get("user") or ""))
            if not who:
                who = str(message.get("username") or "") or "unknown"
            text = str(message.get("text") or "")[:_TEXT_TRUNCATE]
            try:
                ts = float(message.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            when = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
            messages.append({"who": who, "text": text, "when": when})
    except Exception as exc:  # noqa: BLE001 — API failures become terse summaries
        err = _terse_error(exc)
        return VerbResult(ok=False, summary=f"Slack read failed: {err}", error=err)
    # No say= override: the model reads data.messages and speaks its own
    # compact digest — canned prose would just fight the conversation.
    return VerbResult(
        ok=True,
        summary=f"read {len(messages)} from #{channel}",
        data={"messages": messages},
    )


async def _send(args: dict[str, Any]) -> VerbResult:
    token = _token()
    if token is None:
        return VerbResult(ok=False, summary=_MISSING_TOKEN_SUMMARY, data={"setup": "slack"})
    text = str(args.get("text") or "").strip()
    if not text:
        return VerbResult(ok=False, summary="nothing to send", error="missing_text")
    channel = str(args.get("channel") or "").strip().lstrip("#")
    user = str(args.get("user") or "").strip()
    if not channel and not user:
        return VerbResult(
            ok=False, summary="send it where? name a channel or a person", error="missing_target"
        )
    client = AsyncWebClient(token=token)
    try:
        if channel:
            target_id = await _resolve_channel(client, token, channel)
            if target_id is None:
                return VerbResult(
                    ok=False, summary=f"no channel named #{channel}", error="unknown_channel" + _other_workspace_hint()
                )
            label = f"#{channel}"
        else:
            match, candidates = await _resolve_user(client, token, user)
            if match is None:
                if candidates:
                    listing = ", ".join(candidates)
                    return VerbResult(
                        ok=False,
                        summary=f"ambiguous recipient {user} — {len(candidates)} matches",
                        data={"candidates": candidates},
                        error=(
                            f"multiple members match {user}: {listing}. "
                            "Ask which one, then send again with the full name."
                        ),
                    )
                return VerbResult(
                    ok=False, summary=f"no Slack member matching {user}", error="unknown_user"
                )
            user_id, name = match
            opened = await client.conversations_open(users=[user_id])
            target_id = str((opened.get("channel") or {}).get("id") or "")
            if not target_id:
                return VerbResult(
                    ok=False, summary=f"couldn't open a DM with {name}", error="dm_open_failed"
                )
            label = f"@{name}"
        await client.chat_postMessage(channel=target_id, text=text)
    except Exception as exc:  # noqa: BLE001
        err = _terse_error(exc)
        return VerbResult(ok=False, summary=f"Slack send failed: {err}", error=err)
    # No undo closure: a sent message is sent — deleting it later is a
    # different action, not a reversal.
    return VerbResult(ok=True, summary=f"→ {label}: {text[:60]}")


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="slack.read",
            description="Read recent messages from a Slack channel by name",
            params={
                "channel": {"type": "string", "description": "channel name, # optional"},
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _READ_MAX_COUNT,
                    "description": "how many messages (default 8)",
                },
            },
            required=["channel"],
            tier="auto",
            run=_read,
        )
    )
    registry.register(
        Verb(
            name="slack.send",
            description="Send a Slack message to a channel, or DM a person by name",
            params={
                "channel": {"type": "string", "description": "channel name, # optional"},
                "user": {"type": "string", "description": "person's display or real name (DM)"},
                "text": {"type": "string"},
            },
            required=["text"],
            tier="confirm",
            run=_send,
        )
    )
