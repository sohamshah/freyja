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


@pytest.mark.parametrize("scope, subtype", [("all", "compaction_complete"),
                                             ("tool_results_only", "context_pruning")])
async def test_the_agents_compaction_event_carries_the_sizes_the_chat_card_reads(
    telemetry_rows, tmp_path, monkeypatch, scope, subtype,
):
    """The chat's compacted card reads the sizes from context_tokens_before and
    context_tokens_after, which the runtime's events carry. The agent's events
    carried only tokens_before and tokens_after, so its card showed no size
    change."""
    import bridge.freyja_bridge as fb

    monkeypatch.setattr("bridge.tools.summarize_context_tool.SummarizeContextTool._save_original",
                        lambda _self, _text, _name: str(tmp_path / "saved.txt"))
    sess = _bridge_session(tmp_path, tool_results=scope == "tool_results_only")
    request_before = sess.session.estimate_tokens()

    with patch.object(fb, "emit") as emitted:
        result = await _tool(sess, []).execute("sc1", {"scope": scope})

    assert not result.is_error, result.content
    request_after = sess.session.estimate_tokens()
    [done] = [c.args[0] for c in emitted.call_args_list if c.args[0].get("subtype") == subtype]
    details = done["details"]
    assert (details.get("context_tokens_before"), details.get("context_tokens_after")) == (
        request_before, request_after)
    assert (details["tokens_before"], details["tokens_after"]) == (request_before, request_after)


@pytest.mark.parametrize("scope, subtype", [("all", "compaction_complete"),
                                             ("tool_results_only", "context_pruning")])
async def test_the_agents_compaction_rows_carry_the_window_and_pressure(
    telemetry_rows, tmp_path, monkeypatch, scope, subtype,
):
    """The runtime's compaction events carry the usable window and the
    pressure before and after (Runner._compaction_event_details), and their
    telemetry rows keep them. The agent's carried neither, so its rows had no
    pressure: none of the 359 agent-driven compaction_complete and
    context_pruning rows on disk has one."""
    import bridge.freyja_bridge as fb
    from engine.runner import AsyncAgentRunner
    from engine.types import AgentConfig

    monkeypatch.setattr("bridge.tools.summarize_context_tool.SummarizeContextTool._save_original",
                        lambda _self, _text, _name: str(tmp_path / "saved.txt"))
    sess = _bridge_session(tmp_path, tool_results=scope == "tool_results_only")
    logged: list[dict] = []
    sess._append_compaction_log = logged.append
    sess.runner = AsyncAgentRunner(_Provider(), config=AgentConfig())
    window = _Provider.context_window - AgentConfig().max_tokens_per_turn
    assert sess._effective_window() == window
    tool = _wired_tool(sess)

    with patch.object(fb, "emit") as emitted:
        result = await tool.execute("sc1", {"scope": scope})

    assert not result.is_error, result.content
    [done] = [c.args[0]["details"] for c in emitted.call_args_list
              if c.args[0].get("subtype") == subtype]
    before, after = done["tokens_before"], done["tokens_after"]
    assert done.get("effective_window") == window
    assert done.get("pressure_pct_before") == round(before / window * 100, 1)
    assert done.get("pressure_pct_after") == round(after / window * 100, 1)
    pressure = (window, done["pressure_pct_before"], done["pressure_pct_after"])
    [row] = [r for r in telemetry_rows if r.get("subtype") == subtype]
    [log_row] = [r for r in logged if r.get("subtype") == subtype]
    for entry in (row, log_row):
        assert (entry.get("effective_window"), entry.get("pressure_pct_before"),
                entry.get("pressure_pct_after")) == pressure
    [start] = [r for r in telemetry_rows if r.get("subtype") == "compaction_start"]
    assert (start.get("effective_window"), start.get("pressure_pct_before")) == pressure[:2]


def _wired_tool(sess):
    """The tool as the desktop registry builds it from the bridge's getters."""
    from bridge.tools.registry import build_desktop_registry

    registry = build_desktop_registry(
        workspace=str(sess.workspace), include_web=False, include_computer=False,
        include_subagents=False,
        summarize_context_session_getter=lambda: sess.session,
        summarize_context_provider_getter=_Provider,
        summarize_context_compactor_getter=SummaryCompaction,
        summarize_context_window_getter=lambda: sess._effective_window(),
        summarize_context_on_system_event=sess._emit_summarize_event,
    )
    return registry._tools["summarize_context"]  # noqa: SLF001


async def test_the_agents_summary_event_and_rows_carry_the_summary_fields(telemetry_rows, tmp_path, monkeypatch):
    """The runtime's compaction events carry summary_tokens and
    summarizer_duration_ms (Runner._compaction_event_details), and their rows
    keep them with resumed_from_previous. The agent's event left out the first
    two and its telemetry row all three, though the compactor's result has
    them."""
    import bridge.freyja_bridge as fb
    from bridge.tools.summarize_context_tool import SummarizeContextTool

    dispatch = SummarizeContextTool._dispatch
    results = []

    def _dispatch(self, *args, **kwargs):
        result = dispatch(self, *args, **kwargs)
        result.summarizer_duration_ms = 4_321
        result.resumed_from_previous = True
        results.append(result)
        return result

    monkeypatch.setattr(SummarizeContextTool, "_dispatch", _dispatch)
    sess = _bridge_session(tmp_path)
    logged: list[dict] = []
    sess._append_compaction_log = logged.append

    with patch.object(fb, "emit") as emitted:
        result = await _tool(sess, []).execute("sc1", {"scope": "all"})

    assert not result.is_error, result.content
    [compacted] = results
    assert compacted.summary_tokens > 0
    fields = (compacted.summary_tokens, 4_321)
    [done] = [c.args[0]["details"] for c in emitted.call_args_list
              if c.args[0].get("subtype") == "compaction_complete"]
    assert (done.get("summary_tokens"), done.get("summarizer_duration_ms")) == fields
    [row] = [r for r in telemetry_rows if r.get("subtype") == "compaction_complete"]
    assert (row.get("summary_tokens"), row.get("summarizer_duration_ms"),
            row.get("resumed_from_previous")) == (*fields, True)
    [log_row] = [r for r in logged if r.get("subtype") == "compaction_complete"]
    assert (log_row.get("summary_tokens"), log_row.get("resumed_from_previous")) == (fields[0], True)
