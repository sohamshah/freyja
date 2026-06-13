"""Unit tests for the offline working-memory backfill pass.

Covers the scan's eligibility/staleness logic, the shared apply
projection, the per-session extraction round-trip with a fake provider,
and the attempt-marker debounce — all against a temp FREYJA_HOME so no
real session state is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge.wm_backfill as wb
from bridge.working_memory import WorkingMemory, apply_wm_result


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect every storage root the backfill touches into tmp_path."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path))
    monkeypatch.setattr(wb, "_sessions_dir", lambda: sessions)
    import bridge.project_paths as pp

    monkeypatch.setattr(
        pp, "project_output_dir",
        lambda sid: projects / pp.safe_session_id(sid),
    )
    return SimpleNamespace(root=tmp_path, sessions=sessions, projects=projects)


def _write_transcript(
    home,
    session_id: str,
    *,
    n_messages: int = 4,
    last_activity: float | None = None,
    text: str = (
        "We discussed the scheduler design at length and decided to use "
        "per-job fcntl flocks for cross-process coordination, because the "
        "kernel releases them automatically when the holding process exits "
        "— even on SIGKILL — which removes the whole stale-lock recovery "
        "problem class from the design."
    ),
):
    """Write a minimal version-1 transcript the engine can round-trip."""
    entries = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        entries.append({
            "id": f"e{i}",
            "is_compaction": False,
            "timestamp": time.time() - 1000 + i,
            "message": {"role": role, "content": f"{text} (turn {i})"},
        })
    data = {
        "version": 1,
        "session_id": session_id,
        "created_at": time.time() - 2000,
        "last_activity": last_activity if last_activity is not None else time.time() - 7200,
        "compaction_count": 0,
        "tool_tokens": 0,
        "metadata": {},
        "transcript": {"entries": entries, "head_id": entries[-1]["id"] if entries else ""},
    }
    safe = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in session_id)[:160]
    path = home.sessions / f"{safe}.transcript.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class FakeStructuredProvider:
    """Provider double exposing complete_structured the way the
    extraction expects: async, returns .data + .usage."""

    model_id = "fake-haiku"
    name = "fake"

    def __init__(self, result: dict | None):
        self._result = result
        self.calls = 0

    async def complete_structured(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            data=self._result,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


WM_RESULT = {
    "summary": "Built the scheduler flock layer.",
    "actions_completed": ["added FileLock", "wired owner lock"],
    "entities": [
        {"type": "workstream", "title": "Scheduler hardening", "status": "active"},
        {
            "type": "decision",
            "title": "Use per-job fcntl flocks",
            "rationale": "kernel releases on crash",
            "workstream": "Scheduler hardening",
        },
        {
            "type": "open_thread",
            "text": "misfire_policy still unimplemented",
            "workstream": "Scheduler hardening",
        },
    ],
}


# ─── Scan eligibility ──────────────────────────────────────────────────


def test_scan_skips_subagents_and_small_and_active(home):
    _write_transcript(home, "sub_abc123_1")                       # prefix skip
    _write_transcript(home, "comp_xyz_2")                          # prefix skip
    _write_transcript(home, "scheduler_job.ephemeral")             # prefix skip
    _write_transcript(home, "session-boot")                        # id skip
    _write_transcript(home, "desktop-tiny", n_messages=1)          # too small
    _write_transcript(home, "desktop-live", last_activity=time.time())  # active
    _write_transcript(home, "desktop-good")                        # eligible

    report = wb.scan_sessions()
    ids = [c.session_id for c in report.candidates]
    assert ids == ["desktop-good"]
    assert report.skipped_prefix == 4
    assert report.skipped_small == 1
    assert report.skipped_active == 1


def test_scan_reason_missing_vs_stale_vs_fresh(home):
    # missing — no working_memory.json at all
    _write_transcript(home, "desktop-missing")

    # stale — overview older than the transcript's last activity
    last = time.time() - 7200
    _write_transcript(home, "desktop-stale", last_activity=last)
    stale_dir = home.projects / "desktop-stale"
    wm = WorkingMemory(session_id="desktop-stale", project_dir=stale_dir)
    wm.ensure()
    wm.set_overview(summary="old summary", actions_completed=[])
    doc = json.loads((stale_dir / "working_memory.json").read_text())
    doc["overview"]["updatedAt"] = int((last - 3600) * 1000)
    (stale_dir / "working_memory.json").write_text(json.dumps(doc))

    # fresh — overview newer than last activity
    _write_transcript(home, "desktop-fresh", last_activity=time.time() - 7200)
    fresh_dir = home.projects / "desktop-fresh"
    wm2 = WorkingMemory(session_id="desktop-fresh", project_dir=fresh_dir)
    wm2.ensure()
    wm2.set_overview(summary="current", actions_completed=["x"])

    report = wb.scan_sessions()
    by_id = {c.session_id: c.reason for c in report.candidates}
    assert by_id == {"desktop-missing": "missing", "desktop-stale": "stale"}
    assert report.skipped_fresh == 1


def test_scan_respects_failure_marker_until_retry_window(home):
    tpath = _write_transcript(home, "desktop-marked")
    data = json.loads(tpath.read_text())
    proj = home.projects / "desktop-marked"
    proj.mkdir(parents=True, exist_ok=True)
    wb._write_marker(proj, {
        "attempted_at": time.time() - 60,            # just attempted
        "status": "failed",
        "transcript_last_activity": data["last_activity"],
    })
    report = wb.scan_sessions()
    assert not report.candidates
    assert report.skipped_marker == 1

    # After the retry window the candidate reappears.
    wb._write_marker(proj, {
        "attempted_at": time.time() - wb.RETRY_AFTER_SECONDS - 10,
        "status": "failed",
        "transcript_last_activity": data["last_activity"],
    })
    report = wb.scan_sessions()
    assert [c.session_id for c in report.candidates] == ["desktop-marked"]


def test_scan_transcript_change_overrides_marker(home):
    tpath = _write_transcript(home, "desktop-moved")
    proj = home.projects / "desktop-moved"
    proj.mkdir(parents=True, exist_ok=True)
    wb._write_marker(proj, {
        "attempted_at": time.time() - 60,
        "status": "failed",
        "transcript_last_activity": 12345.0,          # ≠ current
    })
    report = wb.scan_sessions()
    assert [c.session_id for c in report.candidates] == ["desktop-moved"]


# ─── Shared apply projection ───────────────────────────────────────────


def test_apply_wm_result_dict_shape(tmp_path):
    wm = WorkingMemory(session_id="s", project_dir=tmp_path)
    wm.ensure()
    counts = apply_wm_result(wm, WM_RESULT)
    assert counts["overview"] == 1
    assert counts["upserts"] == 3
    ov = wm.overview()
    assert ov["summary"] == "Built the scheduler flock layer."
    assert ov["actionsCompleted"] == ["added FileLock", "wired owner lock"]
    ws = wm.list(type="workstream")
    assert len(ws) == 1 and ws[0]["title"] == "Scheduler hardening"
    threads = wm.list(type="open_thread")
    assert threads and threads[0]["workstreamId"] == ws[0]["id"]


def test_apply_wm_result_legacy_list_and_garbage(tmp_path):
    wm = WorkingMemory(session_id="s", project_dir=tmp_path)
    wm.ensure()
    counts = apply_wm_result(
        wm, [{"type": "workstream", "title": "Legacy"}, "noise", 42],
    )
    assert counts == {"overview": 0, "upserts": 1}
    assert wm.overview() is None
    assert apply_wm_result(wm, None) == {"overview": 0, "upserts": 0}
    assert apply_wm_result(wm, "garbage") == {"overview": 0, "upserts": 0}


# ─── Per-session round trip ────────────────────────────────────────────


def test_backfill_session_roundtrip(home):
    _write_transcript(home, "desktop-rt")
    report = wb.scan_sessions()
    [cand] = report.candidates

    provider = FakeStructuredProvider(WM_RESULT)
    res = wb.backfill_session(cand, provider)
    assert res["status"] == "ok"
    assert res["overview"] == 1
    assert res["upserts"] == 3
    assert provider.calls == 1

    # WM doc actually persisted with the overview + entities.
    doc = json.loads(
        (home.projects / "desktop-rt" / "working_memory.json").read_text()
    )
    assert doc["overview"]["summary"] == "Built the scheduler flock layer."
    assert any(
        e.get("type") == "decision" for e in doc["entities"].values()
    )

    # Marker recorded; a re-scan now sees the session as fresh.
    marker = wb._read_marker(home.projects / "desktop-rt")
    assert marker["status"] == "ok"
    report2 = wb.scan_sessions()
    assert not report2.candidates


def test_backfill_session_empty_result_writes_marker(home):
    _write_transcript(home, "desktop-empty")
    [cand] = wb.scan_sessions().candidates
    res = wb.backfill_session(cand, FakeStructuredProvider(None))
    assert res["status"] == "empty"
    marker = wb._read_marker(home.projects / "desktop-empty")
    assert marker["status"] == "empty"
    # Marker suppresses an immediate retry.
    assert not wb.scan_sessions().candidates


def test_vacuous_result_writes_empty_marker_not_ok(home):
    """A Call B result with empty summary + inapplicable entities applies
    nothing — the marker must say 'empty' (retryable after the window),
    never 'ok' (which would strand the session forever)."""
    _write_transcript(home, "desktop-vacuous")
    [cand] = wb.scan_sessions().candidates
    vacuous = {"summary": "", "actions_completed": [], "entities": []}
    res = wb.backfill_session(cand, FakeStructuredProvider(vacuous))
    assert res["status"] == "empty"
    marker = wb._read_marker(home.projects / "desktop-vacuous")
    assert marker["status"] == "empty"


def test_ok_marker_not_trusted_when_wm_missing(home):
    """An ok marker contradicted by a demonstrably-missing WM file must
    not suppress retries past the retry window (covers external deletion
    / swallowed save failures)."""
    tpath = _write_transcript(home, "desktop-clobbered")
    data = json.loads(tpath.read_text())
    proj = home.projects / "desktop-clobbered"
    proj.mkdir(parents=True, exist_ok=True)
    # ok marker at the current transcript state — but no WM file at all.
    wb._write_marker(proj, {
        "attempted_at": time.time() - wb.RETRY_AFTER_SECONDS - 10,
        "status": "ok",
        "transcript_last_activity": data["last_activity"],
    })
    report = wb.scan_sessions()
    assert [c.session_id for c in report.candidates] == ["desktop-clobbered"]
    # Within the window it's still suppressed (no hammering)…
    wb._write_marker(proj, {
        "attempted_at": time.time() - 60,
        "status": "ok",
        "transcript_last_activity": data["last_activity"],
    })
    assert not wb.scan_sessions().candidates


def test_broken_transcript_writes_failed_marker(home):
    """A transcript that fails deserialization must get a failed marker
    so it can't starve the pass budget by re-attempting every hour."""
    tpath = _write_transcript(home, "desktop-broken")
    # Corrupt an entry so TranscriptManager.from_dict raises (message
    # present — passes the scan's shallow check — but missing 'role').
    data = json.loads(tpath.read_text())
    data["transcript"]["entries"][0]["message"] = {"content": "no role key"}
    tpath.write_text(json.dumps(data))
    [cand] = wb.scan_sessions().candidates
    res = wb.backfill_session(cand, FakeStructuredProvider(WM_RESULT))
    assert res["status"] == "failed"
    marker = wb._read_marker(home.projects / "desktop-broken")
    assert marker["status"] == "failed"
    # Marker now suppresses the immediate retry.
    assert not wb.scan_sessions().candidates


