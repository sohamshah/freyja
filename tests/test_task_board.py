"""The `tasks` tool: dependencies given on create, refused links, and which
dependencies still block (found in the wedding booking session, 2026-10-17)."""

import asyncio

from bridge.tools.task_board import SessionTaskBoard, TaskBoardTool


def tool():
    return TaskBoardTool(SessionTaskBoard(), actor_id="s1", actor_label="parent")


def call(t, **args):
    return asyncio.run(t.execute("c", args))


def ids(t):
    return {task.id: task for task in t._board._tasks.values()}  # noqa: SLF001


def test_create_links_the_dependencies_it_is_given():
    # A task created with add_blocked_by and add_blocks had neither edge.
    t = tool()
    call(t, action="create", title="Search Jan 20 to 24")
    call(t, action="create", title="Summarize")
    out = call(t, action="create", title="Search Jan 21 to 24", add_blocked_by=["task_001"], add_blocks=["task_002"])
    assert not out.is_error
    new = ids(t)["task_003"]
    assert new.blocked_by == ["task_001"] and new.blocks == ["task_002"]
    assert ids(t)["task_002"].blocked_by == ["task_003"]
    assert "newly linked: task_001, task_002" in out.content
    assert "blocked by: task_001" in out.content


def test_create_with_an_unknown_dependency_makes_nothing():
    t = tool()
    out = call(t, action="create", title="Orphan", add_blocked_by=["task_009"])
    assert out.is_error and "task_009" in out.content and "nothing was created" in out.content
    assert ids(t) == {}


def test_a_refused_link_is_reported_not_dropped():
    t = tool()
    call(t, action="create", title="A")
    call(t, action="create", title="B", add_blocked_by=["task_001"])
    # A waits on B, while B already waits on A: a cycle.
    out = call(t, action="update", task_id="task_001", add_blocked_by=["task_002"])
    assert "links not made: task_002 → task_001: link would create a dependency cycle" in out.content


def test_a_finished_dependency_no_longer_reads_as_blocking():
    # task_003 printed "blocked by: task_002" long after task_002 was done.
    t = tool()
    call(t, action="create", title="First")
    call(t, action="create", title="Second", add_blocked_by=["task_001"])
    before = call(t, action="show", task_id="task_002")
    assert "blocked by: task_001" in before.content and "(blocked)" in before.content
    call(t, action="complete", task_id="task_001")
    after = call(t, action="show", task_id="task_002")
    assert "blocked by:" not in after.content and "(blocked)" not in after.content
    assert "depended on (finished): task_001" in after.content
