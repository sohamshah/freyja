"""A model call that fails mid-stream and is retried must not show twice.

Listeners (the bridge, the renderer, Slack) append every delta to what
they show. The runner marks where each streamed call begins
(``stream_start``) and, before a retry of a call that had streamed
something, tells them to cut back to that mark (``stream_reset``). A
final failure sends no reset: its partial stays beside the error. The
provider's mid-stream refusal fallback (``text_reset``) is a reset too.

Also here: GPT reasoning-summary parts stream with a paragraph break
between them, so step titles stay on their own lines.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, SendResult
from bridge.gateway.stream_consumer import SlackStreamConsumer
from engine.openai_provider import _summary_delta
from engine.providers import APIUsage, ProviderError, ProviderResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.types import AgentConfig, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent


class _ScriptedProvider:
    name = "fake"
    model_id = "fake-model"
    context_window = 1_000_000

    def __init__(self, steps):
        self.steps = list(steps)

    async def stream_to_response(self, *, messages, on_event=None, **_kw):
        return await self.steps.pop(0)(on_event)

    async def complete_async(self, *, messages, **_kw):
        return await self.stream_to_response(messages=messages)


def _streams(*events, then: Any):
    async def step(on_event):
        for ev in events:
            await on_event(ev)
        if isinstance(then, Exception):
            raise then
        return then

    return step


def _end(text: str) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage())


def _overloaded() -> ProviderError:
    return ProviderError("overloaded_error: Overloaded", status=529, retryable=True)


async def _run(steps, config: AgentConfig | None = None):
    seen: list[str] = []

    def on_stream(ev):
        seen.append(ev.type)

    runner = AsyncAgentRunner(
        _ScriptedProvider(steps),
        config=config or AgentConfig(),
        on_stream=on_stream,
    )
    # No real backoff in tests.
    import engine.runner as runner_mod

    async def _no_sleep(_s):
        return None

    orig = runner_mod.asyncio.sleep
    runner_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        result = await runner.run(Session.create(system_prompt="t"), "go")
    finally:
        runner_mod.asyncio.sleep = orig  # type: ignore[assignment]
    return seen, result


async def test_retry_after_a_partial_stream_resets_first():
    seen, result = await _run([
        _streams(ThinkingDeltaEvent(thinking="Plan"), TextDeltaEvent(text="Hel"), then=_overloaded()),
        _streams(TextDeltaEvent(text="Hello."), then=_end("Hello.")),
    ])
    assert result.success
    assert seen == [
        "stream_start", "thinking_delta", "text_delta",
        "stream_reset",
        "stream_start", "text_delta",
    ]


async def test_retry_after_nothing_streamed_sends_no_reset():
    seen, result = await _run([
        _streams(then=_overloaded()),
        _streams(TextDeltaEvent(text="Hi."), then=_end("Hi.")),
    ])
    assert result.success
    assert seen == ["stream_start", "stream_start", "text_delta"]


async def test_final_failure_keeps_its_partial():
    seen, result = await _run(
        [_streams(TextDeltaEvent(text="Half a rep"), then=ProviderError("invalid_request", retryable=False))],
    )
    assert not result.success
    assert "stream_reset" not in seen


async def test_refusal_fallback_text_reset_becomes_stream_reset():
    seen, result = await _run([
        _streams(
            TextDeltaEvent(text="I can't"),
            StreamEvent(type="text_reset"),
            TextDeltaEvent(text="Sure."),
            then=_end("Sure."),
        ),
    ])
    assert result.success
    assert seen == ["stream_start", "text_delta", "stream_reset", "text_delta"]


# ── Slack consumer ────────────────────────────────────────────────────


class _FakeAdapter:
    max_message_chars = 39_000

    def __init__(self) -> None:
        self.sent: list[str] = []

    def format_content(self, content: str) -> str:
        return content

    async def send(self, chat_id: str, content: str, **kwargs: Any) -> SendResult:
        self.sent.append(content)
        return SendResult(ok=True, message_id="1.0")

    async def start_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True, message_id="stream-ts")

    async def append_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True)

    async def stop_stream(self, *a: Any, **kw: Any) -> SendResult:
        return SendResult(ok=True)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        return None

    async def stop_typing(self, *a: Any, **kw: Any) -> None:
        return None


def _consumer(adapter: _FakeAdapter) -> SlackStreamConsumer:
    source = MessageSource(
        platform="slack", workspace_id="T1", chat_id="C1", chat_type="channel",
        user_id="U1", message_id="1.1", thread_id="1.1",
    )
    return SlackStreamConsumer(
        adapter,  # type: ignore[arg-type]
        source,
        session_key="freyja:slack:T1:channel:C1:1.1",
        verbosity="off",
    )


@pytest.mark.asyncio
async def test_slack_body_drops_the_failed_call_and_keeps_paragraphs():
    adapter = _FakeAdapter()
    consumer = _consumer(adapter)
    for ev in [
        {"type": "stream_start"},
        {"type": "text_delta", "text": "First."},
        {"type": "tool_use_start", "id": "t1", "name": "bash"},
        {"type": "tool_result", "id": "t1", "preview": "ok"},
        {"type": "stream_start"},
        {"type": "text_delta", "text": "Seco"},
        {"type": "stream_reset"},
        {"type": "stream_start"},
        {"type": "text_delta", "text": "Second."},
        {"type": "turn_complete"},
    ]:
        await consumer._handle_event_async(ev)
    assert adapter.sent == ["First.\n\nSecond."]


# ── GPT reasoning summaries ───────────────────────────────────────────


def test_summary_parts_of_one_item_are_paragraphs():
    def ev(item, idx, delta):
        return SimpleNamespace(item_id=item, summary_index=idx, delta=delta)

    out, at = [], None
    for e in [
        ev("rs_1", 0, "**Reading**\n\nThe file."),
        ev("rs_1", 0, " Done."),
        ev("rs_1", 1, "**Writing**"),
        ev("rs_2", 0, "**Next item**"),
    ]:
        delta, at = _summary_delta(e, at)
        out.append(delta)
    assert "".join(out) == "**Reading**\n\nThe file. Done.\n\n**Writing**\n\n**Next item**"
