"""The prompt cache survives a compaction (OpenAI) and a pause (Anthropic).

OpenAI resumes a cached prefix only at a lookup boundary: the end of an
earlier request, or an explicit breakpoint. The top-level `instructions` can't
carry a breakpoint, so the first request after a compaction read 0 tokens from
the cache. The system prompt now goes in as the first developer message with
an explicit breakpoint, and so does the compaction summary after it.

Anthropic keeps a prompt 5 minutes after its last use. Between turns of a
session someone replies to, the runner re-reads it just before it expires, at
most twice, so a reply within about 13 minutes finds it cached.
"""

from __future__ import annotations

import asyncio

import pytest

import engine.runner as runner_module
from engine.compaction import SummaryCompaction
from engine.providers import APIUsage, ProviderResponse, ToolCallResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig, Message, ThinkingBlock, ThinkingConfig

BREAKPOINT = {"mode": "explicit"}


# ─── OpenAI: explicit breakpoints on the system prompt and the summary ─


def _openai(model: str, **kw):
    from engine.openai_provider import OpenAIConfig, OpenAIProvider

    return OpenAIProvider(OpenAIConfig(api_key="test", model=model, **kw))


def test_openai_system_prompt_is_the_first_developer_message_with_a_breakpoint():
    params = _openai("gpt-6.1-sol").build_request_params(
        [Message(role="user", content="hi")], system_prompt="be terse",
    )
    assert "instructions" not in params
    first = params["input"][0]
    assert first["role"] == "developer"
    assert first["content"] == [
        {"type": "input_text", "text": "be terse", "prompt_cache_breakpoint": BREAKPOINT},
    ]
    assert params["input"][1]["role"] == "user"
    assert "prompt_cache_breakpoint" not in str(params["input"][1])


def test_openai_compaction_summary_gets_the_second_breakpoint():
    summary = Message(role="system", content="[Previous conversation summary]\nwe built the parser")
    params = _openai("gpt-6-astra-ultrafast").build_request_params(
        [summary, Message(role="user", content="next")], system_prompt="be terse",
    )
    roles = [item["role"] for item in params["input"]]
    assert roles == ["developer", "developer", "user"]
    marked = [item for item in params["input"] if "prompt_cache_breakpoint" in str(item)]
    assert marked == params["input"][:2]


def test_openai_older_models_and_other_servers_keep_instructions():
    for provider in (_openai("gpt-5.5"), _openai("gpt-6.1-sol", base_url="https://example.test/v1")):
        params = provider.build_request_params(
            [Message(role="user", content="hi")], system_prompt="be terse",
        )
        assert params["instructions"] == "be terse"
        assert "prompt_cache_breakpoint" not in str(params["input"])


# ─── Anthropic: the keep-alive request ───────────────────────────────


def _anthropic():
    from engine.anthropic_provider import AnthropicConfig, AnthropicProvider

    return AnthropicProvider(AnthropicConfig(api_key="test", model="claude-sonnet-5-5"))


async def test_the_keepalive_moves_the_conversation_breakpoint_onto_the_reply(monkeypatch):
    provider = _anthropic()
    provider._image_files = None
    seen: dict = {}

    async def create(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(provider._async_client.messages, "create", create)
    reply = Message(
        role="assistant", content="All done.",
        thinking_blocks=[ThinkingBlock(thinking="checked", signature="sig")],
    )
    tool = ToolDefinition(name="read", description="read a file", summary="read")
    with pytest.raises(RuntimeError):
        await provider.keep_prompt_cached(
            [Message(role="user", content="do it"), reply],
            tools=[tool], system_prompt="be terse",
            thinking=ThinkingConfig(enabled=True, effort="high"),
        )

    messages = seen["messages"]
    assert messages[-1]["role"] == "user" and "cache_control" not in str(messages[-1])
    reply_blocks = messages[-2]["content"]
    assert reply_blocks[0]["type"] == "thinking" and "cache_control" not in reply_blocks[0]
    assert reply_blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert seen["max_tokens"] == 1
    assert seen["tool_choice"] == {"type": "none"}
    assert seen["output_config"] == {"effort": "low"}
    # system + last tool + the reply: inside Anthropic's 4-breakpoint limit
    assert str(seen).count("cache_control") == 3


# ─── the runner's keep-alive schedule ────────────────────────────────


class _PlainProvider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 1_000_000

    def __init__(self, steps, *, ttl=0.6):
        self.steps = list(steps)
        self.prompt_cache_ttl_s = ttl
        self.pings: list[list[Message]] = []

    async def stream_to_response(self, *, messages, **_kw):
        return self.steps.pop(0)


class _Provider(_PlainProvider):
    async def keep_prompt_cached(self, messages, **_kw):
        self.pings.append(list(messages))
        usage = APIUsage(cache_read_tokens=5_000, output_tokens=1)
        return ProviderResponse(content="", usage=usage)


class _ReadTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="read", description="read a file", summary="read")

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(call_id=call_id, content="file contents", is_error=False)


