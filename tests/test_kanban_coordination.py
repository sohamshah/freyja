import json

import pytest

from bridge.tools.coordination import (
    STRATEGY_BUS,
    STRATEGY_ISOLATED,
    STRATEGY_KANBAN,
    coordination_prompt,
    normalize_coordination_strategy,
    strategy_uses_kanban,
    strategy_uses_message_bus,
)
from bridge.tools.kanban_board import KanbanTool, SessionKanbanBoard
from bridge.tools.registry import build_desktop_registry


def test_coordination_strategy_normalization_and_capabilities() -> None:
    assert normalize_coordination_strategy(None) == STRATEGY_BUS
    assert normalize_coordination_strategy("message_bus") == STRATEGY_BUS
    assert normalize_coordination_strategy("solo") == STRATEGY_ISOLATED
    assert normalize_coordination_strategy("board") == STRATEGY_KANBAN
    assert strategy_uses_message_bus(STRATEGY_BUS)
    assert not strategy_uses_message_bus(STRATEGY_ISOLATED)
    assert strategy_uses_kanban(STRATEGY_KANBAN)
    assert "KANBAN BOARD" in coordination_prompt(STRATEGY_KANBAN)


@pytest.mark.asyncio
async def test_kanban_tool_create_link_complete_and_promote() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    first = await tool.execute(
        "call-1",
        {"action": "create", "title": "Research source material"},
    )
    assert not first.is_error
    first_payload = json.loads(first.content)
    first_id = first_payload["task"]["id"]
    assert first_payload["task"]["status"] == "ready"

    second = await tool.execute(
        "call-2",
        {
            "action": "create",
            "title": "Synthesize findings",
            "parents": [first_id],
            "assignee": "writer",
        },
    )
    second_payload = json.loads(second.content)
    second_id = second_payload["task"]["id"]
    assert second_payload["task"]["status"] == "todo"

    complete = await tool.execute(
        "call-3",
        {
            "action": "complete",
            "task_id": first_id,
            "summary": "Found the relevant coordination patterns.",
            "created_cards": [second_id],
        },
    )
    assert not complete.is_error

    show = await tool.execute("call-4", {"action": "show", "task_id": second_id})
    show_payload = json.loads(show.content)
    assert show_payload["task"]["status"] == "ready"


def test_registry_only_exposes_kanban_tool_in_kanban_mode(tmp_path) -> None:
    plain_registry = build_desktop_registry(
        workspace=tmp_path,
        include_bash=False,
        include_web=False,
        include_subagents=False,
        include_computer=False,
    )
    assert plain_registry.get_catalog_entry("kanban") is None

    board_registry = build_desktop_registry(
        workspace=tmp_path,
        include_bash=False,
        include_web=False,
        include_subagents=False,
        include_computer=False,
        coordination_strategy=STRATEGY_KANBAN,
        kanban_board=SessionKanbanBoard(),
    )
    assert board_registry.get_catalog_entry("kanban") is not None

