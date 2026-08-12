"""briefing.read verb (bridge/voice/adapters/briefing.py).

Reads the briefer's on-disk edition; tests point FREYJA_HOME at a tmp dir
so nothing touches the real ~/.freyja/briefing.
"""

from __future__ import annotations

import json

import pytest

from bridge.voice.adapters import briefing
from bridge.voice.verbs import VerbRegistry

pytestmark = pytest.mark.asyncio


@pytest.fixture
def reg():
    r = VerbRegistry()
    briefing.register(r)
    return r


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    return tmp_path


def _write(home, date, edition):
    d = home / "briefing" / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "briefing.json").write_text(json.dumps(edition), encoding="utf-8")


_EDITION = {
    "date": "2026-07-08",
    "since_label": "06:00 yesterday",
    "hero": {"projects_in_motion": 3, "events_since": 12},
    "projects": [
        {"name": "Alpha", "state": "ready", "attention": True, "summary": "shipping soon"},
        {"name": "Beta", "state": "quiet", "attention": False, "summary": "idle"},
    ],
    "decisions": [
        {"verb": "approve", "project": "Alpha", "ref": "PR 12", "body": "the fix is ready"},
    ],
    "today": [
        {"time": "09:00", "project": "Alpha", "what": "ship the fix"},
        {"time": "10:00", "project": "Beta", "what": "review"},
    ],
}


async def test_read_todays_edition(reg, home, monkeypatch):
    import time as _t

    monkeypatch.setattr(_t, "localtime", lambda *a: _t.struct_time((2026, 7, 8, 9, 0, 0, 0, 0, 0)))
    _write(home, "2026-07-08", _EDITION)
    res = await reg.get("briefing.read").run({})
    assert res.ok
    assert res.summary == "briefing for 2026-07-08"
    assert res.data["stale"] is False
    # The spoken script carries the hero line, the decision, and today.
    assert "3 projects in motion, 12 events since 06:00 yesterday." in res.say
    assert "approve Alpha (PR 12)" in res.say
    assert "09:00 ship the fix" in res.say
    assert len(res.data["projects"]) == 2 and len(res.data["decisions"]) == 1


async def test_read_falls_back_to_latest_when_today_missing(reg, home, monkeypatch):
    import time as _t

    # "today" is the 9th, but only the 8th exists → offer it, flagged stale.
    monkeypatch.setattr(_t, "localtime", lambda *a: _t.struct_time((2026, 7, 9, 9, 0, 0, 0, 0, 0)))
    _write(home, "2026-07-08", _EDITION)
    res = await reg.get("briefing.read").run({})
    assert res.ok
    assert res.data["stale"] is True
    assert res.say.startswith("(from 2026-07-08, not today) ")


async def test_read_explicit_date(reg, home):
    _write(home, "2026-07-04", {**_EDITION, "date": "2026-07-04"})
    res = await reg.get("briefing.read").run({"date": "2026-07-04"})
    assert res.ok and res.data["date"] == "2026-07-04"


async def test_read_no_briefing_at_all(reg, home):
    res = await reg.get("briefing.read").run({})
    assert not res.ok
    assert res.error == "no_briefing"


async def test_read_singular_counts(reg, home, monkeypatch):
    import time as _t

    monkeypatch.setattr(_t, "localtime", lambda *a: _t.struct_time((2026, 7, 8, 9, 0, 0, 0, 0, 0)))
    _write(
        home,
        "2026-07-08",
        {"date": "2026-07-08", "hero": {"projects_in_motion": 1, "events_since": 1}, "since_label": "your last visit"},
    )
    res = await reg.get("briefing.read").run({})
    assert "1 project in motion, 1 event since your last visit." in res.say
