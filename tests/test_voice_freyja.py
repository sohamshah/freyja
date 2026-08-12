"""freyja.* verbs (bridge/voice/adapters/freyja_query.py).

Read-only over the operator's own Freyja state: the session index, each
session's working memory, and the latest briefing. Everything points
FREYJA_HOME at a tmp tree so no test touches the real home; the briefing
read goes through bridge.briefing.read_briefing (which honors
FREYJA_HOME) against a fixture briefing.json.
"""

from __future__ import annotations

import json
import time

import pytest

from bridge.voice.adapters import freyja_query
from bridge.voice.verbs import VerbRegistry


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp FREYJA_HOME with sessions/ and projects/ scaffolding."""
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "projects").mkdir()
    return tmp_path


def _write_index(home, sessions):
    (home / "sessions" / "_index.json").write_text(
        json.dumps({"version": 1, "updatedAt": 0, "sessions": sessions}),
        encoding="utf-8",
    )


def _write_wm(home, session_id, summary):
    d = home / "projects" / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "working_memory.json").write_text(
        json.dumps({"overview": {"summary": summary}}), encoding="utf-8"
    )


def _write_briefing(home, date, projects):
    day = home / "briefing" / date
    day.mkdir(parents=True, exist_ok=True)
    (day / "briefing.json").write_text(
        json.dumps({"version": 1, "date": date, "projects": projects}),
        encoding="utf-8",
    )


def _reg():
    reg = VerbRegistry()
    freyja_query.register(reg)
    return reg


async def _run(verb, args=None):
    return await _reg().get(verb).run(dict(args or {}))


# ── freyja.sessions ────────────────────────────────────────────────────────


async def test_sessions_newest_first_capped_with_preview(home):
    now = time.time() * 1000
    _write_index(
        home,
        [
            {"id": "s-old", "title": "Older work", "updatedAt": now - 3 * 86400 * 1000},
            {"id": "s-new", "title": "Fresh thing", "updatedAt": now - 120 * 1000},
            {"id": "s-mid", "title": "Middle", "updatedAt": now - 3600 * 1000},
        ],
    )
    _write_wm(home, "s-new", "Wiring the galdr voice rework " * 8)

    res = await _run("freyja.sessions")
    assert res.ok
    sessions = res.data["sessions"]
    assert [s["sessionId"] for s in sessions] == ["s-new", "s-mid", "s-old"]
    assert sessions[0]["title"] == "Fresh thing"
    # Relative times.
    assert sessions[0]["updatedAt"] == "2m ago"
    assert sessions[1]["updatedAt"] == "1h ago"
    assert sessions[2]["updatedAt"] == "3d ago"
    # Working-memory preview only where present, truncated.
    assert "preview" in sessions[0] and sessions[0]["preview"].endswith("…")
    assert "preview" not in sessions[1]
    assert res.summary == "3 recent sessions"


async def test_sessions_filters_subagents_and_scheduler(home):
    now = time.time() * 1000
    _write_index(
        home,
        [
            {"id": "sub_abc_1", "title": "sub agent", "updatedAt": now},
            {"id": "comp_xyz_2", "title": "compaction", "updatedAt": now},
            {"id": "scheduler:job", "title": "sched", "updatedAt": now},
            {"id": "real-one", "title": "Real session", "updatedAt": now - 1000},
        ],
    )
    res = await _run("freyja.sessions")
    assert [s["sessionId"] for s in res.data["sessions"]] == ["real-one"]


async def test_sessions_respects_limit_and_cap(home):
    now = time.time() * 1000
    _write_index(
        home,
        [{"id": f"s{i}", "title": f"t{i}", "updatedAt": now - i * 1000} for i in range(20)],
    )
    res = await _run("freyja.sessions", {"limit": 3})
    assert len(res.data["sessions"]) == 3
    # Cap at 8 even when asked for more.
    res = await _run("freyja.sessions", {"limit": 50})
    assert len(res.data["sessions"]) == 8


async def test_sessions_empty_when_no_index(home):
    res = await _run("freyja.sessions")
    assert res.ok
    assert res.data["sessions"] == []
    assert res.summary == "no recent sessions"


async def test_sessions_survives_corrupt_index(home):
    (home / "sessions" / "_index.json").write_text("{not json", encoding="utf-8")
    res = await _run("freyja.sessions")
    assert res.ok and res.data["sessions"] == []


# ── freyja.project_status ──────────────────────────────────────────────────


async def test_project_status_exact_and_fuzzy_match(home):
    _write_briefing(
        home,
        "2026-07-09",
        [
            {
                "name": "Galdr voice",
                "state": "in_motion",
                "attention": True,
                "summary": "Visual computer control landed; tests green.",
                "session_id": "voice-abc",
            },
            {
                "name": "Release ops",
                "state": "quiet",
                "attention": False,
                "summary": "Nothing new.",
                "session_id": None,
            },
        ],
    )
    # Exact-ish.
    res = await _run("freyja.project_status", {"name": "galdr voice"})
    assert res.ok
    assert res.data["name"] == "Galdr voice"
    assert res.data["state"] == "in_motion"
    assert res.data["attention"] is True
    assert res.data["sessionId"] == "voice-abc"
    assert "Visual computer control" in res.summary

    # Fuzzy: shared word finds it without an exact string.
    res = await _run("freyja.project_status", {"name": "the release project"})
    assert res.ok
    assert res.data["name"] == "Release ops"
    # A null session_id is dropped, not surfaced as the string "None".
    assert "sessionId" not in res.data


async def test_project_status_no_match_lists_known(home):
    _write_briefing(
        home,
        "2026-07-09",
        [{"name": "Galdr voice", "state": "in_motion", "summary": "x", "session_id": None}],
    )
    res = await _run("freyja.project_status", {"name": "quantum teleporter"})
    assert res.ok is False
    assert "quantum teleporter" in res.summary
    assert res.data["projects"] == ["Galdr voice"]


async def test_project_status_no_briefing(home):
    res = await _run("freyja.project_status", {"name": "anything"})
    assert res.ok is False
    assert res.data == {"setup": "briefing"}


async def test_project_status_requires_name(home):
    res = await _run("freyja.project_status", {"name": "  "})
    assert res.ok is False
    assert res.error == "missing_name"


# ── registration ───────────────────────────────────────────────────────────


def test_registers_read_only_verbs():
    reg = _reg()
    names = {v.name for v in reg.all()}
    assert names == {"freyja.sessions", "freyja.project_status"}
    for v in reg.all():
        assert v.tier == "auto"  # read-only, never a confirm


# ── the relative-time helper ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "age_s,expected",
    [
        (10, "just now"),
        (44, "just now"),
        (90, "2m ago"),
        (3599, "60m ago"),
        (3600, "1h ago"),
        (7200, "2h ago"),
        (86400, "1d ago"),
        (3 * 86400, "3d ago"),
    ],
)
def test_relative_time(age_s, expected):
    now = 1_000_000_000_000.0
    assert freyja_query._relative_time(now - age_s * 1000, now_ms=now) == expected
