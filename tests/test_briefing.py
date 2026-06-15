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
        self.updated = []  # (job_id, patch)

    async def list_jobs(self, _filt):
        return self.jobs

    async def create_job(self, spec):
        spec.id = spec.id or "sched_test_briefer"
        self.created.append(spec)
        self.jobs.append(spec)
        return spec

    async def update_job(self, job_id, patch):
        self.updated.append((job_id, patch))
        for j in self.jobs:
            if getattr(j, "id", None) == job_id and patch.prompt is not None:
                j.prompt = patch.prompt
        return next((j for j in self.jobs if getattr(j, "id", None) == job_id), None)


def _seed_sessions(home, sessions):
    """sessions: iterable of (session_id, updated_ms, title) → _index.json."""
    sdir = home / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "_index.json").write_text(json.dumps({
        "version": 1,
        "updatedAt": max((u for _, u, _ in sessions), default=0),
        "sessions": [{"id": sid, "updatedAt": u, "title": t} for sid, u, t in sessions],
    }))


def _seed_wm(home, session_id, summary, open_threads=0):
    pdir = home / "projects" / session_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "working_memory.json").write_text(json.dumps({
        "overview": {"summary": summary, "actionsCompleted": []},
        "open_threads": [{"status": "open"} for _ in range(open_threads)],
    }))


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


# ─── Recency shortlist (the fix) ───────────────────────────────────────

NOW = 1_781_528_400.0  # a fixed "now" in epoch seconds
H = 3600.0


def test_recency_shortlist_orders_and_excludes_subagents(home):
    _seed_sessions(home, [
        ("sub_xxx_1",          int((NOW - 1 * H) * 1000), "explore [sub]"),   # newest but excluded
        ("comp_yyy",           int((NOW - 2 * H) * 1000), "compaction"),       # excluded
        ("scheduler:ephem",    int((NOW - 1 * H) * 1000), "sched run"),        # excluded
        ("session-fresh",      int((NOW - 3 * H) * 1000), "Fresh work"),
        ("desktop-older",      int((NOW - 30 * H) * 1000), "Older work"),
    ])
    out = briefing.recency_shortlist(now=NOW, since=NOW - 48 * H)
    ids = [sid for _, sid, _ in out]
    assert ids[0] == "session-fresh"            # newest non-subagent first
    assert "session-fresh" in ids and "desktop-older" in ids
    assert not any(i.startswith(("sub_", "comp_", "scheduler")) for i in ids)


def test_recency_shortlist_always_includes_latest_even_when_all_stale(home):
    # Every session is far older than the `since` window — the invariant
    # is that the most-recent non-subagent session is NEVER dropped.
    _seed_sessions(home, [
        ("session-a", int((NOW - 200 * H) * 1000), "A"),
        ("session-b", int((NOW - 100 * H) * 1000), "B"),  # the most recent
        ("session-c", int((NOW - 300 * H) * 1000), "C"),
    ])
    out = briefing.recency_shortlist(now=NOW, since=NOW - 1 * H)  # window catches nothing
    ids = [sid for _, sid, _ in out]
    assert ids[0] == "session-b"
    assert len(ids) >= min(3, 3)


def test_render_recency_block_inlines_overview_and_flags_missing_wm(home):
    _seed_sessions(home, [
        ("session-pwc",  int((NOW - 5 * H) * 1000), "PWC Eval Report"),
        ("session-bare", int((NOW - 6 * H) * 1000), "No WM yet"),
    ])
    _seed_wm(home, "session-pwc", "Prepped all 5 client questions for Thursday sync.", open_threads=3)
    # session-bare has NO working_memory.json on purpose.
    block = briefing.render_recency_block(now=NOW)
    assert "Sessions active since your last briefing" in block
    assert "session-pwc" in block
    assert "Prepped all 5 client questions" in block       # overview inlined
    assert "3 open thread(s)" in block                      # open-thread count surfaced
    assert "session-bare" in block                          # listed despite missing WM
    assert "not yet summarized" in block                    # and flagged for a ledger read


def test_fire_context_block_only_fires_for_briefer(home):
    _seed_sessions(home, [("session-x", int((NOW - 2 * H) * 1000), "X")])
    briefer_job = SimpleNamespace(tags=[briefing.BRIEFER_TAG, "system"])
    other_job = SimpleNamespace(tags=["weekly-report"])
    assert briefing.fire_context_block(other_job) == ""
    out = briefing.fire_context_block(briefer_job)
    assert "session-x" in out and "READ EVERY ONE" in out


def test_prompt_points_at_the_injected_block(home):
    p = briefing.briefer_prompt()
    assert "authoritative reading list" in p
    assert "Sessions active since your last briefing" in p


def test_ensure_briefer_job_refreshes_stale_prompt(home):
    existing = SimpleNamespace(id="sched_existing", tags=[briefing.BRIEFER_TAG],
                               prompt="OUTDATED PROMPT FROM OLD CODE")
    sched = FakeScheduler(existing=[existing])
    state = SimpleNamespace(scheduler=sched, permission_tier="yolo")
    job_id = asyncio.run(briefing.ensure_briefer_job(state))
    assert job_id == "sched_existing"
    assert len(sched.updated) == 1                          # patched once
    assert sched.updated[0][0] == "sched_existing"
    assert sched.updated[0][1].prompt == briefing.briefer_prompt()
    # Idempotent: a second boot with the now-current prompt is a no-op.
    sched.updated.clear()
    asyncio.run(briefing.ensure_briefer_job(state))
    assert sched.updated == []
