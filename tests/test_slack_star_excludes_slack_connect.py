""""*" in the Slack allowlist lets in members of that workspace only,
never a Slack Connect user from another workspace in a shared channel.

Everyone the allowlist lets in gets the full toolset. Stop, a rename and a
reaction took the team from the event's workspace (the event names no
other), and a message with no ``team`` fell back to the channel's
workspace, so with "*" on that workspace an external user in a shared
channel passed. The author's own team is used when Slack gives it
(``user_team``, a message's ``team``, a click's ``user.team_id``); when it
does not and the envelope says the channel is shared with another
organization, "*" cannot tell a member from an external user, so only a
listed user id passes.
"""

from __future__ import annotations

import inspect
from typing import Any

from bridge.gateway import slack_blocks as b
from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.slack_bindings import ThreadBindings

OPERATOR, MEMBER, EXTERNAL = "U0OPERATOR", "U0MEMBER", "U0EXTERNAL"
# The envelope Slack sends around an event from a Slack Connect channel.
SHARED = {"type": "event_callback", "team_id": "T1", "is_ext_shared_channel": True}
NOT_SHARED = {"type": "event_callback", "team_id": "T1", "is_ext_shared_channel": False}


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def chat_postEphemeral(self, **kw: Any) -> dict:
        self.calls.append(("ephemeral", kw))
        return {"ok": True}

    async def views_open(self, **kw: Any) -> dict:
        self.calls.append(("views_open", kw))
        return {"ok": True}

    async def agents_sessions_rename(self, **kw: Any) -> dict:
        self.calls.append(("rename", kw))
        return {"ok": True}


class _App:
    """Collects the listeners the adapter registers, as Bolt would."""

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


async def _deliver(a: SlackAdapter, name: str, **args: Any) -> None:
    """Call a listener with the arguments it asks for, like Bolt does."""
    fn = a._app.listeners[name]  # type: ignore[union-attr]
    params = inspect.signature(fn).parameters
    args.setdefault("say", None)
    await fn(**{k: v for k, v in args.items() if k in params})


async def _ack(**_kw: Any) -> None:
    pass


def _adapter(tmp_path, allowed: list[str]) -> tuple[SlackAdapter, _Client, list[tuple[str, Any]]]:
    a = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": allowed}))
    client = _Client()
    a._app = _App()  # type: ignore[assignment]
    a._register_event_handlers()
    a._team_clients = {"T1": client}  # type: ignore[assignment]
    a._team_bot_user_ids = {"T1": "UBOT"}
    a._bot_user_id = "UBOT"
    a._channel_team = {"C1": "T1"}
    a.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    a.bindings.add_message_ts("9.9")
    seen: list[tuple[str, Any]] = []

    async def resolve_name(user_id: str, _team: str) -> str:
        return user_id

    a._resolve_user_name = resolve_name  # type: ignore[assignment]

    async def on_event(message: Any) -> None:
        seen.append(("message", message))

    a._on_event = on_event

    def hook(name: str):
        async def _hook(info: dict[str, Any]) -> None:
            seen.append((name, info))
        return _hook

    a.on_session_stopped = hook("stopped")
    a.on_session_title_changed = hook("renamed")
    a.on_feedback = hook("feedback")
    return a, client, seen


def _mention(user: str, ts: str, **fields: Any) -> dict[str, Any]:
    return {"type": "message", "channel": "C1", "channel_type": "channel", "user": user,
            "ts": ts, "text": "<@UBOT> run this", **fields}


# Shaped like Slack's reference payloads: Stop names no team at all.
def _stop(user: str) -> dict[str, Any]:
    return {"type": "agent_session_stopped", "channel": "C1", "thread_ts": "5.5",
            "user": user, "streaming_message_ts": ["6.6"]}


def _rename(user: str) -> dict[str, Any]:
    return {"type": "agent_session_title_changed", "channel": "C1", "thread_ts": "5.5",
            "team_id": "T1", "title": "Erin's now", "previous_title": "Deploy", "user": user}


def _reaction(user: str) -> dict[str, Any]:
    return {"type": "reaction_added", "user": user, "reaction": "-1",
            "item": {"type": "message", "channel": "C1", "ts": "9.9"}}


def _thumbs(user: str, user_team: str) -> dict[str, Any]:
    return {
        "actions": [{"action_id": b.FEEDBACK_ACTION_ID, "value": b.feedback_value("freyja:slack:T1:channel:C1:5.5", "5.5", "-")}],
        "channel": {"id": "C1"}, "message": {"ts": "9.9", "thread_ts": "5.5"},
        "user": {"id": user, "team_id": user_team}, "team": {"id": "T1"}, "trigger_id": "trig",
    }


