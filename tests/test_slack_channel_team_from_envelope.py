"""A channel's workspace is taken from the event envelope, never from
whoever wrote a message there.

The gateway remembers each channel's workspace for the events that name
none (Stop, reactions, some message subtypes). It took it from a message's
``team``, which is the author's own workspace, before the allowlist check.
So in a channel shared with another organization one external message
moved the channel to the external workspace: the operator's own Stop was
then checked against that workspace's allowlist and refused, and so were
their messages that carry no ``team``.
"""

from __future__ import annotations

import inspect
from typing import Any

from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.slack_bindings import ThreadBindings

OPERATOR, EXTERNAL = "U0OPERATOR", "U0EXTERNAL"
# The envelope Slack sends around an event from a Slack Connect channel.
SHARED = {"type": "event_callback", "team_id": "T1", "is_ext_shared_channel": True}


class _Client:
    async def chat_postEphemeral(self, **_kw: Any) -> dict:
        return {"ok": True}


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


async def _deliver(a: SlackAdapter, name: str, **args: Any) -> None:
    fn = a._app.listeners[name]  # type: ignore[union-attr]
    params = inspect.signature(fn).parameters
    args.setdefault("say", None)
    await fn(**{k: v for k, v in args.items() if k in params})


def _adapter(tmp_path, teams: tuple[str, ...] = ("T1",)) -> tuple[SlackAdapter, list[tuple[str, Any]]]:
    a = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    a._app = _App()  # type: ignore[assignment]
    a._register_event_handlers()
    a._team_clients = {t: _Client() for t in teams}  # type: ignore[assignment]
    a._team_bot_user_ids = {t: "UBOT" for t in teams}
    a._bot_user_id = "UBOT"
    a.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    a.bindings.add_message_ts("9.9")
    a.bindings.add_thread("5.5")
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
    a.on_feedback = hook("feedback")
    return a, seen


def _reply(user: str, ts: str, **fields: Any) -> dict[str, Any]:
    return {"type": "message", "channel": "C1", "channel_type": "channel", "user": user,
            "ts": ts, "thread_ts": "5.5", "text": "<@UBOT> look", **fields}


def _stop(user: str) -> dict[str, Any]:
    return {"type": "agent_session_stopped", "channel": "C1", "thread_ts": "5.5",
            "user": user, "streaming_message_ts": ["6.6"]}


def _reaction(user: str) -> dict[str, Any]:
    return {"type": "reaction_added", "user": user, "reaction": "-1",
            "item": {"type": "message", "channel": "C1", "ts": "9.9"}}


async def _external_message_then_operator(a: SlackAdapter) -> None:
    await _deliver(a, "message", event=_reply(OPERATOR, "1.1", team="T1"), body=SHARED)
    await _deliver(a, "message", event=_reply(EXTERNAL, "1.2", team="T9", user_team="T9"), body=SHARED)


async def test_an_external_message_does_not_lock_the_operator_out_of_stop(tmp_path):
    a, seen = _adapter(tmp_path)
    await _external_message_then_operator(a)
    await _deliver(a, "agent_session_stopped", event=_stop(OPERATOR), body=SHARED)
    await _deliver(a, "reaction_added", event=_reaction(OPERATOR), body=SHARED)
    assert [s[0] for s in seen] == ["message", "stopped", "feedback"]
    assert seen[1][1]["team_id"] == "T1"


async def test_the_operators_message_with_no_team_still_gets_through_as_their_workspace(tmp_path):
    a, seen = _adapter(tmp_path)
    await _external_message_then_operator(a)
    await _deliver(a, "message", event=_reply(OPERATOR, "1.3"), body=SHARED)
    assert [m.source.workspace_id for _kind, m in seen] == ["T1", "T1"]


async def test_the_channel_takes_the_envelopes_workspace_when_two_are_installed(tmp_path):
    a, seen = _adapter(tmp_path, teams=("T1", "T2"))
    await _deliver(a, "message", event=_reply(EXTERNAL, "1.4", team="T2", user_team="T2"), body=SHARED)
    await _deliver(a, "agent_session_stopped", event=_stop(OPERATOR), body=SHARED)
    assert [s[0] for s in seen] == ["stopped"]
    assert a._channel_team == {"C1": "T1"}
