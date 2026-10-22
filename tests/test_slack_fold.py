"""Sealed messages fold their step cards into one collapsed plan block.

A long turn used to leave a thread of card rows: the thinking and
every tool call as its own ``task_card``, dozens of rows between two
paragraphs. Slack keeps a sealed streamed message as ordinary blocks
and lets ``chat.update`` replace them, and a ``plan`` block holds up to
50 tasks collapsed under one title, one plan per message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.gateway import stream_consumer as sc
from bridge.gateway.platforms.base import MessageSource
from bridge.gateway.slack_fold import PLAN_TASK_LIMIT, fold_task_cards, fold_title, step_name
from bridge.gateway.stream_consumer import SlackStreamConsumer
from tests.slack_fakes import StreamingFakeAdapter


def _text(text: str, block_id: str = "p") -> dict[str, Any]:
    return {
        "type": "rich_text", "block_id": block_id,
        "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}],
    }


def _card(task_id: str, title: str, status: str = "complete", *, output: str | None = None,
          sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "task_card", "block_id": f"task-{task_id}", "task_id": task_id,
        "title": title, "status": status,
    }
    if output is not None:
        card["output"] = _text(output, "o")
    if sources:
        card["sources"] = sources
    return card


_FEEDBACK = {
    "type": "context_actions", "block_id": "fb", "elements": [{"type": "feedback_buttons"}],
}


# ── the pure fold ──


def test_cards_fold_into_one_plan_where_the_first_card_was():
    blocks = [
        _text("Looking.", "a"),
        _card("t1", "bash: Run the tests", output="1 failed"),
        _card("th", "Thinking", output="The second file matters."),
        _card("t2", "bash: Find the caller", "error", output="no such file",
              sources=[{"type": "url", "url": "https://x.test", "text": "x"}]),
        _text("Found it.", "b"),
        _card("t3", "read_file: /tmp/a.py"),
        _text("Done.", "c"),
        _FEEDBACK,
    ]
    folded = fold_task_cards(blocks)
    assert folded is not None
    assert [b["type"] for b in folded] == [
        "rich_text", "plan", "rich_text", "rich_text", "context_actions",
    ]
    assert all("block_id" not in b for b in folded)
    plan = folded[1]
    assert plan["title"] == "4 steps, 1 failed (bash ×2, Thinking, read_file)"
    assert [t["task_id"] for t in plan["tasks"]] == ["t1", "th", "t2", "t3"]
    assert [t["status"] for t in plan["tasks"]] == ["complete", "complete", "error", "complete"]
    assert plan["tasks"][0]["output"]["elements"][0]["elements"][0]["text"] == "1 failed"
    assert "block_id" not in plan["tasks"][0]["output"]
    assert plan["tasks"][2]["sources"][0]["url"] == "https://x.test"
    assert "output" not in plan["tasks"][3]
    # The prose and the feedback buttons are the same blocks, in order.
    assert folded[2]["elements"] == blocks[4]["elements"]
    assert folded[4]["elements"] == _FEEDBACK["elements"]


def test_a_message_without_cards_is_left_alone():
    assert fold_task_cards([_text("Only prose."), _FEEDBACK]) is None
    assert fold_task_cards([]) is None


def test_past_fifty_steps_the_last_task_stands_for_the_rest():
    blocks = [_text("Go.")] + [
        _card(f"t{i}", f"bash: step {i}", "error" if i == 57 else "complete") for i in range(60)
    ]
    folded = fold_task_cards(blocks)
    assert folded is not None
    plan = folded[1]
    assert len(plan["tasks"]) == PLAN_TASK_LIMIT
    assert [t["task_id"] for t in plan["tasks"][:3]] == ["t0", "t1", "t2"]
    last = plan["tasks"][-1]
    assert last["task_id"] == "fold-overflow"
    assert last["title"] == "11 more steps, 1 failed"
    assert last["status"] == "error"
    listed = last["output"]["elements"][0]["elements"]
    titles = [item["elements"][0]["text"] for item in listed]
    assert titles[:2] == ["bash: step 49", "bash: step 50"]
    assert len(listed) == 11
    assert plan["title"] == "60 steps, 1 failed (bash ×60)"


def test_keep_output_off_leaves_titles_and_sources_only():
    blocks = [
        _card("t1", "web_fetch: https://x.test", output="a page",
              sources=[{"type": "url", "url": "https://x.test", "text": "x"}]),
    ]
    folded = fold_task_cards(blocks, keep_output=False)
    assert folded is not None
    task = folded[0]["tasks"][0]
    assert "output" not in task and "details" not in task
    assert task["sources"][0]["url"] == "https://x.test"


def test_an_unknown_status_becomes_complete():
    folded = fold_task_cards([_card("t1", "bash: x", "completed")])
    assert folded is not None
    assert folded[0]["tasks"][0]["status"] == "complete"


def test_step_names_and_the_title():
    assert step_name("bash: Inspect the repo `git status`") == "bash"
    assert step_name("mcp__slack__slack_read_thread: x=1") == "mcp__slack__slack_read_thread"
    assert step_name("Thinking") == "Thinking"
    assert step_name("sub_agent: [explore] — map the harness") == "sub_agent"
    assert step_name("Two words: with a colon") == "Two"
    cards = [_card(f"t{i}", t) for i, t in enumerate(
        ["bash: a", "bash: b", "read_file: c", "Thinking", "web_fetch: d", "tasks: e"],
    )]
    assert fold_title(cards) == "6 steps (bash ×2, read_file, Thinking, more)"
    assert fold_title([_card("t", "bash: a")]) == "1 step (bash)"


# ── the consumer ──


def _source() -> MessageSource:
    return MessageSource(
        platform="slack", workspace_id="T1", chat_id="D1", chat_type="dm",
        user_id="U1", message_id="10.1", thread_id=None,
    )


def _consumer(adapter: Any) -> SlackStreamConsumer:
    c = SlackStreamConsumer(
        adapter, _source(), session_key="freyja:slack:T1:dm:D1", verbosity="all",
    )
    c.edit_interval_ms = 0
    return c


async def _feed(consumer: SlackStreamConsumer, events: list[dict[str, Any]]) -> None:
    for ev in events:
        await consumer._handle_event_async(ev)


def _turn(tool_ids: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "stream_start"}, {"type": "text_delta", "text": "Checking.\n\n"},
    ]
    for tid in tool_ids:
        events += [
            {"type": "tool_use_start", "id": tid, "name": "bash"},
            {"type": "tool_input_end", "id": tid, "arguments": {"command": f"echo {tid}"}},
            {"type": "tool_result", "id": tid, "preview": f"{tid} ok"},
        ]
    events += [{"type": "text_delta", "text": "All green."}, {"type": "turn_complete"}]
    return events


@pytest.mark.asyncio
async def test_the_sealed_message_folds_its_cards_into_one_plan():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    await _feed(consumer, _turn(["t1", "t2"]))
    assert consumer.finalized
    stored = adapter.stored_message("stream-ts")
    assert [b["type"] for b in stored["blocks"]] == [
        "rich_text", "task_card", "task_card", "rich_text", "context_actions",
    ]
    update = adapter.updated["stream-ts"]
    assert [b["type"] for b in update["blocks"]] == [
        "rich_text", "plan", "rich_text", "context_actions",
    ]
    plan = update["blocks"][1]
    assert plan["title"] == "2 steps (bash ×2)"
    assert [t["task_id"] for t in plan["tasks"]] == ["t1", "t2"]
    assert plan["tasks"][1]["output"]["elements"][0]["elements"][0]["text"] == "t2 ok"
    assert update["blocks"][2]["elements"][0]["elements"][0]["text"] == "All green."
    assert update["metadata"]["event_type"] == "freyja_session"
    assert update["text"]


@pytest.mark.asyncio
async def test_a_prose_only_turn_makes_no_update():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    await _feed(consumer, [
        {"type": "stream_start"},
        {"type": "text_delta", "text": "Just words."},
        {"type": "turn_complete"},
    ])
    assert consumer.finalized
    assert adapter.updated == {}


@pytest.mark.asyncio
async def test_each_message_of_a_long_turn_folds_as_it_seals(monkeypatch):
    """A rollover seals the first message while the turn goes on; that
    message folds right away, the last one at the end."""
    monkeypatch.setattr(sc, "_STREAM_BUDGET", 60)
    monkeypatch.setattr(sc, "_CHUNK_OVERHEAD", 0)
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    events: list[dict[str, Any]] = [{"type": "stream_start"}]
    for tid in ("t1", "t2", "t3", "t4"):
        events += [
            {"type": "tool_use_start", "id": tid, "name": "bash"},
            {"type": "tool_input_end", "id": tid, "arguments": {"command": f"echo {tid}"}},
            {"type": "tool_result", "id": tid, "preview": "x" * 30},
        ]
    events += [{"type": "text_delta", "text": "Done."}, {"type": "turn_complete"}]
    await _feed(consumer, events)
    assert consumer.finalized
    assert adapter.streams_started >= 2
    # Every message that streamed a card was folded; one with prose only
    # (the last one here) needs no update.
    with_cards = {
        ts for ts in adapter.streams_stopped
        if any(b["type"] == "task_card" for b in adapter.stored_message(ts)["blocks"])
    }
    assert len(with_cards) >= 2
    assert set(adapter.updated) == with_cards
    seen: list[str] = []
    for ts, update in adapter.updated.items():
        plans = [b for b in update["blocks"] if b["type"] == "plan"]
        assert len(plans) == 1, ts
        seen += [t["task_id"] for t in plans[0]["tasks"]]
    # Every step is in exactly one plan.
    assert sorted(seen) == ["t1", "t2", "t3", "t4"]
    assert not consumer._fold_tasks


@pytest.mark.asyncio
async def test_a_too_long_update_goes_again_with_titles_only():
    adapter = StreamingFakeAdapter()
    adapter.update_errors = ["msg_blocks_too_long"]
    consumer = _consumer(adapter)
    await _feed(consumer, _turn(["t1"]))
    plan = next(b for b in adapter.updated["stream-ts"]["blocks"] if b["type"] == "plan")
    assert plan["tasks"][0]["title"].startswith("bash:")
    assert "output" not in plan["tasks"][0]


@pytest.mark.asyncio
async def test_a_failed_update_leaves_the_message_as_streamed():
    adapter = StreamingFakeAdapter()
    adapter.update_errors = ["invalid_blocks", "invalid_blocks"]
    consumer = _consumer(adapter)
    await _feed(consumer, _turn(["t1"]))
    assert consumer.finalized
    assert adapter.updated == {}
    assert adapter.stop_calls[-1]["session_status"] == "active"


@pytest.mark.asyncio
async def test_no_fold_after_the_user_pressed_stop():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    await _feed(consumer, _turn(["t1"])[:-2])
    consumer.abort(stopped_by_user=True)
    await asyncio.sleep(0.02)
    assert adapter.updated == {}


@pytest.mark.asyncio
async def test_an_adapter_without_updates_is_left_alone():
    class NoUpdates(StreamingFakeAdapter):
        read_message = None  # type: ignore[assignment]
        update_message_blocks = None  # type: ignore[assignment]

    adapter = NoUpdates()
    consumer = _consumer(adapter)
    await _feed(consumer, _turn(["t1"]))
    assert consumer.finalized
    assert adapter.updated == {}


@pytest.mark.asyncio
async def test_folding_can_be_turned_off():
    adapter = StreamingFakeAdapter()
    consumer = _consumer(adapter)
    consumer.fold_steps = False
    await _feed(consumer, _turn(["t1"]))
    assert adapter.updated == {}
