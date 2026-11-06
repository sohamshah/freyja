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


# ─── A long log is read a line at a time ──────────────────────────────
#
# The largest raw log is 96 MB, and recall read every log whole, on every
# call. It reads one line at a time now, and a search of a single log reads
# it from the end and stops at the newest matches.


async def test_recall_reads_a_long_log_a_line_at_a_time(tmp_path):
    import tracemalloc

    rows = [_said(i, f"<row {i}> " + "x" * 10_000) for i in range(1_000)]
    _jsonl(tmp_path / "raw_messages.jsonl", rows)
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    for args, first in (
        ({"action": "search", "query": "<row 999>"}, "<row 999>"),
        ({"action": "search", "query": "<row 3>"}, "<row 3>"),
        ({"action": "timeline", "limit": 2}, "<row 998>"),
    ):
        tracemalloc.start()
        res = await tool.execute("c1", args)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        results = json.loads(res.content)["results"]
        assert (results[0].get("snippet") or results[0].get("first_line")).startswith(first)
        # The log is 10 MB.
        assert peak < 4 * 2**20, (args, peak)


async def test_a_row_holding_a_line_separator_is_found(tmp_path):
    # Text from web pages can hold U+2028. The bridge writes it as it is
    # (ensure_ascii=False), and splitting the file on it cut the row in two.
    row = json.dumps(_said(1, "a page\u2028with the needle"), ensure_ascii=False)
    (tmp_path / "raw_messages.jsonl").write_text(row + "\n", encoding="utf-8")
    tool = RecallTool(session_id="s1", project_output_dir=tmp_path)
    assert await _search(tool, "needle") == ["a page\u2028with the needle"]


def _reference(logs, recovered, action, query, limit):
    """What recall returns, from every row held in memory: each log in its
    own order and the recovered rows sorted, merged by time, a live row first
    on a tie, and a recovered row with a call or result a live row has left
    out."""
    import heapq

    from bridge.tools.recall_tool import _snippet

    def rows(text):
        out = []
        for line in text.split("\n"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def msg(row):
        return row.get("message") or {}

    def keys(row):
        out = {f"call:{c['id']}" for c in msg(row).get("tool_calls") or []}
        if msg(row).get("role") == "tool_result":
            out.add(f"result:{msg(row)['tool_call_id']}")
        return out

    def text(row):
        return _message_text(msg(row))

    def ts(row):
        return row.get("ts") if isinstance(row.get("ts"), (int, float)) else 0

    logs = [rows(t) for t in logs]
    live = set().union(*(keys(r) for log in logs for r in log))
    kept = [r for r in sorted(rows(recovered), key=ts) if not keys(r) & live]
    timeline = list(heapq.merge(*logs, kept, key=ts))
    if action == "timeline":
        return len(timeline), [
            (r.get("turn_id"), msg(r).get("role"),
             (text(r).strip().splitlines() or [""])[0][:160])
            for r in timeline[-limit:]
        ]
    hits = [
        (r.get("turn_id"), msg(r).get("role"), r.get("ts"), _snippet(text(r), query))
        for r in timeline
        if query.lower() in text(r).lower()
    ]
    return len(hits[-limit:]), hits[-limit:]


@pytest.mark.parametrize("seed", range(40))
async def test_a_streamed_recall_returns_what_reading_every_row_returns(
    tmp_path, monkeypatch, seed
):
    import random

    rnd = random.Random(seed)
    words = [
        "Needle", "needle", "hay", "K\u212a", "stra\u00dfe", 'q"uote', "back\\slash",
        "line\nbreak", "sep\u2028arator", "\ud835", "a/b", "NEEDLE in",
    ]

    def row(ts, i, recovered=False):
        text = " ".join(rnd.choice(words) for _ in range(rnd.randint(0, 6)))
        kind = rnd.random()
        call = f"c{rnd.randint(0, 30)}"
        if kind < 0.3:
            msg = {"role": "assistant", "content": [{"type": "text", "text": text}],
                   "tool_calls": [{"id": call, "name": "bash", "arguments": {}}]}
        elif kind < 0.6:
            msg = {"role": "tool_result", "tool_call_id": call, "content": text}
        else:
            msg = {"role": rnd.choice(["user", "assistant"]), "content": text}
        out = {"ts": ts, "turn_id": f"t{i}", "message": msg}
        if recovered:
            out["recovered"] = True
        return json.dumps(out, ensure_ascii=rnd.random() < 0.2)

    def log(n, recovered=False):
        # Stamps that tie, and now and then go backward.
        stamps = [rnd.choice([rnd.randint(0, 40), rnd.randint(0, 40) + 0.5]) for _ in range(n)]
        lines = [row(ts, i, recovered) for i, ts in enumerate(stamps)]
        if not recovered:
            lines.sort(key=lambda line: json.loads(line)["ts"] if rnd.random() < 0.8 else 0)
        for odd in ('{"ts": 3, "cut short', "{}", "[1, 2]", ""):
            if rnd.random() < 0.2:
                lines.insert(rnd.randint(0, len(lines)), odd)
        return "\n".join(lines) + ("\n" if rnd.random() < 0.7 else "")

    import bridge.tools.recall_tool as recall_tool

    texts = {}
    shape = rnd.choice(["one", "one", "two", "recovered", "only recovered"])
    sidecar = tmp_path / "sessions" / "s1.raw_messages.jsonl"
    project = tmp_path / "p" / "raw_messages.jsonl"
    recovered_path = project.with_name("raw_messages.recovered.jsonl")
    if shape != "only recovered":
        texts[project] = log(rnd.randint(0, 60))
    if shape == "two":
        texts[sidecar] = log(rnd.randint(0, 30))
    if shape in ("recovered", "only recovered"):
        texts[recovered_path] = log(rnd.randint(1, 20), recovered=True)
    for path, text in texts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="surrogatepass")
    # Small blocks, so lines cross them.
    monkeypatch.setattr(recall_tool, "_BLOCK", rnd.choice([7, 64, 1 << 20]), raising=False)
    tool = RecallTool(session_id="s1")
    tool._archive_paths = lambda _sid: [sidecar, project]

    def read(path):
        return path.read_bytes().decode("utf-8", "replace") if path.exists() else ""

    logs = [read(p) for p in (sidecar, project) if p.exists()]
    recovered = read(recovered_path)
    for action, query, limit in [
        ("search", "needle", 12), ("search", "NEEDLE in", 3), ("search", "k", 5),
        ("search", "a/b", 50), ("search", "q\"u", 4), ("search", "back\\s", 4),
        ("search", "\u212a", 4), ("search", "line\nbr", 4),
        ("search", "zz", 4), ("search", "needle", -2), ("timeline", "", 7), ("timeline", "", -3),
    ]:
        args = {"action": action, "query": query, "limit": limit}
        payload = json.loads((await tool.execute("c1", args)).content)
        count, expected = _reference(logs, recovered, action, query, limit)
        if "note" in payload:  # nothing archived
            assert (payload["results"], expected) == ([], []), (shape, action, query)
            continue
        if action == "timeline":
            got = [(r["turn_id"], r["role"], r["first_line"]) for r in payload["results"]]
            assert (payload["turn_count"], got) == (count, expected), (shape, action, limit)
        else:
            got = [(r["turn_id"], r["role"], r["ts"], r["snippet"]) for r in payload["results"]]
            assert got == expected, (shape, action, query, limit)
