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

    async def execute(
        self, call_id: str, arguments: dict[str, Any], *, notify_parent: bool = True,
    ) -> Any:
        self.calls.append({"callId": call_id, "notifyParent": notify_parent, **arguments})
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
    session.auto_dispatch_enabled = True
    session.tool_registry = _StubToolRegistry({"sub_agent": sub_tool})
    session.subagent_registry = _StubSubagentRegistry()
    session.queued_messages = []
    session._kanban_dispatched = set()
    session._kanban_judge_pending = set()
    session._kanban_dispatcher_task = None
    # Tick-coalesce bookkeeping (added with the double-fire guard). __init__
    # seeds this to 0; the stub bypasses __init__, so set it explicitly or
    # the very first tick raises AttributeError on the coalesce check.
    session._last_kanban_tick_ms = 0
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
    # The child stays in triage because its parent isn't done — it must
    # not be dispatched. (The parent itself is a ready card with no
    # assignee, so it dispatches under the `general` default; that's
    # expected and not what this test guards.)
    assert child_id not in spawn_targets


@pytest.mark.asyncio
async def test_complete_with_verification_flag_lands_card_in_dispatcher_review_lane() -> None:
    """Integration check across the Move R seam: a card created with
    `requires_verification=True` and completed via the worker path lands
    in `review`, and the dispatcher's review lane hands it to the judge
    on the next tick (not the verifier lane: the flag is informational)."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute(
        "c1",
        {
            "action": "create",
            "title": "verify me",
            "requires_verification": True,
        },
    )
    card_id = json.loads(create.content)["task"]["id"]
    worker = KanbanTool(
        board, actor_id="worker", actor_label="worker", owned_task_id=card_id
    )
    await tool.execute(
        "c2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    await worker.execute("c3", {"action": "complete", "summary": "done"})
    show = await tool.execute("c4", {"action": "show", "task_id": card_id})
    assert json.loads(show.content)["task"]["status"] == "review"

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    judged: list[str] = []

    async def _judge(card: Any, *, sub_tool: Any) -> None:
        judged.append(card.id)

    session._run_kanban_judge_for_card = _judge
    await session._kanban_tick(source="test")
    await asyncio.sleep(0)
    # The review lane hands the card to the judge exactly once; no
    # verifier or worker spawn.
    assert judged == [card_id]
    assert card_id in session._kanban_judge_pending
    assert sub_tool.calls == []


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
    # The tool's `update` refuses done_unverified; legacy cards reach it
    # through the board.
    await board.update(card_id, actor="dispatcher", status="done_unverified")

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
    agent type. Cards without an assignee default to the `general`
    agent type (they're still real work that needs to land on someone)."""
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
    # An unassigned ready card is still real work — the dispatcher defaults
    # it to the `general` agent type rather than silently skipping it (which
    # made autopilot look dead on fresh sessions that hadn't spawned any
    # subagent yet).
    assert len(targets) == 2
    unassigned_id = next(k for k in targets if k != assigned_id)
    assert targets[unassigned_id] == "general"


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


