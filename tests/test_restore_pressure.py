"""A restored session is measured the way the runner measures every turn.

try_restore_transcript decides whether a transcript loaded from disk is
compacted before its first turn. It compared the tokenizer's count to 70% of
the model's raw window, where every other check compares the runner's
measure to 70% of the usable window (the window less the output reserve). A
transcript between the two was left alone on restore and compacted inside
its first turn instead.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from engine.compaction import CompactionResult, SummaryCompaction
from engine.providers import APIUsage, ProviderResponse
from engine.runner import AsyncAgentRunner
from engine.session import Session
from engine.types import AgentConfig

# claude-sonnet-5-5: a 1M window less the 50k output reserve.
WINDOW = 1_000_000


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
    context_window = WINDOW

    def __init__(self):
        self.requests: list[list] = []

    async def stream_to_response(self, *, messages, tools=None, system_prompt=None,
                                 max_tokens=None, thinking=None, on_event=None):
        self.requests.append(list(messages))
        return ProviderResponse(content="done", stop_reason="end_turn", usage=APIUsage(input_tokens=1_000))


def _restoring(monkeypatch, tmp_path, *, tokens: int):
    """A bridge session about to restore a transcript the tokenizer counts at
    ``tokens``, and 5,000 once it is compacted."""
    import bridge.transcript_persistence as tp
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-restore", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    session = Session.create(system_prompt="t")
    session.add_user_message("the earlier task")
    session.restore_transcript = lambda _data: None
    size = {"tokens": tokens}
    session.estimate_tokens = lambda: size["tokens"]
    provider = _Provider()
    runner = AsyncAgentRunner(provider, config=AgentConfig())

    async def _initialize():
        sess.session, sess.provider, sess.runner = session, provider, runner
        sess._bridge_loop = asyncio.get_running_loop()

    sess.initialize = _initialize
    sess._ledger_ground_truth = lambda: None
    sess._restore_goal_state = lambda: False
    sess._restore_inbox = lambda: None
    sess._append_compaction_log = lambda _row: None

    def _compact(_self, transcript, _provider, **_kw):
        size["tokens"] = 5_000
        return CompactionResult(success=True, summary="We did the earlier task.", tokens_before=tokens,
                                tokens_after=5_000)

    monkeypatch.setattr(SummaryCompaction, "compact", _compact)
    monkeypatch.setattr(tp, "load_transcript", lambda _id: {"metadata": {}, "transcript": {"entries": [{}]}})
    return sess, runner, session, provider


def _compactions(rows: list[dict]) -> list[tuple]:
    return [(r["subtype"], r["trigger"], r["reason"]) for r in rows if r["type"] == "compaction_event"]


async def test_a_restore_compacts_past_70_percent_of_the_usable_window(monkeypatch, tmp_path, rows):
    # 680k is 72% of the 950k usable window, but under 70% of the raw 1M.
    sess, _runner, _session, _provider = _restoring(monkeypatch, tmp_path, tokens=680_000)

    with patch("bridge.freyja_bridge.emit"):
        assert await sess.try_restore_transcript()

    assert _compactions(rows) == [
        ("compaction_start", "restore", "pressure"),
        ("compaction_complete", "restore", "pressure"),
    ]


async def test_a_restore_under_70_percent_of_the_usable_window_is_not_compacted(monkeypatch, tmp_path, rows):
    sess, _runner, _session, _provider = _restoring(monkeypatch, tmp_path, tokens=650_000)

    with patch("bridge.freyja_bridge.emit"):
        assert await sess.try_restore_transcript()

    assert _compactions(rows) == []
