"""Unit tests for the recall tool (bridge/tools/recall_tool.py)."""

from __future__ import annotations

import json

from bridge.tools.recall_tool import RecallTool, _message_text


def _write_archive(tmp_path, rows):
    p = tmp_path / "raw_messages.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


async def test_search_finds_text_in_strings_and_blocks_and_needs_a_query(tmp_path):
    # Nothing archived yet: an honest empty answer, not an error.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty = RecallTool(session_id="s1", project_output_dir=empty_dir)
    payload = json.loads((await empty.execute("c0", {"action": "search", "query": "x"})).content)
    assert payload["results"] == [] and payload["exists"] is False

    _write_archive(tmp_path, [
        {"ts": 1, "turn_id": "t1", "message": {"role": "user", "content": "build widgets"}},
        {"ts": 2, "turn_id": "t2", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "I created widget_tools.py with show_widget"}]}},
        {"ts": 3, "turn_id": "t3",
         "message": {"role": "assistant", "content": "unrelated chatter"}},
    ])
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    assert (await tool.execute("c1", {"action": "search"})).is_error
    res = await tool.execute("c2", {"action": "search", "query": "widget_tools"})
    assert not res.is_error
    payload = json.loads(res.content)
    assert payload["match_count"] == 1
    assert payload["results"][0]["turn_id"] == "t2"
    assert "widget_tools.py" in payload["results"][0]["snippet"]
    # Text nested in a tool result is searchable too.
    nested = {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "deep"}]}]}
    assert "deep" in _message_text(nested)


async def test_timeline_lists_turns_with_their_first_line(tmp_path):
    _write_archive(tmp_path, [
        {"ts": 1, "turn_id": "t1", "message": {"role": "user", "content": "first line\nsecond"}},
        {"ts": 2, "turn_id": "t2", "message": {"role": "assistant", "content": "ok"}},
    ])
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    res = await tool.execute("c1", {"action": "timeline"})
    payload = json.loads(res.content)
    assert payload["turn_count"] == 2
    assert payload["results"][0]["first_line"] == "first line"