@pytest.mark.asyncio
async def test_dispatcher_reclaims_stuck_running_card() -> None:
    """A running card with no activity past KANBAN_RECLAIM_SECONDS is
    flipped to `crashed` and the dispatcher will re-pick it up on the
    next tick."""
    import time as _time

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute(
        "c1", {"action": "create", "title": "work", "assignee": "code"},
    )
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute(
        "c2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    # Backdate the card's last activity past the reclaim window.
    card = await board.get(card_id)
    assert card is not None
    card.updated_at = _time.time() - (_BridgeSession.KANBAN_RECLAIM_SECONDS + 60)

    # Wire the kanban tool into the stubbed registry so the sweep can
    # invoke `update`.
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    kanban_tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    session.tool_registry._tools["kanban"] = kanban_tool

    await session._kanban_tick(source="test")
    refreshed = await board.get(card_id)
    assert refreshed is not None
    assert refreshed.status == "crashed"
    assert refreshed.consecutive_failures == 1


@pytest.mark.asyncio
async def test_dispatcher_does_not_reclaim_fresh_running_card() -> None:
    """A running card that's only mildly stale (past STALE_SECONDS but
    not RECLAIM_SECONDS) gets a `kanban_stale` signal but no state
    change — the operator decides what to do."""
    import time as _time

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute(
        "c1", {"action": "create", "title": "work", "assignee": "code"},
    )
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute(
        "c2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    card = await board.get(card_id)
    assert card is not None
    card.updated_at = _time.time() - (_BridgeSession.KANBAN_STALE_SECONDS + 30)

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.tool_registry._tools["kanban"] = KanbanTool(
        board, actor_id="parent", actor_label="parent"
    )
    await session._kanban_tick(source="test")
    refreshed = await board.get(card_id)
    assert refreshed is not None
    assert refreshed.status == "running"
    assert refreshed.consecutive_failures == 0


@pytest.mark.asyncio
async def test_heartbeat_refreshes_card_updated_at() -> None:
    """A heartbeat is the worker saying 'still alive' — it must move
    `updated_at` forward so the dispatcher doesn't reclaim a card
    whose worker is making slow but real progress."""
    import time as _time

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "slow"})
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute(
        "c2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    card = await board.get(card_id)
    assert card is not None
    card.updated_at = _time.time() - 120  # 2 minutes ago

    worker = KanbanTool(
        board,
        actor_id="worker",
        actor_label="worker",
        owned_task_id=card_id,
    )
    await worker.execute("h1", {"action": "heartbeat"})
    refreshed = await board.get(card_id)
    assert refreshed is not None
    assert _time.time() - refreshed.updated_at < 1.0


@pytest.mark.asyncio
async def test_dispatcher_redispatches_crashed_card() -> None:
    """A `crashed` card (reclaimed after its worker vanished) is
    retry-eligible: the worker lane re-dispatches it to a fresh worker on
    the next tick. Bounded by the circuit breaker, which trips the card to
    `failed` after repeated flaps — so this can't loop forever."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute(
        "c1", {"action": "create", "title": "flaky", "assignee": "code"},
    )
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute(
        "u1", {"action": "update", "task_id": card_id, "status": "running"},
    )
    await tool.execute(
        "u2", {"action": "update", "task_id": card_id, "status": "crashed"},
    )

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    await session._kanban_tick(source="test")

    targets = {call["kanban_task_id"]: call["agent_type"] for call in sub_tool.calls}
    assert card_id in targets
    assert targets[card_id] == "code"


@pytest.mark.asyncio
async def test_sweep_skips_reclaim_when_worker_still_live() -> None:
    """A running card quiet past the reclaim window is NOT flipped to
    `crashed` while its worker is still in the sub-agent registry —
    reclaiming a live-but-quiet worker (mid long tool call, no heartbeat)
    would kill nothing and unfairly charge it a circuit-breaker failure."""
    import time as _time

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute(
        "c1", {"action": "create", "title": "slow", "assignee": "code"},
    )
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute(
        "u1", {"action": "update", "task_id": card_id, "status": "running"},
    )
    card = await board.get(card_id)
    assert card is not None
    card.updated_at = _time.time() - (_BridgeSession.KANBAN_RECLAIM_SECONDS + 60)

    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.tool_registry._tools["kanban"] = KanbanTool(
        board, actor_id="parent", actor_label="parent"
    )
    # The worker is still live in the registry for this card.
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=card_id)
    ]
    await session._kanban_tick(source="test")

    refreshed = await board.get(card_id)
    assert refreshed is not None
    # Not reclaimed — still running, no breaker charge.
    assert refreshed.status == "running"
    assert refreshed.consecutive_failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("done", "review_iteration", "status"),
    [(True, 1, "done"), (False, 1, "running"), (False, 3, "blocked")],
)
async def test_judge_verdict_sends_the_card_it_moved(
    monkeypatch, done: bool, review_iteration: int, status: str,
) -> None:
    """The verdict moves the card on the board directly, so the bridge
    sends the moved card the way the kanban tool does. Without it the
    renderer kept the card in `review` until a later event carried it."""
    from bridge import freyja_bridge
    from bridge.tools.goal_loop import GoalVerdict

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "judge me"})
    card_id = json.loads(create.content)["task"]["id"]
    worker = KanbanTool(
        board, actor_id="sub_w", actor_label="worker", owned_task_id=card_id
    )
    await tool.execute(
        "c2", {"action": "update", "task_id": card_id, "status": "running"},
    )
    await worker.execute("c3", {"action": "complete", "summary": "done"})
    card = await board.get(card_id)
    assert card is not None and card.status == "review"
    card.review_iteration = review_iteration

    session = _make_session(board=board, sub_tool=_StubSubAgentTool())

    async def _wake(*args: Any, **kwargs: Any) -> bool:
        return True

    session._wake_subagent_with_task = _wake
    await session._handle_kanban_verdict(card, GoalVerdict(done=done, reason="r"))

    moved = [
        event["details"]["task"]
        for event in sent
        if event.get("subtype") == "kanban_update"
    ]
    assert [task["status"] for task in moved] == [status]
    assert moved[0]["id"] == card_id
    assert moved[0]["events"][-1]["actor"] == "kanban-judge"


# Every path where the bridge writes the board itself, and how many direct
# `kanban_board` write calls in freyja_bridge.py it drives. The reclaim
# goes through the kanban tool, which sends its own event.
_BOARD_WRITERS: dict[str, int] = {
    "reclaim": 0,
    "nothing_to_review": 1,
    "judge_pinned": 1,
    "verdict_done": 1,
    "verdict_rework": 1,
    "verdict_blocked": 1,
    "verdict_requeued": 1,
    "operator_create": 2,
    "operator_cancel": 1,
    "operator_unblock": 1,
    "operator_unblock_orphan": 1,
    "operator_reassign": 1,
    # A write that also moves the card's children: the judge's pass
    # promotes them out of triage, a cancel marks them orphaned. Each
    # drives a write counted above.
    "verdict_done_promotes_child": 0,
    "operator_cancel_orphans_child": 0,
}


async def _review_card(board: SessionKanbanBoard, tool: KanbanTool, *, worker: bool) -> Any:
    """A card in review, finished by a worker or (worker=False) with no
    worker session and no artifacts."""
    create = await tool.execute("c1", {"action": "create", "title": "judge me"})
    card_id = json.loads(create.content)["task"]["id"]
    await tool.execute("c2", {"action": "update", "task_id": card_id, "status": "running"})
    if worker:
        await KanbanTool(
            board, actor_id="sub_w", actor_label="worker", owned_task_id=card_id
        ).execute("c3", {"action": "complete", "summary": "done"})
    else:
        await board.update(card_id, actor="worker", status="review", review_iteration=1)
    card = await board.get(card_id)
    assert card is not None and card.status == "review"
    return card


async def _set_up_writer(name: str, session: Any, board: SessionKanbanBoard, tool: KanbanTool) -> Any:
    """Bring the board to where `name` writes it; return the write."""
    import time as _time
    import types

    from bridge import freyja_bridge
    from bridge.tools.goal_loop import GoalVerdict

    async def _ensure_session(_session_id: str) -> Any:
        return session

    state = types.SimpleNamespace(active_session_id=session.id, ensure_session=_ensure_session)

    def _command(**cmd: Any) -> Any:
        return lambda: freyja_bridge._handle_command(state, {"sessionId": session.id, **cmd})

    async def _woken(*args: Any, **kwargs: Any) -> bool:
        return True

    session._wake_subagent_with_task = _woken

    if name == "reclaim":
        create = await tool.execute("c1", {"action": "create", "title": "quiet"})
        card_id = json.loads(create.content)["task"]["id"]
        await tool.execute("c2", {"action": "update", "task_id": card_id, "status": "running"})
        card = await board.get(card_id)
        card.updated_at = _time.time() - (_BridgeSession.KANBAN_RECLAIM_SECONDS + 60)
        return lambda: session._kanban_tick(source="test")
    if name == "nothing_to_review":
        await _review_card(board, tool, worker=False)
        return lambda: session._kanban_tick(source="test")
    if name == "judge_pinned":
        card = await _review_card(board, tool, worker=True)
        sub_tool = _StubSubAgentTool()

        async def _spawn(**kwargs: Any) -> Any:
            return types.SimpleNamespace(id="sub_judge"), None, None

        async def _synthesis_fails(*args: Any) -> Any:
            raise RuntimeError("synthesis failed")

        sub_tool.spawn_programmatically = _spawn
        session._synthesize_judge_verdict = _synthesis_fails
        return lambda: session._run_kanban_judge_for_card(card, sub_tool=sub_tool)
    if name == "verdict_done_promotes_child":
        card = await _review_card(board, tool, worker=True)
        await tool.execute("c4", {"action": "create", "title": "next", "parents": [card.id]})
        return lambda: session._handle_kanban_verdict(card, GoalVerdict(done=True, reason="r"))
    if name.startswith("verdict_"):
        card = await _review_card(board, tool, worker=name != "verdict_requeued")
        if name == "verdict_blocked":
            card.review_iteration = _BridgeSession.KANBAN_MAX_REVIEW_ITERATIONS
        verdict = GoalVerdict(done=name == "verdict_done", reason="r")
        return lambda: session._handle_kanban_verdict(card, verdict)
    if name == "operator_create":
        create = await tool.execute("c1", {"action": "create", "title": "later"})
        child_id = json.loads(create.content)["task"]["id"]
        return _command(type="kanban_operator_create", title="first", children=[child_id])
    if name.startswith("operator_cancel"):
        create = await tool.execute("c1", {"action": "create", "title": "stop me"})
        card_id = json.loads(create.content)["task"]["id"]
        if name == "operator_cancel_orphans_child":
            await tool.execute("c2", {"action": "create", "title": "next", "parents": [card_id]})
        await board.update(card_id, actor="worker", status="running", worker_session_id="sub_w")
        session.subagent_registry.kill = lambda _session_id: None
        return _command(type="kanban_operator_cancel", cardId=card_id)
    if name == "operator_unblock":
        create = await tool.execute("c1", {"action": "create", "title": "stuck"})
        card_id = json.loads(create.content)["task"]["id"]
        await board.update(card_id, actor="worker", status="blocked")
        return _command(type="kanban_operator_unblock", cardId=card_id)
    if name == "operator_unblock_orphan":
        parent = await tool.execute("c1", {"action": "create", "title": "plan"})
        parent_id = json.loads(parent.content)["task"]["id"]
        child = await tool.execute(
            "c2", {"action": "create", "title": "do", "parents": [parent_id]}
        )
        child_id = json.loads(child.content)["task"]["id"]
        await tool.execute(
            "c3", {"action": "update", "task_id": parent_id, "status": "cancelled"}
        )
        return _command(type="kanban_operator_unblock", cardId=child_id)
    if name == "operator_reassign":
        create = await tool.execute("c1", {"action": "create", "title": "move me"})
        card_id = json.loads(create.content)["task"]["id"]
        return _command(type="kanban_operator_reassign", cardId=card_id, assignee="code")
    raise AssertionError(f"no set-up for {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_BOARD_WRITERS))
async def test_every_bridge_board_write_sends_the_cards_it_changed(monkeypatch, name: str) -> None:
    """The renderer learns a card's state only from kanban_* events that
    carry it. Each path where the bridge writes the board must send every
    card it changed, in the state the board now holds, or the app's board
    stays where it was."""
    from bridge import freyja_bridge

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    # The board sends the children a write moves on its own, as the
    # bridge wires it when it builds the board.
    board = SessionKanbanBoard(
        on_child_changed=lambda child: session._emit_kanban_card(child, source="kanban_board")
    )
    session = _make_session(board=board, sub_tool=_StubSubAgentTool())
    # The parent's kanban tool sends through the bridge's emit, as
    # build_desktop_registry wires it.
    tool = KanbanTool(
        board,
        actor_id=session.id,
        actor_label="parent",
        emit_event=lambda event: freyja_bridge.emit(event),
        parent_session_id=session.id,
    )
    session.tool_registry._tools["kanban"] = tool
    write = await _set_up_writer(name, session, board, tool)
    before = {task.id: task.to_dict() for task in await board.list()}
    sent.clear()

    await write()

    seen: dict[str, dict[str, Any]] = {}
    for event in sent:
        details = event.get("details") or {}
        if not str(event.get("subtype") or "").startswith("kanban_"):
            continue
        for task in [details.get("task"), *(details.get("tasks") or [])]:
            if isinstance(task, dict):
                seen[task["id"]] = task
    changed = [task for task in await board.list() if before.get(task.id) != task.to_dict()]
    assert changed, f"{name} changed no card"
    for task in changed:
        assert task.id in seen, f"{name} changed {task.id} and sent no event with it"
        current = task.to_dict(include_history=False)
        assert {key: seen[task.id].get(key) for key in current} == current


def test_every_direct_board_write_in_the_bridge_is_driven_above() -> None:
    """A new place where the bridge writes the board must send the cards
    it changes (`_emit_kanban_card`) and join `_BOARD_WRITERS`."""
    import ast
    import inspect

    from bridge import freyja_bridge

    writes = [
        node.lineno
        for node in ast.walk(ast.parse(inspect.getsource(freyja_bridge)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "update", "link", "unblock"}
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "kanban_board"
    ]
    assert len(writes) == sum(_BOARD_WRITERS.values()), (
        f"direct board writes at freyja_bridge.py lines {writes}"
    )


@pytest.mark.asyncio
async def test_worker_ending_at_the_review_cap_reports_the_block() -> None:
    """A worker that dies at the review cap skips review and parks its
    card in blocked. That path must say so the way the judge's own cap
    path does, or the chat and the dashboard feed never learn why the
    card stopped."""
    import types

    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry
    from bridge.tools.sub_agent_tool import SubAgentSpec, SubAgentTool

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    create = await tool.execute("c1", {"action": "create", "title": "flaky"})
    card_id = json.loads(create.content)["task"]["id"]
    await board.update(
        card_id,
        actor="worker",
        status="running",
        worker_session_id="sub_w",
        review_iteration=_BridgeSession.KANBAN_MAX_REVIEW_ITERATIONS,
    )
    sent: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        sent.append(event)

    sub_tool = SubAgentTool(
        SubAgentSpec(
            parent_workspace="/tmp",
            parent_model="fake-model",
            build_provider=lambda *a, **k: None,
            parent_registry=ToolRegistry(),
            registry=SubAgentRegistry(),
            emit_event=_emit,
            parent_session_id="parent-1",
            coordination_strategy="kanban",
            kanban_board=board,
        )
    )
    record = types.SimpleNamespace(
        id="sub_w", label="worker", kanban_task_id=card_id, created_files=[]
    )

    await sub_tool._mark_kanban_terminal(record, "crashed", "ran out of road")

    assert (await board.get(card_id)).status == "blocked"
    [blocked] = [event for event in sent if event["subtype"] == "kanban_blocked"]
    assert blocked["sessionId"] == "parent-1"
    assert blocked["details"]["cardId"] == card_id
    assert blocked["details"]["chatVisible"] is True


async def _dispatched(session: Any, sub_tool: _StubSubAgentTool) -> set[str]:
    """The cards one tick hands to a worker, verifier or judge."""
    judged: list[str] = []

    async def _judge(card: Any, *, sub_tool: Any) -> None:
        judged.append(card.id)

    session._run_kanban_judge_for_card = _judge
    sub_tool.calls.clear()
    await session._kanban_tick(source="test", force=True)
    await asyncio.sleep(0)
    return {call["kanban_task_id"] for call in sub_tool.calls} | set(judged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lane",
    [
        ["review", "ready"],
        ["crashed"],
        ["timed_out"],
        ["done_unverified"],
        ["review"],
    ],
)
async def test_dispatcher_holds_a_card_past_triage_while_a_parent_is_pending(
    lane: list[str],
) -> None:
    """link() records a parent on a card a worker has picked up and
    leaves it in its lane, and review sends a card back to ready. The
    dispatcher checked parents only for triage cards, so it handed such
    a card to a worker, verifier or judge while its parent was still
    pending. The card now waits in its lane until the parent is done;
    the mission root counts as done."""
    board = SessionKanbanBoard()
    await board.create(title="mission", metadata={"role": "mission_root"})
    parent = (await board.create(title="parent")).id
    card = (await board.create(title="card", assignee="code")).id
    await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
    await board.link(parent, card, actor="parent")
    for status in lane:
        await board.update(card, actor="worker", status=status)
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)

    assert card not in await _dispatched(session, sub_tool)

    await board.update(parent, actor="worker", status="running")
    await board.update(parent, actor="kanban-judge", status="done")
    assert card in await _dispatched(session, sub_tool)


@pytest.mark.asyncio
async def test_dispatcher_sends_a_card_unblock_released_past_a_failed_parent() -> None:
    """unblock releases a triage card whose parent failed to ready. That
    parent never finishes, so the dispatcher must not hold the card on
    it."""
    board = SessionKanbanBoard()
    parent = (await board.create(title="parent")).id
    card = (await board.create(title="card", parents=[parent])).id
    await board.update(parent, actor="parent", status="cancelled")
    _, message = await board.unblock(card, actor="parent")
    assert message == "unblocked"
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)

    assert card in await _dispatched(session, sub_tool)


@pytest.mark.asyncio
async def test_the_session_and_its_board_name_one_mission_root() -> None:
    """The session kept its own copy of the mission root id, and the
    triage lane read it where every other lane reads the board's. A board
    that already had a root (the agent made one before a user message
    reached the session) got a second one, and the session kept that
    one's id, so the triage lane counted another card as done than the
    board did."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    await tool.execute(
        "r1",
        {"action": "create", "title": "mission", "metadata": {"role": "mission_root"}},
    )
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.tool_registry._tools["kanban"] = tool

    await session._ensure_mission_root_card("plan the release")

    roots = [t.id for t in await board.list() if t.metadata.get("role") == "mission_root"]
    assert roots == [board._mission_root_id]
    # A triage card whose other parent finished while its promotion row
    # was lost: the triage lane counts the board's root as done.
    parent = (await board.create(title="parent")).id
    card = (await board.create(title="card", parents=[board._mission_root_id, parent])).id
    board._tasks[parent].status = "done"
    assert card in await _dispatched(session, sub_tool)
