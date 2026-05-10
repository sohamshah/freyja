"""Persistence + replay for the kanban board (Move G).

The journal is best-effort durable storage for cross-restart resume.
These tests pin three guarantees:

1. Every board mutation writes a journal line.
2. A fresh board can replay a journal and end up in the same state
   the original mutations produced.
3. Replay is idempotent (running it twice is a no-op).
"""

from __future__ import annotations

import json

import pytest

from bridge.kanban_journal import KanbanJournal
from bridge.tools.kanban_board import KanbanTool, SessionKanbanBoard


@pytest.mark.asyncio
async def test_journal_writes_one_line_per_mutation(tmp_path) -> None:
    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    board = SessionKanbanBoard(journal=journal)
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    await tool.execute("c1", {"action": "create", "title": "first"})
    await tool.execute("c2", {"action": "create", "title": "second"})
    # `complete` is an `update` under the hood so it should add an update line.
    cards = await board.list()
    await tool.execute(
        "c3", {"action": "complete", "task_id": cards[0].id, "summary": "done"}
    )

    lines = (tmp_path / "kanban.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["create", "create", "update"]


@pytest.mark.asyncio
async def test_replay_reconstructs_board_state(tmp_path) -> None:
    """Run a sequence of operations through one board, then replay its
    journal into a fresh board and confirm both boards observe the
    same card state."""
    journal_one = KanbanJournal(tmp_path / "kanban.jsonl")
    live = SessionKanbanBoard(journal=journal_one)
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")

    root_result = await tool.execute(
        "c1",
        {
            "action": "create",
            "title": "the mission",
            "metadata": {"role": "mission_root"},
        },
    )
    root_id = json.loads(root_result.content)["task"]["id"]

    work = await tool.execute(
        "c2", {"action": "create", "title": "do the thing", "assignee": "code"},
    )
    work_id = json.loads(work.content)["task"]["id"]

    await tool.execute(
        "c3", {"action": "update", "task_id": work_id, "status": "running"},
    )
    await tool.execute(
        "c4", {"action": "complete", "task_id": work_id, "summary": "finished"},
    )

    # Now build a fresh board and replay the journal.
    journal_two = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal_two)
    restored.replay_events(journal_two.read_all())

    live_cards = sorted([c.id for c in await live.list()])
    restored_cards = sorted([c.id for c in await restored.list()])
    assert live_cards == restored_cards

    # Mission root anchor is preserved.
    assert restored._mission_root_id == root_id  # noqa: SLF001

    # Status of the completed card matches.
    restored_work = await restored.get(work_id)
    assert restored_work is not None
    assert restored_work.status == "done"
    assert restored_work.summary == "finished"


@pytest.mark.asyncio
async def test_replay_does_not_rewrite_journal(tmp_path) -> None:
    """When replaying events, the board must NOT append duplicate lines
    back to the journal — otherwise every restart would double the
    file."""
    journal_one = KanbanJournal(tmp_path / "kanban.jsonl")
    live = SessionKanbanBoard(journal=journal_one)
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")
    await tool.execute("c1", {"action": "create", "title": "anchor"})

    before_lines = (tmp_path / "kanban.jsonl").read_text().splitlines()
    assert len(before_lines) == 1

    journal_two = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal_two)
    restored.replay_events(journal_two.read_all())

    after_lines = (tmp_path / "kanban.jsonl").read_text().splitlines()
    assert after_lines == before_lines


@pytest.mark.asyncio
async def test_replay_is_idempotent(tmp_path) -> None:
    """Replaying the same events twice should not duplicate cards or
    advance the id counter beyond the original max."""
    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    live = SessionKanbanBoard(journal=journal)
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")
    await tool.execute("c1", {"action": "create", "title": "a"})
    await tool.execute("c2", {"action": "create", "title": "b"})

    events = journal.read_all()
    restored = SessionKanbanBoard()
    restored.replay_events(events)
    restored.replay_events(events)  # second pass

    cards = await restored.list()
    assert len(cards) == 2
    # The internal counter is at the highest existing card id, not double.
    assert restored._counter == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_journal_survives_malformed_line(tmp_path) -> None:
    """A corrupt line in the journal must not abort the rest of the
    replay — long missions are too expensive to throw away over one
    bad write."""
    path = tmp_path / "kanban.jsonl"
    path.write_text(
        json.dumps({"ts": 1, "kind": "create", "task": {"id": "card_001", "title": "A"}})
        + "\n"
        + "not-json-at-all\n"
        + json.dumps({"ts": 2, "kind": "create", "task": {"id": "card_002", "title": "B"}})
        + "\n"
    )
    journal = KanbanJournal(path)
    events = journal.read_all()
    # The two valid lines came through; the broken middle line was
    # skipped without raising.
    assert [e.get("task", {}).get("id") for e in events] == ["card_001", "card_002"]
