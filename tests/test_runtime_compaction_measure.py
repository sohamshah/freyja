"""A pre-request or overflow compaction reports its sizes by one measure.

The size before was the reading that set the compaction off: the larger of
the last call's measured size and the tokenizer's count. The size after was
the tokenizer's count, since no call has measured the compacted transcript.
On Claude the tokenizer reads about two thirds of the measured size, so the
gap between the two measures was reported as saved. Both sizes are now the
tokenizer's count of the request; the reading that set the compaction off
stays in trigger_tokens, as it says why the compaction ran.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from engine.compaction import CompactionResult, SummaryCompaction
from engine.providers import APIUsage, ContextOverflowError, ProviderResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.types import AgentConfig


@pytest.fixture
def rows(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))  # the session's project directory
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    rows: list[dict] = []
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = rows.append
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    return rows


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 100_000  # 50k usable with the 50k output reserve

    def __init__(self, steps):
        self.steps = list(steps)

    async def stream_to_response(self, **_kw):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _end(text: str, input_tokens: int) -> ProviderResponse:
    return ProviderResponse(content=text, stop_reason="end_turn", usage=APIUsage(input_tokens=input_tokens))


async def _compact_through(monkeypatch, steps, *, tokenizer_before: int):
    """Two turns; the second compacts. The tokenizer counts the prompt at
    ``tokenizer_before`` and at 5k once compacted. Returns the runner's
    compaction events."""
    events: list = []

    async def _on_event(ev):
        events.append(ev)

    provider = _Provider(steps)
    runner = AsyncAgentRunner(provider, config=AgentConfig(max_tokens_per_turn=50_000),
                              compaction_strategy=SummaryCompaction(), on_system_event=_on_event)
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    size = {"tokens": tokenizer_before}
    session.estimate_tokens = lambda: size["tokens"]

    def _compact(_self, transcript, _provider, **_kw):
        size["tokens"] = 5_000
        return CompactionResult(success=True, summary="We did the first task.", tokens_before=24_000,
                                tokens_after=4_000)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    result = await runner.run(session, "second task")
    assert result.success
    return {e.type: e.details for e in events if e.type.startswith("compaction_")}


def _mirrored(details_by_type: dict, tmp_path) -> dict:
    """The compaction_complete row the bridge writes for these events."""
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-runtime", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    rows: list[dict] = []
    sys.modules["bridge.compaction_telemetry"].append_telemetry = rows.append
    sess._mirror_compaction_event("compaction_complete", details_by_type["compaction_complete"])
    return rows[0]


async def test_a_pre_request_compaction_reports_both_sizes_by_the_tokenizer(monkeypatch, tmp_path, rows):
    # The first turn's call measures the prompt at 38k: 76% of the usable
    # window, so the second turn compacts before its first call.
    details = await _compact_through(
        monkeypatch, [_end("first done", 38_000), _end("second done", 6_000)], tokenizer_before=25_000,
    )

    complete = details["compaction_complete"]
    assert complete["trigger"] == "pre_request"
    assert (complete["tokens_before"], complete["tokens_after"]) == (25_000, 5_000)
    assert (complete["context_tokens_before"], complete["context_tokens_after"]) == (25_000, 5_000)
    assert (complete["pressure_pct_before"], complete["pressure_pct_after"]) == (50.0, 10.0)
    assert (complete["trigger_tokens"], complete["trigger_pressure_pct"]) == (38_000, 76.0)
    start = details["compaction_start"]
    assert (start["tokens_before"], start["trigger_tokens"]) == (25_000, 38_000)

    row = _mirrored(details, tmp_path)
    assert (row["tokens_before"], row["tokens_after"]) == (25_000, 5_000)
    assert (row["trigger_tokens"], row["trigger_pressure_pct"]) == (38_000, 76.0)


async def test_an_overflow_compaction_reports_both_sizes_by_the_tokenizer(monkeypatch, tmp_path, rows):
    # The provider refuses the second turn's request as too long; the last
    # call measured the prompt at 30k, the tokenizer counts it at 20k.
    details = await _compact_through(
        monkeypatch,
        [_end("first done", 30_000), ContextOverflowError("prompt is too long"), _end("second done", 6_000)],
        tokenizer_before=20_000,
    )

    complete = details["compaction_complete"]
    assert complete["trigger"] == "overflow_cascade"
    assert (complete["tokens_before"], complete["tokens_after"]) == (20_000, 5_000)
    assert (complete["trigger_tokens"], complete["trigger_pressure_pct"]) == (30_000, 60.0)
    assert (details["compaction_start"]["tokens_before"], details["compaction_start"]["trigger_tokens"]) == (
        20_000, 30_000,
    )

    row = _mirrored(details, tmp_path)
    assert (row["tokens_before"], row["tokens_after"]) == (20_000, 5_000)
    assert row["trigger_tokens"] == 30_000
