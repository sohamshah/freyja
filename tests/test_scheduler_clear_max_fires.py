"""A job's max_fires can be cleared.

A None in a JobPatch means "leave unchanged", so emptying max fires on the
dashboard (its edit sends max_fires null to the bridge) or passing null to
the schedule tool left the job's max_fires as it was.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.scheduler.models import IntervalSchedule, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_job
from bridge.scheduler.service import SchedulerService
from bridge.tools.schedule_tool import ScheduleTool


@pytest.fixture
def service(monkeypatch):
    svc = SchedulerService(SimpleNamespace())
    monkeypatch.setattr(svc, "emit_event", lambda **_kw: None)
    return svc


async def _capped(service) -> JobRecord:
    return await service.create_job(JobRecord(
        id="sched_hourly", name="hourly", schedule=IntervalSchedule(seconds=3600),
        prompt="check", sinks=[NoopSinkSpec()], max_fires=5,
    ))


@pytest.mark.parametrize("args, max_fires", [
    ({"max_fires": None}, None),
    ({"max_fires": 8}, 8),
    ({"name": "every hour"}, 5),
])
async def test_the_schedule_tool_clears_max_fires_with_null(service, args, max_fires):
    job = await _capped(service)
    tool = ScheduleTool(service=service, current_session_id="s1")

    result = await tool.execute("call_1", {"action": "update", "job_id": job.id, **args})

    assert not result.is_error, result.content
    assert load_job(job.id).max_fires == max_fires


@pytest.mark.parametrize("payload, max_fires", [
    ({"max_fires": None}, None),
    ({"max_fires": 8}, 8),
    ({"name": "every hour"}, 5),
])
async def test_emptying_max_fires_on_the_dashboard_clears_it(
    service, monkeypatch, tmp_path, payload, max_fires,
):
    import bridge.freyja_bridge as fb

    job = await _capped(service)
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    state = SimpleNamespace(scheduler=service, active_session_id=None)

    await fb._handle_command(state, {
        "type": "scheduler.update_job", "id": "req_1", "jobId": job.id, "payload": payload,
    })

    [response] = [e for e in events if e.get("type") == "scheduler_response"]
    assert "error" not in response, response
    assert response["job"]["max_fires"] == max_fires
    assert load_job(job.id).max_fires == max_fires