def _steps():
    return [
        ProviderResponse(content="", stop_reason="tool_use", usage=APIUsage(),
                         tool_calls=[ToolCallResponse(id="c1", name="read", arguments={})]),
        ProviderResponse(content="All done.", stop_reason="end_turn", usage=APIUsage()),
    ]


@pytest.fixture
def quick_cache(monkeypatch):
    # A 0.6 s "cache": keep-alives 0.3 s before it would expire.
    monkeypatch.setattr(runner_module, "CACHE_KEEPALIVE_LEAD_S", 0.3)
    monkeypatch.setattr(runner_module, "PROMPT_CACHE_TTL_MARGIN_S", 0.1)


def _runner(provider, *, keep=True, calls=None):
    registry = ToolRegistry()
    registry.register(_ReadTool())
    return AsyncAgentRunner(
        provider, config=AgentConfig(), tool_registry=registry,
        compaction_strategy=SummaryCompaction(),
        on_llm_call=(calls.append if calls is not None else None),
        keep_cache_warm=keep,
    )


async def test_two_keepalives_follow_a_turn_then_stop(quick_cache):
    calls: list[dict] = []
    provider = _Provider(_steps())
    runner = _runner(provider, calls=calls)
    session = Session.create(system_prompt="t")
    await runner.run(session, "read it")

    while not provider.pings:
        await asyncio.sleep(0.02)
    # The keep-alive's prompt ends with the reply: a summary can still continue it.
    assert runner.cached_prompt_prefix(session.transcript) is not None
    await asyncio.sleep(1.4)

    assert len(provider.pings) == 2
    for sent in provider.pings:
        assert sent[-1].role == "assistant" and sent[-1].content == "All done."
    assert [c.get("call_kind") for c in calls if c.get("call_kind")] == ["cache_keepalive"] * 2
    # After the last one the cache runs out as usual.
    assert runner.cached_prompt_prefix(session.transcript) is None


async def test_a_new_turn_cancels_the_pending_keepalive(quick_cache):
    provider = _Provider(_steps() + _steps())
    runner = _runner(provider)
    session = Session.create(system_prompt="t")
    await runner.run(session, "read it")
    await runner.run(session, "again")
    runner.stop_keeping_cache()
    await asyncio.sleep(0.5)
    assert provider.pings == []


async def test_no_keepalive_unless_asked_and_supported(quick_cache):
    for provider, keep in ((_Provider(_steps()), False), (_PlainProvider(_steps()), True)):
        runner = _runner(provider, keep=keep)
        await runner.run(Session.create(system_prompt="t"), "read it")
        assert runner._cache_keeper is None
    await asyncio.sleep(0.7)


async def test_no_keepalive_once_the_transcript_changed(quick_cache):
    provider = _Provider(_steps())
    runner = _runner(provider)
    session = Session.create(system_prompt="t")
    await runner.run(session, "read it")
    session.add_user_message("a message that has not been sent yet")
    await asyncio.sleep(0.6)
    assert provider.pings == []
