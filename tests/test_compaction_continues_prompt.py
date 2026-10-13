"""Compaction continues the session's own cached prompt.

The summary request is the prompt the last model call sent, unchanged, with
the instruction as a new turn after it, so the provider reads that prompt from
its cache at 2.5-10% of the input rate. Before this, compaction sent a separate
digest of the conversation, billed in full, with long messages cut to 1,800
characters and the middle dropped past 400,000.

What the request must keep, measured with live probes:
- the same system prompt, tools and thinking type;
- the instruction in a new turn after the reply: on Anthropic, a block added
  to the cached user turn reads nothing from the cache;
- Anthropic keeps the cache when only effort or tool_choice change; OpenAI
  keeps it when only tool_choice changes, not when the effort changes.
"""

from __future__ import annotations

import json

import pytest

from engine.compaction import (
    PromptPrefix,
    SummaryCompaction,
    _NOT_RUN_YET,
    _effort_at_most,
    _results_for_open_tool_calls,
    message_signature,
)
from engine.providers import APIUsage, ProviderResponse, ToolCallResponse
from engine.runner import AsyncAgentRunner, RunnerContext
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig, Message, ThinkingConfig, ToolCall

WORKING_MEMORY = {
    "summary": "Building the parser.",
    "actions_completed": ["I edited parser.py"],
    "entities": [],
}
LONG_RESULT = "line of output\n" * 400  # ~6k chars: big enough to halve


def _tools(call_id: str, *, input_tokens: int = 1_000) -> ProviderResponse:
    return ProviderResponse(
        content="",
        stop_reason="tool_use",
        usage=APIUsage(input_tokens=input_tokens),
        tool_calls=[ToolCallResponse(id=call_id, name="read", arguments={"n": call_id})],
    )


def _end(text: str, *, input_tokens: int = 1_000) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage(input_tokens=input_tokens))


class _CachingProvider:
    """Plays back the main calls; answers the summary requests compaction
    sends through the synchronous complete()."""

    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    prompt_cache_ttl_s = 300.0

    def __init__(self, steps, *, context_window: int = 1_000_000, summary_reply=None):
        self.steps = list(steps)
        self.context_window = context_window
        self.requests: list[dict] = []
        self.summary_requests: list[dict] = []
        self.summary_reply = summary_reply

    async def stream_to_response(self, *, messages, tools=None, system_prompt=None,
                                 max_tokens=None, thinking=None, on_event=None):
        self.requests.append({
            "messages": list(messages), "tools": tools,
            "system_prompt": system_prompt, "thinking": thinking,
        })
        return self.steps.pop(0)

    async def complete_async(self, *, messages, **kw):
        return await self.stream_to_response(messages=messages, **kw)

    def complete(self, messages, *, tools=None, system_prompt=None, max_tokens=None,
                 thinking=None, tool_choice=None):
        self.summary_requests.append({
            "messages": list(messages), "tools": tools, "system_prompt": system_prompt,
            "thinking": thinking, "tool_choice": tool_choice, "max_tokens": max_tokens,
        })
        usage = APIUsage(input_tokens=40, cache_read_tokens=90_000, output_tokens=500)
        if self.summary_reply is not None:
            reply = self.summary_reply(messages)
            if reply is not None:
                return reply
        if "JSON Schema" in messages[-1].get_text():
            return ProviderResponse(content=json.dumps(WORKING_MEMORY), usage=usage, stop_reason="end_turn")
        return ProviderResponse(content="<summary>We built the parser.</summary>", usage=usage, stop_reason="end_turn")


class _ReadTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="read", description="read a file", summary="read")

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(call_id=call_id, content=LONG_RESULT, is_error=False)


def _runner(provider, *, compaction=None, thinking=None, events=None) -> AsyncAgentRunner:
    registry = ToolRegistry()
    registry.register(_ReadTool())

    async def _on_event(ev):
        if events is not None:
            events.append(ev)

    return AsyncAgentRunner(
        provider,
        config=AgentConfig(),
        tool_registry=registry,
        compaction_strategy=compaction or SummaryCompaction(),
        thinking=thinking or ThinkingConfig(enabled=True, effort="high"),
        on_system_event=_on_event,
    )


async def _ran(steps, *, turns=("read the files",), **kw):
    provider = _CachingProvider(steps, **{k: v for k, v in kw.items() if k in ("context_window", "summary_reply")})
    runner = _runner(provider, **{k: v for k, v in kw.items() if k in ("compaction", "thinking", "events")})
    session = Session.create(system_prompt="you are terse")
    for text in turns:
        result = await runner.run(session, text)
        assert result.success
    return provider, runner, session


