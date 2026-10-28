"""Compaction runs in a worker thread, not on the event loop.

Compaction makes blocking provider calls (``complete()``) and waits minutes
for the summary. Run on the loop thread it froze the whole process:
the gateway missed Slack's Socket Mode pings and Slack redelivered the
message; the desktop bridge sent no events and queued every command. Restore
compaction moved off the loop on 2026-10-06 (9e0472b); these cover the
runner's pre-request and overflow compactions and the agent's own
summarize_context.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import ModuleType

import pytest

from engine.compaction import SummaryCompaction
from engine.providers import APIUsage, ContextOverflowError, ProviderResponse, ToolCallResponse
from engine.runner import AsyncAgentRunner, RunnerContext
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig, ThinkingConfig

SUMMARY_DELAY_S = 0.5
LONG_RESULT = "line of output\n" * 400


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every write off the real ~/.freyja (the thrash path imports the
    telemetry module, which freezes Path.home() at import)."""
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


def _tools(call_id: str, *, input_tokens: int = 1_000) -> ProviderResponse:
    return ProviderResponse(
        content="",
        stop_reason="tool_use",
        usage=APIUsage(input_tokens=input_tokens),
        tool_calls=[ToolCallResponse(id=call_id, name="read", arguments={"n": call_id})],
    )


def _end(text: str, *, input_tokens: int = 1_000) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage(input_tokens=input_tokens))


class _SlowSummaryProvider:
    """Plays back the main calls. The summary requests compaction sends
    through the synchronous complete() take SUMMARY_DELAY_S, like a real
    blocking HTTP call."""

    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    prompt_cache_ttl_s = 300.0

    def __init__(self, steps, *, context_window: int = 100_000):
        self.steps = list(steps)
        self.context_window = context_window
        self.summary_requests: list[list] = []
        self.summary_started = threading.Event()
        self.summary_threads: list[str] = []

    async def stream_to_response(self, *, messages, tools=None, system_prompt=None,
                                 max_tokens=None, thinking=None, on_event=None):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def complete(self, messages, *, tools=None, system_prompt=None, max_tokens=None,
                 thinking=None, tool_choice=None):
        self.summary_requests.append(list(messages))
        self.summary_threads.append(threading.current_thread().name)
        self.summary_started.set()
        time.sleep(SUMMARY_DELAY_S)
        usage = APIUsage(input_tokens=40, output_tokens=500)
        if "JSON Schema" in messages[-1].get_text():
            return ProviderResponse(content='{"summary": "wm", "actions_completed": [], "entities": []}',
                                    usage=usage, stop_reason="end_turn")
        return ProviderResponse(content="<summary>We read the files.</summary>", usage=usage,
                                stop_reason="end_turn")


class _ReadTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="read", description="read a file", summary="read")

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        return ToolResult(call_id=call_id, content=LONG_RESULT, is_error=False)


def _runner(provider, events: list, **kw) -> AsyncAgentRunner:
    registry = ToolRegistry()
    registry.register(_ReadTool())

    async def _on_event(ev):
        events.append(ev)

    return AsyncAgentRunner(
        provider,
        config=AgentConfig(),
        tool_registry=registry,
        compaction_strategy=SummaryCompaction(keep_recent_messages=2, min_messages_to_compact=2),
        thinking=ThinkingConfig(enabled=True, effort="high"),
        on_system_event=_on_event,
        **kw,
    )


async def _with_ticker(aw):
    """Await ``aw`` while a task ticks every 10 ms; return its result and
    the longest the ticker went without running."""
    ticks = [time.monotonic()]
    done = False

    async def tick():
        while not done:
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    ticker = asyncio.create_task(tick())
    try:
        result = await aw
    finally:
        ticks.append(time.monotonic())
        done = True
        await ticker
    return result, max(b - a for a, b in zip(ticks, ticks[1:]))


def _types(events: list, kind: str) -> list:
    return [e for e in events if getattr(e, "type", "") == kind]


