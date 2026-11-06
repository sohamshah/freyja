"""The SessionMemoryTool `on_mutation` callback.

This callback is the primitive that lets the bridge fire a mid-session
working-memory extraction (Call B) every time the agent writes to the
session_memory scratchpad, the agent's natural "consolidate now" signal. The
bridge owns the debounce and scheduling; the tool's contract is narrow: fire
only on successful mutations (write, append, clear), never on read, never on
errors or unknown actions, and a raising callback must not break the tool.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bridge.tools.session_memory_tool import SessionMemoryTool


@pytest.fixture()
def _tmp_project(monkeypatch, tmp_path):
    """Route project_output_dir into a tmp_path so writes don't escape."""
    monkeypatch.setattr(
        "bridge.tools.session_memory_tool.project_output_dir",
        lambda sid: tmp_path,
    )
    (tmp_path / "memory.md").write_text("# seeded\n", encoding="utf-8")
    return tmp_path


def _tool(calls: list[str]) -> SessionMemoryTool:
    return SessionMemoryTool(session_id="sess-1", on_mutation=calls.append)


@pytest.mark.parametrize("action", ["write", "append", "clear"])
async def test_a_successful_mutation_fires_the_callback(_tmp_project, action):
    calls: list[str] = []
    args = {"action": action} if action == "clear" else {"action": action, "content": "x"}
    res = await _tool(calls).execute("c1", args)
    assert not res.is_error
    assert calls == [action]


async def test_reads_bad_actions_and_failed_writes_do_not_fire(_tmp_project):
    calls: list[str] = []
    tool = _tool(calls)
    # Read is observational, never a "consolidate now" signal.
    assert not (await tool.execute("c1", {"action": "read"})).is_error
    assert (await tool.execute("c2", {"action": "delete"})).is_error
    # A write that raises returns is_error; we never consolidate off a non-event.
    with patch.object(type(tool._path), "write_text", side_effect=OSError("disk full")):
        res = await tool.execute("c3", {"action": "write", "content": "x"})
    assert res.is_error
    assert calls == []


async def test_a_raising_callback_does_not_break_the_tool(_tmp_project):
    def boom(_action: str) -> None:
        raise RuntimeError("hook crashed")

    tool = SessionMemoryTool(session_id="sess-1", on_mutation=boom)
    res = await tool.execute("c1", {"action": "write", "content": "x"})
    assert not res.is_error  # the mutation alone decides success
    assert (_tmp_project / "memory.md").read_text(encoding="utf-8") == "x"
