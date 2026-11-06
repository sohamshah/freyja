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
    session._kanban_dispatch_backoff = {}
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


async def _force_dispatch(monkeypatch: Any, session: Any, card_id: str) -> list[dict[str, Any]]:
    """Send the app's force-dispatch command; return what the bridge emits."""
    import types

    from bridge import freyja_bridge

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)

    async def _ensure_session(_session_id: str) -> Any:
        return session

    state = types.SimpleNamespace(active_session_id=session.id, ensure_session=_ensure_session)
    await freyja_bridge._handle_command(
        state,
        {"type": "kanban_operator_force_dispatch", "sessionId": session.id, "cardId": card_id},
    )
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_force_dispatch_holds_a_card_while_a_parent_can_still_finish(
    monkeypatch, linked: bool,
) -> None:
    """Force dispatch says parents still apply, but it checked none, so it
    sent a triage card (or a ready card link() gave a new parent) to a
    worker while that parent was pending. It now refuses and says why,
    and sends the card once the parent is done."""
    board = SessionKanbanBoard()
    parent = (await board.create(title="parent")).id
    if linked:
        card = (await board.create(title="card", assignee="code")).id
        await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
        await board.link(parent, card, actor="parent")
        await board.update(card, actor="kanban-judge", status="review")
        await board.update(card, actor="kanban-judge", status="ready")
    else:
        card = (await board.create(title="card", parents=[parent])).id
        assert (await board.get(card)).status == "triage"
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)

    sent = await _force_dispatch(monkeypatch, session, card)

    assert sub_tool.calls == []
    [refused] = [e for e in sent if e.get("subtype") == "kanban_operator_force_dispatch_failed"]
    assert parent in refused["message"]

    await board.update(parent, actor="worker", status="running")
    await board.update(parent, actor="kanban-judge", status="done")
    await _force_dispatch(monkeypatch, session, card)
    assert [call["kanban_task_id"] for call in sub_tool.calls] == [card]


@pytest.mark.asyncio
async def test_force_dispatch_waits_for_a_free_worker_slot(monkeypatch) -> None:
    """Force dispatch says capacity still applies, but it sent a worker
    with KANBAN_MAX_PARALLEL already running."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL)
    ]

    sent = await _force_dispatch(monkeypatch, session, card)

    assert sub_tool.calls == []
    assert [e["subtype"] for e in sent if e["type"] == "system_event"] == [
        "kanban_operator_force_dispatch_failed"
    ]


@pytest.mark.asyncio
async def test_force_dispatch_gives_a_triage_card_the_specifier_the_tick_would(monkeypatch) -> None:
    """The tick sends a triage card whose parents are done to a specifier,
    which writes its spec and moves it to ready. Force dispatch sent it
    to a worker, which cannot move a card out of triage."""
    board = SessionKanbanBoard()
    parent = (await board.create(title="parent")).id
    card = (await board.create(title="card", assignee="code", parents=[parent])).id
    # Its promotion row was lost: done parent, card still in triage.
    board._tasks[parent].status = "done"
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False

    await _force_dispatch(monkeypatch, session, card)
    forced = [(call["agent_type"], call["label"]) for call in sub_tool.calls]
    sub_tool.calls.clear()
    await session._kanban_tick(source="test", force=True)
    ticked = [(call["agent_type"], call["label"]) for call in sub_tool.calls]

    assert forced == ticked == [("specifier", f"specifier {card}")]


@pytest.mark.asyncio
async def test_force_dispatch_sends_a_card_again_once_its_worker_is_gone(monkeypatch) -> None:
    """Only the tick refreshed the set of dispatched cards from the
    sub-agent registry, and no tick runs with autopilot off. A card force
    dispatched once and back in ready after its worker left could not be
    force dispatched again, and the bridge said nothing."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False

    await _force_dispatch(monkeypatch, session, card)
    # The worker runs: a second press is refused, and says why.
    session.subagent_registry.records = [_StubRecord(is_running=True, kanban_task_id=card)]
    sent = await _force_dispatch(monkeypatch, session, card)
    [refused] = [e for e in sent if e.get("subtype") == "kanban_operator_force_dispatch_failed"]
    assert "already" in refused["message"]
    # It blocks and leaves; the card goes back to ready.
    await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
    await board.update(card, actor="worker", status="blocked")
    session.subagent_registry.records = [_StubRecord(is_running=False, kanban_task_id=card)]
    await board.update(card, actor="parent", status="ready")

    sent = await _force_dispatch(monkeypatch, session, card)

    assert [call["kanban_task_id"] for call in sub_tool.calls] == [card, card]
    assert [e["subtype"] for e in sent if e["type"] == "system_event"] == ["kanban_dispatched"]


