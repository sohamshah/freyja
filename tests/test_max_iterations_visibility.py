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

The user-facing half matters as much as the flag: the final message is usually
a statement of intent, so silence reads as the agent simply not doing what it
said it would.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from bridge.freyja_bridge import _format_user_facing_runner_failure
from engine.providers import APIUsage, ProviderResponse
from engine.runner import AgentRunner, StopCondition
from engine.session import Session, TranscriptManager
from engine.tools import ToolDefinition
from engine.types import ToolCall, ToolResult

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "engine" / "runner.py"


# ── the runner marks the ceiling distinctly ──────────────────────────

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


def test_ceiling_is_reported_as_failure_not_success():
    """The regression itself: this returned success=True, error=None."""
    res = _run_to_ceiling()
    assert res.success is False
    assert res.error is not None
    assert res.error.code == "max_iterations"


def test_ceiling_preserves_the_partial_response():
    """Reporting the truncation must not throw away the work done."""
    res = _run_to_ceiling()
    assert res.response == "working on it"


def test_ceiling_reports_the_iteration_count():
    res = _run_to_ceiling(max_iterations=3)
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


def test_async_loop_checks_the_ceiling_too():
    """The gateway runs the async loop; it must share the sync behaviour.
    Driving it needs the full async harness, so pin the branch statically."""
    tree = ast.parse(RUNNER.read_text())
    async_loops = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_loop"
    ]
    assert len(async_loops) == 1
    src = ast.unparse(async_loops[0])
    assert "ctx.iteration >= max_iterations" in src
    assert "iteration_limit" in src


# ── the operator is told ─────────────────────────────────────────────

def test_step_limit_message_shows_even_after_streamed_prose():
    """The whole point: the streamed text is a promise the ceiling broke."""
    out = _format_user_facing_runner_failure(
        reason="unknown",
        message="Reached maximum iterations (100)",
        already_streamed=True,
        code="max_iterations",
    )
    assert out, "silent truncation after streamed prose is the actual bug"
    assert "100 steps" in out
    assert "did NOT" in out  # explicitly negates the announced action
    assert out.startswith("\n\n---\n")  # separated from the prose above


def test_step_limit_message_without_prior_prose_has_no_separator():
    out = _format_user_facing_runner_failure(
        reason="unknown",
        message="Reached maximum iterations (100)",
        already_streamed=False,
        code="max_iterations",
    )
    assert out.startswith("⚠️")


def test_step_limit_message_survives_an_unparseable_count():
    out = _format_user_facing_runner_failure(
        reason="unknown",
        message="Reached maximum iterations",
        already_streamed=False,
        code="max_iterations",
    )
    assert "step limit" in out
    assert "()" not in out


def test_other_failures_still_stay_quiet_after_streamed_prose():
    """The new branch must not widen the existing policy: a late provider
    error behind good prose is still noise."""
    out = _format_user_facing_runner_failure(
        reason="rate_limit",
        message="429 overloaded",
        already_streamed=True,
        code="",
    )
    assert out == ""


def test_code_defaults_so_existing_callers_are_unaffected():
    out = _format_user_facing_runner_failure(
        reason="rate_limit", message="429", already_streamed=False
    )
    assert "overloaded" in out