# The second turn's last call reports 40k of the 50k usable window (80%):
# the request after it compacts before it is sent.
_PRE_REQUEST_STEPS = [_end("first done"), _tools("c1"), _tools("c2", input_tokens=40_000), _end("second done")]


@pytest.mark.parametrize("with_working_memory", [False, True])
async def test_pre_request_compaction_keeps_the_event_loop_running(with_working_memory):
    events: list = []
    provider = _SlowSummaryProvider(_PRE_REQUEST_STEPS)
    # The bridge wires a working-memory sink: Call A and Call B then run in a
    # pool and the caller waits on them with future.result(timeout=...).
    upserts: list = []
    runner = _runner(provider, events, **({"apply_working_memory_upserts": upserts.append}
                                          if with_working_memory else {}))
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    result, longest_gap = await _with_ticker(runner.run(session, "second task"))

    assert result.success
    complete = _types(events, "compaction_complete")
    assert len(complete) == 1 and complete[0].details["trigger"] == "pre_request"
    assert provider.summary_requests, "compaction made no summary call"
    # The ticker kept running while the summary call blocked its thread.
    assert longest_gap < SUMMARY_DELAY_S / 2, f"event loop blocked for {longest_gap:.2f}s"
    assert "MainThread" not in provider.summary_threads


async def test_overflow_compaction_keeps_the_event_loop_running():
    events: list = []
    steps = [_tools("c1"), _tools("c2"), _tools("c3"), _end("first done"),
             ContextOverflowError("prompt is too long: 210000 tokens > 200000 maximum"),
             _end("second done")]
    provider = _SlowSummaryProvider(steps, context_window=1_000_000)
    runner = _runner(provider, events)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    result, longest_gap = await _with_ticker(runner.run(session, "second task"))

    assert result.success
    complete = _types(events, "compaction_complete")
    assert len(complete) == 1 and complete[0].details["trigger"] == "overflow_cascade"
    assert longest_gap < SUMMARY_DELAY_S / 2, f"event loop blocked for {longest_gap:.2f}s"
    assert "MainThread" not in provider.summary_threads


async def test_a_stop_during_compaction_waits_for_the_worker_before_the_turn_ends():
    # The worker rewrites the transcript when the summary returns. A stop
    # can't cut a thread short, so the turn must not end (and hand the
    # transcript to the next turn, a save or a fork) while it still works.
    events: list = []
    provider = _SlowSummaryProvider(_PRE_REQUEST_STEPS)
    runner = _runner(provider, events)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    turn = asyncio.create_task(runner.run(session, "second task"))
    while not provider.summary_started.is_set():
        await asyncio.sleep(0.01)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert session.transcript.entries[0].is_compaction
    # The stop lands after the runner recorded the compaction, as it did
    # when compaction ran on the loop, and the turn goes no further.
    assert len(_types(events, "compaction_complete")) == 1
    assert not any("second done" in m.get_text() for m in session.get_messages())
    assert not runner.turn_active


async def test_an_interrupt_during_compaction_injects_the_message_after_it():
    # The thread can't be cut short, so the interrupt lands once the summary
    # is in: the message goes in after the compaction, and the turn goes on.
    events: list = []
    queued: list[str] = []
    provider = _SlowSummaryProvider(_PRE_REQUEST_STEPS[:3] + [_end("took the follow-up"), _end("spare")])

    async def _inject(session, iteration):
        while queued:
            session.add_user_message(queued.pop(0))

    runner = _runner(provider, events, on_pre_iteration=_inject, has_pending_input=lambda: bool(queued))
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    turn = asyncio.create_task(runner.run(session, "second task"))
    while not provider.summary_started.is_set():
        await asyncio.sleep(0.01)
    queued.append("also check the tests")
    assert runner.request_interrupt()
    result = await turn

    assert result.success
    assert _types(events, "compaction_complete")
    entries = session.transcript.entries
    assert entries[0].is_compaction
    assert any(e.message is not None and "also check the tests" in e.message.get_text() for e in entries[1:])


