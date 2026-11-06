"""Only the misfire policies the scheduler knows can be saved.

The dashboard's misfire menu offered "run_now", which the scheduler does
not know and silently treated as skip, and it did not offer fire_once or
fire_all. Nothing checked the value on its way in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.scheduler.models import IntervalSchedule, JobPatch, JobRecord, NoopSinkSpec
from bridge.scheduler.persistence import load_job
from bridge.scheduler.service import SchedulerService


def _job(**fields) -> JobRecord:
    return JobRecord(id="", name="digest", schedule=IntervalSchedule(seconds=3600),
                     prompt="digest", sinks=[NoopSinkSpec()], **fields)


async def test_an_unknown_misfire_policy_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    service = SchedulerService(SimpleNamespace())
    job = await service.create_job(_job())

    with pytest.raises(ValueError, match="run_now"):
        await service.update_job(job.id, JobPatch(misfire_policy="run_now"))
    with pytest.raises(ValueError, match="run_now"):
        await service.create_job(_job(misfire_policy="run_now"))

    assert load_job(job.id).misfire_policy is None


async def test_the_dashboard_default_puts_the_policy_back_to_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    service = SchedulerService(SimpleNamespace())
    job = await service.create_job(_job(misfire_policy="fire_all"))

    await service.update_job(job.id, JobPatch(misfire_policy=""))

    assert load_job(job.id).misfire_policy is None
