"""A sidebar rename from someone not on the allowlist is put back in Slack.

Slack applies a session rename before it tells the app, so refusing it
in the gateway (3ba1e42) left the new title showing in everyone's
sidebar. The gateway now renames the session back with
``agents.sessions.rename``: to the title it stored, or to the title
Slack says the session had before.
"""

from __future__ import annotations

from typing import Any

from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.slack_bindings import ThreadBindings

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def chat_postEphemeral(self, **kw: Any) -> dict:
        self.calls.append(("ephemeral", kw))
        return {"ok": True}

    async def agents_sessions_rename(self, **kw: Any) -> dict:
        self.calls.append(("rename", kw))
        return {"ok": True}


def _adapter(tmp_path) -> tuple[SlackAdapter, _Client, list[dict]]:
    a = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    client = _Client()
    a._app = object()  # type: ignore[assignment]
    a._team_clients = {"T1": client}  # type: ignore[assignment]
    a._team_bot_user_ids = {"T1": "UBOT"}
    a._bot_user_id = "UBOT"
    a.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    seen: list[dict] = []

    async def renamed(info: dict[str, Any]) -> None:
        seen.append(info)

    a.on_session_title_changed = renamed
    return a, client, seen


def _rename(user: str, title: str = "Mallory's now", previous: str = "Deploy") -> dict:
    return {"channel": "C1", "thread_ts": "5.5", "title": title, "previous_title": previous,
            "user": user, "team_id": "T1"}


def _renames(client: _Client) -> list[dict]:
    return [kw for name, kw in client.calls if name == "rename"]


async def test_a_refused_rename_is_put_back_to_the_stored_title(tmp_path):
    a, client, seen = _adapter(tmp_path)
    a.bindings.set_title("C1", "5.5", "Deploy failure", by_user=False)
    await a._handle_session_title_changed(_rename(MEMBER, previous="Something else"))
    assert seen == []
    assert _renames(client) == [{"channel_id": "C1", "thread_ts": "5.5", "title": "Deploy failure"}]
    assert a.bindings.title_for("C1", "5.5") == "Deploy failure"


async def test_with_no_stored_title_it_goes_back_to_the_previous_one(tmp_path):
    a, client, _seen = _adapter(tmp_path)
    await a._handle_session_title_changed(_rename(MEMBER))
    assert _renames(client) == [{"channel_id": "C1", "thread_ts": "5.5", "title": "Deploy"}]
    # Stored, so a second event for the same title changes nothing.
    assert a.bindings.title_for("C1", "5.5") == "Deploy"
    await a._handle_session_title_changed(_rename(MEMBER, title="Deploy", previous="Mallory's now"))
    assert len(_renames(client)) == 1


async def test_the_operators_rename_is_kept_and_not_put_back(tmp_path):
    a, client, seen = _adapter(tmp_path)
    a.bindings.set_title("C1", "5.5", "Deploy failure", by_user=False)
    await a._handle_session_title_changed(_rename(OPERATOR))
    assert len(seen) == 1
    assert _renames(client) == []
    assert a.bindings.title_for("C1", "5.5") == "Mallory's now"


async def test_freyjas_own_rename_is_neither_refused_nor_put_back(tmp_path):
    a, client, seen = _adapter(tmp_path)
    a.bindings.set_title("C1", "5.5", "Deploy", by_user=False)
    await a._handle_session_title_changed(_rename("UBOT", title="Deploy failure"))
    assert seen == []
    assert client.calls == []
