"""`app_context_changed` is cached only for someone on the allowlist, and
an event with no team is keyed by the workspace, never the bot's user id.

The adapter keeps what each user is looking at in a 500-entry cache that
drops the oldest entry when full, and the operator's turn reads their
entry. Every user's event was cached, so 500 channel members switching
views pushed the operator's entry out. An event with no `team_id` was
keyed by the bot's user id, which no reader ever looks up.
"""

from __future__ import annotations

import inspect
from typing import Any

from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter

OPERATOR = "U0OPERATOR"
VIEWING = {"entities": [{"type": "slack#/types/channel_id", "value": "C123"}]}


class _App:
    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}

    def _keep(self, key: str):
        def deco(fn):
            self.listeners[key] = fn
            return fn
        return deco

    def event(self, name: str):
        return self._keep(name)

    def action(self, name: Any):
        return self._keep(str(name))

    def view(self, name: str):
        return self._keep(name)

    def command(self, name: Any):
        return self._keep("command")


def _adapter(allowed: list[str]) -> SlackAdapter:
    a = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": allowed}))
    a._app = _App()  # type: ignore[assignment]
    a._register_event_handlers()
    a._team_clients = {"T1": object()}  # type: ignore[assignment]
    a._team_bot_user_ids = {"T1": "UBOT"}
    a._bot_user_id = "UBOT"
    return a


async def _context(a: SlackAdapter, user: str, body: dict | None = None, **event: Any) -> None:
    """Deliver app_context_changed with the arguments the listener asks for, like Bolt."""
    fn = a._app.listeners["app_context_changed"]  # type: ignore[union-attr]
    args = {
        "event": {"type": "app_context_changed", "user": user, "context": VIEWING, **event},
        "body": body or {"type": "event_callback", "team_id": "T1"},
        "say": None,
    }
    params = inspect.signature(fn).parameters
    await fn(**{k: v for k, v in args.items() if k in params})


async def test_a_member_not_on_the_allowlist_is_not_cached():
    a = _adapter([OPERATOR])
    await _context(a, "U0MEMBER", team_id="T1")
    assert a._app_context == {}


async def test_other_users_cannot_push_out_the_operators_entry():
    a = _adapter([OPERATOR])
    await _context(a, OPERATOR, team_id="T1")
    for i in range(a._APP_CONTEXT_MAX + 5):
        await _context(a, f"U0M{i:04d}", team_id="T1")
    assert a.describe_app_context("T1", OPERATOR) == "channel C123"


async def test_an_event_with_no_team_is_keyed_by_the_workspace_not_the_bot():
    a = _adapter([OPERATOR])
    await _context(a, OPERATOR)
    assert list(a._app_context) == [("T1", OPERATOR)]
    assert a.describe_app_context("T1", OPERATOR) == "channel C123"


async def test_a_slack_connect_user_is_not_cached_under_star():
    a = _adapter(["*"])
    await _context(a, "U0EXTERNAL", team_id="T1", user_team="T9")
    assert a._app_context == {}
    await _context(a, "U0MEMBER", team_id="T1", user_team="T1")
    assert list(a._app_context) == [("T1", "U0MEMBER")]
