"""Mid-turn input: follow-ups slide into a running turn, inject-now cuts in.

Engine-level contract of AsyncAgentRunner:

* ``has_pending_input`` — input that lands while the final reply streams
  keeps the turn going for one more step instead of being stranded.
* ``request_interrupt`` — cuts the in-flight provider call or tool batch
  short so the pre-iteration hook injects the message now. Streamed text
  is kept (only when something is about to be injected — otherwise it
  would be an assistant prefill), running tools get ``[interrupted]``,
  tools that never started get ``[not run]``, and a real stop (outer
  cancel) is never swallowed.
"""

from __future__ import annotations

import asyncio

import pytest

from engine.providers import APIUsage, ProviderResponse, ToolCallResponse
from engine.runner import (
    INTERRUPTED_TOOL_RESULT,
    SKIPPED_TOOL_RESULT,
    AsyncAgentRunner,
)
from engine.session import Session
from engine.tools import ToolDefinition, ToolRegistry, ToolResult
from engine.types import AgentConfig, TextDeltaEvent


def _end(text: str) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage())


def _tools(*calls: tuple[str, str]) -> ProviderResponse:
    return ProviderResponse(
        content="",
        stop_reason="tool_use",
        usage=APIUsage(),
        tool_calls=[ToolCallResponse(id=cid, name=name, arguments={}) for cid, name in calls],
    )


class _ScriptedProvider:
    """Plays back a list of steps. A step is a ProviderResponse, or a
    callable ``(on_event) -> awaitable ProviderResponse`` for calls that
    stream or block."""

    name = "fake"
    model_id = "fake-model"
    context_window = 1_000_000

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests: list[list] = []

    async def stream_to_response(self, *, messages, on_event=None, **_kw):
        self.requests.append(list(messages))
        step = self.steps.pop(0)
        if callable(step):
            return await step(on_event)
        return step

    async def complete_async(self, *, messages, **_kw):
        return await self.stream_to_response(messages=messages)


class _Tool:
    def __init__(self, name: str, behavior):
        self._name = name
        self._behavior = behavior
        self.started = asyncio.Event()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self._name, description=self._name, summary=self._name)

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        self.started.set()
        content = await self._behavior()
        return ToolResult(call_id=call_id, content=content, is_error=False)


async def _forever():
    await asyncio.Event().wait()
    return "unreachable"


async def _quick():
    return "quick result"


class _Inbox:
    """Minimal stand-in for the bridge inbox: the pre-iteration hook
    injects whatever is queued; has_pending_input reports it."""

    def __init__(self):
        self.queue: list[str] = []
        self.injected_at: list[int] = []

    def has(self) -> bool:
        return bool(self.queue)

    async def drain(self, session, iteration):
        while self.queue:
            session.add_user_message(self.queue.pop(0))
            self.injected_at.append(iteration)


def _runner(provider, inbox: _Inbox, *, registry=None, config=None, events=None):
    async def _on_system_event(ev):
        if events is not None:
            events.append(ev)

    return AsyncAgentRunner(
        provider,
        config=config or AgentConfig(),
        tool_registry=registry,
        on_pre_iteration=inbox.drain,
        has_pending_input=inbox.has,
        on_system_event=_on_system_event,
    )


def _texts(session) -> list[tuple[str, str]]:
    out = []
    for m in session.get_messages():
        content = m.content if isinstance(m.content, str) else str(m.content)
        out.append((m.role, content))
    return out


async def test_request_interrupt_is_a_noop_when_idle():
    runner = _runner(_ScriptedProvider([]), _Inbox())
    assert runner.turn_active is False
    assert runner.request_interrupt() is False


async def test_followup_during_final_reply_extends_the_turn():
    inbox = _Inbox()

    async def final_reply(on_event):
        # The operator hits Enter while the model is writing its answer.
        inbox.queue.append("also check the tests")
        return _end("first answer")

    provider = _ScriptedProvider([final_reply, _end("second answer")])
    events: list = []
    runner = _runner(provider, inbox, events=events)
    session = Session.create(system_prompt="t")

    result = await runner.run(session, "do the thing")

    assert result.success is True
    # Both replies are this turn's answer (the goal judge reads it).
    assert result.response == "first answer\n\nsecond answer"
    assert len(provider.requests) == 2
    assert inbox.injected_at == [2]
    roles = [r for r, _ in _texts(session)]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert any(getattr(e, "type", "") == "turn_extended" for e in events)
    # The extended call ends on the injected user message, never a prefill.
    assert provider.requests[1][-1].role == "user"


async def test_turn_ends_normally_with_nothing_pending():
    inbox = _Inbox()
    provider = _ScriptedProvider([_end("done")])
    runner = _runner(provider, inbox)
    result = await runner.run(Session.create(system_prompt="t"), "hi")
    assert result.success is True
    assert len(provider.requests) == 1


