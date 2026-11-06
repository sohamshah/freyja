"""A sub-agent keeps a compaction log, as a root session does.

A root session appends one row per compaction event, with the full summary, to
``<project dir>/compactions.jsonl``, and its export reads them. A sub-agent's
compactions (the runner's, and the ones it asks for with summarize_context)
went nowhere but the parent's event stream, so its export had none. A
sub-agent now appends the same rows to ``~/.freyja/sessions/<id>.compactions.jsonl``,
beside its other sidecars: not under projects/, which the briefing scans.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from engine.types import SystemEvent, ToolCall

LONG_RESULT = "".join(f"line {i} of the output\n" for i in range(400))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


async def _run_child(monkeypatch, tmp_path, record_id: str, body):
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

    async def _emit(_event):
        return None

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

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


async def test_a_sub_agent_logs_the_runners_compactions_with_the_full_summary(monkeypatch, tmp_path):
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


async def test_a_computer_use_child_logs_its_compactions(monkeypatch, tmp_path):
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
