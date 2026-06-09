"""Tests for the SessionMemoryTool `on_mutation` callback hook.

This callback is the primitive that lets the bridge fire a mid-session
working-memory extraction (Call B) every time the agent writes to the
session_memory scratchpad — which is the agent's natural "consolidate
now" signal. The bridge owns the debounce + async scheduling; the tool's
contract here is narrow:

  • fire ONLY on successful mutations (write, append, clear)
  • NEVER on read
  • NEVER on errors / unknown actions
  • a raising callback must not break the tool

These tests pin all four corners.
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
    return tmp_path


async def test_mutation_fires_callback(_tmp_project):
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda action: calls.append(action)
    )
    res = await tool.execute("c1", {"action": "write", "content": "x"})
    assert not res.is_error
    assert calls == ["write"]


async def test_append_fires_callback(_tmp_project):
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda a: calls.append(a)
    )
    res = await tool.execute("c1", {"action": "append", "content": "note"})
    assert not res.is_error
    assert calls == ["append"]


async def test_clear_fires_callback(_tmp_project):
    # Seed the file so clear actually has something to remove (callback
    # must still fire even when the file didn't exist, but the realistic
    # path is the one the agent actually walks).
    (_tmp_project / "memory.md").write_text("# seeded\n", encoding="utf-8")
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda a: calls.append(a)
    )
    res = await tool.execute("c1", {"action": "clear"})
    assert not res.is_error
    assert calls == ["clear"]


async def test_read_does_not_fire_callback(_tmp_project):
    # Read is observational, never a "consolidate now" signal.
    (_tmp_project / "memory.md").write_text("# seeded\n", encoding="utf-8")
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda a: calls.append(a)
    )
    res = await tool.execute("c1", {"action": "read"})
    assert not res.is_error
    assert calls == []


async def test_failed_mutation_does_not_fire(_tmp_project):
    """If the underlying write raises, the tool returns is_error=True and
    the callback MUST NOT fire — we never want to consolidate off a
    non-event."""
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda a: calls.append(a)
    )
    with patch.object(
        type(tool._path),
        "write_text",
        side_effect=OSError("disk full"),
    ):
        res = await tool.execute("c1", {"action": "write", "content": "x"})
    assert res.is_error
    assert calls == []


async def test_unknown_action_does_not_fire(_tmp_project):
    calls: list[str] = []
    tool = SessionMemoryTool(
        session_id="sess-1", on_mutation=lambda a: calls.append(a)
    )
    res = await tool.execute("c1", {"action": "delete"})
    assert res.is_error
    assert calls == []


async def test_callback_exception_does_not_break_tool(_tmp_project):
    """A buggy hook on the bridge side must never propagate into the
    tool's return value — the tool's success/failure is decided by the
    mutation alone."""

    def boom(_action: str) -> None:
        raise RuntimeError("hook crashed")

    tool = SessionMemoryTool(session_id="sess-1", on_mutation=boom)
    res = await tool.execute("c1", {"action": "write", "content": "x"})
    assert not res.is_error  # mutation still succeeded
    # And the on-disk effect is real.
    assert (_tmp_project / "memory.md").read_text(encoding="utf-8") == "x"


async def test_no_callback_is_legal_for_back_compat(_tmp_project):
    """SessionMemoryTool must remain constructible without `on_mutation`
    — any existing call site that didn't pass the kwarg keeps working."""
    tool = SessionMemoryTool(session_id="sess-1")
    res = await tool.execute("c1", {"action": "write", "content": "x"})
    assert not res.is_error
