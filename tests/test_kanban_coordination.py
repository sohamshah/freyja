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


@pytest.mark.asyncio
async def test_kanban_circuit_breaker_trips_after_threshold() -> None:
    """Repeated crashed/timed_out transitions accumulate `consecutiveFailures`
    on the card. Past the threshold (3) the next failure transition is
    rewritten to `failed` so the dispatcher locks the card out instead of
    respawning a flapping worker forever."""
    from bridge.tools.kanban_board import FAILURE_THRESHOLD

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    create = await tool.execute("c1", {"action": "create", "title": "fragile"})
    card_id = json.loads(create.content)["task"]["id"]

    # Crash, recover, crash, recover … until the breaker is about to trip.
    for i in range(FAILURE_THRESHOLD - 1):
        await tool.execute(
            f"r{i}", {"action": "update", "task_id": card_id, "status": "running"},
        )
        await tool.execute(
            f"x{i}", {"action": "update", "task_id": card_id, "status": "crashed"},
        )

    show = await tool.execute("s1", {"action": "show", "task_id": card_id})
    pre_payload = json.loads(show.content)["task"]
    assert pre_payload["consecutiveFailures"] == FAILURE_THRESHOLD - 1
    assert pre_payload["status"] == "crashed"

    # One more crash from running must rewrite to `failed`.
    await tool.execute(
        "rN", {"action": "update", "task_id": card_id, "status": "ready"},
    )
    await tool.execute(
        "rN2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    trip = await tool.execute(
        "trip", {"action": "update", "task_id": card_id, "status": "crashed"},
    )
    assert not trip.is_error
    final = json.loads(trip.content)["task"]
    assert final["status"] == "failed"
    assert final["consecutiveFailures"] == FAILURE_THRESHOLD


@pytest.mark.asyncio
async def test_kanban_circuit_breaker_resets_on_done() -> None:
    """A successful completion resets the counter so a transient blip
    doesn't leave the card with a permanent failure budget."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    create = await tool.execute("c1", {"action": "create", "title": "flaky"})
    card_id = json.loads(create.content)["task"]["id"]

    await tool.execute("r1", {"action": "update", "task_id": card_id, "status": "running"})
    await tool.execute("x1", {"action": "update", "task_id": card_id, "status": "crashed"})
    await tool.execute("r2", {"action": "update", "task_id": card_id, "status": "ready"})
    await tool.execute("r3", {"action": "update", "task_id": card_id, "status": "running"})

    mid = await tool.execute("s1", {"action": "show", "task_id": card_id})
    assert json.loads(mid.content)["task"]["consecutiveFailures"] == 1

    # Successful completion zeroes the counter.
    await tool.execute("done", {"action": "complete", "task_id": card_id})
    show = await tool.execute("s2", {"action": "show", "task_id": card_id})
    assert json.loads(show.content)["task"]["consecutiveFailures"] == 0


@pytest.mark.asyncio
async def test_kanban_worker_mode_blocks_parent_only_actions() -> None:
    """A KanbanTool constructed with `owned_task_id` runs in worker mode:
    the schema enum is narrowed to the worker surface, and `create`,
    `claim`, `link`, `unblock` are refused outright."""
    from bridge.tools.kanban_board import WORKER_ALLOWED_ACTIONS

    board = SessionKanbanBoard()
    parent_tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    parent_created = await parent_tool.execute(
        "p1", {"action": "create", "title": "do the thing"}
    )
    card_id = json.loads(parent_created.content)["task"]["id"]

    worker = KanbanTool(
        board, actor_id="worker", actor_label="worker", owned_task_id=card_id
    )
    # The advertised enum drops parent-only actions.
    enum = worker.definition.parameters["properties"]["action"]["enum"]
    assert set(enum) == set(WORKER_ALLOWED_ACTIONS)
    assert "create" not in enum
    assert "claim" not in enum
    assert "link" not in enum
    assert "unblock" not in enum

    # Even if the worker forges a create request, the gate rejects it.
    bad_create = await worker.execute(
        "w1", {"action": "create", "title": "smuggled card"}
    )
    assert bad_create.is_error
    assert "not available to workers" in bad_create.content


@pytest.mark.asyncio
async def test_kanban_worker_mode_rejects_mutations_on_other_cards() -> None:
    """The ownership gate refuses worker mutations whose `task_id` points
    at any card other than the worker's owned card. This is the primary
    defence against models hallucinating ids from context."""
    board = SessionKanbanBoard()
    parent_tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    own_id = json.loads(
        (await parent_tool.execute("p1", {"action": "create", "title": "mine"})).content
    )["task"]["id"]
    other_id = json.loads(
        (await parent_tool.execute("p2", {"action": "create", "title": "not mine"})).content
    )["task"]["id"]

    worker = KanbanTool(
        board, actor_id="worker", actor_label="worker", owned_task_id=own_id
    )

    bad = await worker.execute(
        "w1",
        {"action": "comment", "task_id": other_id, "comment": "hallucinated id"},
    )
    assert bad.is_error
    assert "refusing mutation" in bad.content

    # The other card was not mutated.
    show_other = await parent_tool.execute(
        "p3", {"action": "show", "task_id": other_id}
    )
    assert json.loads(show_other.content)["task"]["commentCount"] == 0

    # Reading other cards is fine (workers need digest/show for context).
    digest = await worker.execute("w2", {"action": "digest"})
    assert not digest.is_error

    # Mutation on the OWNED card (task_id omitted, collapses to owned) is fine.
    good = await worker.execute(
        "w3", {"action": "comment", "comment": "all clear"}
    )
    assert not good.is_error


@pytest.mark.asyncio
async def test_kanban_spec_fields_are_inlined_when_set() -> None:
    """Move D — the specifier agent writes definition_of_done /
    references / verify_with / token_budget into `metadata`. The board
    surfaces them at the top level of show()/list() under `spec` so the
    worker that follows doesn't have to dig through metadata."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")

    create = await tool.execute(
        "c1", {"action": "create", "title": "wire the dispatcher"},
    )
    card_id = json.loads(create.content)["task"]["id"]

    await tool.execute(
        "u1",
        {
            "action": "update",
            "task_id": card_id,
            "metadata": {
                "definition_of_done": [
                    "tests pass",
                    "dispatcher emits kanban_dispatched events",
                ],
                "references": {"files": ["bridge/freyja_bridge.py"]},
                "verify_with": "pytest tests/test_kanban_coordination.py",
                "token_budget": 8000,
            },
        },
    )

    show = await tool.execute("c2", {"action": "show", "task_id": card_id})
    task = json.loads(show.content)["task"]
    assert "spec" in task
    assert task["spec"]["verify_with"] == "pytest tests/test_kanban_coordination.py"
    assert task["spec"]["token_budget"] == 8000
    assert len(task["spec"]["definition_of_done"]) == 2

    # A card without specifier-set metadata has no `spec` field — keeps
    # the worker payload tight when there's nothing to surface.
    plain = await tool.execute(
        "c3", {"action": "create", "title": "no spec"},
    )
    assert "spec" not in json.loads(plain.content)["task"]


def test_specifier_agent_type_registered_for_kanban() -> None:
    """The specifier profile is wired into the global registry so the
    dispatcher (Move A) can spawn it against triage cards."""
    from bridge.tools.agent_types import get_agent_type

    specifier = get_agent_type("specifier")
    assert specifier.name == "specifier"
    # Worker-narrowed: kanban + read-only file tools, nothing else.
    assert specifier.tool_include is not None
    assert "kanban" in specifier.tool_include
    assert "write_file" not in specifier.tool_include
    assert "bash" not in specifier.tool_include


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