# Two turns, the first with five tool calls: enough older history for the
# compactor to summarize while it keeps the last 4 messages.
_LONG_SESSION = [_tools("c1"), _tools("c2"), _tools("c3"), _tools("c4"), _tools("c5"),
                 _end("all files read"), _end("parser done")]
_TWO_TURNS = ("read the files", "now write the parser")


# ─── what the runner records, and when it still counts ────────────────


async def test_the_prefix_is_the_last_call_unchanged_plus_its_reply():
    provider, runner, session = await _ran([_tools("c1"), _end("done")])

    prefix = runner.cached_prompt_prefix(session.transcript)

    assert prefix is not None
    sent = provider.requests[-1]
    n = len(sent["messages"])
    assert prefix.messages[:n] == sent["messages"]
    assert [m.role for m in prefix.messages[n:]] == ["assistant"]
    assert prefix.system_prompt == "you are terse"
    assert [t.name for t in prefix.tools] == ["read"]
    assert prefix.thinking == ThinkingConfig(enabled=True, effort="high")
    assert prefix.provider is provider


async def test_no_prefix_once_the_cache_has_expired_or_the_prompt_changed():
    _, runner, session = await _ran([_tools("c1"), _end("done")])
    assert runner.cached_prompt_prefix(session.transcript) is not None

    # A rewrite of an earlier message (pruning, halving) breaks the match.
    first_result = next(m for m in session.get_messages() if m.role == "tool_result")
    original = first_result.content
    first_result.content = original[: len(original) // 2]
    assert runner.cached_prompt_prefix(session.transcript) is None
    first_result.content = original
    assert runner.cached_prompt_prefix(session.transcript) is not None

    session.system_prompt = "a new system prompt"
    assert runner.cached_prompt_prefix(session.transcript) is None
    session.system_prompt = "you are terse"

    runner._sent_prompt.sent_at -= 300.0
    assert runner.cached_prompt_prefix(session.transcript) is None


async def test_no_prefix_when_nothing_follows_the_cached_turn():
    # A summary right after the cached user turn would add its instruction to
    # that turn, which reads nothing from Anthropic's cache.
    provider, runner, session = await _ran([_tools("c1"), _end("done")])
    session.add_user_message("one more thing")
    runner._sent_prompt = runner._record_prompt(
        session, provider, session.get_messages(), runner._request_tool_definitions(),
        runner._sent_prompt.thinking,
    )
    assert runner.cached_prompt_prefix(session.transcript) is None


async def test_no_prefix_when_the_summary_request_would_not_fit():
    # 100k window, 50k reserved for output: a 48k prompt + headroom can't fit.
    _, runner, session = await _ran(
        [_tools("c1", input_tokens=48_000), _end("done", input_tokens=48_000)],
        context_window=100_000,
    )
    assert runner.cached_prompt_prefix(session.transcript) is None


async def test_no_prefix_for_a_provider_without_a_known_cache_lifetime():
    provider = _CachingProvider([_tools("c1"), _end("done")])
    provider.prompt_cache_ttl_s = None
    runner = _runner(provider)
    session = Session.create(system_prompt="t")
    await runner.run(session, "go")
    assert runner.cached_prompt_prefix(session.transcript) is None


# ─── the summary request ─────────────────────────────────────────────


async def test_compaction_continues_the_cached_prompt():
    provider, runner, session = await _ran(
        list(_LONG_SESSION), turns=_TWO_TURNS, compaction=SummaryCompaction(keep_recent_messages=4),
    )
    prefix = runner.cached_prompt_prefix(session.transcript)
    upserts: list = []
    calls: list[dict] = []

    result = runner.compaction.compact(
        session.transcript, provider,
        on_summarizer_call=calls.append, on_working_memory_upserts=upserts.append,
    )

    assert result.success and result.summary == "We built the parser."
    assert result.summary_continued_prompt is True
    assert result.summarizer_cache_read_tokens == 90_000
    assert len(provider.summary_requests) == 2
    for request in provider.summary_requests:
        # The prompt as sent, then one new user turn: the instruction.
        assert request["messages"][: len(prefix.messages)] == prefix.messages
        assert len(request["messages"]) == len(prefix.messages) + 1
        assert request["messages"][-1].role == "user"
        assert request["system_prompt"] == prefix.system_prompt
        assert [t.name for t in request["tools"]] == ["read"]
        assert request["tool_choice"] == "none"
    efforts = sorted(r["thinking"].effort for r in provider.summary_requests)
    assert efforts == ["low", "medium"]
    assert upserts == [{
        "summary": "Building the parser.",
        "actions_completed": ["I edited parser.py"],
        "entities": [],
    }]
    assert {c["call_kind"] for c in calls} == {"summarizer", "working_memory_extraction"}
    assert all(c["continued"] for c in calls)
    # The transcript changed, so the old prompt no longer continues.
    assert runner.cached_prompt_prefix(session.transcript) is None


async def test_the_instruction_names_the_messages_kept_verbatim():
    provider, runner, session = await _ran(
        list(_LONG_SESSION), turns=_TWO_TURNS, compaction=SummaryCompaction(keep_recent_messages=4),
    )
    runner.compaction.compact(session.transcript, provider)

    instruction = provider.summary_requests[0]["messages"][-1].get_text()
    # keep_recent=4 lands on a tool result; the safe split keeps its call too.
    assert "The last 5 messages, from your call to read, stay in the context" in instruction
    assert "<summary></summary>" in instruction
    assert "CONVERSATION TO SUMMARIZE" not in instruction


async def test_a_reply_without_a_summary_falls_back_to_the_digest():
    def reply(messages):
        if len(messages) == 1:  # the digest request
            return ProviderResponse(content="<summary>from the digest</summary>", usage=APIUsage())
        return ProviderResponse(
            content="", usage=APIUsage(),
            tool_calls=[ToolCallResponse(id="x", name="read", arguments={})],
        )

    provider, runner, session = await _ran(
        list(_LONG_SESSION), turns=_TWO_TURNS, compaction=SummaryCompaction(keep_recent_messages=4),
        summary_reply=reply,
    )
    result = runner.compaction.compact(session.transcript, provider)

    assert result.success and result.summary == "from the digest"
    assert result.summary_continued_prompt is False
    digest = provider.summary_requests[-1]["messages"]
    assert len(digest) == 1 and "CONVERSATION TO SUMMARIZE" in digest[0].get_text()


def test_tool_calls_without_a_result_get_one_before_the_instruction():
    # summarize_context runs inside a tool call: its own call (and any after
    # it in the batch) has no result yet. Both APIs reject that.
    calls = [ToolCall(id="a", name="bash", arguments={}), ToolCall(id="b", name="summarize_context", arguments={})]
    messages = [
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=calls),
        Message(role="tool_result", content="ok", tool_call_id="a"),
    ]
    added = _results_for_open_tool_calls(messages)
    assert [(m.role, m.tool_call_id, m.content) for m in added] == [("tool_result", "b", _NOT_RUN_YET)]

    compactor = SummaryCompaction()
    provider = _CachingProvider([])
    prefix = PromptPrefix(provider=provider, system_prompt="s", tools=None, messages=messages,
                          thinking=None, max_tokens=1000)
    compactor._continue_prompt(prefix, "summarize", effort="medium")
    sent = provider.summary_requests[0]
    assert [m.role for m in sent["messages"]] == ["user", "assistant", "tool_result", "tool_result", "user"]
    assert sent["tool_choice"] is None  # no tools, so no tool_choice


