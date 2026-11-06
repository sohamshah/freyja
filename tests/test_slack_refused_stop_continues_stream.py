"""A Stop the gateway refuses leaves the turn streaming in a new message.

Slack can show Stop to everyone in a channel, and it seals the turn's
streaming message when anyone presses it. The gateway refuses a Stop from
someone not on the allowlist, so the turn goes on, but its consumer only
learned that the message was sealed when the next append failed. Cards
were then turned off for the rest of the turn and the remaining reply
went out as one plain `chat.postMessage` at the end. The refused Stop now
tells the turn's consumer, which continues in a fresh stream, the way a
rollover at Slack's size cap does.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.base import MessageSource, Platform, SendResult
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.run import GatewayDaemon
from bridge.gateway.slack_bindings import ThreadBindings
from bridge.gateway.stream_consumer import SlackStreamConsumer
from tests.slack_fakes import StreamingFakeAdapter

OPERATOR, MEMBER = "U0OPERATOR", "U0MEMBER"


class _SealableAdapter(StreamingFakeAdapter):
    """Answers the way Slack does for a message it has sealed."""

    def __init__(self) -> None:
        super().__init__()
        self.sealed: set[str] = set()

    async def append_stream(self, chat_id: str, ts: str, *, chunks: list[Any] | None = None, **kw: Any) -> SendResult:
        if ts in self.sealed:
            return SendResult(ok=False, error="message_not_in_streaming_state")
        return await super().append_stream(chat_id, ts, chunks=chunks, **kw)

    async def stop_stream(self, chat_id: str, ts: str, **kw: Any) -> SendResult:
        if ts in self.sealed:
            return SendResult(ok=False, error="message_not_in_streaming_state")
        return await super().stop_stream(chat_id, ts, **kw)


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


class _Client:
    async def chat_postEphemeral(self, **_kw: Any) -> dict:
        return {"ok": True}


def _wire(tmp_path, consumer: SlackStreamConsumer) -> tuple[SlackAdapter, list[dict]]:
    """The Slack adapter and the gateway, wired the way the gateway wires
    them at start, with the operator's turn running in C1 / 5.5."""
    slack = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    slack._app = _App()  # type: ignore[assignment]
    slack._register_event_handlers()
    slack._team_clients = {"T1": _Client()}  # type: ignore[assignment]
    slack._team_bot_user_ids = {"T1": "UBOT"}
    slack._bot_user_id = "UBOT"
    slack.bindings = ThreadBindings(tmp_path / "b.json", flush_delay=0)
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    daemon.adapters = [slack]
    key = daemon._slack_key_for(slack, {"chat_id": "C1", "thread_ts": "5.5", "team_id": "T1"})
    session = SimpleNamespace(_active_stream_consumer=consumer, pending_task=None)
    daemon.state = SimpleNamespace(sessions={key: session})
    stopped: list[dict] = []

    async def _stopped(info: dict) -> None:
        stopped.append(info)

    slack.on_session_stopped = _stopped
    slack.on_session_stop_refused = daemon._on_session_stop_refused
    return slack, stopped


async def _press_stop(slack: SlackAdapter, user: str) -> None:
    fn = slack._app.listeners["agent_session_stopped"]  # type: ignore[union-attr]
    event = {"type": "agent_session_stopped", "channel": "C1", "thread_ts": "5.5",
             "user": user, "streaming_message_ts": ["stream-ts"]}
    body = {"type": "event_callback", "team_id": "T1"}
    args = {"event": event, "body": body, "say": None}
    await fn(**{k: v for k, v in args.items() if k in inspect.signature(fn).parameters})


def _consumer(adapter: StreamingFakeAdapter) -> SlackStreamConsumer:
    source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id=OPERATOR, message_id="5.5", thread_id="5.5",
    )
    c = SlackStreamConsumer(adapter, source, session_key="freyja:slack:T1:channel:C1:5.5")
    c.edit_interval_ms = 0
    return c


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


BEFORE = [
    {"type": "stream_start"},
    {"type": "text_delta", "text": "Looking at the deploy."},
    {"type": "tool_use_start", "id": "t1", "name": "read_file"},
    {"type": "tool_input_end", "id": "t1", "arguments": {"path": "deploy.yaml"}},
]
AFTER = [
    {"type": "tool_result", "id": "t1", "name": "read_file", "content": "replicas: 3", "is_error": False},
    {"type": "text_delta", "text": " It runs three replicas."},
]


async def test_after_a_refused_stop_the_reply_goes_on_streaming_in_a_new_message(tmp_path):
    adapter = _SealableAdapter()
    consumer = _consumer(adapter)
    slack, stopped = _wire(tmp_path, consumer)
    await _feed(consumer, BEFORE)
    # A channel member presses Stop: Slack seals the message, the gateway refuses.
    adapter.sealed.add("stream-ts")
    await _press_stop(slack, MEMBER)
    assert stopped == []
    await _feed(consumer, AFTER)
    await consumer.finalize()
    assert adapter.sent == []
    assert adapter.streams_started == 2
    assert "three replicas" in "".join(
        c.text for c in adapter.stream_chunks["stream-ts-2"] if hasattr(c, "text")
    )
    # The read_file card was still running: it is reopened and finished there.
    assert ("t1", "complete") in {(c.id, c.status) for c in adapter.stream_chunks["stream-ts-2"] if hasattr(c, "status")}
    assert adapter.stop_calls[-1]["session_status"] == "active"


async def test_the_operators_own_stop_still_ends_the_turn_with_nothing_sent(tmp_path):
    adapter = _SealableAdapter()
    consumer = _consumer(adapter)
    slack, stopped = _wire(tmp_path, consumer)
    await _feed(consumer, BEFORE)
    adapter.sealed.add("stream-ts")
    await _press_stop(slack, OPERATOR)
    assert len(stopped) == 1
    assert adapter.streams_started == 1


async def test_an_append_that_failed_on_the_sealed_message_first_does_not_matter(tmp_path):
    adapter = _SealableAdapter()
    consumer = _consumer(adapter)
    slack, _stopped = _wire(tmp_path, consumer)
    await _feed(consumer, BEFORE)
    adapter.sealed.add("stream-ts")
    # The next card reaches Slack before the Stop event reaches the gateway.
    await _feed(consumer, [
        {"type": "tool_use_start", "id": "t2", "name": "grep"},
        {"type": "tool_input_end", "id": "t2", "arguments": {"pattern": "replicas"}},
    ])
    await _press_stop(slack, MEMBER)
    await _feed(consumer, AFTER)
    await consumer.finalize()
    assert adapter.sent == []
    assert adapter.streams_started == 2
    assert "three replicas" in "".join(
        c.text for c in adapter.stream_chunks["stream-ts-2"] if hasattr(c, "text")
    )
