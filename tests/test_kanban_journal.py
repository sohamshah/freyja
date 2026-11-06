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
    # `claim` and `complete` are `update`s under the hood, so each adds an
    # update line. (The orchestrator must claim before it can complete.)
    cards = await board.list()
    await tool.execute("c3", {"action": "claim", "task_id": cards[0].id})
    await tool.execute(
        "c4", {"action": "complete", "task_id": cards[0].id, "summary": "done"}
    )

    lines = (tmp_path / "kanban.jsonl").read_text().strip().splitlines()
    assert len(lines) == 4
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["create", "create", "update", "update"]


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

    await tool.execute("c3", {"action": "claim", "task_id": work_id})
    await tool.execute(
        "c4", {"action": "complete", "task_id": work_id, "summary": "finished"},
    )
    # The judge's pass verdict seals it (Move R: complete lands in review).
    await live.update(work_id, actor="kanban-judge", status="done")

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


@pytest.mark.asyncio
async def test_replay_preserves_sticky_worker_and_judge_bindings(tmp_path) -> None:
    """Move R sticky bindings — worker/judge session ids, review iteration,
    and worker terminal state — must survive a journal replay. Without
    them a bridge restart rebuilds the card with empty ids + a reset
    iteration, which trips `_handle_kanban_verdict` into an unbounded
    requeue→respawn→reject loop."""
    journal_one = KanbanJournal(tmp_path / "kanban.jsonl")
    live = SessionKanbanBoard(journal=journal_one)
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "work"})
    card_id = json.loads(create.content)["task"]["id"]
    # Stamp the sticky bindings the way the spawn hook + judge path do.
    await live.update(
        card_id,
        actor="worker",
        status="running",
        worker_session_id="worker-sess-1",
    )
    await live.update(
        card_id,
        actor="kanban-judge",
        status="review",
        judge_session_id="judge-sess-1",
        review_iteration=2,
        worker_terminal_state="done",
    )

    journal_two = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal_two)
    restored.replay_events(journal_two.read_all())

    card = await restored.get(card_id)
    assert card is not None
    assert card.worker_session_id == "worker-sess-1"
    assert card.judge_session_id == "judge-sess-1"
    assert card.review_iteration == 2
    assert card.worker_terminal_state == "done"


@pytest.mark.asyncio
async def test_replayed_cards_carry_the_judges_verdict(tmp_path, monkeypatch) -> None:
    """The renderer reads the judge's verdict from a card's events. The
    board the bridge sends after a restart must carry them, or a card
    the judge passed shows as plain done until a fresh event arrives."""
    from bridge import freyja_bridge
    from bridge.freyja_bridge import _BridgeSession

    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "work"})
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute("c2", {"action": "update", "task_id": card_id, "status": "running"})
    worker = KanbanTool(live, actor_id="sub_w", actor_label="worker", owned_task_id=card_id)
    await worker.execute("c3", {"action": "complete", "summary": "built"})
    await live.update(card_id, actor="kanban-judge", status="done", summary="checked")

    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    session = _BridgeSession.__new__(_BridgeSession)
    session.id = "test-session"
    session.kanban_board = SessionKanbanBoard(journal=journal)
    events = journal.read_all()
    session.kanban_board.replay_events(events)
    sent: list[dict] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    session._emit_kanban_replay(len(events), 0)

    [replay] = [event for event in sent if event["subtype"] == "kanban_replay"]
    [card] = replay["details"]["tasks"]
    assert card["status"] == "done"
    moves = [(e["actor"], e["details"].get("status")) for e in card["events"]]
    assert moves[-1] == ("kanban-judge", "done")


