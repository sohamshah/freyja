"""A manual compaction reports its sizes before and after by one measure.

force_compact took the size before as the larger of the last call's measured
size and the tokenizer's count, and the size after as the tokenizer's count:
no call has measured the compacted transcript yet. On Claude the tokenizer
reads about two thirds of the measured size, so the gap between the two
measures was reported as saved. The dashboard's tokens saved and savings
trend, and the compaction card's percentage, read these two fields.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from engine.compaction import CompactionResult, SummaryCompaction
from engine.providers import APIUsage, ProviderResponse
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
    context_window = 100_000

    async def stream_to_response(self, **_kw):
        # The provider measures the prompt at 38k.
        return ProviderResponse(content="done", stop_reason="end_turn", usage=APIUsage(input_tokens=38_000))


async def test_a_manual_compaction_reports_both_sizes_by_the_tokenizer(monkeypatch, tmp_path, rows):
    import bridge.freyja_bridge as fb
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    provider = _Provider()
    runner = AsyncAgentRunner(provider, config=AgentConfig(max_tokens_per_turn=50_000))
    session = Session.create(system_prompt="t")
    await runner.run(session, "first task")
    # The tokenizer counts the same prompt at 25k, and 5k once compacted.
    size = {"tokens": 25_000}
    session.estimate_tokens = lambda: size["tokens"]

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-manual", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)

    async def _initialize():
        sess.session, sess.provider, sess.runner = session, provider, runner

    sess.initialize = _initialize
    sess._append_compaction_log = lambda _row: None
    sess._write_compaction_snapshot = lambda **_k: {}
    sess._save_transcript = lambda: None
    sess._ledger_ground_truth = lambda: None

    def _compact(_self, transcript, _provider, **_kw):
        size["tokens"] = 5_000
        return CompactionResult(success=True, summary="We did the first task.", tokens_before=24_000,
                                tokens_after=4_000)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    await sess.force_compact()

    complete = [r for r in rows if r.get("subtype") == "compaction_complete"][0]
    assert (complete["tokens_before"], complete["tokens_after"]) == (25_000, 5_000)
    details = [e["details"] for e in events if e.get("subtype") == "compaction_complete"][0]
    assert (details["context_tokens_before"], details["context_tokens_after"]) == (25_000, 5_000)
    assert details["last_provider_context_tokens"] == 38_000
    start = [r for r in rows if r.get("subtype") == "compaction_start"][0]
    assert start["tokens_before"] == 25_000