def test_effort_is_lowered_never_raised():
    high = ThinkingConfig(enabled=True, effort="high")
    assert _effort_at_most(high, "medium").effort == "medium"
    assert _effort_at_most(ThinkingConfig(enabled=True, effort="low"), "medium").effort == "low"
    assert _effort_at_most(None, "medium") is None
    off = ThinkingConfig(enabled=False)
    assert _effort_at_most(off, "medium") is off


def test_the_message_fingerprint_sees_content_not_bookkeeping():
    a = [Message(role="user", content="hi"), Message(role="assistant", content="yo", output_tokens=5)]
    b = [Message(role="user", content="hi"), Message(role="assistant", content="yo", output_tokens=99)]
    assert message_signature(a) == message_signature(b)
    b[1].content = "yo!"
    assert message_signature(a) != message_signature(b)


# ─── old tool results are halved only while the cache is cold ────────


def _halved(session) -> int:
    marker = "Request specific sections if needed.]"
    return sum(
        1 for m in session.get_messages()
        if m.role == "tool_result" and isinstance(m.content, str) and marker in m.content
    )


def test_an_old_tool_result_is_halved_once_not_on_every_call():
    from engine.session import TranscriptManager

    transcript = TranscriptManager()
    for i in range(6):
        call = ToolCall(id=f"c{i}", name="read", arguments={})
        transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        transcript.append_message(Message(role="tool_result", content=LONG_RESULT, tool_call_id=call.id))
    assert transcript.prune_old_tool_results(keep_recent=3) == 3
    sizes = [len(e.message.content) for e in transcript.entries if e.message.role == "tool_result"]
    # Before the fix every later call halved the same three again.
    assert transcript.prune_old_tool_results(keep_recent=3) == 0
    assert [len(e.message.content) for e in transcript.entries if e.message.role == "tool_result"] == sizes


