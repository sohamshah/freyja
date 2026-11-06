"""A sub-agent keeps a raw message log, as a root session does.

A root session appends every message to ``<project dir>/raw_messages.jsonl``,
and exports read each tool result whole from it. A sub-agent's session had no
such log, so its export fell back to the app's record, where the bridge cuts
each result's preview to 2,000 characters. A sub-agent now appends to
``~/.freyja/sessions/<id>.raw_messages.jsonl``, beside its other sidecars.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from engine.types import ToolCall

LONG_RESULT = "".join(f"line {i} of the output\n" for i in range(2_000))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)


async def _run_child(monkeypatch, tmp_path, record_id: str, *, snapshot=None):
    """Run a sub-agent whose runner reads one long tool result, then stops."""
    import engine.runner as runner_module
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    class _Runner(runner_module.AsyncAgentRunner):
        async def run(self, session, prompt, **_k):
            session.add_user_message(prompt)
            session.add_assistant_message("", tool_calls=[ToolCall(id="c1", name="bash", arguments={})])
            session.add_tool_result("c1", LONG_RESULT)
            raise RuntimeError("stop here")

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)

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
    record = registry.register(id=record_id, label="reader", task="read it", mode="background")
    if snapshot is not None:
        record.restored_transcript = snapshot
        record.resume_mode = True
    with pytest.raises(RuntimeError, match="stop here"):
        await sat.SubAgentTool(spec)._run_child(record)


def _rows(tmp_path, record_id: str) -> list[dict]:
    log = tmp_path / "sessions" / f"{record_id}.raw_messages.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


async def test_a_sub_agent_logs_each_message_with_its_tool_result_whole(monkeypatch, tmp_path):
    await _run_child(monkeypatch, tmp_path, "sub_t_1")

    rows = _rows(tmp_path, "sub_t_1")
    assert [r["message"]["role"] for r in rows] == ["user", "assistant", "tool_result"]
    assert {r["session_id"] for r in rows} == {"sub_t_1"}
    assert rows[2]["message"]["tool_call_id"] == "c1"
    assert rows[2]["message"]["content"] == LONG_RESULT
    # Nothing in the parent's project directory: that log is the parent's.
    assert not (tmp_path / ".freyja" / "projects" / "parent-1" / "raw_messages.jsonl").exists()


async def test_a_re_woken_sub_agent_keeps_appending_to_its_log(monkeypatch, tmp_path):
    from engine.session import Session

    earlier = Session.create(system_prompt="t")
    earlier.add_user_message("the first task")
    await _run_child(monkeypatch, tmp_path, "sub_t_2", snapshot=earlier.serialize_transcript())

    rows = _rows(tmp_path, "sub_t_2")
    # The restored message was logged in its own run; only the new ones are added.
    assert [r["message"]["role"] for r in rows] == ["user", "assistant", "tool_result"]
    assert rows[0]["message"]["content"].startswith("[RESUME]")
