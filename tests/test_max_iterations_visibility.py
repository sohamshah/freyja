"""Hitting the per-turn step ceiling must be reported as a truncation.

`StopCondition.should_stop()` returns True for several unrelated reasons —
end_turn, a stop phrase, a refusal, and the iteration ceiling. Both run loops
collapsed all of them into `ctx.state = RunnerState.COMPLETED`, which made the
post-loop `elif ctx.iteration >= max_iterations` branch unreachable. A run that
burned its whole budget therefore returned success=True with no error and no
event, exactly like a model that chose to stop.

Observed 2026-09-08 in a Slack thread: two turns ran the full 100 iterations
investigating alerts. The second one's last streamed sentence was "I have all
the findings now. Posting comments for each of the last 7 untouched alerts in
their own threads." The ceiling cut the run one iteration later. Nothing was
posted and nothing said why — the turn looked like a clean success.

The operator-facing text for the ceiling is in test_runner_failure_surface.py.
"""

from __future__ import annotations

from engine.providers import APIUsage, ProviderResponse
from engine.runner import AgentRunner, AsyncAgentRunner, StopCondition
from engine.session import Session, TranscriptManager
from engine.tools import ToolDefinition, ToolRegistry
from engine.types import ToolCall, ToolResult


class _LoopingProvider:
    """Always asks for another tool call, so only the ceiling can stop it."""

    name = "fake"
    model_id = "fake-model"
    context_window = 100_000

    def complete(self, messages, **kw):
        return ProviderResponse(
            content="working on it",
            tool_calls=[ToolCall(id="t1", name="noop", arguments={})],
            usage=APIUsage(input_tokens=5, output_tokens=5),
            stop_reason="tool_use",
            model="fake-model",
        )

    async def complete_async(self, *, messages, **kw):
        return self.complete(messages, **kw)

    async def stream_to_response(self, *, messages, on_event=None, **kw):
        return self.complete(messages, **kw)


class _NoopTool:
    @property
    def definition(self):
        return ToolDefinition(
            name="noop",
            description="noop",
            summary="noop",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, call_id, arguments):
        return ToolResult(call_id=call_id, content="ok")


def _run_to_ceiling(max_iterations: int = 3):
    runner = AgentRunner(_LoopingProvider())
    runner.tool_registry.register(_NoopTool())
    return runner.run(
        Session(id="s1", transcript=TranscriptManager(), system_prompt="sp"),
        "go",
        stop_condition=StopCondition(max_iterations=max_iterations),
    )


def test_ceiling_is_reported_as_a_truncation_with_the_work_kept():
    """The regression itself returned success=True, error=None. Reporting
    the truncation must not throw away the partial response."""
    res = _run_to_ceiling(max_iterations=3)
    assert res.success is False
    assert res.error is not None
    assert res.error.code == "max_iterations"
    assert res.response == "working on it"
    assert res.iterations == 3
    assert "(3)" in res.error.message


def test_natural_stop_is_still_a_success():
    """The fix must not turn ordinary completions into failures."""

    class _EndsImmediately(_LoopingProvider):
        def complete(self, messages, **kw):
            return ProviderResponse(
                content="all done",
                tool_calls=None,
                usage=APIUsage(input_tokens=5, output_tokens=5),
                stop_reason="end_turn",
                model="fake-model",
            )

    runner = AgentRunner(_EndsImmediately())
    res = runner.run(
        Session(id="s2", transcript=TranscriptManager(), system_prompt="sp"),
        "go",
        stop_condition=StopCondition(max_iterations=10),
    )
    assert res.success is True
    assert res.error is None
    assert res.response == "all done"


async def test_async_loop_reports_the_ceiling_too():
    """The gateway runs the async loop; it must share the sync behaviour."""
    registry = ToolRegistry()
    registry.register(_NoopTool())
    runner = AsyncAgentRunner(_LoopingProvider(), tool_registry=registry)
    res = await runner.run(
        Session(id="s3", transcript=TranscriptManager(), system_prompt="sp"),
        "go",
        stop_condition=StopCondition(max_iterations=3),
    )
    assert res.success is False
    assert res.error is not None
    assert res.error.code == "max_iterations"
    assert res.response == "working on it"
    assert res.iterations == 3
