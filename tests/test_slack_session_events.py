"""Adapter handlers for the agent session surface, and the gateway's
mapping from a Slack (channel, thread) pair to a session key."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.slack_bindings import ThreadBindings


class _Client:
    def __init__(self, parent: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.parent = parent

    async def agents_sessions_setStatus(self, **kw):
        self.calls.append(("setStatus", kw))
        return {"ok": True}

    async def views_open(self, **kw):
        self.calls.append(("views_open", kw))
        return {"ok": True}

    async def conversations_replies(self, **kw):
        self.calls.append(("replies", kw))
        return {"ok": True, "messages": [self.parent] if self.parent else []}


def _adapter(tmp_path, client: _Client | None = None) -> SlackAdapter:
    a = SlackAdapter.__new__(SlackAdapter)
    a._app = object()
    a._team_clients = {"T1": client or _Client()}
    a._team_bot_user_ids = {"T1": "UBOT"}
    a._bot_user_id = "UBOT"
    a._channel_team = {}
    a._not_our_threads = {}
    a.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    a.on_session_stopped = None
    a.on_session_title_changed = None
    a.on_home_opened = None
    a.on_feedback = None
    return a


def _capture():
    seen: list[dict] = []

    async def hook(info: dict[str, Any]) -> None:
        seen.append(info)

    return seen, hook


def test_stop_event_reaches_the_gateway_hook(tmp_path):
    a = _adapter(tmp_path)
    seen, a.on_session_stopped = _capture()
    asyncio.run(a._handle_session_stopped({
        "channel": "C1", "thread_ts": "5.5", "user": "U1", "streaming_message_ts": ["6.6"],
    }))
    assert seen == [{
        "chat_id": "C1", "thread_ts": "5.5", "user_id": "U1", "team_id": "T1",
        "streaming_message_ts": ["6.6"],
    }]


def test_title_change_is_recorded_as_the_operators(tmp_path):
    a = _adapter(tmp_path)
    seen, a.on_session_title_changed = _capture()
    asyncio.run(a._handle_session_title_changed({
        "channel": "D1", "thread_ts": "1.1", "title": "Bora Bora", "previous_title": "Scuba", "user": "U1", "team_id": "T1",
    }))
    assert a.bindings.title_for("D1", "1.1") == "Bora Bora" and a.bindings.title_is_users("D1", "1.1")
    assert seen[0]["title"] == "Bora Bora" and seen[0]["previous_title"] == "Scuba"


def test_reaction_on_a_freyja_message_is_feedback_and_others_are_not(tmp_path):
    a = _adapter(tmp_path)
    seen, a.on_feedback = _capture()
    a.bindings.add_message_ts("9.9")
    asyncio.run(a._handle_reaction({"user": "U1", "reaction": "+1", "item": {"type": "message", "channel": "D1", "ts": "9.9"}}))
    asyncio.run(a._handle_reaction({"user": "U1", "reaction": "eyes", "item": {"type": "message", "channel": "D1", "ts": "9.9"}}))
    asyncio.run(a._handle_reaction({"user": "U1", "reaction": "-1", "item": {"type": "message", "channel": "D1", "ts": "7.7"}}))
    asyncio.run(a._handle_reaction({"user": "U1", "reaction": "-1", "item_user": "UBOT", "item": {"type": "message", "channel": "D1", "ts": "7.7"}}))
    assert [(s["verdict"], s["message_ts"]) for s in seen] == [("+", "9.9"), ("-", "7.7")]


def test_thumbs_down_click_records_and_opens_the_note_modal(tmp_path):
    client = _Client()
    a = _adapter(tmp_path, client)
    seen, a.on_feedback = _capture()
    from bridge.gateway import slack_blocks as b

    value = b.feedback_value("freyja:slack:T1:dm:D1", "10.1", "-")
    asyncio.run(a._handle_feedback_click({
        "actions": [{"type": "feedback_buttons", "action_id": b.FEEDBACK_ACTION_ID, "value": value}],
        "channel": {"id": "D1"}, "message": {"ts": "11.1", "thread_ts": "10.1"},
        "user": {"id": "U1"}, "team": {"id": "T1"}, "trigger_id": "trig",
    }))
    assert seen[0]["verdict"] == "-" and seen[0]["session_key"] == "freyja:slack:T1:dm:D1"
    assert seen[0]["turn_ts"] == "10.1" and seen[0]["kind"] == "button"
    assert client.calls and client.calls[-1][0] == "views_open"
    view = client.calls[-1][1]["view"]
    assert view["callback_id"] == b.FEEDBACK_NOTE_CALLBACK_ID


def test_note_submission_carries_the_text(tmp_path):
    a = _adapter(tmp_path)
    seen, a.on_feedback = _capture()
    from bridge.gateway import slack_blocks as b

    asyncio.run(a._handle_feedback_note(
        {"user": {"id": "U1"}, "team": {"id": "T1"}},
        {
            "private_metadata": '{"k": "freyja:slack:T1:dm:D1", "c": "D1", "m": "11.1", "t": "10.1"}',
            "state": {"values": {b.FEEDBACK_NOTE_BLOCK_ID: {b.FEEDBACK_NOTE_ACTION_ID: {"value": "wrong file"}}}},
        },
    ))
    assert seen[0]["note"] == "wrong file" and seen[0]["kind"] == "note" and seen[0]["verdict"] == "-"


def test_thread_ownership_is_checked_once_against_the_parent(tmp_path):
    parent = {"ts": "1.1", "user": "UBOT", "text": "hi"}
    client = _Client(parent)
    a = _adapter(tmp_path, client)
    assert asyncio.run(a._thread_is_ours("C1", "1.1", "T1")) is True
    assert a.bindings.has_thread("1.1")
    other = _Client({"ts": "2.2", "user": "UHUMAN", "text": "hi"})
    a2 = _adapter(tmp_path, other)
    assert asyncio.run(a2._thread_is_ours("C1", "2.2", "T1")) is False
    assert asyncio.run(a2._thread_is_ours("C1", "2.2", "T1")) is False
    assert len([c for c in other.calls if c[0] == "replies"]) == 1
    meta = _Client({"ts": "3.3", "user": "UHUMAN", "metadata": {"event_type": "freyja_session"}})
    a3 = _adapter(tmp_path, meta)
    assert asyncio.run(a3._thread_is_ours("C1", "3.3", "T1")) is True


def test_approval_post_suspends_the_session(tmp_path):
    client = _Client()
    a = _adapter(tmp_path, client)

    async def post(**kw):
        client.calls.append(("post", kw))
        return {"ts": "4.4"}

    client.chat_postMessage = post
    res = asyncio.run(a.send_approval_request(
        "D1", "req1", tool_name="bash", command_preview="rm -rf x", reason="destructive", thread_id="1.1",
    ))
    assert res.ok
    assert [c for c in client.calls if c[0] == "setStatus"][-1][1]["status"] == "suspended"


def test_gateway_maps_slack_coordinates_to_session_keys(tmp_path):
    from bridge.gateway.run import GatewayDaemon

    a = _adapter(tmp_path)
    a.bindings.add_code_channel("C9", {"name": "job"})
    key = GatewayDaemon._slack_key_for
    assert key(a, {"chat_id": "D1", "thread_ts": "1.1", "team_id": "T1"}) == "freyja:slack:T1:dm:D1"
    assert key(a, {"chat_id": "C1", "thread_ts": "1.1", "team_id": "T1"}) == "freyja:slack:T1:channel:C1:1.1"
    assert key(a, {"chat_id": "C9", "thread_ts": None, "team_id": "T1"}) == "freyja:slack:T1:code:C9"


def test_clean_title_drops_what_slack_rejects(tmp_path):
    a = _adapter(tmp_path)
    assert a._clean_title("Fix: the `bug` in *main*") == "Fix the bug in main"
    long = a._clean_title("word " * 40)
    assert len(long) <= a._THREAD_TITLE_MAX + 1 and long.endswith("…")
    assert a._clean_title("   ") == ""