def _note(user: str, user_team: str) -> tuple[dict, dict]:
    return (
        {"user": {"id": user, "team_id": user_team}, "team": {"id": "T1"}},
        {
            "private_metadata": '{"k": "freyja:slack:T1:channel:C1:5.5", "c": "C1", "m": "9.9", "t": "5.5"}',
            "state": {"values": {b.FEEDBACK_NOTE_BLOCK_ID: {b.FEEDBACK_NOTE_ACTION_ID: {"value": "this was bad"}}}},
        },
    )


# ── the config ──


def test_star_lets_in_members_of_that_workspace_only():
    cfg = SlackConfig(allowed_user_ids={"T1": ["*"]})
    assert cfg.user_allowed("T1", MEMBER, "T1")
    assert not cfg.user_allowed("T1", EXTERNAL, "T9")
    # Slack did not say, in a channel shared with another organization.
    assert not cfg.user_allowed("T1", MEMBER, "")
    # Nothing suggests another workspace: as before.
    assert cfg.user_allowed("T1", MEMBER)


def test_a_listed_user_id_passes_whatever_the_team():
    cfg = SlackConfig(allowed_user_ids={"T1": ["*", OPERATOR]})
    assert cfg.user_allowed("T1", OPERATOR, "")
    assert cfg.user_allowed("T1", OPERATOR, "T9")


# ── messages ──


async def test_a_message_naming_another_team_only_as_user_team_is_denied(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    # No `team`, so the gate fell back to the channel's workspace.
    await _deliver(a, "message", event=_mention(EXTERNAL, "1.1", user_team="T9"), body=SHARED)
    assert seen == []


async def test_a_message_with_no_team_in_a_shared_channel_is_denied(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "message", event=_mention(EXTERNAL, "1.2"), body=SHARED)
    assert seen == []


async def test_a_members_message_in_a_shared_channel_still_gets_through(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "message", event=_mention(MEMBER, "1.3", team="T1", user_team="T1"), body=SHARED)
    await _deliver(a, "message", event=_mention(MEMBER, "1.4"), body=NOT_SHARED)
    assert [s[0] for s in seen] == ["message", "message"]


# ── Stop, rename, reactions ──


async def test_stop_from_a_slack_connect_user_cancels_nothing(tmp_path):
    a, client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "agent_session_stopped", event=_stop(EXTERNAL), body=SHARED)
    assert seen == []
    assert [c[0] for c in client.calls] == ["ephemeral"]


async def test_a_rename_from_a_slack_connect_user_is_not_kept(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "agent_session_title_changed", event=_rename(EXTERNAL), body=SHARED)
    assert seen == []
    assert a.bindings.title_for("C1", "5.5") != "Erin's now"


async def test_a_reaction_from_a_slack_connect_user_is_not_feedback(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "reaction_added", event=_reaction(EXTERNAL), body=SHARED)
    assert seen == []


async def test_a_listed_operator_still_stops_renames_and_reacts_in_a_shared_channel(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*", OPERATOR])
    await _deliver(a, "agent_session_stopped", event=_stop(OPERATOR), body=SHARED)
    await _deliver(a, "agent_session_title_changed", event=_rename(OPERATOR), body=SHARED)
    await _deliver(a, "reaction_added", event=_reaction(OPERATOR), body=SHARED)
    assert [s[0] for s in seen] == ["stopped", "renamed", "feedback"]


async def test_a_member_still_stops_in_a_channel_not_shared_with_another_org(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, "agent_session_stopped", event=_stop(MEMBER), body=NOT_SHARED)
    assert [s[0] for s in seen] == ["stopped"]


# ── clicks and the feedback note ──


async def test_a_thumbs_click_from_a_slack_connect_user_records_nothing(tmp_path):
    a, client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, b.FEEDBACK_ACTION_ID, ack=_ack, body=_thumbs(EXTERNAL, "T9"), respond=_ack)
    assert seen == []
    assert client.calls == []


async def test_a_feedback_note_from_a_slack_connect_user_is_not_recorded(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    body, view = _note(EXTERNAL, "T9")
    await _deliver(a, b.FEEDBACK_NOTE_CALLBACK_ID, ack=_ack, body=body, view=view)
    assert seen == []


async def test_a_members_click_still_counts(tmp_path):
    a, _client, seen = _adapter(tmp_path, ["*"])
    await _deliver(a, b.FEEDBACK_ACTION_ID, ack=_ack, body=_thumbs(MEMBER, "T1"), respond=_ack)
    assert [s[0] for s in seen] == ["feedback"]