def _failing_spawn(sub_tool: _StubSubAgentTool, failure: str) -> None:
    """Make `sub_tool` refuse each spawn as sub_agent does, with an error
    result, or (failure="raises") raise."""
    from bridge.tools.base import ToolResult

    async def _execute(
        call_id: str, arguments: dict[str, Any], *, notify_parent: bool = True,
    ) -> Any:
        sub_tool.calls.append({"callId": call_id, **arguments})
        if failure == "raises":
            raise RuntimeError("registry closed")
        return ToolResult(
            call_id=call_id,
            content="Error: no available model for `code` profile (m: no key)",
            is_error=True,
        )

    sub_tool.execute = _execute


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "raises"])
async def test_a_card_whose_worker_did_not_start_is_not_reported_dispatched(
    monkeypatch, failure: str,
) -> None:
    """The tick sent kanban_dispatched before it spawned the worker, and
    took an error result (no model for the agent type, the sub-agent cap)
    as a spawn, so the feed said the card went out and the tick held it
    as dispatched. It now says why the card did not go out, and leaves it
    where it was for the next tick."""
    from bridge import freyja_bridge

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    sub_tool = _StubSubAgentTool()
    _failing_spawn(sub_tool, failure)
    session = _make_session(board=board, sub_tool=sub_tool)

    await session._kanban_tick(source="test")

    assert [call["kanban_task_id"] for call in sub_tool.calls] == [card]
    subtypes = [e["subtype"] for e in sent if e["type"] == "system_event"]
    assert "kanban_dispatched" not in subtypes
    [failed] = [e for e in sent if e.get("subtype") == "kanban_dispatch_failed"]
    assert failed["details"]["cardId"] == card
    reason = "registry closed" if failure == "raises" else "no available model for `code`"
    assert reason in failed["details"]["reason"]
    assert reason in failed["message"]
    assert card not in session._kanban_dispatched
    assert (await board.get(card)).status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "raises"])
async def test_force_dispatch_says_why_a_worker_did_not_start(monkeypatch, failure: str) -> None:
    """Force dispatch toasted "Force-dispatched" on a kanban_dispatched
    sent before the spawn, though the spawn failed. It now answers with
    the reason instead."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    sub_tool = _StubSubAgentTool()
    _failing_spawn(sub_tool, failure)
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False

    sent = await _force_dispatch(monkeypatch, session, card)

    [failed] = [e for e in sent if e["type"] == "system_event"]
    assert failed["subtype"] == "kanban_operator_force_dispatch_failed"
    reason = "registry closed" if failure == "raises" else "no available model for `code`"
    assert reason in failed["message"]
    assert failed["details"]["cardId"] == card
    assert card not in session._kanban_dispatched
    assert (await board.get(card)).status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "running", "no_sub_agent"])
async def test_force_dispatch_says_why_it_sent_nothing(monkeypatch, case: str) -> None:
    """Force dispatch sent no event for a card not on the board, a card
    whose status had moved on since the app drew it, or a session with no
    sub_agent tool, so the app waited on an answer that never came."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    if case == "running":
        await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
    if case == "no_sub_agent":
        session.tool_registry._tools.clear()

    sent = await _force_dispatch(monkeypatch, session, "card_404" if case == "missing" else card)

    assert sub_tool.calls == []
    [refused] = [e for e in sent if e["type"] == "system_event"]
    assert refused["subtype"] == "kanban_operator_force_dispatch_failed"
    assert refused["details"]["cardId"] == ("card_404" if case == "missing" else card)
    assert refused["details"]["reason"] == {
        "missing": "it is not on the board",
        "running": "it is running",
        "no_sub_agent": "this session has no sub_agent tool",
    }[case]


