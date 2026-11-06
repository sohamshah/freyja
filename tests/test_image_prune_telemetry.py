"""An image prune's telemetry row says how many images left the context.

Every image prune's media_pruning event carries omitted_images, and the
renderer's activity panel adds them up as "img trims". The compaction_event
rows the dashboard reads dropped it: none of the 778 media_pruning rows on
disk has one, so the dashboard could not count the trimmed images.
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


async def _image_prune_event():
    events: list = []

    async def _on_event(ev):
        events.append(ev)

    runner = AsyncAgentRunner(_Provider(), config=AgentConfig(), on_system_event=_on_event)
    session = Session.create(system_prompt="t")
    # Three 2 MiB images: 6 MiB against the 4 MiB the overflow cascade aims
    # for, so the oldest goes.
    for i in range(3):
        session.transcript.append_message(Message(role="user", content=[
            TextBlock(text=f"screenshot {i}"),
            ImageBlock(source_type="base64", data="a" * (2 * 1024 * 1024), media_type="image/png"),
        ]))
    assert await runner._handle_overflow_cascade(session, RunnerContext())
    [event] = events
    assert (event.type, event.details["omitted_images"]) == ("media_pruning", 1)
    return event


async def test_a_root_sessions_image_prune_row_counts_the_images(rows, tmp_path):
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    event = await _image_prune_event()
    state = _BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = _BridgeSession("test-img-prune", workspace=str(tmp_path), model_id="claude-sonnet-5-5",
                          reasoning_level="auto", coordination_strategy="bus", state=state)
    sess._append_compaction_log = lambda _row: None
    sess._mirror_compaction_event(event.type, event.details)

    [row] = rows
    assert (row["subtype"], row.get("omitted_images")) == ("media_pruning", 1)


async def test_a_sub_agents_image_prune_row_counts_the_images(rows):
    from bridge.transcript_persistence import append_subagent_compaction

    event = await _image_prune_event()
    append_subagent_compaction("sub-img-prune", model="claude-sonnet-5-5", subtype=event.type,
                               details=event.details, default_trigger="runtime",
                               agent_type="operator", parent_session_id="parent-1")

    [row] = rows
    assert (row["subtype"], row.get("omitted_images")) == ("media_pruning", 1)

