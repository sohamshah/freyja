"""Every model call's cost reaches the session's total, whichever thread
reports it.

Compaction reports its summary and working-memory calls from worker threads
(engine/runner.py run_off_loop, and the summary's own pool thread, which
reports late when the summary outlives its deadline). The loop thread reports
the main calls and the keep-alive pings, which run between turns, when the
operator can start a manual compaction. Both added to cumulative_cost with a
read, an add and a write, so one report could overwrite the other's.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    # A session's project directory (compactions.jsonl) is read from HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


async def test_a_summary_cost_from_a_worker_and_a_ping_on_the_loop_both_count(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    import engine.providers as providers
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-cost", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._bridge_loop = asyncio.get_running_loop()
    loop_thread = threading.current_thread()
    worker_holding = threading.Event()
    loop_added = threading.Event()

    class _Cost:
        """A cost whose conversion holds a worker thread until the loop has
        added its own: the worker has read the total and not yet written it
        back, the window a thread switch can fall into."""

        def __init__(self, usd: float) -> None:
            self.usd = usd

        def __float__(self) -> float:
            if threading.current_thread() is not loop_thread:
                worker_holding.set()
                loop_added.wait(5)
            return self.usd

        def __format__(self, spec: str) -> str:
            return format(self.usd, spec)

    monkeypatch.setattr(providers, "compute_cost",
                        lambda _model, **kw: _Cost(1.0 if kw["output_tokens"] > 1 else 0.25))
    summary = {"provider": "anthropic", "model": "claude-sonnet-5-5", "output_tokens": 900,
               "call_kind": "summarizer"}
    ping = {"provider": "anthropic", "model": "claude-sonnet-5-5", "output_tokens": 1,
            "call_kind": "cache_keepalive"}

    with patch.object(fb, "emit"):
        worker = asyncio.ensure_future(asyncio.to_thread(sess._on_llm_call, summary))
        while not (worker_holding.is_set() or worker.done()):
            await asyncio.sleep(0.005)
        sess._on_llm_call(ping)
        loop_added.set()
        await worker
        for _ in range(3):
            await asyncio.sleep(0)

    assert sess.cumulative_cost == pytest.approx(1.25)


async def test_a_restore_compactions_summary_call_counts(monkeypatch, tmp_path):
    # Restore compaction (a long transcript reloaded, or a Slack thread
    # revisited after hours) summarizes the whole history on a fresh runner,
    # with no cached prompt to continue: the costliest summary there is. It
    # passed no on_summarizer_call, so the spend reached neither the session's
    # total nor the llm_call_metric rows.
    import bridge.freyja_bridge as fb
    import bridge.transcript_persistence as tp
    import engine.constants as constants
    from bridge.freyja_bridge import _BridgeSession, _BridgeState
    from engine.compaction import CompactionResult, SummaryCompaction
    from engine.session import Session

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-restore-cost", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    session = Session.create(system_prompt="t")
    session.add_user_message("u " + "x" * 2_000)
    session.restore_transcript = lambda _data: None

    async def _initialize():
        sess.session = session
        sess.provider = SimpleNamespace(name="anthropic", model_id="claude-sonnet-5-5")
        sess._bridge_loop = asyncio.get_running_loop()

    sess.initialize = _initialize
    sess._ledger_ground_truth = lambda: None
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None

    def _compact(_self, transcript, provider, *, on_summarizer_call=None, **_kw):
        if on_summarizer_call is not None:
            on_summarizer_call({"call_kind": "summarizer", "provider": "anthropic",
                                "model": "claude-sonnet-5-5", "input_tokens": 400_000,
                                "output_tokens": 3_000, "duration_ms": 90_000})
        return CompactionResult(success=True, summary="s", tokens_before=400_000, tokens_after=5_000)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    monkeypatch.setattr(tp, "load_transcript", lambda _id: {"metadata": {}, "transcript": {"entries": [{}]}})
    monkeypatch.setattr(constants, "CONTEXT_COMPACTION_THRESHOLD", 0.0)

    with patch.object(fb, "emit"):
        assert await sess.try_restore_transcript()

    assert sess.cumulative_cost > 0