async def _operator_command(monkeypatch: Any, session: Any, **cmd: Any) -> list[dict[str, Any]]:
    """Send one of the app's card commands; return the system events the
    bridge sends."""
    import types

    from bridge import freyja_bridge

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)

    async def _ensure_session(_session_id: str) -> Any:
        return session

    state = types.SimpleNamespace(active_session_id=session.id, ensure_session=_ensure_session)
    await freyja_bridge._handle_command(state, {"sessionId": session.id, **cmd})
    return [event for event in sent if event["type"] == "system_event"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,case,reason",
    [
        ("kanban_operator_cancel", "missing", "it is not on the board"),
        ("kanban_operator_cancel", "done", "invalid transition 'done' -> 'cancelled'"),
        ("kanban_operator_unblock", "missing", "it is not on the board"),
        ("kanban_operator_unblock", "raises", "journal closed"),
        ("kanban_operator_reassign", "missing", "it is not on the board"),
    ],
)
async def test_a_card_command_the_bridge_cannot_carry_out_says_why(
    monkeypatch, command: str, case: str, reason: str,
) -> None:
    """The app waits for the bridge's answer to Cancel, Unblock and
    Reassign. Cancel sent nothing for a card not on the board or one it
    could not move, Unblock nothing for a card not on the board or a
    write that raised, and Reassign said it reassigned a card not on the
    board."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card")).id
    session = _make_session(board=board, sub_tool=_StubSubAgentTool())
    if case == "done":
        await board.update(card, actor="worker", status="running")
        await board.update(card, actor="kanban-judge", status="done")
    if case == "raises":
        await board.update(card, actor="worker", status="blocked")

        async def _raises(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("journal closed")

        board.update = _raises
    card_id = "card_404" if case == "missing" else card

    sent = await _operator_command(monkeypatch, session, type=command, cardId=card_id, assignee="code")

    [refused] = sent
    assert refused["subtype"] == f"{command}_failed"
    assert refused["details"]["cardId"] == card_id
    assert reason in refused["details"]["reason"]
    assert reason in refused["message"]


@pytest.mark.asyncio
async def test_force_dispatch_sends_a_review_card_to_the_judge_with_autopilot_off(monkeypatch) -> None:
    """Only the tick's review lane runs the judge, and no tick runs with
    autopilot off, so a card in review waited for a judge that never
    came, and force dispatch refused it. Force dispatch now starts the
    judge the review lane would, outside the worker cap as there, and
    refuses while a judge is on the card or when there is nothing to
    review."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    card = (await _review_card(board, tool, worker=True)).id
    empty = (await _review_card(board, tool, worker=False)).id
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL)
    ]
    judged: list[str] = []

    async def _judge(card: Any, *, sub_tool: Any) -> None:
        judged.append(card.id)

    session._run_kanban_judge_for_card = _judge

    sent = await _force_dispatch(monkeypatch, session, card)
    await asyncio.sleep(0)

    assert judged == [card]
    assert sub_tool.calls == []
    # The judge says it started (kanban_review_started) or crashed.
    assert [e for e in sent if e["type"] == "system_event"] == []
    assert card in session._kanban_judge_pending

    refusals = {}
    for card_id in (card, empty):
        sent = await _force_dispatch(monkeypatch, session, card_id)
        [refused] = [e for e in sent if e["type"] == "system_event"]
        assert refused["subtype"] == "kanban_operator_force_dispatch_failed"
        refusals[card_id] = refused["details"]["reason"]
    await asyncio.sleep(0)
    assert judged == [card]
    assert refusals == {
        card: "a judge is already on it",
        empty: "it has no worker session and no artifacts to review",
    }


@pytest.mark.asyncio
async def test_a_full_board_still_judges_and_reclaims() -> None:
    """The tick returned once KANBAN_MAX_PARALLEL workers ran, before the
    review lane and the stale sweep, though neither takes a worker slot.
    On a full board no card was judged and no stuck card was reclaimed.
    Both now run; only the worker lanes wait for a free slot."""
    import time as _time

    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    review = (await _review_card(board, tool, worker=True)).id
    stuck = (await board.create(title="stuck", assignee="code")).id
    await board.update(stuck, actor="worker", status="running", worker_session_id="sub_s")
    board._tasks[stuck].updated_at = _time.time() - (_BridgeSession.KANBAN_RECLAIM_SECONDS + 60)
    await board.create(title="next", assignee="code")
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.tool_registry._tools["kanban"] = tool
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL)
    ]
    judged: list[str] = []

    async def _judge(card: Any, *, sub_tool: Any) -> None:
        judged.append(card.id)

    session._run_kanban_judge_for_card = _judge

    await session._kanban_tick(source="test")
    await asyncio.sleep(0)

    assert judged == [review]
    assert (await board.get(stuck)).status == "crashed"
    assert sub_tool.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("autopilot", [False, True])
async def test_add_task_sends_only_the_new_card(monkeypatch, autopilot: bool) -> None:
    """+ Add Task ran a whole forced tick for the operator's new card, so
    with autopilot off it also sent every other ready card to a worker
    and every card in review to a judge. It now sends only the new card;
    the rest wait for autopilot or the operator, as before."""
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    await board.create(title="waiting", assignee="code")
    await _review_card(board, tool, worker=True)
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = autopilot
    judged: list[str] = []

    async def _judge(card: Any, *, sub_tool: Any) -> None:
        judged.append(card.id)

    session._run_kanban_judge_for_card = _judge

    await _operator_command(monkeypatch, session, type="kanban_operator_create", title="mine")
    await asyncio.sleep(0)

    [new] = [t.id for t in await board.list() if t.title == "mine"]
    assert [call["kanban_task_id"] for call in sub_tool.calls] == [new]
    assert judged == []