def test_live_session_deferred_at_fire_time(home):
    """A session that went live between scan and fire is skipped without
    a marker (retry next pass once idle)."""
    tpath = _write_transcript(home, "desktop-wentlive")
    [cand] = wb.scan_sessions().candidates
    # Session goes live AFTER the scan.
    data = json.loads(tpath.read_text())
    data["last_activity"] = time.time()
    tpath.write_text(json.dumps(data))
    provider = FakeStructuredProvider(WM_RESULT)
    res = wb.backfill_session(cand, provider)
    assert res["status"] == "empty"
    assert "went live" in (res.get("error") or "")
    assert provider.calls == 0
    assert wb._read_marker(home.projects / "desktop-wentlive") is None


def test_backfill_session_dry_run_makes_no_calls(home):
    _write_transcript(home, "desktop-dry")
    [cand] = wb.scan_sessions().candidates
    provider = FakeStructuredProvider(WM_RESULT)
    res = wb.backfill_session(cand, provider, dry_run=True)
    assert res["status"] == "dry_run"
    assert provider.calls == 0
    assert not (home.projects / "desktop-dry" / "working_memory.json").exists()


# ─── Pass runner ───────────────────────────────────────────────────────


def test_run_backfill_pass_dry_run_reports_scan(home):
    _write_transcript(home, "desktop-a")
    _write_transcript(home, "desktop-b")
    report = wb.run_backfill_pass(limit=10, dry_run=True)
    assert report["status"] == "ok"
    assert report["scan"]["candidates"] == 2
    assert all(r["status"] == "dry_run" for r in report["results"])


def test_run_backfill_pass_respects_limit(home, monkeypatch):
    for i in range(5):
        _write_transcript(home, f"desktop-l{i}")
    import engine.providers as providers

    monkeypatch.setattr(
        providers, "create_provider",
        lambda *a, **k: FakeStructuredProvider(WM_RESULT),
    )
    report = wb.run_backfill_pass(limit=2)
    assert len(report["results"]) == 2
    assert all(r["status"] == "ok" for r in report["results"])
    # The remaining three are still candidates on the next scan.
    assert wb.scan_sessions().candidates and len(wb.scan_sessions().candidates) == 3
