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
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig, ThinkingConfig

# How long a summary call blocks its thread in the tests that watch the event
# loop. The gap assertions scale with it.
SUMMARY_DELAY_S = 0.2
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
    through the synchronous complete() block their thread like a real HTTP
    call: for ``delay`` seconds, or until ``release`` is set."""

    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    prompt_cache_ttl_s = 300.0

    def __init__(self, steps, *, context_window: int = 100_000, delay: float = SUMMARY_DELAY_S,
                 release: threading.Event | None = None):
        self.steps = list(steps)
        self.context_window = context_window
        self.delay = delay
        self.release = release
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
        if self.release is not None:
            self.release.wait(5)
        else:
            time.sleep(self.delay)
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


async def _once_the_summary_is_in_flight(turn: asyncio.Task, provider: _SlowSummaryProvider) -> None:
    while not provider.summary_started.is_set():
        assert not turn.done(), "the turn ended without a summary call"
        await asyncio.sleep(0.01)


async def _let_the_loop_run(steps: int = 20) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


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


async def test_overflow_compaction_keeps_the_event_loop_running(monkeypatch):
    # After the cascade the runner backs off 0.5 s before it retries the
    # request, as after any provider error. That wait is not what this
    # measures: clamp every sleep to the ticker's 10 ms.
    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args, **kwargs):
        return await real_sleep(min(delay, 0.01), *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
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
    release = threading.Event()
    provider = _SlowSummaryProvider(_PRE_REQUEST_STEPS, release=release)
    runner = _runner(provider, events)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    turn = asyncio.create_task(runner.run(session, "second task"))
    try:
        await _once_the_summary_is_in_flight(turn, provider)
        turn.cancel()
        await _let_the_loop_run()
        assert not turn.done(), "the stop did not wait for the worker"
    finally:
        release.set()
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
    release = threading.Event()
    provider = _SlowSummaryProvider(_PRE_REQUEST_STEPS[:3] + [_end("took the follow-up"), _end("spare")],
                                    release=release)

    async def _inject(session, iteration):
        while queued:
            session.add_user_message(queued.pop(0))

    runner = _runner(provider, events, on_pre_iteration=_inject, has_pending_input=lambda: bool(queued))
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")

    turn = asyncio.create_task(runner.run(session, "second task"))
    try:
        await _once_the_summary_is_in_flight(turn, provider)
        queued.append("also check the tests")
        assert runner.request_interrupt()
    finally:
        release.set()
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
    provider = _SlowSummaryProvider([], delay=0.05)
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
    release = threading.Event()
    summarize_call = ProviderResponse(
        content="", stop_reason="tool_use", usage=APIUsage(input_tokens=1_000),
        tool_calls=[ToolCallResponse(id="sc1", name="summarize_context", arguments={"scope": "all"})],
    )
    provider = _SlowSummaryProvider(
        [_tools("c1"), _tools("c2"), _tools("c3"), _end("first done"), summarize_call, _end("took the follow-up")],
        context_window=1_000_000, release=release,
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
    try:
        await _once_the_summary_is_in_flight(turn, provider)
        queued.append("also check the tests")
        assert runner.request_interrupt()
    finally:
        release.set()
    result = await turn

    assert result.success
    assert [e["subtype"] for e in tool_events] == ["compaction_start", "compaction_complete"]
    entries = session.transcript.entries
    assert entries[0].is_compaction
    roles = [(e.message.role, e.message.get_text()) for e in entries[1:]]
    results = [i for i, (role, _) in enumerate(roles) if role == "tool_result"]
    follow_up = [i for i, (_, text) in enumerate(roles) if "also check the tests" in text]
    assert len(results) == 1 and len(follow_up) == 1 and results[0] < follow_up[0]


# ─── restore and manual compaction (the bridge) ──────────────────────


class _Worker:
    """Stands in for SummaryCompaction.compact: blocks its thread like the
    summary call until released, then rewrites the transcript, and notes
    whether the bridge still counted a compaction as running when it
    finished."""

    def __init__(self, sess):
        self.sess = sess
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.in_flight_at_finish: list[bool] = []

    def compact(self, _compactor, transcript, provider, **_kw):
        from engine.compaction import CompactionResult

        self.started.set()
        self.release.wait(5)
        transcript.append_compaction(summary="We read the files.", first_kept_id=transcript.entries[-1].id,
                                     tokens_before=10_000)
        self.in_flight_at_finish.append(self.sess._compaction_in_flight)
        self.finished.set()
        return CompactionResult(success=True, summary="We read the files.", tokens_before=10_000,
                                tokens_after=100)


def _bridge_session(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-off-loop", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    session = Session.create(system_prompt="t")
    for i in range(6):
        session.add_user_message(f"u{i} " + "x" * 2_000)
        session.add_assistant_message(f"a{i}")

    async def _initialize():
        sess.session = session
        sess.provider = _SlowSummaryProvider([])

    sess.initialize = _initialize
    # Nothing here may write the real ~/.freyja.
    sess._mirror_compaction_event = lambda *_a, **_k: None
    sess._write_compaction_snapshot = lambda **_k: {}
    sess._save_transcript = lambda: None
    sess._ledger_ground_truth = lambda: None
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None
    worker = _Worker(sess)
    monkeypatch.setattr(SummaryCompaction, "compact", lambda self, *a, **k: worker.compact(self, *a, **k))
    return sess, session, worker


async def _stop_while_compacting(aw, worker: _Worker) -> None:
    task = asyncio.create_task(aw)
    try:
        while not worker.started.is_set():
            assert not task.done(), "returned without compacting"
            await asyncio.sleep(0.01)
        task.cancel()
        await _let_the_loop_run()
        # A thread can't be cut short: the stop waits for the worker to
        # write the transcript, and the flag holds off the mid-session
        # extraction until then.
        assert not task.done(), "the stop did not wait for the worker"
    finally:
        worker.release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        worker.finished.wait(5)
    assert worker.finished.is_set()
    assert worker.in_flight_at_finish == [True]


async def test_a_stop_during_restore_compaction_waits_for_the_worker(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp
    import engine.runner as runner_module

    sess, session, worker = _bridge_session(monkeypatch, tmp_path)
    session.restore_transcript = lambda _data: None
    initialize = sess.initialize

    async def _initialize_with_runner():
        await initialize()
        sess.runner = AsyncAgentRunner(sess.provider, config=AgentConfig())

    sess.initialize = _initialize_with_runner
    monkeypatch.setattr(tp, "load_transcript", lambda _id: {"metadata": {}, "transcript": {"entries": [{}]}})
    monkeypatch.setattr(runner_module, "CONTEXT_COMPACTION_THRESHOLD", 0.0)

    await _stop_while_compacting(sess.try_restore_transcript(), worker)

    assert any(e.is_compaction for e in session.transcript.entries)


async def test_a_stop_during_manual_compaction_waits_for_the_worker(monkeypatch, tmp_path):
    sess, session, worker = _bridge_session(monkeypatch, tmp_path)
    await sess.initialize()

    await _stop_while_compacting(sess.force_compact(), worker)

    assert any(e.is_compaction for e in session.transcript.entries)


async def test_manual_compaction_stops_the_keepalive_before_its_worker_starts(monkeypatch, tmp_path):
    # Between turns a keep-alive re-reads the cached prompt just before it
    # expires. A manual compaction runs between turns: the ping read the
    # transcript while the worker rewrote it, and paid to keep a prompt the
    # compaction was about to replace.
    import engine.runner as runner_module

    monkeypatch.setattr(runner_module, "CACHE_KEEPALIVE_LEAD_S", 0.3)
    monkeypatch.setattr(runner_module, "PROMPT_CACHE_TTL_MARGIN_S", 0.1)

    class _KeepAliveProvider(_SlowSummaryProvider):
        prompt_cache_ttl_s = 0.6

        def __init__(self, steps):
            super().__init__(steps)
            self.pings: list = []

        async def keep_prompt_cached(self, messages, **_kw):
            self.pings.append(list(messages))
            return ProviderResponse(content="", usage=APIUsage(cache_read_tokens=5_000, output_tokens=1))

    sess, session, worker = _bridge_session(monkeypatch, tmp_path)
    provider = _KeepAliveProvider([_end("done")])
    sess.runner = AsyncAgentRunner(provider, config=AgentConfig(), keep_cache_warm=True)
    await sess.runner.run(session, "a task")
    assert sess.runner._cache_keeper is not None

    await sess.force_compact()

    assert worker.finished.is_set()
    assert provider.pings == []
