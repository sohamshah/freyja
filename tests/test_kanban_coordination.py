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
    assert second_payload["task"]["status"] == "triage"

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


@pytest.mark.asyncio
async def test_cancelled_parent_emits_orphan_and_unblock_promotes_child() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    parent_result = await tool.execute(
        "call-1", {"action": "create", "title": "Plan refactor"}
    )
    parent_id = json.loads(parent_result.content)["task"]["id"]

    child_result = await tool.execute(
        "call-2",
        {"action": "create", "title": "Execute refactor", "parents": [parent_id]},
    )
    child_id = json.loads(child_result.content)["task"]["id"]
    assert json.loads(child_result.content)["task"]["status"] == "triage"

    # Cancelling the parent should NOT auto-promote the child to ready,
    # but should record an `orphaned` event on the child.
    cancel_result = await tool.execute(
        "call-3",
        {
            "action": "update",
            "task_id": parent_id,
            "status": "cancelled",
            "comment": "abandoning this approach",
        },
    )
    assert not cancel_result.is_error

    show_after_cancel = await tool.execute(
        "call-4", {"action": "show", "task_id": child_id}
    )
    child_payload = json.loads(show_after_cancel.content)["task"]
    assert child_payload["status"] == "triage"
    kinds = [event["kind"] for event in child_payload["events"]]
    assert "orphaned" in kinds

    # Unblock action overrides the orphan and promotes the child to ready.
    unblock_result = await tool.execute(
        "call-5", {"action": "unblock", "task_id": child_id}
    )
    assert not unblock_result.is_error
    assert json.loads(unblock_result.content)["task"]["status"] == "ready"


@pytest.mark.asyncio
async def test_unblock_refuses_when_no_failed_parent() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    parent_result = await tool.execute(
        "call-1", {"action": "create", "title": "Plan"}
    )
    parent_id = json.loads(parent_result.content)["task"]["id"]
    child_result = await tool.execute(
        "call-2",
        {"action": "create", "title": "Execute", "parents": [parent_id]},
    )
    child_id = json.loads(child_result.content)["task"]["id"]

    refusal = await tool.execute(
        "call-3", {"action": "unblock", "task_id": child_id}
    )
    assert refusal.is_error
    assert "no parent has terminated in failure" in refusal.content


@pytest.mark.asyncio
async def test_kanban_list_sorts_by_priority_within_status_bucket() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    # Create three ready cards with different priorities. p0 is highest.
    p3_result = await tool.execute(
        "c1", {"action": "create", "title": "low priority", "priority": 3}
    )
    p0_result = await tool.execute(
        "c2", {"action": "create", "title": "urgent", "priority": 0}
    )
    p2_result = await tool.execute(
        "c3", {"action": "create", "title": "default", "priority": 2}
    )

    listing = await tool.execute("c4", {"action": "list"})
    tasks = json.loads(listing.content)["tasks"]
    # All three are in `ready`, so they appear in priority order: 0, 2, 3.
    assert [t["priority"] for t in tasks] == [0, 2, 3]
    assert tasks[0]["id"] == json.loads(p0_result.content)["task"]["id"]
    assert tasks[1]["id"] == json.loads(p2_result.content)["task"]["id"]
    assert tasks[2]["id"] == json.loads(p3_result.content)["task"]["id"]


@pytest.mark.asyncio
async def test_kanban_list_groups_by_status_first() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    a = await tool.execute("c1", {"action": "create", "title": "A", "priority": 5})
    b = await tool.execute("c2", {"action": "create", "title": "B", "priority": 0})
    b_id = json.loads(b.content)["task"]["id"]

    # Move B to running. Even though B has higher priority (p0), the
    # `ready` bucket (A, p5) should sort BEFORE `running` (B, p0).
    await tool.execute(
        "c3", {"action": "update", "task_id": b_id, "status": "running"}
    )

    listing = await tool.execute("c4", {"action": "list"})
    tasks = json.loads(listing.content)["tasks"]
    statuses = [t["status"] for t in tasks]
    # STATUSES order: triage, ready, running, …  — ready comes before running,
    # so A appears before B regardless of priority.
    assert statuses == ["ready", "running"]
    assert tasks[0]["title"] == "A"


