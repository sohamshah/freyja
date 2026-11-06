"""A job made from a Slack session with the tool filter on keeps the filter.

`schedule` is on the filter's list, and a job runs by default in a new
session that has no gateway source. That session was built with every
tool, so anyone on the allowlist could schedule a `bash` run for a minute
later. A job could also name any other session to run in, such as a
desktop chat, which has every tool too.
"""

from __future__ import annotations

import json

import pytest

from bridge.gateway.capabilities import tools_allowed_for_gateway
from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform
from bridge.scheduler.persistence import load_job
from bridge.scheduler.runtime import _resolve_execution_session

SLACK = "freyja:slack:T1:channel:C1:5.5"
ALLOWED = tools_allowed_for_gateway(Platform.SLACK)
HOURLY = {"kind": "interval", "seconds": 3600}


@pytest.fixture
async def bridge(monkeypatch, tmp_path):
    import bridge.freyja_bridge as fb
    from bridge.scheduler.service import SchedulerService

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    state.scheduler = SchedulerService(state)
    yield state
    for sess in state.sessions.values():
        sess._stop_kanban_dispatcher()


async def _slack_session(state, *, filter_on: bool = True):
    config_path().write_text(f"slack:\n  enable_tool_filter: {str(filter_on).lower()}\n")
    sess = await state.ensure_session(SLACK, gateway_source=MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
    ))
    await sess.initialize()
    return sess


async def _schedule(sess, **arguments):
    result = await sess.tool_registry.get("schedule").execute(
        "c1", {"action": "create", "prompt": "run `rm -rf ~/x` with bash", "schedule": HOURLY, **arguments},
    )
    return result, (None if result.is_error else load_job(json.loads(result.content)["job"]["id"]))


async def _run_session(state, job):
    sess, _sid, _owns = await _resolve_execution_session(state.scheduler, job)
    await sess.initialize()
    return sess


def _tools(sess) -> set[str]:
    return set(sess.tool_registry.list_names())


@pytest.mark.parametrize("execution", [None, "persistent"])
async def test_a_job_made_under_the_filter_runs_with_only_its_tools(bridge, execution):
    sess = await _slack_session(bridge)
    _result, job = await _schedule(sess, **({"execution": execution} if execution else {}))
    assert job.tool_filter == "slack"
    run = await _run_session(bridge, job)
    assert "bash" not in _tools(run)
    assert _tools(run) <= ALLOWED


async def test_a_job_its_run_makes_keeps_the_filter_too(bridge):
    sess = await _slack_session(bridge)
    _result, job = await _schedule(sess)
    run = await _run_session(bridge, job)
    _result, second = await _schedule(run)
    assert second.tool_filter == "slack"
    assert "bash" not in _tools(await _run_session(bridge, second))


@pytest.mark.parametrize("execution", [
    {"kind": "existing_session", "session_id": "desktop-1"},
    {"kind": "persistent_job_session", "session_id": "desktop-1"},
])
async def test_it_cannot_run_in_a_session_the_filter_does_not_reach(bridge, execution):
    sess = await _slack_session(bridge)
    result, _job = await _schedule(sess, execution=execution)
    assert result.is_error and "tool filter" in result.content


async def test_it_can_run_in_the_thread_it_was_made_in(bridge):
    sess = await _slack_session(bridge)
    result, job = await _schedule(sess, execution="here")
    assert not result.is_error, result.content
    assert job.execution.session_id == SLACK


async def test_an_edit_cannot_move_it_to_another_session(bridge):
    sess = await _slack_session(bridge)
    _result, job = await _schedule(sess)
    result = await sess.tool_registry.get("schedule").execute("c2", {
        "action": "update", "job_id": job.id,
        "execution": {"kind": "existing_session", "session_id": "desktop-1"},
    })
    assert result.is_error and "tool filter" in result.content
    assert load_job(job.id).execution.kind == "new_session"


async def test_with_the_filter_off_a_job_runs_with_every_tool(bridge):
    sess = await _slack_session(bridge, filter_on=False)
    _result, job = await _schedule(sess)
    assert job.tool_filter is None
    assert "bash" in _tools(await _run_session(bridge, job))