async def test_interrupt_mid_stream_keeps_partial_text_and_injects():
    inbox = _Inbox()
    runner_ref: dict = {}

    async def long_stream(on_event):
        await on_event(TextDeltaEvent(text="Starting on the refactor by "))
        inbox.queue.append("stop — use the other approach")
        assert runner_ref["r"].request_interrupt() is True
        return await _forever()

    provider = _ScriptedProvider([long_stream, _end("switching approach")])
    events: list = []
    runner = _runner(provider, inbox, events=events)
    runner_ref["r"] = runner
    session = Session.create(system_prompt="t")

    result = await asyncio.wait_for(runner.run(session, "refactor it"), timeout=5)

    assert result.success is True
    assert result.response == "switching approach"
    msgs = _texts(session)
    assert msgs[1] == ("assistant", "Starting on the refactor by ")
    assert msgs[2] == ("user", "stop — use the other approach")
    interrupted = [e for e in events if getattr(e, "type", "") == "turn_interrupted"]
    assert interrupted and interrupted[0].details["phase"] == "llm"
    note = runner.consume_interrupt_note()
    assert note == {"phase": "llm", "partial_chars": len("Starting on the refactor by"), "tools": []}
    assert runner.consume_interrupt_note() is None


async def test_interrupt_without_pending_input_drops_partial_text():
    """A partial reply is only kept when a message is about to follow it —
    a transcript ending on an assistant turn is a prefill (400 on 4.6+)."""
    inbox = _Inbox()
    runner_ref: dict = {}

    async def long_stream(on_event):
        await on_event(TextDeltaEvent(text="half a thought"))
        runner_ref["r"].request_interrupt()
        return await _forever()

    provider = _ScriptedProvider([long_stream, _end("ok")])
    runner = _runner(provider, inbox)
    runner_ref["r"] = runner
    session = Session.create(system_prompt="t")

    await asyncio.wait_for(runner.run(session, "go"), timeout=5)

    assert ("assistant", "half a thought") not in _texts(session)
    assert provider.requests[1][-1].role == "user"


async def test_interrupt_during_sequential_tools_marks_running_and_skipped():
    inbox = _Inbox()
    slow = _Tool("slow", _forever)
    fast = _Tool("fast", _quick)
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(fast)
    provider = _ScriptedProvider([_tools(("c1", "slow"), ("c2", "fast")), _end("adjusted")])
    events: list = []
    runner = _runner(
        provider, inbox, registry=registry,
        config=AgentConfig(parallel_tool_execution=False), events=events,
    )
    session = Session.create(system_prompt="t")

    async def cut_in():
        await slow.started.wait()
        inbox.queue.append("never mind, do X")
        runner.request_interrupt()

    cutter = asyncio.create_task(cut_in())
    result = await asyncio.wait_for(runner.run(session, "work"), timeout=5)
    await cutter

    assert result.response == "adjusted"
    results = {
        m.tool_call_id: m.content for m in session.get_messages() if m.role == "tool_result"
    }
    assert results == {"c1": INTERRUPTED_TOOL_RESULT, "c2": SKIPPED_TOOL_RESULT}
    assert not fast.started.is_set()
    # The message is injected right after the tool results.
    msgs = session.get_messages()
    assert msgs[-2].role == "user" and msgs[-2].content == "never mind, do X"
    ev = next(e for e in events if getattr(e, "type", "") == "turn_interrupted")
    assert ev.details["phase"] == "tools"
    assert ev.details["cut"] == [
        {"id": "c1", "name": "slow", "started": True},
        {"id": "c2", "name": "fast", "started": False},
    ]


async def test_interrupt_during_parallel_tools_keeps_finished_results():
    inbox = _Inbox()
    slow = _Tool("slow", _forever)
    fast = _Tool("fast", _quick)
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(fast)
    provider = _ScriptedProvider([_tools(("c1", "fast"), ("c2", "slow")), _end("ok")])
    runner = _runner(provider, inbox, registry=registry)
    session = Session.create(system_prompt="t")

    async def cut_in():
        await slow.started.wait()
        await asyncio.sleep(0.05)  # let the fast tool finish
        inbox.queue.append("hold on")
        runner.request_interrupt()

    cutter = asyncio.create_task(cut_in())
    await asyncio.wait_for(runner.run(session, "work"), timeout=5)
    await cutter

    results = {
        m.tool_call_id: m.content for m in session.get_messages() if m.role == "tool_result"
    }
    assert results["c1"] == "quick result"
    assert results["c2"] == INTERRUPTED_TOOL_RESULT


