"""Every compaction_event row says whose it is and which turn it ran in.

The dashboard groups rows by model and by the agent that wrote them, and the
other rows a turn writes (llm_call_metric, summarize_context_call, the
compaction log) carry its turn id. The thrash detector's rows had no model,
agent_type or parent_session_id, and no compaction_event row had a turn_id:
none of the 6,783 on disk has one.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from engine.runner import AgentRunner, AsyncAgentRunner, RunnerContext
from engine.session import Session
from engine.types import AgentConfig


@pytest.fixture
def rows(monkeypatch, tmp_path) -> list[dict]:
    import bridge.transcript_persistence as tp

    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    rows: list[dict] = []
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = rows.append
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    return rows


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 200_000


def _thrashing_session(metadata: dict | None = None):
    session = Session.create(system_prompt="t", session_id="sess-thrash", metadata=metadata)
    session.add_user_message("hello")
    ctx = RunnerContext()
    ctx.ineffective_compaction_count = 2
    return session, ctx


SUB_AGENT = {"model_id": "claude-sonnet-5-5", "agent_type": "general", "parent_session_id": "parent-1"}


@pytest.mark.parametrize("metadata, tags", [
    (None, ("claude-sonnet-5-5", None, None)),
    (SUB_AGENT, ("claude-sonnet-5-5", "general", "parent-1")),
])
async def test_a_thrash_skip_row_names_the_model_and_the_agent(rows, metadata, tags):
    session, ctx = _thrashing_session(metadata)
    result = await AsyncAgentRunner(_Provider(), config=AgentConfig())._attempt_compaction(session, ctx)

    assert not result.success
    [row] = rows
    assert row["subtype"] == "thrash_skip"
    assert (row.get("model"), row.get("agent_type"), row.get("parent_session_id")) == tags


def test_the_sync_runners_thrash_skip_row_names_the_model_and_the_agent(rows):
    session, ctx = _thrashing_session(SUB_AGENT)
    result = AgentRunner(_Provider(), config=AgentConfig())._attempt_compaction(session, ctx)

    assert not result.success
    [row] = rows
    assert (row.get("model"), row.get("agent_type"), row.get("parent_session_id")) == (
        "claude-sonnet-5-5", "general", "parent-1")


def _bridge_session(tmp_path):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-turn-ids", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    sess.current_turn_id = "turn-7"
    return sess


def test_a_root_sessions_runtime_compaction_row_carries_its_turn(rows, tmp_path):
    sess = _bridge_session(tmp_path)
    sess._mirror_compaction_event("compaction_complete", {
        "tokens_before": 150_000, "tokens_after": 20_000, "trigger": "pre_request",
    })

    [row] = rows
    assert (row["subtype"], row.get("turn_id")) == ("compaction_complete", "turn-7")


def test_a_root_sessions_agent_compaction_row_carries_its_turn(rows, tmp_path):
    import bridge.freyja_bridge as fb

    sess = _bridge_session(tmp_path)
    with patch.object(fb, "emit"):
        sess._emit_summarize_event({
            "type": "system_event", "subtype": "context_pruning",
            "details": {"trigger": "agent_summarize_context", "scope": "tool_results_only",
                        "tokens_before": 90_000, "tokens_after": 60_000},
        })

    [row] = rows
    assert (row["subtype"], row.get("turn_id")) == ("context_pruning", "turn-7")
