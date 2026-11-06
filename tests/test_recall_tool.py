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


# ─── Each session searches its own log ────────────────────────────────
#
# A root session's raw log is <project dir>/raw_messages.jsonl. A sub-agent's
# is ~/.freyja/sessions/<id>.raw_messages.jsonl, beside its sidecars (as is
# the log of a session whose project dir is another session's). recall read
# only project dirs, and a sub-agent's recall was the parent's tool, bound to
# the parent's log.


@pytest.fixture
def _home(monkeypatch, tmp_path):
    import bridge.transcript_persistence as tp

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tp, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


def _log(path, *texts, role="assistant"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i, text in enumerate(texts):
            fh.write(json.dumps({"ts": i + 1, "turn_id": None,
                                 "message": {"role": role, "content": text}}) + "\n")


async def _search(tool, query, **extra):
    res = await tool.execute("c1", {"action": "search", "query": query, **extra})
    return [r["snippet"] for r in json.loads(res.content)["results"]]


async def test_a_sub_agent_session_is_searched_in_its_own_log(_home):
    _log(_home / "sessions" / "sub_r_1.raw_messages.jsonl", "the child found the needle")
    _log(_home / ".freyja" / "projects" / "parent-1" / "raw_messages.jsonl", "the parent's needle")

    own = RecallTool(session_id="sub_r_1")
    assert await _search(own, "needle") == ["the child found the needle"]
    # Another session's log, by id: a root asks for its sub-agent's.
    root = RecallTool(session_id="parent-1")
    assert await _search(root, "needle", session_id="sub_r_1") == ["the child found the needle"]
    assert await _search(root, "needle") == ["the parent's needle"]


def test_a_root_registry_searches_the_sessions_own_log_not_a_borrowed_project_dir(_home):
    from bridge.tools.registry import build_desktop_registry

    # A sub-agent opened as a root writes into the parent's project dir for
    # what it makes, but its own messages are logged beside its sidecars.
    _log(_home / "sessions" / "sub_old_1.raw_messages.jsonl", "my own needle")
    _log(_home / ".freyja" / "projects" / "parent-1" / "raw_messages.jsonl", "the parent's needle")
    registry = build_desktop_registry(
        workspace=_home, include_web=False, include_computer=False, include_subagents=False,
        subagent_parent_session_id="sub_old_1",
        project_output_dir=_home / ".freyja" / "projects" / "parent-1",
    )

    import asyncio

    assert asyncio.run(_search(registry._tools["recall"], "needle")) == ["my own needle"]


async def test_a_sub_agents_recall_is_its_own_and_reaches_its_parent_by_id(monkeypatch, _home):
    import sys
    from types import ModuleType

    import engine.runner as runner_module
    import bridge.tools.sub_agent_tool as sat
    from bridge.tools.base import ToolRegistry
    from bridge.tools.sub_agent_registry import SubAgentRegistry

    telemetry = ModuleType("bridge.compaction_telemetry")
    telemetry.append_telemetry = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "bridge.compaction_telemetry", telemetry)
    _log(_home / ".freyja" / "projects" / "parent-1" / "raw_messages.jsonl", "the parent's needle")
    seen: dict = {}

    class _Runner(runner_module.AsyncAgentRunner):
        async def run(self, session, prompt, **_k):
            session.add_user_message("find the child's needle")
            tool = self.tool_registry.get("recall")
            seen["own"] = await _search(tool, "needle")
            seen["parent"] = await _search(tool, "needle", session_id="parent-1")
            seen["description"] = tool.definition.description
            raise RuntimeError("stop here")

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)

    async def _emit(_event):
        return None

    class _Provider:
        name = "anthropic"
        model_id = "claude-sonnet-5-5"
        context_window = 200_000

    parent_registry = ToolRegistry()
    parent_registry.register(RecallTool(session_id="parent-1"))
    registry = SubAgentRegistry()
    spec = sat.SubAgentSpec(
        parent_workspace=str(_home), parent_model="claude-sonnet-5-5",
        build_provider=lambda *a, **k: _Provider(), parent_registry=parent_registry,
        registry=registry, emit_event=_emit, parent_session_id="parent-1",
    )
    record = registry.register(id="sub_r_2", label="reader", task="read it", mode="background")
    with pytest.raises(RuntimeError, match="stop here"):
        await sat.SubAgentTool(spec)._run_child(record)

    assert seen["own"] == ["find the child's needle"]
    assert seen["parent"] == ["the parent's needle"]
    assert 'session_id="parent-1"' in seen["description"]


# ─── Rows the backfill recovered ──────────────────────────────────────
#
# scripts/backfill_raw_messages.py writes the messages a raw log is missing
# to raw_messages.recovered.jsonl beside it, marked recovered. recall reads
# them among the live rows, by time, and a live row wins, as exports do
# (src/main/sessionRecord.ts).


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _call(ts, call_id, **extra):
    calls = [{"id": call_id, "name": "bash", "arguments": {}}]
    return {"ts": ts, "turn_id": None, **extra,
            "message": {"role": "assistant", "content": "", "tool_calls": calls}}


def _result(ts, call_id, text, **extra):
    return {"ts": ts, "turn_id": None, **extra,
            "message": {"role": "tool_result", "tool_call_id": call_id, "content": text}}


def _said(ts, text, role="user", **extra):
    return {"ts": ts, "turn_id": None, **extra, "message": {"role": role, "content": text}}


async def test_recall_reads_the_recovered_rows_in_time_order_and_a_live_row_wins(_home):
    project = _home / ".freyja" / "projects" / "root-1"
    _jsonl(project / "raw_messages.jsonl", [
        _said(1, "before the restart"),
        _call(2, "live-1"),
        _result(3, "live-1", "live needle"),
        _said(30, "after the fix"),
    ])
    recovered = {"recovered": True, "whole": True}
    _jsonl(project / "raw_messages.recovered.jsonl", [
        _call(10, "rec-1", **recovered),
        _result(11, "rec-1", "recovered needle", **recovered),
        # A recovered row never stands over a live one.
        _result(12, "live-1", "stale needle", **recovered),
    ])
    tool = RecallTool(session_id="root-1")

    assert await _search(tool, "needle") == ["live needle", "recovered needle"]
    payload = json.loads((await tool.execute("c1", {"action": "timeline"})).content)
    assert payload["turn_count"] == 6
    assert [r["first_line"] for r in payload["results"]] == [
        "before the restart", "", "live needle", "", "recovered needle", "after the fix",
    ]


async def test_a_sub_agent_with_only_recovered_rows_is_searched(_home):
    _jsonl(_home / "sessions" / "sub_r_3.raw_messages.recovered.jsonl", [
        _said(1, "the child's recovered needle", recovered=True, whole=True),
    ])
    tool = RecallTool(session_id="sub_r_3")
    res = json.loads((await tool.execute("c1", {"action": "search", "query": "needle"})).content)
    assert [r["snippet"] for r in res["results"]] == ["the child's recovered needle"]
    assert "sub_r_3.raw_messages.recovered.jsonl" in res["archive"]