@pytest.mark.asyncio
async def test_a_card_whose_spawn_keeps_failing_backs_off_and_takes_no_slot(monkeypatch) -> None:
    """A card whose spawn sub_agent refused went out again on every tick:
    each tick sent kanban_dispatch_failed and gave the card a worker slot,
    so the card behind it never went out. It now waits before each new
    try, twice as long each time up to a cap, and takes no slot while it
    waits. A spawn that starts ends the backoff."""
    from bridge import freyja_bridge
    from bridge.tools.base import ToolResult

    now = [1_000_000.0]
    monkeypatch.setattr(freyja_bridge.time, "time", lambda: now[0])
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    board = SessionKanbanBoard()
    failing = (await board.create(title="failing", assignee="code")).id
    behind = (await board.create(title="behind", assignee="general")).id
    sub_tool = _StubSubAgentTool()
    spawns_fail = [True]
    succeed = sub_tool.execute

    async def _execute(call_id: str, arguments: dict[str, Any], *, notify_parent: bool = True) -> Any:
        if arguments["kanban_task_id"] == failing and spawns_fail[0]:
            sub_tool.calls.append(arguments)
            return ToolResult(call_id=call_id, content="Error: no available model for `code`", is_error=True)
        return await succeed(call_id, arguments, notify_parent=notify_parent)

    sub_tool.execute = _execute
    session = _make_session(board=board, sub_tool=sub_tool)
    # One free worker slot.
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL - 1)
    ]

    async def _tick() -> tuple[list[str], list[dict[str, Any]]]:
        sub_tool.calls.clear()
        sent.clear()
        await session._kanban_tick(source="test", force=True)
        failed = [e["details"] for e in sent if e.get("subtype") == "kanban_dispatch_failed"]
        return [call["kanban_task_id"] for call in sub_tool.calls], failed

    tried, [failed] = await _tick()
    assert tried == [failing]
    [message] = [e["message"] for e in sent if e.get("subtype") == "kanban_dispatch_failed"]
    assert message.endswith("no available model for `code`. Next try in 30s")
    # The card that failed takes no slot while it waits.
    assert await _tick() == ([behind], [])
    await board.update(behind, actor="worker", status="running", worker_session_id="sub_b")

    waits = [failed["retryInSeconds"]]
    for _ in range(6):
        now[0] += waits[-1] - 1
        assert await _tick() == ([], [])
        now[0] += 1
        tried, [failed] = await _tick()
        assert tried == [failing]
        waits.append(failed["retryInSeconds"])
    assert waits == [30, 60, 120, 240, 480, 600, 600]

    spawns_fail[0] = False
    now[0] += 600
    assert await _tick() == ([failing], [])
    assert failing not in session._kanban_dispatch_backoff


