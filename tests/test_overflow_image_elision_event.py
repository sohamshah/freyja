"""An overflow recovery that only drops old images reports an image prune.

When the provider refuses a request as too large, the overflow cascade first
replaces the oldest images with text markers, with no summary. It reported
that as a compaction_complete with no strategy, so the bridge wrote it as a
"summary" row: the dashboard counted 5 such rows as summaries and as the
runtime's compactions, the conversation showed a "compacted" card, and the
agent was told its work had been relocated to the ledger. The other image
recoveries (oversized images, screenshots, image sizes) report media_pruning
with their own strategy.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from engine.runner import AsyncAgentRunner, RunnerContext
from engine.session import Session
from engine.types import AgentConfig, ImageBlock, Message, TextBlock


@pytest.fixture
def rows(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))  # the session's project directory
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    rows: list[dict] = []
    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = rows.append
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    return rows


class _Provider:
    name = "anthropic"
    model_id = "claude-sonnet-5-5"
    context_window = 200_000


async def test_dropping_old_images_after_an_overflow_is_reported_as_an_image_prune(rows, tmp_path):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    events: list = []

    async def _on_event(ev):
        events.append(ev)

    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(), on_system_event=_on_event)
    session = Session.create(system_prompt="t")
    # Three 2 MiB images: 6 MiB against the 4 MiB the cascade aims for, so
    # the oldest goes.
    for i in range(3):
        session.transcript.append_message(Message(role="user", content=[
            TextBlock(text=f"screenshot {i}"),
            ImageBlock(source_type="base64", data="a" * (2 * 1024 * 1024), media_type="image/png"),
        ]))

    assert await runner._handle_overflow_cascade(session, RunnerContext())

    [event] = events
    assert event.type == "media_pruning"
    assert event.details["trigger"] == "overflow_cascade_image_elision"
    assert event.details["strategy"] == "cumulative_image_prune"
    assert event.details["omitted_images"] == event.details["elided"] == 1

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-elision", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    sess._mirror_compaction_event(event.type, event.details)
    [row] = rows
    assert (row["subtype"], row["mechanism"]) == ("media_pruning", "cumulative_image_prune")