@pytest.mark.asyncio
async def test_kanban_events_and_comments_tail_caps_with_counters() -> None:
    from bridge.tools.kanban_board import DEFAULT_HISTORY_TAIL

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    create = await tool.execute("c1", {"action": "create", "title": "card"})
    card_id = json.loads(create.content)["task"]["id"]

    # Generate well past the tail cap so we can confirm trimming behaviour.
    extra = DEFAULT_HISTORY_TAIL + 5
    for i in range(extra):
        await tool.execute(
            f"e{i}", {"action": "comment", "task_id": card_id, "comment": f"note {i}"}
        )

    show = await tool.execute("c2", {"action": "show", "task_id": card_id})
    payload = json.loads(show.content)["task"]
    assert payload["commentCount"] == extra
    assert len(payload["comments"]) == DEFAULT_HISTORY_TAIL
    # Every `comment` action also appends an `updated` event; the create
    # event adds one more on top. So event_count should be extra + 1.
    assert payload["eventCount"] == extra + 1
    assert len(payload["events"]) == DEFAULT_HISTORY_TAIL
    # Tail keeps the most recent entries.
    assert payload["comments"][-1]["body"] == f"note {extra - 1}"

    history = await tool.execute(
        "c3", {"action": "show_history", "task_id": card_id}
    )
    history_payload = json.loads(history.content)["history"]
    assert history_payload["commentCount"] == extra
    assert history_payload["tailSize"] == DEFAULT_HISTORY_TAIL


@pytest.mark.asyncio
async def test_kanban_digest_groups_cards_by_actionable_bucket() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    # Build a board with one card in each interesting state:
    # running, ready, blocked, triage (waiting on parent).
    p_ready_high = await tool.execute(
        "c1", {"action": "create", "title": "urgent ready", "priority": 0}
    )
    p_ready_low = await tool.execute(
        "c2", {"action": "create", "title": "background ready", "priority": 4}
    )
    p_running = await tool.execute(
        "c3", {"action": "create", "title": "in flight", "assignee": "writer"}
    )
    p_running_id = json.loads(p_running.content)["task"]["id"]
    await tool.execute(
        "c4", {"action": "update", "task_id": p_running_id, "status": "running"}
    )
    p_blocked = await tool.execute(
        "c5", {"action": "create", "title": "needs spec", "assignee": "writer"}
    )
    p_blocked_id = json.loads(p_blocked.content)["task"]["id"]
    await tool.execute(
        "c6",
        {
            "action": "update",
            "task_id": p_blocked_id,
            "status": "blocked",
            "comment": "spec missing input",
        },
    )
    p_parent = await tool.execute(
        "c7", {"action": "create", "title": "still-running parent"}
    )
    p_parent_id = json.loads(p_parent.content)["task"]["id"]
    await tool.execute(
        "c8", {"action": "update", "task_id": p_parent_id, "status": "running"}
    )
    await tool.execute(
        "c9",
        {"action": "create", "title": "child waits", "parents": [p_parent_id]},
    )

    digest_result = await tool.execute("c10", {"action": "digest"})
    digest = json.loads(digest_result.content)["digest"]

    assert digest["totals"]["running"] == 2
    assert digest["totals"]["ready"] == 2
    assert digest["totals"]["blocked"] == 1
    assert digest["totals"]["waiting"] == 1

    # Ready bucket is ordered by priority asc (p0 floats above p4).
    ready_ids = [r["id"] for r in digest["ready"]]
    assert ready_ids[0] == json.loads(p_ready_high.content)["task"]["id"]
    assert ready_ids[-1] == json.loads(p_ready_low.content)["task"]["id"]

    # Blocked bucket exposes the latest comment for triage.
    assert digest["blocked"][0]["lastComment"] == "spec missing input"
    # Waiting bucket lists the unresolved parents.
    assert digest["waiting"][0]["unresolvedParents"] == [p_parent_id]


@pytest.mark.asyncio
async def test_kanban_show_inlines_parent_context() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    p_a = await tool.execute("c1", {"action": "create", "title": "research"})
    a_id = json.loads(p_a.content)["task"]["id"]
    # Finish parent A with a summary + artifact so the inlined context is rich.
    await tool.execute(
        "c2",
        {
            "action": "complete",
            "task_id": a_id,
            "summary": "Found three patterns we care about.",
            "artifacts": ["docs/notes.md"],
        },
    )
    p_b = await tool.execute("c3", {"action": "create", "title": "still working"})
    b_id = json.loads(p_b.content)["task"]["id"]

    child = await tool.execute(
        "c4",
        {"action": "create", "title": "synthesize", "parents": [a_id, b_id]},
    )
    child_id = json.loads(child.content)["task"]["id"]

    show = await tool.execute("c5", {"action": "show", "task_id": child_id})
    task = json.loads(show.content)["task"]

    assert "parentContext" in task
    assert len(task["parentContext"]) == 2
    by_id = {p["id"]: p for p in task["parentContext"]}
    # Completed parent's summary + artifact are inlined.
    assert by_id[a_id]["status"] == "done"
    assert by_id[a_id]["summary"] == "Found three patterns we care about."
    assert "docs/notes.md" in by_id[a_id]["artifacts"]
    # In-flight parent shows up too with its current status.
    assert by_id[b_id]["status"] == "ready"


