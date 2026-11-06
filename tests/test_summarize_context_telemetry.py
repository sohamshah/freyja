"""The agent's own summarize_context records what its summary call cost.

The runner's, the overflow and the manual compactions write the summarizer's
tokens and cost into their compaction_event rows (cross-session telemetry and
the session's compactions.jsonl). The agent path's rows left them out, though
the tool's event carries them: 312 of 312 agent-driven compaction_complete
rows have no summarizer cost.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from engine.compaction import SummaryCompaction
from engine.providers import APIUsage, ProviderResponse
from engine.session import Session, TranscriptManager
from engine.types import Message


@pytest.fixture
def telemetry_rows(monkeypatch, tmp_path) -> list[dict]:
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

    def complete(self, messages, **_kw):
        return ProviderResponse(content="<summary>We read the files.</summary>", stop_reason="end_turn",
                                usage=APIUsage(input_tokens=20_000, output_tokens=800))


async def test_the_agents_summary_call_cost_reaches_both_logs(telemetry_rows, tmp_path):
    import bridge.freyja_bridge as fb
    from bridge.freyja_bridge import _BridgeSession, _BridgeState
    from bridge.tools.summarize_context_tool import SummarizeContextTool

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-sc-cost", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    per_session: list[dict] = []
    sess._append_compaction_log = per_session.append
    transcript = TranscriptManager()
    for i in range(15):
        transcript.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))
    sess.session = Session(id="test-sc-cost", transcript=transcript, system_prompt="")
    tool = SummarizeContextTool(
        get_session=lambda: sess.session,
        get_provider=_Provider,
        get_compactor=SummaryCompaction,
        on_system_event=sess._emit_summarize_event,
    )

    with patch.object(fb, "emit"):
        result = await tool.execute("sc1", {"scope": "all"})

    assert not result.is_error, result.content
    [row] = [r for r in telemetry_rows if r.get("subtype") == "compaction_complete"]
    [logged] = [r for r in per_session if r.get("subtype") == "compaction_complete"]
    for entry in (row, logged):
        assert entry["summarizer_input_tokens"] == 20_000
        assert entry["summarizer_output_tokens"] == 800
        assert entry["summarizer_cost_usd"] and entry["summarizer_cost_usd"] > 0
