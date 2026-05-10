"""Integration-style tests for the kanban auto-dispatcher (Move A+C).

The dispatcher tick is a method on `_BridgeSession`, but the entire
session class is heavyweight to construct. To test the *behaviour* of
the tick without standing up a full bridge, we use a duck-typed stub
that exposes the exact attribute shape the tick reads, plus a stub
sub-agent tool that records spawn requests instead of actually
running anything."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from bridge.freyja_bridge import _BridgeSession
from bridge.tools.kanban_board import KanbanTool, SessionKanbanBoard


class _StubSubAgentTool:
    """Records each spawn request so the test can assert on what the
    dispatcher decided to do, without firing any real sub-agent."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> Any:
        self.calls.append({"callId": call_id, **arguments})
        return None


class _StubToolRegistry:
    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools


class _StubSubagentRegistry:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def list_all(self) -> list[Any]:
        return list(self.records)


class _StubRecord:
    def __init__(self, is_running: bool, kanban_task_id: str = "") -> None:
        self.is_running = is_running
        self.kanban_task_id = kanban_task_id


def _make_session(*, board: SessionKanbanBoard, sub_tool: _StubSubAgentTool) -> Any:
    """Build the smallest object the tick will accept."""
    session = _BridgeSession.__new__(_BridgeSession)
    session.id = "test-session"
    session.coordination_strategy = "kanban"
    session.kanban_board = board
    session.mission_root_card_id = None
    session.auto_dispatch_enabled = True
    session.tool_registry = _StubToolRegistry({"sub_agent": sub_tool})
    session.subagent_registry = _StubSubagentRegistry()
    session.queued_messages = []
    session._kanban_dispatched = set()
    session._kanban_dispatcher_task = None
    return session


@pytest.mark.asyncio
async def test_dispatcher_skips_triage_cards_with_unsatisfied_parents() -> None:
    """A triage card whose non-root parents are still in flight must
    not get a specifier spawned: there's nothing to expand yet."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    parent = await tool.execute("p1", {"action": "create", "title": "parent"})
    parent_id = json.loads(parent.content)["task"]["id"]
    child = await tool.execute(
        "c1", {"action": "create", "title": "child", "parents": [parent_id]}
    )
    child_id = json.loads(child.content)["task"]["id"]
    # Parent is still in `ready` — child sits in `triage`.
    assert json.loads(child.content)["task"]["status"] == "triage"

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    await session._kanban_tick(source="test")

    spawn_targets = [call.get("kanban_task_id") for call in sub_tool.calls]
    # Parent IS in `ready`, but it has no assignee — also skipped.
    # Child stays in triage because its parent isn't done.
    assert parent_id not in spawn_targets
    assert child_id not in spawn_targets


@pytest.mark.asyncio
async def test_dispatcher_spawns_verify_for_done_unverified_cards() -> None:
    """A card in `done_unverified` triggers a verify-profile spawn, with
    the card id passed as `kanban_task_id` so the verifier's tool
    surface scopes to it."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "work"})
    card_id = json.loads(create.content)["task"]["id"]
    # Drive the card through running → done_unverified.
    await tool.execute(
        "u1", {"action": "update", "task_id": card_id, "status": "running"}
    )
    await tool.execute(
        "u2", {"action": "update", "task_id": card_id, "status": "done_unverified"}
    )

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    await session._kanban_tick(source="test")

    assert len(sub_tool.calls) == 1
    call = sub_tool.calls[0]
    assert call["kanban_task_id"] == card_id
    assert call["agent_type"] == "verify"
    assert call["mode"] == "background"


@pytest.mark.asyncio
async def test_dispatcher_spawns_assignee_for_ready_card() -> None:
    """A `ready` card with an `assignee` becomes a spawn for that
    agent type. Cards without an assignee are skipped (the parent
    is still planning)."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    assigned = await tool.execute(
        "c1", {"action": "create", "title": "assigned", "assignee": "code"}
    )
    assigned_id = json.loads(assigned.content)["task"]["id"]
    await tool.execute("c2", {"action": "create", "title": "unassigned"})

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    await session._kanban_tick(source="test")

    targets = {call["kanban_task_id"]: call["agent_type"] for call in sub_tool.calls}
    assert assigned_id in targets
    assert targets[assigned_id] == "code"
    # The unassigned ready card was not spawned for.
    assert len(targets) == 1


@pytest.mark.asyncio
async def test_dispatcher_honors_max_parallel_capacity() -> None:
    """When `KANBAN_MAX_PARALLEL` is saturated by in-flight workers,
    the tick spawns nothing new."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    for i in range(3):
        await tool.execute(
            f"c{i}",
            {"action": "create", "title": f"work {i}", "assignee": "code"},
        )

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    # Pretend 3 sub-agents are already running against other cards.
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL)
    ]
    await session._kanban_tick(source="test")

    assert sub_tool.calls == []


@pytest.mark.asyncio
async def test_dispatcher_skips_mission_root_card() -> None:
    """The mission root is a container, not work — it must never get
    a worker spawned for it even though it's `running` and has no
    completed parents."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    root = await tool.execute(
        "r1",
        {
            "action": "create",
            "title": "mission",
            "metadata": {"role": "mission_root"},
        },
    )
    root_id = json.loads(root.content)["task"]["id"]
    # Also create a regular ready card so we can confirm the dispatcher
    # still spawns for non-root work in the same tick.
    other = await tool.execute(
        "c1", {"action": "create", "title": "other", "assignee": "code"}
    )
    other_id = json.loads(other.content)["task"]["id"]

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.mission_root_card_id = root_id
    await session._kanban_tick(source="test")

    targets = [call["kanban_task_id"] for call in sub_tool.calls]
    assert root_id not in targets
    assert other_id in targets


@pytest.mark.asyncio
async def test_dispatcher_no_op_when_disabled() -> None:
    """Disabled auto-dispatch must short-circuit without examining the
    board (this is the user-visible guarantee of the toggle)."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    await tool.execute(
        "c1", {"action": "create", "title": "work", "assignee": "code"}
    )

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False
    await session._kanban_tick(source="test")
    assert sub_tool.calls == []


@pytest.mark.asyncio
async def test_dispatcher_preempted_by_queued_user_messages() -> None:
    """Queued user input takes precedence over autopilot ticks so the
    parent can respond to the human instead of burning a turn."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    await tool.execute(
        "c1", {"action": "create", "title": "work", "assignee": "code"}
    )

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.queued_messages = [("a queued user note", None)]
    await session._kanban_tick(source="test")
    assert sub_tool.calls == []
