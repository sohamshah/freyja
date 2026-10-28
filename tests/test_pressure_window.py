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
    """A runner whose context measures ``tokens[0]`` before halving and
    ``tokens[1]`` after it. 100k window, 50k reserved for output."""
    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(max_tokens_per_turn=50_000),
                              compaction_strategy=_Compactor(), on_system_event=events.append)
    session = Session.create(system_prompt="t")
    halved = []

    def _halve(**_kw):
        halved.append(True)
        return 3

    session.transcript.prune_old_tool_results = _halve
    session.transcript.prune_old_appshot_text = lambda: 0
    runner._current_context_tokens = lambda _s: tokens[1] if halved else tokens[0]
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
