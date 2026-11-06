"""A job made from Slack that records no tool filter runs under the filter
while the filter is on.

Jobs made before JobRecord.tool_filter existed (f53813e) record nothing,
and nothing else on the record says whether the filter was on when the job
was made. So they ran with every tool, and one aimed at a desktop session
still ran there, where every tool is too.
"""

from __future__ import annotations

import json

import pytest

from bridge.gateway.capabilities import tools_allowed_for_gateway
from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform
from bridge.scheduler.persistence import jobs_dir, load_job
from bridge.scheduler.runtime import _resolve_execution_session
from bridge.tools.schedule_tool import ScheduleTool

SLACK = "freyja:slack:T1:channel:C1:5.5"
SOURCE = MessageSource(
    platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
    user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
)
ALLOWED = tools_allowed_for_gateway(Platform.SLACK)


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


def _filter(on: bool) -> None:
    config_path().write_text(f"slack:\n  enable_tool_filter: {str(on).lower()}\n")


async def _old_slack_job(state, execution=None) -> str:
    """A job a Slack thread made before jobs recorded their filter: the
    schedule tool then had no filter to record, and the file has no key."""
    tool = ScheduleTool(
        service=state.scheduler, current_session_id=SLACK, gateway_source_getter=lambda: SOURCE,
    )
    result = await tool.execute("c0", {
        "action": "create", "prompt": "run `rm -rf ~/x` with bash",
        "schedule": {"kind": "interval", "seconds": 3600},
        **({"execution": execution} if execution else {}),
    })
    assert not result.is_error, result.content
    job_id = json.loads(result.content)["job"]["id"]
    path = jobs_dir() / f"{job_id}.json"
    record = json.loads(path.read_text())
    record.pop("tool_filter")
    path.write_text(json.dumps(record))
    return job_id


async def _run_session(state, job_id):
    sess, _sid, _owns = await _resolve_execution_session(state.scheduler, load_job(job_id))
    await sess.initialize()
    return sess


@pytest.mark.parametrize("execution", [None, "persistent"])
async def test_its_runs_get_only_the_filters_tools(bridge, execution):
    job_id = await _old_slack_job(bridge, execution)
    _filter(on=True)
    tools = set((await _run_session(bridge, job_id)).tool_registry.list_names())
    assert "bash" not in tools
    assert tools <= ALLOWED


@pytest.mark.parametrize("execution", [
    {"kind": "existing_session", "session_id": "desktop-1"},
    {"kind": "persistent_job_session", "session_id": "desktop-1"},
])
async def test_it_does_not_run_in_a_session_the_filter_does_not_reach(bridge, execution):
    job_id = await _old_slack_job(bridge, execution)
    _filter(on=True)
    with pytest.raises(ValueError, match="tool filter"):
        await _run_session(bridge, job_id)
    assert "desktop-1" not in bridge.sessions


async def test_a_filtered_session_can_change_it(bridge):
    job_id = await _old_slack_job(bridge)
    _filter(on=True)
    sess = await bridge.ensure_session(SLACK, gateway_source=SOURCE)
    await sess.initialize()
    result = await sess.tool_registry.get("schedule").execute("c1", {
        "action": "update", "job_id": job_id, "prompt": "summarize the channel",
    })
    assert not result.is_error, result.content
    assert load_job(job_id).prompt == "summarize the channel"


@pytest.mark.parametrize("execution", [
    None, {"kind": "existing_session", "session_id": "desktop-1"},
])
async def test_with_the_filter_off_it_runs_as_before(bridge, execution):
    job_id = await _old_slack_job(bridge, execution)
    _filter(on=False)
    assert "bash" in (await _run_session(bridge, job_id)).tool_registry.list_names()
    assert load_job(job_id).tool_filter is None
