"""A restore compaction reaches the compaction telemetry.

Restore compaction (a long transcript reloaded, or a Slack thread revisited
after hours) only sent its events to the renderer. Every other compaction
also writes a compaction_event row to telemetry/compaction.jsonl, which the
metrics dashboard counts, and a row with the full summary to the session's
compactions.jsonl, which exports read. No telemetry row had a restore
trigger, though the gateway log shows restore compactions.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from engine.compaction import CompactionResult, SummaryCompaction
from engine.session import Session
from engine.types import AgentConfig


@pytest.fixture
def rows(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    # A session's project directory is read from HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    rows: list[dict] = []
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = rows.append
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    return rows


def _restoring_session(monkeypatch, tmp_path, result: CompactionResult):
    import bridge.transcript_persistence as tp
    import engine.runner as runner_module
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-restore", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    session = Session.create(system_prompt="t")
    session.add_user_message(" ".join(f"word{i}" for i in range(2_000)))
    session.restore_transcript = lambda _data: None
    provider = SimpleNamespace(name="anthropic", model_id="claude-sonnet-5-5", context_window=200_000)

    async def _initialize():
        sess.session = session
        sess.provider = provider
        sess.runner = runner_module.AsyncAgentRunner(provider, config=AgentConfig())
        sess._bridge_loop = asyncio.get_running_loop()

    sess.initialize = _initialize
    sess._ledger_ground_truth = lambda: None
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None
    logged: list[dict] = []
    sess._append_compaction_log = logged.append

    def _compact(_self, transcript, _provider, **_kw):
        if result.success:
            transcript.append_compaction(result.summary, first_kept_id="", tokens_before=result.tokens_before)
        return result

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    monkeypatch.setattr(tp, "load_transcript", lambda _id: {"metadata": {}, "transcript": {"entries": [{}]}})
    monkeypatch.setattr(runner_module, "CONTEXT_COMPACTION_THRESHOLD", 0.0)
    return sess, logged


def _compaction_rows(rows: list[dict]) -> list[tuple]:
    return [(r["subtype"], r["trigger"], r["reason"]) for r in rows if r["type"] == "compaction_event"]


async def test_a_restore_compaction_writes_its_telemetry_and_summary(monkeypatch, tmp_path, rows):
    result = CompactionResult(success=True, summary="We built the parser.", tokens_before=400_000,
                              tokens_after=5_000, entries_removed=40)
    sess, logged = _restoring_session(monkeypatch, tmp_path, result)

    with patch("bridge.freyja_bridge.emit"):
        assert await sess.try_restore_transcript()

    assert _compaction_rows(rows) == [
        ("compaction_start", "restore", "pressure"),
        ("compaction_complete", "restore", "pressure"),
    ]
    complete = [r for r in rows if r.get("subtype") == "compaction_complete"][0]
    assert complete["tokens_before"] > complete["tokens_after"] > 0
    assert complete["summary_text"] == "We built the parser."
    assert logged[-1]["summary_text"] == "We built the parser."


async def test_a_failed_restore_compaction_writes_its_telemetry(monkeypatch, tmp_path, rows):
    result = CompactionResult(success=False, summary=None, tokens_before=400_000, tokens_after=400_000,
                              error="compaction summary call exceeded 180s")
    sess, _logged = _restoring_session(monkeypatch, tmp_path, result)

    with patch("bridge.freyja_bridge.emit"):
        assert await sess.try_restore_transcript()

    assert _compaction_rows(rows) == [
        ("compaction_start", "restore", "pressure"),
        ("compaction_failed", "restore", "pressure"),
    ]
