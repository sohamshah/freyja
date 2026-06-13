"""Unit tests for the morning-briefing module (bridge/briefing.py)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import bridge.briefing as briefing


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    return tmp_path


class FakeScheduler:
    def __init__(self, existing=None):
        self.jobs = list(existing or [])
        self.created = []

    async def list_jobs(self, _filt):
        return self.jobs

    async def create_job(self, spec):
        spec.id = spec.id or "sched_test_briefer"
        self.created.append(spec)
        self.jobs.append(spec)
        return spec


def test_prompt_renders_schema_and_escapes(home):
    p = briefing.briefer_prompt()
    assert '"version": 1,' in p                       # schema embedded intact
    assert "({summary, actionsCompleted})" in p        # {{}} escape resolved
    assert "{BRIEFING_SCHEMA_DOC}" not in p            # interpolation happened
    assert "STAGE work" in p                           # execution boundary stated
    # Paths honor FREYJA_HOME (the `home` fixture redirects it).
    assert f"{home}/projects/*/working_memory.json" in p
    assert "~/.freyja/" not in p                       # no hardcoded default home


def test_ensure_briefer_job_creates_once(home):
    sched = FakeScheduler()
    state = SimpleNamespace(scheduler=sched, permission_tier="yolo")
    job_id = asyncio.run(briefing.ensure_briefer_job(state))
    assert job_id == "sched_test_briefer"
    assert len(sched.created) == 1
    spec = sched.created[0]
    assert briefing.BRIEFER_TAG in spec.tags
    assert spec.artifact == str(briefing.briefing_root())
    assert spec.memory.enabled is True
    assert spec.schedule.expression == "0 6 * * *"
    # Pointer mirrored for the renderer.
    ptr = json.loads(briefing.briefer_pointer_path().read_text())
    assert ptr["job_id"] == "sched_test_briefer"


def test_ensure_briefer_job_idempotent_via_tag(home):
    existing = SimpleNamespace(id="sched_existing", tags=[briefing.BRIEFER_TAG])
    sched = FakeScheduler(existing=[existing])
    state = SimpleNamespace(scheduler=sched, permission_tier="low")
    job_id = asyncio.run(briefing.ensure_briefer_job(state))
    assert job_id == "sched_existing"
    assert not sched.created
    ptr = json.loads(briefing.briefer_pointer_path().read_text())
    assert ptr["job_id"] == "sched_existing"


def test_read_briefing_empty_and_populated(home):
    # Empty state.
    res = briefing.read_briefing()
    assert res["dates"] == [] and res["date"] is None
    assert res["json"] is None and res["markdown"] is None

    # Two days on disk; newest wins by default.
    for date, n in (("2026-06-11", 1), ("2026-06-12", 2)):
        d = briefing.briefing_root() / date
        d.mkdir(parents=True)
        (d / "briefing.json").write_text(json.dumps({"version": 1, "date": date, "n": n}))
        (d / "briefing.md").write_text(f"# briefing {date}")
    res = briefing.read_briefing()
    assert res["dates"] == ["2026-06-12", "2026-06-11"]
    assert res["date"] == "2026-06-12" and res["json"]["n"] == 2
    assert "2026-06-12" in res["markdown"]

    # Explicit date selection.
    res = briefing.read_briefing("2026-06-11")
    assert res["date"] == "2026-06-11" and res["json"]["n"] == 1

    # Unknown date falls back to newest.
    res = briefing.read_briefing("2020-01-01")
    assert res["date"] == "2026-06-12"


def test_read_briefing_malformed_json_falls_to_none(home):
    d = briefing.briefing_root() / "2026-06-12"
    d.mkdir(parents=True)
    (d / "briefing.json").write_text("{not json")
    (d / "briefing.md").write_text("narrative survives")
    res = briefing.read_briefing()
    assert res["json"] is None
    assert res["markdown"] == "narrative survives"


def test_list_briefing_dates_ignores_non_date_dirs(home):
    root = briefing.briefing_root()
    (root / "2026-06-12").mkdir(parents=True)
    (root / "not-a-date").mkdir()
    (root / "outputs").mkdir()
    assert briefing.list_briefing_dates() == ["2026-06-12"]
