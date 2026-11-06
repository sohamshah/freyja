"""A sub-agent keeps a compaction log, as a root session does.

A root session appends one row per compaction event, with the full summary, to
``<project dir>/compactions.jsonl``, and its export reads them. A sub-agent's
compactions (the runner's, and the ones it asks for with summarize_context)
went nowhere but the parent's event stream, so its export had none. A
sub-agent now appends the same rows to ``~/.freyja/sessions/<id>.compactions.jsonl``,
beside its other sidecars: not under projects/, which the briefing scans. Its
compactions also write the dashboard's ``compaction_event`` rows, and the
summary call its own summarize_context makes is costed like any other call.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from engine.types import SystemEvent, ToolCall

LONG_RESULT = "".join(f"line {i} of the output\n" for i in range(400))


@pytest.fixture(autouse=True)
def telemetry_rows(monkeypatch, tmp_path) -> list[dict]:
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    rows: list[dict] = []
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = rows.append
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    return rows


async def _run_child(monkeypatch, tmp_path, record_id: str, body, events: list | None = None):
    """Run a sub-agent whose runner does ``body(runner, session)``, then stops."""
    import engine.runner as runner_module
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry
    from bridge.tools.summarize_context_tool import SummarizeContextTool

    class _Runner(runner_module.AsyncAgentRunner):
        async def run(self, session, prompt, **_k):
            session.add_user_message(prompt)
            await body(self, session)
            raise RuntimeError("stop here")

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)

    async def _emit(event):
        if events is not None:
            events.append(event)

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

        def complete(self, messages, **_kw):
            from engine.providers import APIUsage, ProviderResponse

            return ProviderResponse(content="<summary>We read the files.</summary>", stop_reason="end_turn",
                                    usage=APIUsage(input_tokens=20_000, output_tokens=800))

    # The parent's summarize_context, which the child swaps for its own.
    parent_registry = ToolRegistry()
    parent_registry.register(
        SummarizeContextTool(get_session=lambda: None, get_provider=lambda: None,
                             get_compactor=lambda: None)
    )
    registry = SubAgentRegistry()
    spec = sat.SubAgentSpec(
        parent_workspace=str(tmp_path),
        parent_model="claude-sonnet-5-5",
        build_provider=lambda *a, **k: _Provider(),
        parent_registry=parent_registry,
        registry=registry,
        emit_event=_emit,
        parent_session_id="parent-1",
    )
    record = registry.register(id=record_id, label="reader", task="read it", mode="background")
    with pytest.raises(RuntimeError, match="stop here"):
        await sat.SubAgentTool(spec)._run_child(record)


def _rows(tmp_path, record_id: str) -> list[dict]:
    log = tmp_path / "sessions" / f"{record_id}.compactions.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


async def test_a_sub_agent_logs_the_runners_compactions_with_the_full_summary(
    monkeypatch, tmp_path, telemetry_rows,
):
    summary = "S" * 5_000

    async def body(runner, _session):
        await runner._emit_system_event(SystemEvent(
            type="compaction_start", message="",
            details={"tokens_before": 150_000, "trigger": "pre_request"},
        ))
        await runner._emit_system_event(SystemEvent(
            type="compaction_complete", message="",
            details={"tokens_before": 150_000, "tokens_after": 20_000, "trigger": "pre_request",
                     "strategy": "llm_summary", "entries_removed": 40,
                     "summary_preview": summary[:700], "summary_text": summary},
        ))
        # Not a compaction: not logged.
        await runner._emit_system_event(SystemEvent(type="verification_nudge", message=""))

    await _run_child(monkeypatch, tmp_path, "sub_c_1", body)

    rows = _rows(tmp_path, "sub_c_1")
    assert [r["subtype"] for r in rows] == ["compaction_start", "compaction_complete"]
    done = rows[1]
    assert done["session_id"] == "sub_c_1"
    assert done["model"] == "claude-sonnet-5-5"
    assert (done["trigger"], done["mechanism"]) == ("pre_request", "llm_summary")
    assert (done["tokens_before"], done["tokens_after"], done["entries_removed"]) == (150_000, 20_000, 40)
    assert done["summary_text"] == summary
    # Nothing in the parent's project directory: that log is the parent's.
    assert not (tmp_path / ".freyja" / "projects" / "parent-1" / "compactions.jsonl").exists()
    # The dashboard's rows, tagged with the child's profile and parent.
    events = [r for r in telemetry_rows if r.get("type") == "compaction_event"]
    assert [(r["session_id"], r["subtype"]) for r in events] == [
        ("sub_c_1", "compaction_start"), ("sub_c_1", "compaction_complete"),
    ]
    assert (events[1]["agent_type"], events[1]["parent_session_id"]) == ("general", "parent-1")
    assert (events[1]["mechanism"], events[1]["tokens_after"]) == ("llm_summary", 20_000)


async def test_a_sub_agent_logs_the_compactions_it_asks_for(monkeypatch, tmp_path):
    async def body(runner, session):
        call = ToolCall(id="c1", name="grep", arguments={})
        session.add_assistant_message("", tool_calls=[call])
        session.add_tool_result("c1", LONG_RESULT)
        tool = runner.tool_registry.get("summarize_context")
        result = await tool.execute("sc1", {"scope": "tool_results_only", "reason": "done reading"})
        assert not result.is_error, result.content
        # A summary scope's event carries an excerpt; the row has the summary
        # whole, from the compaction entry the summary left in the transcript.
        session.transcript.append_compaction("T" * 5_000, first_kept_id="", tokens_before=9)
        tool._emit_system_event({
            "type": "system_event", "subtype": "compaction_complete",
            "details": {"trigger": "agent_summarize_context", "scope": "all",
                        "summary_excerpt": "T" * 240},
        })

    await _run_child(monkeypatch, tmp_path, "sub_c_2", body)

    rows = _rows(tmp_path, "sub_c_2")
    assert [r["subtype"] for r in rows] == ["compaction_start", "context_pruning", "compaction_complete"]
    assert {r["trigger"] for r in rows} == {"agent_summarize_context"}
    assert rows[1]["scope"] == "tool_results_only"
    assert rows[1]["reason"] == "done reading"
    assert rows[2]["summary_text"] == "T" * 5_000


async def test_a_sub_agents_own_summary_is_costed_and_reaches_the_telemetry_and_its_pane(
    monkeypatch, tmp_path, telemetry_rows,
):
    # The summary call summarize_context makes for the child, its compaction
    # events and their telemetry rows, as the parent's tool reports them.
    from engine.types import Message

    async def body(runner, session):
        for i in range(15):
            session.transcript.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))
        tool = runner.tool_registry.get("summarize_context")
        result = await tool.execute("sc1", {"scope": "all", "reason": "fold it"})
        assert not result.is_error, result.content

    events: list[dict] = []
    await _run_child(monkeypatch, tmp_path, "sub_c_3", body, events)

    [call] = [r for r in telemetry_rows if r.get("type") == "llm_call_metric"]
    assert (call["session_id"], call["call_kind"]) == ("sub_c_3", "summarizer")
    assert (call["input_tokens"], call["output_tokens"]) == (20_000, 800)
    assert call["cost_usd"] and call["cost_usd"] > 0
    compactions = [r for r in telemetry_rows if r.get("type") == "compaction_event"]
    assert [r["subtype"] for r in compactions] == ["compaction_start", "compaction_complete"]
    done = compactions[1]
    assert (done["session_id"], done["agent_type"], done["parent_session_id"]) == ("sub_c_3", "general", "parent-1")
    assert (done["trigger"], done["scope"], done["reason"]) == ("agent_summarize_context", "all", "fold it")
    assert done["summarizer_input_tokens"] == 20_000
    assert "We read the files." in done["summary_text"]
    assert "We read the files." in _rows(tmp_path, "sub_c_3")[1]["summary_text"]
    # The child's pane shows it, as the runner's compactions are shown.
    shown = [e for e in events if e.get("type") == "system_event" and "compaction" in e["subtype"]]
    assert [(e["sessionId"], e["subtype"]) for e in shown] == [
        ("sub_c_3", "compaction_start"), ("sub_c_3", "compaction_complete"),
    ]


async def test_a_computer_use_child_logs_its_compactions(monkeypatch, tmp_path, telemetry_rows):
    import engine.runner as runner_module
    import bridge.tools.computer_use_tool as cut
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    class _Runner(runner_module.AsyncAgentRunner):
        async def run(self, session, prompt, **_k):
            await self._emit_system_event(SystemEvent(
                type="media_pruning", message="",
                details={"tokens_before": 90_000, "tokens_after": 60_000, "trigger": "pre_request"},
            ))
            raise RuntimeError("stop here")

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)
    monkeypatch.setattr(cut, "build_computer_tools", lambda _spec: [])

    async def _emit(_event):
        return None

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

    registry = SubAgentRegistry()
    spec = sat.SubAgentSpec(
        parent_workspace=str(tmp_path),
        parent_model="claude-sonnet-5-5",
        build_provider=lambda *a, **k: _Provider(),
        parent_registry=ToolRegistry(),
        registry=registry,
        emit_event=_emit,
        parent_session_id="parent-1",
    )
    record = registry.register(id="comp_c_1", label="computer", task="look", mode="background")

    result = await cut.ComputerUseTool(sub_spec=spec)._run_child(
        None, record, goal="look", target_app=None, target_display=None, max_steps=3,
    )

    assert result.is_error and "stop here" in result.content
    (row,) = _rows(tmp_path, "comp_c_1")
    assert (row["session_id"], row["subtype"], row["trigger"]) == ("comp_c_1", "media_pruning", "pre_request")
    assert (row["tokens_before"], row["tokens_after"]) == (90_000, 60_000)
    [event] = [r for r in telemetry_rows if r.get("type") == "compaction_event"]
    assert (event["session_id"], event["agent_type"], event["mechanism"]) == ("comp_c_1", "computer", "image_prune")
