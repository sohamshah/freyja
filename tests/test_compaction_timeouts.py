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