@pytest.mark.asyncio
async def test_kanban_invalid_transition_is_rejected() -> None:
    """Terminal states (done/failed/cancelled) are absorbing — writing
    any further status to a completed card must be rejected. This is the
    backflow guard that keeps autonomy from accidentally resurrecting
    sealed work."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    create = await tool.execute("c1", {"action": "create", "title": "card"})
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute("c2", {"action": "complete", "task_id": card_id})

    bad = await tool.execute(
        "c3", {"action": "update", "task_id": card_id, "status": "running"}
    )
    assert bad.is_error
    assert "invalid transition" in bad.content

    # The card stays `done` after a rejected write — no partial mutation.
    show = await tool.execute("c4", {"action": "show", "task_id": card_id})
    assert json.loads(show.content)["task"]["status"] == "done"


@pytest.mark.asyncio
async def test_kanban_legacy_todo_normalizes_to_triage() -> None:
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    parent_result = await tool.execute(
        "c1", {"action": "create", "title": "parent"}
    )
    parent_id = json.loads(parent_result.content)["task"]["id"]
    child_result = await tool.execute(
        "c2", {"action": "create", "title": "child", "parents": [parent_id]},
    )
    child_id = json.loads(child_result.content)["task"]["id"]
    # Fresh child sits in triage because parent is still in flight.
    assert json.loads(child_result.content)["task"]["status"] == "triage"

    # A caller using the legacy `todo` spelling for `update` must be
    # accepted and round-trip as `triage` on read.
    re_update = await tool.execute(
        "c3", {"action": "update", "task_id": child_id, "status": "todo"}
    )
    assert not re_update.is_error
    show = await tool.execute("c4", {"action": "show", "task_id": child_id})
    assert json.loads(show.content)["task"]["status"] == "triage"


@pytest.mark.asyncio
async def test_kanban_new_end_states_are_reachable_from_running() -> None:
    """Running cards must be able to reach crashed/timed_out/done_unverified
    so the dispatcher can record outcomes precisely. done_unverified must
    then be able to settle to done."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    async def make_running() -> str:
        create = await tool.execute(
            f"c-{board._counter}", {"action": "create", "title": "work"},
        )
        cid = json.loads(create.content)["task"]["id"]
        await tool.execute(
            f"r-{board._counter}",
            {"action": "update", "task_id": cid, "status": "running"},
        )
        return cid

    for next_status in ("crashed", "timed_out", "done_unverified"):
        cid = await make_running()
        result = await tool.execute(
            f"t-{cid}",
            {"action": "update", "task_id": cid, "status": next_status},
        )
        assert not result.is_error, f"{next_status}: {result.content}"
        show = await tool.execute(f"s-{cid}", {"action": "show", "task_id": cid})
        assert json.loads(show.content)["task"]["status"] == next_status

    # done_unverified → done is the verifier's seal-the-deal transition.
    cid = await make_running()
    await tool.execute(
        f"u-{cid}", {"action": "update", "task_id": cid, "status": "done_unverified"},
    )
    seal = await tool.execute(
        f"d-{cid}", {"action": "update", "task_id": cid, "status": "done"},
    )
    assert not seal.is_error
    show = await tool.execute(f"f-{cid}", {"action": "show", "task_id": cid})
    assert json.loads(show.content)["task"]["status"] == "done"


@pytest.mark.asyncio
async def test_kanban_mission_root_auto_adopts_children_and_is_gating_transparent() -> None:
    """A card tagged `metadata.role == "mission_root"` becomes the board's
    mission anchor. The root starts in `running` (not `ready`) and is
    treated as always-satisfied by `_parents_done`, so subsequent cards
    with no explicit parents (a) get the root injected as their parent
    automatically, and (b) start in `ready` rather than `triage`."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    root_result = await tool.execute(
        "r1",
        {
            "action": "create",
            "title": "Build the new dashboard",
            "metadata": {"role": "mission_root"},
        },
    )
    root_payload = json.loads(root_result.content)["task"]
    assert root_payload["status"] == "running"
    root_id = root_payload["id"]

    # A subsequent create with no parents adopts root automatically.
    child_result = await tool.execute(
        "c1", {"action": "create", "title": "Research"}
    )
    child_payload = json.loads(child_result.content)["task"]
    assert child_payload["parents"] == [root_id]
    # And because root is gating-transparent, the child starts in ready.
    assert child_payload["status"] == "ready"

    # Digest exposes the missionRoot id at the top level so the renderer
    # and downstream callers can find the anchor cheaply.
    digest_result = await tool.execute("d1", {"action": "digest"})
    digest = json.loads(digest_result.content)["digest"]
    assert digest["missionRoot"] == root_id


@pytest.mark.asyncio
async def test_kanban_mission_root_does_not_attach_to_itself() -> None:
    """The mission root must not get itself as its own parent."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    root_result = await tool.execute(
        "r1",
        {"action": "create", "title": "Mission", "metadata": {"role": "mission_root"}},
    )
    root_payload = json.loads(root_result.content)["task"]
    assert root_payload["parents"] == []


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