@pytest.mark.asyncio
async def test_replay_keeps_the_children_a_write_moved(tmp_path) -> None:
    """A parent's write can move its children: done promotes them out of
    triage, a failure marks them orphaned. Replay must restore both, or
    after a restart the promoted child is back in triage and the
    dispatcher sends it a specifier instead of a worker."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")

    async def create(title: str, parents: list[str] | None = None) -> str:
        result = await tool.execute(
            title, {"action": "create", "title": title, "parents": parents or []}
        )
        return json.loads(result.content)["task"]["id"]

    passed = await create("passed")
    dropped = await create("dropped")
    promoted = await create("promoted", [passed])
    orphaned = await create("orphaned", [dropped])
    await tool.execute("claim", {"action": "claim", "task_id": passed})
    await tool.execute("complete", {"action": "complete", "task_id": passed})
    await live.update(passed, actor="kanban-judge", status="done")
    await tool.execute(
        "cancel", {"action": "update", "task_id": dropped, "status": "cancelled"}
    )

    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal)
    restored.replay_events(journal.read_all())

    for card_id in (promoted, orphaned):
        live_card = await live.get(card_id)
        card = await restored.get(card_id)
        assert card is not None and live_card is not None
        assert card.status == live_card.status
        assert [e.kind for e in card.events] == [e.kind for e in live_card.events]
    assert (await restored.get(promoted)).status == "ready"
    assert (await restored.get(orphaned)).events[-1].kind == "orphaned"


@pytest.mark.asyncio
async def test_replay_moves_a_linked_child_to_the_lane_link_gave_it(tmp_path) -> None:
    """Linking sets the child's status (triage until its new parent is
    done, else ready) and journals only the ids. Replay must apply the
    same status, or after a restart the child sits in its old lane until
    its next update."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")

    async def create(title: str) -> str:
        result = await tool.execute(title, {"action": "create", "title": title})
        return json.loads(result.content)["task"]["id"]

    parent = await create("parent")
    child = await create("child")
    before = (await live.get(child)).status
    await tool.execute(
        "link", {"action": "link", "parent_id": parent, "child_id": child}
    )
    assert (await live.get(child)).status == "triage" != before

    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal)
    restored.replay_events(journal.read_all())

    card = await restored.get(child)
    assert card is not None
    assert card.parents == [parent]
    assert card.status == "triage"


@pytest.mark.asyncio
async def test_replaying_a_link_survives_a_parent_whose_create_row_was_lost(
    tmp_path,
) -> None:
    """The journal skips a malformed line, so a card can name a parent
    replay never created. Its link row must not stop the replay."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")

    async def create(title: str, parents: list[str] | None = None) -> str:
        result = await tool.execute(
            title, {"action": "create", "title": title, "parents": parents or []}
        )
        return json.loads(result.content)["task"]["id"]

    lost = await create("lost")
    child = await create("child", [lost])
    other = await create("other")
    await tool.execute(
        "link", {"action": "link", "parent_id": other, "child_id": child}
    )

    events = [
        e for e in KanbanJournal(tmp_path / "kanban.jsonl").read_all()
        if (e.get("task") or {}).get("id") != lost
    ]
    restored = SessionKanbanBoard()
    restored.replay_events(events)

    card = await restored.get(child)
    assert card is not None
    assert card.parents == [lost, other]


async def _restore_without(tmp_path, *lost: str) -> SessionKanbanBoard:
    """Cut the rows of the cards in `lost` short, so the journal skips
    them as malformed, and restore a board from it the way the bridge
    does."""
    path = tmp_path / "kanban.jsonl"
    lines = path.read_text().splitlines()
    path.write_text(
        "\n".join(
            line[:40] if json.loads(line).get("task", {}).get("id") in lost else line
            for line in lines
        )
        + "\n"
    )
    journal = KanbanJournal(path)
    restored = SessionKanbanBoard(journal=journal)
    restored.replay_events(journal.read_all())
    return restored


@pytest.mark.asyncio
async def test_a_live_update_survives_a_child_whose_parent_create_row_was_lost(
    tmp_path,
) -> None:
    """After a restart a card can name a parent the board never saw. A
    parent's `done` then promotes its children; the one naming the lost
    parent stays where it is, as replay leaves it, and its siblings still
    move."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    lost = (await live.create(title="lost")).id
    first = (await live.create(title="first")).id
    child = (await live.create(title="child", parents=[lost, first])).id
    sibling = (await live.create(title="sibling", parents=[first])).id
    # The claim's snapshot is the journal row that holds first's children.
    await live.update(first, actor="worker", status="running")
    restored = await _restore_without(tmp_path, lost)

    task = await restored.update(first, actor="kanban-judge", status="done")

    assert task is not None and task.status == "done"
    assert (await restored.get(child)).status == "triage"
    assert (await restored.get(sibling)).status == "ready"