async def test_outer_cancel_still_stops_the_turn():
    """An operator stop cancels the turn task; the interrupt racing must
    not swallow that cancellation."""
    inbox = _Inbox()

    async def hang(on_event):
        return await _forever()

    runner = _runner(_ScriptedProvider([hang]), inbox)
    task = asyncio.create_task(runner.run(Session.create(system_prompt="t"), "go"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.turn_active is False


class _YieldingTool(_Tool):
    """Steps aside by itself shortly after a cut-in (like bash moving its
    command to the background) and returns a normal result."""

    yields_on_interrupt = True

    def __init__(self, name, runner_ref):
        super().__init__(name, None)
        self._runner_ref = runner_ref

    async def execute(self, call_id: str, arguments: dict) -> ToolResult:
        self.started.set()
        runner = self._runner_ref["r"]
        while not runner._interrupt_pending():
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)  # detaching takes a moment
        return ToolResult(call_id=call_id, content="moved to background", is_error=False)


@pytest.mark.parametrize("parallel", [False, True])
async def test_a_yielding_tool_gets_to_step_aside_instead_of_being_cut(parallel):
    inbox = _Inbox()
    ref: dict = {}
    build = _YieldingTool("bash", ref)
    slow = _Tool("slow", _forever)
    registry = ToolRegistry()
    registry.register(build)
    registry.register(slow)
    calls = [("c1", "bash"), ("c2", "slow")] if parallel else [("c1", "bash")]
    provider = _ScriptedProvider([_tools(*calls), _end("ok")])
    runner = _runner(provider, inbox, registry=registry,
                     config=AgentConfig(parallel_tool_execution=parallel))
    ref["r"] = runner
    session = Session.create(system_prompt="t")

    async def cut_in():
        await build.started.wait()
        inbox.queue.append("switch tasks")
        runner.request_interrupt()

    cutter = asyncio.create_task(cut_in())
    await asyncio.wait_for(runner.run(session, "build it"), timeout=5)
    await cutter
    results = {m.tool_call_id: m.content for m in session.get_messages() if m.role == "tool_result"}
    assert results["c1"] == "moved to background"
    if parallel:
        assert results["c2"] == INTERRUPTED_TOOL_RESULT
    assert any(m.role == "user" and m.content == "switch tasks" for m in session.get_messages())


async def test_a_stop_mid_reply_keeps_what_was_said_and_notes_it():
    inbox = _Inbox()

    async def long_stream(on_event):
        await on_event(TextDeltaEvent(text="Here is the plan: first"))
        return await _forever()

    runner = _runner(_ScriptedProvider([long_stream]), inbox)
    session = Session.create(system_prompt="t")
    task = asyncio.create_task(runner.run(session, "plan it"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _texts(session)[-1] == ("assistant", "Here is the plan: first")
    assert runner.consume_stop_note() == {"phase": "llm", "partial_chars": len("Here is the plan: first")}
    assert runner.consume_stop_note() is None


async def test_a_stop_during_tools_notes_which_were_cut():
    inbox = _Inbox()
    slow = _Tool("slow", _forever)
    registry = ToolRegistry()
    registry.register(slow)
    runner = _runner(
        _ScriptedProvider([_tools(("c1", "slow"))]), inbox, registry=registry,
        config=AgentConfig(parallel_tool_execution=False),
    )
    session = Session.create(system_prompt="t")
    task = asyncio.create_task(runner.run(session, "go"))
    await slow.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.consume_stop_note() == {"phase": "tools", "tools": ["slow"]}



async def test_a_cut_in_after_a_handled_stop_is_still_a_cut_in():
    """A turn loop that caught an earlier stop (without uncancel) leaves
    the task's cancelling() at 1. The next turn's cut-in must still be an
    interrupt, not look like another stop."""
    inbox = _Inbox()
    ref: dict = {}

    async def long_stream(on_event):
        await on_event(TextDeltaEvent(text="working on it"))
        inbox.queue.append("new direction")
        ref["r"].request_interrupt()
        return await _forever()

    async def body():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass  # a stop, handled — count stays at 1
        assert asyncio.current_task().cancelling() == 1
        runner = _runner(_ScriptedProvider([long_stream, _end("ok")]), inbox)
        ref["r"] = runner
        return await runner.run(Session.create(system_prompt="t"), "go")

    task = asyncio.create_task(body())
    await asyncio.sleep(0.02)
    task.cancel()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.success is True and result.response == "ok"


async def test_text_before_a_refusal_fallback_is_not_kept():
    from engine.types import StreamEvent

    inbox = _Inbox()
    ref: dict = {}

    async def stream_with_fallback(on_event):
        await on_event(TextDeltaEvent(text="declined model's partial"))
        await on_event(StreamEvent(type="text_reset"))
        await on_event(TextDeltaEvent(text="fallback model's reply"))
        inbox.queue.append("cut in")
        ref["r"].request_interrupt()
        return await _forever()

    runner = _runner(_ScriptedProvider([stream_with_fallback, _end("ok")]), inbox)
    ref["r"] = runner
    session = Session.create(system_prompt="t")
    await asyncio.wait_for(runner.run(session, "go"), timeout=5)
    kept = [c for r, c in _texts(session) if r == "assistant"]
    assert kept[0] == "fallback model's reply"
