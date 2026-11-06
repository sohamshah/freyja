"""A job's budget, artifact, timeout, model and misfire policy can be
cleared.

A None in a JobPatch means "leave unchanged", so a null for any of these
from the schedule tool or the dashboard's bridge command left the job's
value as it was: once set, none could be removed. clear_max_fires
(f29575c) fixed the same for max_fires.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.scheduler.models import BudgetSpec, IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_job
from bridge.scheduler.service import SchedulerService
from bridge.tools.schedule_tool import _PARAMETERS, ScheduleTool

_SET = {
    "budget": BudgetSpec(max_cost_usd_per_day=2.0),
    "artifact": "~/notes/digest.md",
    "timeout_seconds": 600,
    "model_id": "claude-sonnet-5-5",
    "misfire_policy": "fire_once",
}


@pytest.fixture
def service(monkeypatch):
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    return svc


async def _job(service) -> JobRecord:
    return await service.create_job(JobRecord(
        id="sched_hourly", name="hourly", schedule=IntervalSchedule(seconds=3600),
        prompt="check", sinks=[NoopSinkSpec()], **_SET,
    ))


@pytest.mark.parametrize("field", list(_SET))
async def test_the_schedule_tool_clears_a_field_with_null(service, field):
    job = await _job(service)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "update", "job_id": job.id, field: None})

    assert not result.is_error, result.content
    saved = load_job(job.id)
    assert getattr(saved, field) is None
    # The rest stay as they were.
    assert all(getattr(saved, f) == v for f, v in _SET.items() if f != field)


@pytest.mark.parametrize("field", list(_SET))
async def test_a_null_from_the_dashboard_clears_a_field(service, monkeypatch, field):
    import bridge.freyja_bridge as fb

    job = await _job(service)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = SimpleNamespace(scheduler=service, active_session_id=None)

    await fb._handle_command(state, {
        "type": "scheduler.update_job", "id": "req_1", "jobId": job.id,
        "payload": {field: None},
    })

    [response] = [e for e in events if e.get("type") == "scheduler_response"]
    assert "error" not in response, response
    assert getattr(load_job(job.id), field) is None


async def test_the_dashboard_sets_a_budget_and_an_artifact(service, monkeypatch):
    import bridge.freyja_bridge as fb

    job = await _job(service)
    monkeypatch.setattr(fb, "emit", lambda _e: None)
    state = SimpleNamespace(scheduler=service, active_session_id=None)

    await fb._handle_command(state, {
        "type": "scheduler.update_job", "id": "req_1", "jobId": job.id,
        "payload": {"budget": {"max_cost_usd_per_run": 0.5}, "artifact": "~/notes/other.md"},
    })

    saved = load_job(job.id)
    assert saved.budget.max_cost_usd_per_run == 0.5
    assert saved.artifact == "~/notes/other.md"


@pytest.mark.parametrize("field", list(_SET))
def test_the_tool_schema_says_null_clears_each_field(field):
    assert "null" in _PARAMETERS["properties"][field].get("description", "")