@pytest.mark.asyncio
async def test_a_live_link_survives_a_child_whose_parent_create_row_was_lost(
    tmp_path,
) -> None:
    """Linking a card that names a parent the board never saw sends it
    to triage while its new parent is pending, as replaying the link row
    does, so a restart keeps it."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    lost = (await live.create(title="lost")).id
    await live.update(lost, actor="kanban-judge", status="done")
    child = (await live.create(title="child", parents=[lost])).id
    other = (await live.create(title="other")).id
    restored = await _restore_without(tmp_path, lost)
    tool = KanbanTool(restored, actor_id="parent", actor_label="parent")
    assert (await restored.get(child)).status == "ready"

    result = await tool.execute(
        "link", {"action": "link", "parent_id": other, "child_id": child}
    )

    assert not result.is_error, result.content
    card = await restored.get(child)
    assert card.parents == [lost, other]
    assert card.status == "triage"
    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    again = SessionKanbanBoard(journal=journal)
    again.replay_events(journal.read_all())
    assert (await again.get(child)).status == card.status


@pytest.mark.asyncio
async def test_replay_gives_a_parent_the_children_made_after_it(tmp_path) -> None:
    """create() adds the new card to each parent's children but journals
    only the new card's row. Replay must add it too, or after a restart
    the parent has no children until its next update, and its done
    leaves them in triage."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    parent = (await live.create(title="parent")).id
    child = (await live.create(title="child", parents=[parent])).id

    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal)
    restored.replay_events(journal.read_all())

    assert (await restored.get(parent)).children == [child]
    await restored.update(parent, actor="worker", status="running")
    await restored.update(parent, actor="kanban-judge", status="done")
    assert (await restored.get(child)).status == "ready"


@pytest.mark.asyncio
async def test_unblock_releases_a_child_whose_parent_create_row_was_lost(
    tmp_path,
) -> None:
    """A parent the board does not hold never finishes, like a failed
    one: its child is not promoted, and unblock releases it once its
    other parents are done. The digest names the missing parent so the
    agent knows."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    lost = (await live.create(title="lost")).id
    first = (await live.create(title="first")).id
    child = (await live.create(title="child", parents=[lost, first])).id
    restored = await _restore_without(tmp_path, lost)
    await restored.update(first, actor="worker", status="running")
    await restored.update(first, actor="kanban-judge", status="done")
    assert (await restored.get(child)).status == "triage"

    [waiting] = (await restored.digest())["waiting"]
    assert waiting["unresolvedParents"] == [lost]
    assert waiting["missingParents"] == [lost]
    tool = KanbanTool(restored, actor_id="parent", actor_label="parent")
    result = await tool.execute("unblock", {"action": "unblock", "task_id": child})

    assert not result.is_error, result.content
    assert (await restored.get(child)).status == "ready"


@pytest.mark.asyncio
async def test_link_moves_only_a_card_that_has_not_started(tmp_path) -> None:
    """link() set its child to triage or ready whatever lane it was in,
    so linking a running card sent it back to triage, a move the
    transition table forbids. Only a triage or ready card moves; a card
    past that keeps its lane and records the new parent, and replay
    does the same."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    tool = KanbanTool(live, actor_id="parent", actor_label="parent")
    parent = (await live.create(title="parent")).id
    lanes = {
        "ready": [],
        "running": ["running"],
        "review": ["running", "review"],
        "blocked": ["blocked"],
        "cancelled": ["cancelled"],
    }
    children = {}
    for lane, moves in lanes.items():
        children[lane] = (await live.create(title=lane)).id
        for status in moves:
            await live.update(children[lane], actor="worker", status=status)

    for child in children.values():
        result = await tool.execute(
            "link", {"action": "link", "parent_id": parent, "child_id": child}
        )
        assert not result.is_error, result.content

    expected = {**{lane: lane for lane in lanes}, "ready": "triage"}
    journal = KanbanJournal(tmp_path / "kanban.jsonl")
    restored = SessionKanbanBoard(journal=journal)
    restored.replay_events(journal.read_all())
    for board in (live, restored):
        for lane, child in children.items():
            card = await board.get(child)
            assert (card.status, card.parents) == (expected[lane], [parent]), lane
        assert (await board.get(parent)).children == list(children.values())


