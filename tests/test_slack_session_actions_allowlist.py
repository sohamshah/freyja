"""Stop, a session rename, reactions, the thumbs buttons and the
thumbs-down note count only from someone on the allowlist.

Messages, slash commands and card clicks pass the allowlist first. These
five handlers checked no one, so any member of a channel Freyja posts in
could cancel the operator's running turn (when Slack showed them Stop),
retitle the operator's session, or write feedback into the log the skill
outcome rollup reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.gateway import slack_blocks as b
from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def chat_postEphemeral(self, **kw: Any) -> dict:
        self.calls.append(("ephemeral", kw))
        return {"ok": True}

    async def views_open(self, **kw: Any) -> dict:
        self.calls.append(("views_open", kw))
        return {"ok": True}


class _Respond:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw: Any) -> None:
        self.calls.append(kw)


def _adapter(tmp_path) -> tuple[SlackAdapter, _Client, list[tuple[str, dict]]]:
    from bridge.gateway.slack_bindings import ThreadBindings

    a = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    client = _Client()
    a._app = object()  # type: ignore[assignment]
    a._team_clients = {"T1": client}  # type: ignore[assignment]
    a._team_bot_user_ids = {"T1": "UBOT"}
    a._bot_user_id = "UBOT"
    a.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    a.bindings.add_message_ts("9.9")
    seen: list[tuple[str, dict]] = []

    def hook(name: str):
        async def _hook(info: dict[str, Any]) -> None:
            seen.append((name, info))
        return _hook

    a.on_session_stopped = hook("stopped")
    a.on_session_title_changed = hook("renamed")
    a.on_feedback = hook("feedback")
    return a, client, seen


def _stop(user: str) -> dict:
    return {"channel": "C1", "thread_ts": "5.5", "user": user, "team_id": "T1", "streaming_message_ts": ["6.6"]}


def _rename(user: str) -> dict:
    return {"channel": "C1", "thread_ts": "5.5", "title": "Mallory's now", "previous_title": "Deploy", "user": user, "team_id": "T1"}


def _reaction(user: str) -> dict:
    return {"user": user, "reaction": "-1", "item": {"type": "message", "channel": "C1", "ts": "9.9"}}


def _thumbs(user: str) -> dict:
    return {
        "actions": [{"action_id": b.FEEDBACK_ACTION_ID, "value": b.feedback_value("freyja:slack:T1:channel:C1:5.5", "5.5", "-")}],
        "channel": {"id": "C1"}, "message": {"ts": "9.9", "thread_ts": "5.5"},
        "user": {"id": user, "team_id": "T1"}, "team": {"id": "T1"}, "trigger_id": "trig",
    }


def _note(user: str) -> tuple[dict, dict]:
    return (
        {"user": {"id": user, "team_id": "T1"}, "team": {"id": "T1"}},
        {
            "private_metadata": '{"k": "freyja:slack:T1:channel:C1:5.5", "c": "C1", "m": "9.9", "t": "5.5"}',
            "state": {"values": {b.FEEDBACK_NOTE_BLOCK_ID: {b.FEEDBACK_NOTE_ACTION_ID: {"value": "this was bad"}}}},
        },
    )


async def test_stop_from_a_member_not_on_the_allowlist_cancels_nothing(tmp_path):
    a, client, seen = _adapter(tmp_path)
    await a._handle_session_stopped(_stop(MEMBER))
    assert seen == []
    # Told privately, in the thread where they pressed it.
    assert [c[0] for c in client.calls] == ["ephemeral"]
    note = client.calls[0][1]
    assert note["user"] == MEMBER and note["channel"] == "C1" and note["thread_ts"] == "5.5"


async def test_a_rename_from_a_member_not_on_the_allowlist_is_not_kept(tmp_path):
    a, client, seen = _adapter(tmp_path)
    await a._handle_session_title_changed(_rename(MEMBER))
    assert seen == []
    assert a.bindings.title_for("C1", "5.5") is None
    assert [c[0] for c in client.calls] == ["ephemeral"]


async def test_a_reaction_from_a_member_not_on_the_allowlist_is_not_feedback(tmp_path):
    a, client, seen = _adapter(tmp_path)
    await a._handle_reaction(_reaction(MEMBER))
    assert seen == []
    # A reaction is not addressed to Freyja: no note, like channel chatter.
    assert client.calls == []


async def test_a_thumbs_click_from_a_member_not_on_the_allowlist_records_nothing(tmp_path):
    a, client, seen = _adapter(tmp_path)
    respond = _Respond()
    await a._handle_feedback_click(_thumbs(MEMBER), respond)
    assert seen == []
    # No note modal; the member alone is told why, and the buttons stay.
    assert client.calls == []
    assert respond.calls and respond.calls[0]["response_type"] == "ephemeral"
    assert not respond.calls[0].get("replace_original")


async def test_a_note_from_a_member_not_on_the_allowlist_is_not_recorded(tmp_path):
    a, client, seen = _adapter(tmp_path)
    await a._handle_feedback_note(*_note(MEMBER))
    assert seen == []
    assert [c[0] for c in client.calls] == ["ephemeral"]
    assert client.calls[0][1]["user"] == MEMBER


@pytest.mark.parametrize("handler", ["stop", "rename", "reaction", "thumbs", "note"])
async def test_the_operator_still_gets_through(tmp_path, handler):
    a, client, seen = _adapter(tmp_path)
    if handler == "stop":
        await a._handle_session_stopped(_stop(OPERATOR))
    elif handler == "rename":
        await a._handle_session_title_changed(_rename(OPERATOR))
        assert a.bindings.title_for("C1", "5.5") == "Mallory's now"
    elif handler == "reaction":
        await a._handle_reaction(_reaction(OPERATOR))
    elif handler == "thumbs":
        await a._handle_feedback_click(_thumbs(OPERATOR), _Respond())
        assert [c[0] for c in client.calls] == ["views_open"]
    else:
        await a._handle_feedback_note(*_note(OPERATOR))
    assert len(seen) == 1
    assert not [c for c in client.calls if c[0] == "ephemeral"]


async def test_stop_from_another_workspace_cancels_nothing(tmp_path):
    a, _client, seen = _adapter(tmp_path)
    await a._handle_session_stopped({**_stop(OPERATOR), "team_id": "T9"})
    assert seen == []