# ─── the agent's own summarize_context ───────────────────────────────


def _summarize_tool(provider, events: list):
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.session import TranscriptManager
    from engine.types import Message

    transcript = TranscriptManager()
    for i in range(15):
        transcript.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))
    session = Session(id="test-sess", transcript=transcript, system_prompt="")
    compactor = SummaryCompaction()
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: provider,
        get_compactor=lambda: compactor,
        on_system_event=events.append,
    )
    return tool, transcript


async def test_summarize_context_keeps_the_event_loop_running():
    events: list = []
    provider = _SlowSummaryProvider([])
    tool, transcript = _summarize_tool(provider, events)

    result, longest_gap = await _with_ticker(tool.execute("call-1", {"scope": "all"}))

    assert not result.is_error, result.content
    assert transcript.entries[0].is_compaction
    assert [e["subtype"] for e in events] == ["compaction_start", "compaction_complete"]
    assert longest_gap < SUMMARY_DELAY_S / 2, f"event loop blocked for {longest_gap:.2f}s"


async def test_two_summarize_context_calls_in_one_batch_run_one_after_the_other():
    events: list = []
    provider = _SlowSummaryProvider([])
    in_flight = {"now": 0, "most": 0}
    complete = provider.complete

    def _counting_complete(*a, **k):
        in_flight["now"] += 1
        in_flight["most"] = max(in_flight["most"], in_flight["now"])
        try:
            return complete(*a, **k)
        finally:
            in_flight["now"] -= 1

    provider.complete = _counting_complete
    tool, transcript = _summarize_tool(provider, events)

    await asyncio.gather(
        tool.execute("call-1", {"scope": "all"}),
        tool.execute("call-2", {"scope": "all"}),
    )

    # Run at once, both would summarize the same history and both rewrite
    # the transcript.
    assert provider.summary_requests
    assert in_flight["most"] == 1
    assert sum(e.is_compaction for e in transcript.entries) == 1


async def test_an_interrupt_during_summarize_context_waits_for_its_worker():
    # The runner cuts a tool short on an interrupt and then writes its result
    # and the operator's message. Those writes must come after the worker's.
    from bridge.tools.summarize_context_tool import SummarizeContextTool

    events: list = []
    tool_events: list = []
    queued: list[str] = []
    summarize_call = ProviderResponse(
        content="", stop_reason="tool_use", usage=APIUsage(input_tokens=1_000),
        tool_calls=[ToolCallResponse(id="sc1", name="summarize_context", arguments={"scope": "all"})],
    )
    provider = _SlowSummaryProvider(
        [_tools("c1"), _tools("c2"), _tools("c3"), _end("first done"), summarize_call, _end("took the follow-up")],
        context_window=1_000_000,
    )

    async def _inject(session, iteration):
        while queued:
            session.add_user_message(queued.pop(0))

    runner = _runner(provider, events, on_pre_iteration=_inject, has_pending_input=lambda: bool(queued))
    session = Session.create(system_prompt="t")
    runner.tool_registry.register(SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: provider,
        get_compactor=lambda: runner.compaction,
        on_system_event=tool_events.append,
    ))
    await runner.run(session, "first task")

    turn = asyncio.create_task(runner.run(session, "now compact"))
    while not provider.summary_started.is_set():
        await asyncio.sleep(0.01)
    queued.append("also check the tests")
    assert runner.request_interrupt()
    result = await turn

    assert result.success
    assert [e["subtype"] for e in tool_events] == ["compaction_start", "compaction_complete"]
    entries = session.transcript.entries
    assert entries[0].is_compaction
    roles = [(e.message.role, e.message.get_text()) for e in entries[1:]]
    results = [i for i, (role, _) in enumerate(roles) if role == "tool_result"]
    follow_up = [i for i, (_, text) in enumerate(roles) if "also check the tests" in text]
    assert len(results) == 1 and len(follow_up) == 1 and results[0] < follow_up[0]
