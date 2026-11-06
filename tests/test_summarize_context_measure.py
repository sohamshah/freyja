"""The agent's summarize_context reports its sizes by the request, as the
runtime's compactions do.

Its rows took the sizes before and after from the transcript alone, so they
left out the system prompt and the tool definitions, which every request
carries and no compaction removes. The share saved read too high: one Opus 5.5
row read 86.8% where the measured sizes of the calls just before and after it
give 79.7%. The pre-request, overflow, manual and restore compactions count
the system prompt, the transcript and the tools (``Session.estimate_tokens``).
"""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from engine.compaction import SummaryCompaction
from engine.providers import APIUsage, ProviderResponse
from engine.session import Session, TranscriptManager
from engine.tokenizer import count_tokens
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


SYSTEM_PROMPT = "You are a careful agent. " * 400
TOOL_TOKENS = 30_000


def _bridge_session(tmp_path, *, tool_results: bool = False):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-sc-measure", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    transcript = TranscriptManager()
    for i in range(15):
        transcript.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))
        if tool_results:
            transcript.append_message(Message(role="tool", content="line of output\n" * 300,
                                              tool_call_id=f"c{i}"))
    sess.session = Session(id="test-sc-measure", transcript=transcript, system_prompt=SYSTEM_PROMPT,
                           tool_tokens=TOOL_TOKENS)
    return sess


def _tool(sess, calls: list[dict]):
    from bridge.tools.summarize_context_tool import SummarizeContextTool

    return SummarizeContextTool(
        get_session=lambda: sess.session,
        get_provider=_Provider,
        get_compactor=SummaryCompaction,
        on_system_event=sess._emit_summarize_event,
        on_summarize_call=calls.append,
    )


@pytest.mark.parametrize("scope, subtype", [("all", "compaction_complete"),
                                             ("tool_results_only", "context_pruning")])
async def test_the_agents_compaction_counts_the_whole_request_on_both_sides(
    telemetry_rows, tmp_path, monkeypatch, scope, subtype,
):
    import bridge.freyja_bridge as fb

    monkeypatch.setattr("bridge.tools.summarize_context_tool.SummarizeContextTool._save_original",
                        lambda _self, _text, _name: str(tmp_path / "saved.txt"))
    sess = _bridge_session(tmp_path, tool_results=scope == "tool_results_only")
    fixed = count_tokens(SYSTEM_PROMPT) + TOOL_TOKENS
    request_before = sess.session.estimate_tokens()
    calls: list[dict] = []

    with patch.object(fb, "emit"):
        result = await _tool(sess, calls).execute("sc1", {"scope": scope})

    assert not result.is_error, result.content
    request_after = sess.session.estimate_tokens()
    assert request_after < request_before
    [start] = [r for r in telemetry_rows if r.get("subtype") == "compaction_start"]
    [done] = [r for r in telemetry_rows if r.get("subtype") == subtype]
    assert start["tokens_before"] == request_before
    assert (done["tokens_before"], done["tokens_after"]) == (request_before, request_after)
    # The decision row and the agent's result say the same.
    [call] = calls
    assert (call["tokens_before"], call["tokens_after"]) == (request_before, request_after)
    body = json.loads(result.content)
    assert (body["tokens_before"], body["tokens_after"]) == (request_before, request_after)
    assert body["tokens_freed"] == request_before - request_after
    # What no compaction removes is on both sides.
    assert done["tokens_after"] >= fixed
