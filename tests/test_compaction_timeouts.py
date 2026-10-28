"""Compaction must not be held hostage by a hung best-effort call.

2026-10-05: a stuck working-memory call (Call B) kept a Slack thread silent
for 5.7 minutes, until the Anthropic SDK's 300 s timeout fired and retried.
Call B is best-effort, so it gets a short deadline and is dropped.
"""

from __future__ import annotations

import threading
import time

import pytest

from engine import compaction as comp
from engine.compaction import SummaryCompaction


def _run(monkeypatch, *, summary, extract, wm_timeout=0.2, sum_timeout=2.0):
    monkeypatch.setattr(comp, "WORKING_MEMORY_CALL_TIMEOUT_S", wm_timeout)
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_S", sum_timeout)
    c = SummaryCompaction()
    monkeypatch.setattr(c, "_generate_summary", summary)
    monkeypatch.setattr(c, "_extract_working_memory", extract)
    upserts: list = []
    t0 = time.monotonic()
    result = c._run_summary_and_working_memory(
        "conversation", object(), on_working_memory_upserts=upserts.append
    )
    return result, upserts, time.monotonic() - t0


def test_hung_working_memory_call_is_dropped(monkeypatch):
    release = threading.Event()

    def summary(*a, **k):
        return "the summary"

    def extract(*a, **k):
        release.wait(30)  # a hung provider call
        return {"summary": "late"}

    try:
        result, upserts, elapsed = _run(monkeypatch, summary=summary, extract=extract)
    finally:
        release.set()

    assert result == "the summary"
    # The sink still runs, with None, so the ledger refresh happens.
    assert upserts == [None]
    # Bounded by the Call B deadline, not by the hung worker.
    assert elapsed < 2.0


def test_hung_summary_call_raises_timeout(monkeypatch):
    release = threading.Event()

    def summary(*a, **k):
        release.wait(30)
        return "late"

    def extract(*a, **k):
        return {"summary": "ok"}

    try:
        with pytest.raises(TimeoutError):
            _run(monkeypatch, summary=summary, extract=extract, sum_timeout=0.3)
    finally:
        release.set()


def test_fast_calls_unchanged(monkeypatch):
    result, upserts, _ = _run(
        monkeypatch,
        summary=lambda *a, **k: "s",
        extract=lambda *a, **k: {"summary": "wm"},
    )
    assert result == "s"
    assert upserts == [{"summary": "wm"}]


# ─── the summary deadline grows with the transcript ──────────────────


def test_summary_deadline_grows_with_the_transcript(monkeypatch):
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_S", 180.0)
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_PER_KTOKEN_S", 1.0)
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_MAX_S", 600.0)
    assert comp.summary_call_timeout_s(None) == 180.0
    assert comp.summary_call_timeout_s(0) == 180.0
    # The DM transcript that failed twice at 180 s: 197k tokens, and the
    # summary answered about 235 s in.
    assert comp.summary_call_timeout_s(197_403) == pytest.approx(377.4, abs=0.1)
    assert comp.summary_call_timeout_s(2_000_000) == 600.0


def _sized_run(monkeypatch, size_tokens):
    # Floor 0.2 s, plus 0.5 s per 1k tokens: a 1k-token transcript gets 0.7 s.
    monkeypatch.setattr(comp, "WORKING_MEMORY_CALL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(comp, "SUMMARY_CALL_TIMEOUT_PER_KTOKEN_S", 0.5)
    c = SummaryCompaction()

    def summary(*a, **k):
        time.sleep(0.4)
        return "in time"

    monkeypatch.setattr(c, "_generate_summary", summary)
    monkeypatch.setattr(c, "_extract_working_memory", lambda *a, **k: {"summary": "wm"})
    return c._run_summary_and_working_memory(
        "conversation", object(), on_working_memory_upserts=lambda _u: None,
        size_tokens=size_tokens,
    )


def test_a_big_transcript_gets_its_longer_deadline(monkeypatch):
    assert _sized_run(monkeypatch, 1_000) == "in time"


def test_the_floor_still_applies_without_a_size(monkeypatch):
    with pytest.raises(TimeoutError, match="exceeded 0s"):
        _sized_run(monkeypatch, None)