@pytest.mark.asyncio
async def test_link_refuses_a_done_card_a_parent_that_is_not_done() -> None:
    """A done card cannot wait on a new parent: done is final, and its
    children were already released. link() reopened it instead. It now
    refuses, and records a parent that is already done."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    pending = (await board.create(title="pending")).id
    finished = (await board.create(title="finished")).id
    child = (await board.create(title="child")).id
    for card in (finished, child):
        await board.update(card, actor="worker", status="running")
        await board.update(card, actor="kanban-judge", status="done")

    refused = await tool.execute(
        "link", {"action": "link", "parent_id": pending, "child_id": child}
    )
    linked = await tool.execute(
        "link", {"action": "link", "parent_id": finished, "child_id": child}
    )

    assert refused.is_error and "is done" in refused.content, refused.content
    assert not linked.is_error, linked.content
    card = await board.get(child)
    assert (card.status, card.parents) == ("done", [finished])
    assert (await board.get(pending)).children == []


@pytest.mark.asyncio
async def test_unblock_counts_the_mission_root_as_done() -> None:
    """The mission root stays running as the mission's container and
    gates nothing. unblock refused a child of the root and a failed
    parent because the root was still pending, and the digest listed it
    as unresolved."""
    board = SessionKanbanBoard()
    root = (await board.create(title="mission", metadata={"role": "mission_root"})).id
    failed = (await board.create(title="failed")).id
    child = (await board.create(title="child", parents=[root, failed])).id
    await board.update(failed, actor="parent", status="cancelled")

    [waiting] = (await board.digest())["waiting"]
    assert waiting["unresolvedParents"] == [failed]
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    result = await tool.execute("unblock", {"action": "unblock", "task_id": child})

    assert not result.is_error, result.content
    assert (await board.get(child)).status == "ready"


@pytest.mark.asyncio
async def test_create_refuses_parents_the_board_does_not_hold() -> None:
    """create() dropped parent ids it did not know, so a typo made a card
    that does not wait on the parent the agent meant (with no other
    parent, a ready card under the mission root). It now refuses and
    names the unknown ids."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    await board.create(title="mission", metadata={"role": "mission_root"})
    parent = (await board.create(title="parent")).id

    result = await tool.execute(
        "create",
        {"action": "create", "title": "child", "parents": [parent, "card_02", "card_9"]},
    )

    assert result.is_error, result.content
    assert "card_02, card_9" in result.content
    assert {card.title for card in await board.list()} == {"mission", "parent"}


@pytest.mark.asyncio
async def test_replay_never_hands_out_the_id_of_a_card_whose_rows_were_lost(
    tmp_path,
) -> None:
    """Replay restored the id counter from create rows alone. When the
    card whose rows were lost had the highest id, the next card got that
    id, and a child naming the lost card then waited on an unrelated
    one. The counter now passes every id the journal names."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    parent = (await live.create(title="parent")).id
    child = (await live.create(title="child")).id
    # Named by a link row and the child's next snapshot.
    linked = (await live.create(title="linked")).id
    await live.link(linked, child, actor="parent")
    await live.update(child, actor="parent", comment="waits on linked")
    # Named only in its parent's update snapshot.
    made = (await live.create(title="made", parents=[parent])).id
    await live.update(parent, actor="worker", status="running")
    restored = await _restore_without(tmp_path, linked, made)

    fresh = (await restored.create(title="fresh")).id

    assert fresh not in {parent, child, linked, made}
    assert (await restored.get(child)).parents == [linked]
    assert (await restored.get(fresh)).children == []


@pytest.mark.asyncio
async def test_replay_rebuilds_a_card_whose_create_row_was_lost_from_its_update_rows(
    tmp_path,
) -> None:
    """Each update row holds the whole card, but replay skipped update
    rows for a card it did not hold. A card whose create row was lost was
    dropped though its snapshots were there, and its children waited on
    a parent that never finishes."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    lost = (await live.create(title="lost")).id
    await live.update(lost, actor="worker", status="running")
    child = (await live.create(title="child", parents=[lost])).id
    await live.update(lost, actor="worker", comment="halfway")

    events = [
        e for e in KanbanJournal(tmp_path / "kanban.jsonl").read_all()
        if not (e["kind"] == "create" and e["task"]["id"] == lost)
    ]
    restored = SessionKanbanBoard()
    restored.replay_events(events)

    card = await restored.get(lost)
    assert card is not None
    assert (card.title, card.status, card.children) == ("lost", "running", [child])
    assert [c.body for c in card.comments] == ["halfway"]
    await restored.update(lost, actor="kanban-judge", status="done")
    assert (await restored.get(child)).status == "ready"


@pytest.mark.asyncio
async def test_replay_stamps_a_parent_when_a_child_is_made_under_it(tmp_path) -> None:
    """create() stamps each parent's updated_at when it adds the new
    card to its children, and the stale sweep times a running card from
    that stamp. Replay added the child but kept the parent's older stamp,
    so after a restart a running parent looked quiet for longer than it
    was."""
    live = SessionKanbanBoard(journal=KanbanJournal(tmp_path / "kanban.jsonl"))
    parent = (await live.create(title="parent")).id
    await live.update(parent, actor="worker", status="running")
    await live.create(title="child", parents=[parent])
    events = KanbanJournal(tmp_path / "kanban.jsonl").read_all()
    # The child came a minute after the parent's last row.
    events[-1]["task"]["createdAt"] = (events[-2]["ts"] // 1000 + 60) * 1000

    restored = SessionKanbanBoard()
    restored.replay_events(events)

    stamp = (await restored.get(parent)).to_dict()["updatedAt"]
    assert stamp == events[-1]["task"]["createdAt"]
