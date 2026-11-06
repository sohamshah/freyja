"""The bridge's ``_compaction_in_flight`` flag is cleared when a compaction ends.

The flag keeps the mid-session working-memory extraction from running while
a compaction runs its own (Call B). The bridge sets it on compaction_start
and clears it on compaction_complete or compaction_skipped. A compaction that
ended with neither left it set until the next compaction, and no mid-session
extraction ran in between.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

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


class _FailingCompactor:
    prompt_source = None

    def compact(self, transcript, provider, **_kw):
        return CompactionResult(success=False, summary=None, tokens_before=0, tokens_after=0,
                                error="compaction summary call exceeded 180s")


def _runner(on_system_event) -> tuple[AsyncAgentRunner, Session]:
    """A runner whose context is 40k of the 50k usable (80%): the next
    request compacts first, and the compaction fails."""
    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(max_tokens_per_turn=50_000),
                              compaction_strategy=_FailingCompactor(), on_system_event=on_system_event)
    runner._current_context_tokens = lambda _s: 40_000
    return runner, Session.create(system_prompt="t")


def _bridge_session(tmp_path):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-in-flight", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    # Nothing here may write the real ~/.freyja.
    sess._mirror_compaction_event = lambda *_a, **_k: None
    sess._write_compaction_snapshot = lambda **_k: {}
    sess._append_compaction_log = lambda *_a, **_k: None
    sess._save_transcript = lambda: None
    return sess


async def test_a_failed_pre_request_compaction_says_so():
    events: list = []
    runner, session = _runner(events.append)

    await runner._ensure_context_room(session, RunnerContext())

    kinds = [e.type for e in events if e.type.startswith("compaction")]
    assert kinds == ["compaction_start", "compaction_skipped"]
    skipped = events[-1]
    assert skipped.details["trigger"] == "pre_request"
    assert "180s" in skipped.details["reason"]


async def test_the_bridge_clears_its_flag_after_a_failed_pre_request_compaction(tmp_path):
    import bridge.freyja_bridge as fb

    sess = _bridge_session(tmp_path)
    runner, session = _runner(sess._on_system_event)

    with patch.object(fb, "emit"):
        await runner._ensure_context_room(session, RunnerContext())

    assert sess._compaction_in_flight is False


async def test_the_bridge_clears_its_flag_after_a_cheap_summarize_context(tmp_path):
    # tool_results_only and exploration_only rewrite tool results in place and
    # end with context_pruning, not compaction_complete.
    import bridge.freyja_bridge as fb
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction

    sess = _bridge_session(tmp_path)
    session = Session.create(system_prompt="t")
    session.add_user_message("read the logs")
    session.add_tool_result("c1", "log line\n" * 400)
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=_Provider,
        get_compactor=SummaryCompaction,
        on_system_event=sess._emit_summarize_event,
    )

    with patch.object(fb, "emit") as emitted:
        result = await tool.execute("sc1", {"scope": "tool_results_only"})

    assert not result.is_error, result.content
    subtypes = [c.args[0]["subtype"] for c in emitted.call_args_list]
    assert subtypes == ["compaction_start", "context_pruning"]
    assert sess._compaction_in_flight is False
