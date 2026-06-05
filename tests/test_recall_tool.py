"""Unit tests for the recall tool (bridge/tools/recall_tool.py)."""

from __future__ import annotations

import json

import pytest

from bridge.tools.recall_tool import RecallTool, _message_text


def _write_archive(tmp_path, rows):
    p = tmp_path / "raw_messages.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def test_message_text_handles_str_and_blocks():
    assert _message_text({"role": "user", "content": "hello"}) == "hello"
    blocks = {"role": "assistant", "content": [{"type": "text", "text": "wrote widget_tools.py"}]}
    assert "widget_tools.py" in _message_text(blocks)
    nested = {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "deep"}]}]}
    assert "deep" in _message_text(nested)


@pytest.mark.asyncio
async def test_recall_search_finds_match(tmp_path):
    _write_archive(tmp_path, [
        {"ts": 1, "turn_id": "t1", "message": {"role": "user", "content": "build widgets"}},
        {"ts": 2, "turn_id": "t2", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "I created widget_tools.py with show_widget"}]}},
        {"ts": 3, "turn_id": "t3",
         "message": {"role": "assistant", "content": "unrelated chatter"}},
    ])
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    res = await tool.execute("c1", {"action": "search", "query": "widget_tools"})
    assert not res.is_error
    payload = json.loads(res.content)
    assert payload["match_count"] == 1
    assert payload["results"][0]["turn_id"] == "t2"
    assert "widget_tools.py" in payload["results"][0]["snippet"]


@pytest.mark.asyncio
async def test_recall_timeline(tmp_path):
    _write_archive(tmp_path, [
        {"ts": 1, "turn_id": "t1", "message": {"role": "user", "content": "first line\nsecond"}},
        {"ts": 2, "turn_id": "t2", "message": {"role": "assistant", "content": "ok"}},
    ])
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    res = await tool.execute("c1", {"action": "timeline"})
    payload = json.loads(res.content)
    assert payload["turn_count"] == 2
    assert payload["results"][0]["first_line"] == "first line"


@pytest.mark.asyncio
async def test_recall_empty_archive(tmp_path):
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    res = await tool.execute("c1", {"action": "search", "query": "x"})
    payload = json.loads(res.content)
    assert payload["results"] == []
    assert payload["exists"] is False


@pytest.mark.asyncio
async def test_recall_requires_query(tmp_path):
    _write_archive(
        tmp_path, [{"ts": 1, "turn_id": "t1", "message": {"role": "user", "content": "hi"}}]
    )
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    res = await tool.execute("c1", {"action": "search"})
    assert res.is_error