@pytest.mark.asyncio
@pytest.mark.parametrize("sticky", [False, True])
@pytest.mark.parametrize("starts", [False, True])
async def test_review_started_goes_out_once_the_judge_runs(
    monkeypatch, sticky: bool, starts: bool,
) -> None:
    """kanban_review_started went out before the judge's spawn, so a judge
    that never started (no model for judge-deep, the sub-agent cap, a
    sticky judge that would not wake) showed "Judging", then "judge
    crashed". It now goes out once the judge runs; a judge that does not
    start sends only kanban_judge_failed."""
    import types

    from bridge import freyja_bridge
    from bridge.tools import agent_types
    from bridge.tools import sub_agent_tool as sat
    from bridge.tools.agent_types import ModelResolution
    from bridge.tools.base import ToolRegistry
    from bridge.tools.goal_loop import GoalVerdict
    from bridge.tools.sub_agent_registry import SubAgentRegistry, SubAgentState

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(freyja_bridge, "emit", sent.append)
    subtypes = lambda: [e["subtype"] for e in sent if e.get("type") == "system_event"]  # noqa: E731
    seen_when_running: list[list[str]] = []

    async def _run_child(self: Any, record: Any) -> str:
        seen_when_running.append(subtypes())
        self._spec.registry.mark_done(record.id, "looked", SubAgentState.DONE)
        return "looked"

    monkeypatch.setattr(agent_types, "_model_available", lambda *a: (False, "no key"))
    monkeypatch.setattr(
        sat,
        "resolve_model_choice",
        lambda agent_type, parent_model: ModelResolution(
            model="fake-model",
            policy="test",
            candidates=("fake-model",),
            unavailable=() if starts else (("fake-model", "no key"),),
            available=starts,
        ),
    )
    monkeypatch.setattr(sat.SubAgentTool, "_run_child", _run_child)

    async def _emit(_event: Any) -> None:
        return None

    judge_tool = sat.SubAgentTool(
        sat.SubAgentSpec(
            parent_workspace="/tmp",
            parent_model="parent-model",
            build_provider=lambda *a, **k: None,
            parent_registry=ToolRegistry(),
            registry=SubAgentRegistry(),
            emit_event=_emit,
            parent_session_id="test-session",
        )
    )
    board = SessionKanbanBoard()
    tool = KanbanTool(board, actor_id="parent", actor_label="parent")
    card = await _review_card(board, tool, worker=True)
    if sticky:
        card = await board.update(card.id, actor="kanban-dispatcher", judge_session_id="sub_judge")
    session = _make_session(board=board, sub_tool=_StubSubAgentTool())

    async def _wake(*args: Any, **kwargs: Any) -> bool:
        seen_when_running.append(subtypes())
        return starts

    async def _verdict(*args: Any) -> Any:
        return GoalVerdict(done=True, reason="r")

    session._wake_subagent_with_task = _wake
    session.subagent_registry.wait = lambda sid: types.SimpleNamespace(id=sid, result="looked")
    session._synthesize_judge_verdict = _verdict
    sent.clear()

    await session._run_kanban_judge_for_card(card, sub_tool=judge_tool)

    if not starts:
        assert subtypes() == ["kanban_judge_failed"]
        return
    assert subtypes().index("kanban_review_started") < subtypes().index("kanban_judge_verdict")
    # A fresh judge says so while it runs; a sticky one once it is woken.
    [seen] = seen_when_running
    assert ("kanban_review_started" in seen) is not sticky


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["crashed", "timed_out"])
async def test_force_dispatch_sends_a_crashed_or_timed_out_card_as_the_tick_does(
    monkeypatch, status: str,
) -> None:
    """The tick sends a crashed or timed-out card back to a new worker, but
    force dispatch refused it ("it is crashed"), so with autopilot off the
    operator had to unblock it first. It now sends the worker the tick
    would, behind the same gates."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
    await board.update(card, actor="worker", status=status)
    sub_tool = _StubSubAgentTool()
    session = _make_session(board=board, sub_tool=sub_tool)
    session.auto_dispatch_enabled = False
    session.subagent_registry.records = [
        _StubRecord(is_running=True, kanban_task_id=f"phantom_{i}")
        for i in range(_BridgeSession.KANBAN_MAX_PARALLEL)
    ]

    sent = await _force_dispatch(monkeypatch, session, card)
    [refused] = [e for e in sent if e["type"] == "system_event"]
    assert refused["details"]["reason"] == f"{_BridgeSession.KANBAN_MAX_PARALLEL} workers are already running"

    session.subagent_registry.records = []
    await _force_dispatch(monkeypatch, session, card)
    forced = [(call["agent_type"], call["label"]) for call in sub_tool.calls]
    sub_tool.calls.clear()
    await session._kanban_tick(source="test", force=True)
    ticked = [(call["agent_type"], call["label"]) for call in sub_tool.calls]

    assert forced == ticked == [("code", f"code {card}")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "done"])
async def test_cancel_stops_the_worker_only_once_the_card_is_cancelled(monkeypatch, status: str) -> None:
    """Cancel stopped the card's worker before it moved the card. On a
    card that cannot move to cancelled (one the judge passed after the app
    drew it as running, while its worker still ran) the worker was stopped
    and the card stayed where it was. The worker is now stopped only once
    the card is cancelled."""
    board = SessionKanbanBoard()
    card = (await board.create(title="card", assignee="code")).id
    await board.update(card, actor="worker", status="running", worker_session_id="sub_w")
    if status == "done":
        await board.update(card, actor="kanban-judge", status="done")
    session = _make_session(board=board, sub_tool=_StubSubAgentTool())
    killed: list[str] = []
    session.subagent_registry.kill = killed.append

    sent = await _operator_command(monkeypatch, session, type="kanban_operator_cancel", cardId=card)

    if status == "done":
        [refused] = sent
        assert refused["subtype"] == "kanban_operator_cancel_failed"
        assert killed == []
        assert (await board.get(card)).status == "done"
    else:
        assert sent[-1]["subtype"] == "kanban_operator_cancelled"
        assert killed == ["sub_w"]
        assert (await board.get(card)).status == "cancelled"