async def test_tool_results_are_not_halved_while_the_prompt_is_cached():
    # 100k window, 50k reserved for output: 20k is 40%, past the 25% where
    # halving starts and short of the 70% where compaction starts.
    steps = [_tools(f"c{i}", input_tokens=20_000) for i in range(6)] + [_end("done", input_tokens=20_000)]
    provider, runner, session = await _ran(steps, context_window=100_000)
    assert _halved(session) == 0

    # Once the cache has expired, halving is free, so it happens.
    runner._sent_prompt.sent_at -= 300.0
    await runner._ensure_context_room(session, RunnerContext())
    assert _halved(session) > 0


async def test_a_pre_request_compaction_mid_turn_continues_the_cached_prompt():
    # The second call reports 80% of the 50k usable window: the next
    # request's pre-request check compacts, by continuing the cached prompt.
    events: list = []
    provider = _CachingProvider(
        [_end("first done"), _tools("c1"), _tools("c2", input_tokens=40_000), _end("second done")],
        context_window=100_000,
    )
    runner = _runner(provider, compaction=SummaryCompaction(keep_recent_messages=2, min_messages_to_compact=2),
                     events=events)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    result = await runner.run(session, "second task")

    assert result.success
    complete = [e for e in events if getattr(e, "type", "") == "compaction_complete"]
    assert len(complete) == 1
    assert complete[0].details["trigger"] == "pre_request"
    assert complete[0].details["summary_continued_prompt"] is True
    summary_request = provider.summary_requests[0]["messages"]
    sent_before = provider.requests[2]["messages"]
    assert summary_request[: len(sent_before)] == sent_before


# ─── provider plumbing ───────────────────────────────────────────────


def test_providers_take_tool_choice_none_and_know_their_cache_lifetime():
    from engine.anthropic_provider import AnthropicConfig, AnthropicProvider
    from engine.openai_provider import OpenAIConfig, OpenAIProvider

    anthropic = AnthropicProvider(AnthropicConfig(api_key="test", model="claude-sonnet-5-5"))
    request = anthropic._build_request([Message(role="user", content="hi")], tool_choice="none")
    assert request["tool_choice"] == {"type": "none"}
    assert anthropic.prompt_cache_ttl_s == 300.0

    sol = OpenAIProvider(OpenAIConfig(api_key="test", model="gpt-6.1-sol"))
    params = sol.build_request_params([Message(role="user", content="hi")], tool_choice="none")
    assert params["tool_choice"] == "none"
    assert sol.prompt_cache_ttl_s == 1800.0
    assert OpenAIProvider(OpenAIConfig(api_key="test", model="gpt-5.5")).prompt_cache_ttl_s == 300.0
    other = OpenAIProvider(OpenAIConfig(api_key="test", model="gpt-6.1-sol", base_url="https://example.test/v1"))
    assert other.prompt_cache_ttl_s is None


def test_openai_sync_complete_sends_tool_choice_and_ignores_thinking(monkeypatch):
    from engine.openai_provider import OpenAIConfig, OpenAIProvider

    provider = OpenAIProvider(OpenAIConfig(api_key="test", model="gpt-6.1-sol"))
    seen: dict = {}

    def create(**params):
        seen.update(params)
        raise RuntimeError("stop here")

    monkeypatch.setattr(provider._client.responses, "create", create)
    with pytest.raises(RuntimeError):
        provider.complete([Message(role="user", content="hi")], thinking=ThinkingConfig(enabled=True, effort="low"),
                          tool_choice="none")
    assert seen["tool_choice"] == "none"
    assert "low" not in json.dumps(seen.get("reasoning", {}))
