"""After halving old tool results, the pre-request check reads pressure
against the same usable window as before it.

The thresholds (CONTEXT_PRESSURE_THRESHOLD 0.25, CONTEXT_COMPACTION_THRESHOLD
0.70) are fractions of the EFFECTIVE window: the context window minus the
output reserved for the reply, since the provider rejects a request whose
input plus max_tokens exceeds the window. The recompute after halving divided
by the raw window, so with a large output reserve a context still past 70% of
what fits looked like 40% and compaction was skipped.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from engine.compaction import CompactionResult
from engine.runner import AsyncAgentRunner, RunnerContext
from engine.session import Session
from engine.types import AgentConfig


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 100_000


class _Compactor:
    prompt_source = None

    def compact(self, transcript, provider, **_kw):
        return CompactionResult(success=False, summary=None, tokens_before=0, tokens_after=0,
                                error="test stub")


def _runner(events: list, tokens: tuple[int, int]) -> tuple[AsyncAgentRunner, Session]:
    """A runner whose last call measured ``tokens[0]``, and whose halving
    takes the context to ``tokens[1]``. 100k window, 50k reserved for output."""
    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(max_tokens_per_turn=50_000),
                              compaction_strategy=_Compactor(), on_system_event=events.append)
    session = Session.create(system_prompt="t")
    halved = []

    def _halve(**_kw):
        halved.append(True)
        return 3

    session.transcript.prune_old_tool_results = _halve
    session.transcript.prune_old_appshot_text = lambda: 0
    runner._current_context_tokens = lambda _s: tokens[0]
    session.estimate_tokens = lambda: tokens[1] if halved else tokens[0]
    return runner, session


def _types(events: list, kind: str) -> list:
    return [e for e in events if getattr(e, "type", "") == kind]


async def test_halving_that_leaves_the_context_past_70_percent_still_compacts():
    # 45k of the 50k usable (90%) halves to 40k: still 80%, so compaction
    # must run. Read against the raw 100k window, 40k looked like 40%.
    events: list = []
    runner, session = _runner(events, (45_000, 40_000))

    await runner._ensure_context_room(session, RunnerContext())

    assert _types(events, "context_pruning")
    starts = _types(events, "compaction_start")
    assert len(starts) == 1 and starts[0].details["trigger"] == "pre_request"


async def test_halving_that_brings_the_context_under_70_percent_does_not_compact():
    events: list = []
    runner, session = _runner(events, (45_000, 30_000))  # 60% after

    await runner._ensure_context_room(session, RunnerContext())

    assert _types(events, "context_pruning")
    assert not _types(events, "compaction_start")


async def test_a_halving_reports_the_size_before_and_after_it():
    # The pruning rows in telemetry read tokens_before and tokens_after; both
    # were 0 on every row, so what halving saved was never measured.
    events: list = []
    runner, session = _runner(events, (45_000, 30_000))

    await runner._ensure_context_room(session, RunnerContext())

    details = _types(events, "context_pruning")[0].details
    assert (details["tokens_before"], details["tokens_after"]) == (45_000, 30_000)
    assert (details["pressure_pct_before"], details["pressure_pct_after"]) == (90.0, 60.0)
    assert details["effective_window"] == 50_000


async def test_halving_that_brings_a_measured_context_under_70_percent_does_not_compact(tmp_path):
    # The last call measured 37k of the 50k usable (74%). Halving three old
    # results takes off far more than 2k, but the check after it read the
    # last call's size again, unchanged, and compacted anyway.
    from engine.types import Message, ToolCall

    events: list = []
    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(max_tokens_per_turn=50_000),
                              compaction_strategy=_Compactor(), on_system_event=events.append)
    runner.truncator._output_dir = tmp_path / "truncated"
    session = Session.create(system_prompt="t")
    for i in range(6):
        call = ToolCall(id=f"c{i}", name="read", arguments={})
        session.transcript.append_message(Message(role="assistant", content="", tool_calls=[call]))
        session.transcript.append_message(Message(
            role="tool_result", tool_call_id=call.id,
            content="".join(f"{i}.{n} some output on this line\n" for n in range(500)),
        ))
    runner.usage.last_input = 37_000
    before = session.estimate_tokens()

    await runner._ensure_context_room(session, RunnerContext())

    removed = before - session.estimate_tokens()
    assert before < 37_000 and removed > 2_000
    details = _types(events, "context_pruning")[0].details
    assert details["tokens_after"] == 37_000 - removed
    assert not _types(events, "compaction_start")


def test_the_sync_runner_reads_pressure_against_the_usable_window():
    # The old synchronous AgentRunner checks pressure after each call. 40k is
    # 80% of the 50k usable but 40% of the raw 100k window.
    from engine.providers import APIUsage, ProviderResponse
    from engine.runner import AgentRunner

    class _SyncProvider(_Provider):
        def complete(self, **_kw):
            return ProviderResponse(content="done", stop_reason="end_turn", usage=APIUsage(input_tokens=40_000))

    compacted = []

    class _CountingCompactor(_Compactor):
        def compact(self, transcript, provider, **kw):
            compacted.append(True)
            return super().compact(transcript, provider, **kw)

    runner = AgentRunner(_SyncProvider(), config=AgentConfig(max_tokens_per_turn=50_000),
                         compaction_strategy=_CountingCompactor())

    runner.run(Session.create(system_prompt="t"), "a task")

    assert compacted == [True]
