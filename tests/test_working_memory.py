"""Unit tests for structured working memory (Milestone 2 foundation)."""

from __future__ import annotations

import json

import pytest

from bridge.working_memory import (
    WorkingMemory,
    apply_working_memory_upserts,
    render_working_memory,
)
from engine.compaction import (
    SummaryCompaction,
    _working_memory_schema,
)


def _mk(tmp_path):
    wm = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm.ensure()
    return wm


def test_upsert_workstream_and_children(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Port widgets", "request": "do X"})
    assert ws["id"].startswith("ws-port-widgets")
    assert ws["status"] == "active"
    dec = wm.upsert(type="decision", fields={
        "title": "Use SSE", "rationale": "simpler than MCP Apps", "workstreamId": ws["id"],
    })
    assert dec["workstreamId"] == ws["id"]
    assert dec["type"] == "decision"


def test_upsert_updates_existing(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "T", "request": "r1"})
    wm.upsert(type="workstream", entity_id=ws["id"], fields={"request": "r2", "phase": "impl"})
    again = wm.list(type="workstream")[0]
    assert again["request"] == "r2"
    assert again["phase"] == "impl"
    assert len(wm.list(type="workstream")) == 1  # updated, not duplicated


def test_resolve_thread_and_workstream(tmp_path):
    wm = _mk(tmp_path)
    thr = wm.upsert(type="open_thread", fields={"text": "verify deploy"})
    assert thr["status"] == "open"
    assert wm.resolve(thr["id"])["status"] == "resolved"
    ws = wm.upsert(type="workstream", fields={"title": "W"})
    assert wm.resolve(ws["id"])["status"] == "done"


def test_id_slug_dedup(tmp_path):
    wm = _mk(tmp_path)
    a = wm.upsert(type="finding", fields={"text": "same name"})
    b = wm.upsert(type="finding", fields={"text": "same name"})
    assert a["id"] != b["id"]
    assert b["id"].endswith("-2")


def test_persistence_roundtrip(tmp_path):
    wm = _mk(tmp_path)
    wm.upsert(type="workstream", fields={"title": "Persisted"})
    # New instance over the same dir must see it.
    wm2 = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm2.ensure()
    assert len(wm2.list(type="workstream")) == 1
    assert wm2.list(type="workstream")[0]["title"] == "Persisted"
    # File is valid JSON.
    doc = json.loads((tmp_path / "working_memory.json").read_text())
    assert doc["version"] == 1


def test_render_groups_and_hides_done(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Widgets", "request": "ship it"})
    wm.upsert(type="decision", fields={
        "title": "SSE", "rationale": "simple", "workstreamId": ws["id"],
    })
    wm.upsert(type="finding", fields={"text": "MCP Apps differs", "workstreamId": ws["id"]})
    wm.upsert(type="open_thread", fields={"text": "verify deploy", "workstreamId": ws["id"]})
    out = wm.render()
    assert "Widgets" in out
    assert "decided: SSE — simple" in out
    assert "found: MCP Apps differs" in out
    assert "open: verify deploy" in out
    # Resolved workstream is hidden by default.
    wm.resolve(ws["id"])
    out2 = wm.render() or ""
    assert "Widgets" not in out2


def test_render_none_when_empty(tmp_path):
    wm = _mk(tmp_path)
    assert wm.render() is None
    assert render_working_memory([]) is None


def test_render_unfiled_section(tmp_path):
    wm = _mk(tmp_path)
    wm.upsert(type="finding", fields={"text": "orphan finding"})
    out = wm.render()
    assert "Unfiled" in out
    assert "orphan finding" in out


# ── diff-aware artifact_note render (chunk 2 / diff-aware artifact) ─────────

def test_artifact_note_renders_diff_marginmark(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Widgets", "request": "ship"})
    # upsert stores arbitrary non-null fields, so additions/deletions land.
    note = wm.upsert(type="artifact_note", fields={
        "path": "src/widget.py", "note": "wired SSE", "workstreamId": ws["id"],
        "additions": 12, "deletions": 3,
    })
    assert note["additions"] == 12
    assert note["deletions"] == 3
    out = wm.render()
    assert "file: src/widget.py — wired SSE (+12 −3)" in out


def test_artifact_note_without_diff_stats_unchanged(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "W"})
    wm.upsert(type="artifact_note", fields={
        "path": "a.py", "note": "n", "workstreamId": ws["id"],
    })
    out = wm.render()
    assert "file: a.py — n" in out
    assert "(+" not in out  # no MarginMark when no diff stats recorded


def test_artifact_note_zero_deletions_still_renders(tmp_path):
    # A pure-create (deletions absent) still shows +N once additions is present.
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "W"})
    wm.upsert(type="artifact_note", fields={
        "path": "new.py", "note": "created", "workstreamId": ws["id"],
        "additions": 40,
    })
    out = wm.render()
    assert "(+40 −0)" in out


# ── tool ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_upsert_read_resolve(tmp_path):
    from bridge.tools.working_memory_tool import WorkingMemoryTool
    wm = _mk(tmp_path)
    tool = WorkingMemoryTool(memory=wm, get_ledger_effects=lambda: [])

    res = await tool.execute("c1", {
        "action": "upsert", "type": "workstream",
        "fields": {"title": "Build", "request": "x"},
    })
    assert not res.is_error
    ws_id = json.loads(res.content)["entity"]["id"]

    res = await tool.execute("c2", {
        "action": "upsert", "type": "open_thread",
        "fields": {"text": "check perf", "workstream_id": ws_id},
    })
    thr_id = json.loads(res.content)["entity"]["id"]

    res = await tool.execute("c3", {"action": "read"})
    rendered = json.loads(res.content)["rendered"]
    assert "Build" in rendered and "check perf" in rendered

    res = await tool.execute("c4", {"action": "resolve", "id": thr_id})
    assert json.loads(res.content)["entity"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_tool_validates(tmp_path):
    from bridge.tools.working_memory_tool import WorkingMemoryTool
    tool = WorkingMemoryTool(memory=_mk(tmp_path))
    assert (await tool.execute("c1", {"action": "bogus"})).is_error
    assert (await tool.execute("c2", {"action": "upsert", "type": "nope"})).is_error
    assert (await tool.execute("c3", {"action": "resolve"})).is_error


@pytest.mark.asyncio
async def test_tool_read_folds_in_ledger_effects(tmp_path):
    # The read surface shows runtime-recorded files (the bug fix).
    from bridge.tools.working_memory_tool import WorkingMemoryTool
    wm = _mk(tmp_path)
    effects = [{"summary": "created widget_tools.py (680 lines)"}]
    tool = WorkingMemoryTool(memory=wm, get_ledger_effects=lambda: effects)
    res = await tool.execute("c1", {"action": "read"})
    rendered = json.loads(res.content)["rendered"]
    assert "widget_tools.py (680 lines)" in rendered


# ── compaction extraction (Call B) ──────────────────────────────────────────

def test_working_memory_schema_shape():
    schema = _working_memory_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"summary", "actions_completed", "entities"}
    props = schema["properties"]
    assert props["summary"]["type"] == "string"
    assert props["actions_completed"]["type"] == "array"
    assert props["actions_completed"]["items"]["type"] == "string"
    ent = props["entities"]["items"]
    assert ent["required"] == ["type"]
    # Every entity type the store understands is offered to the model.
    assert set(ent["properties"]["type"]["enum"]) == {
        "workstream", "decision", "finding", "open_thread", "artifact_note",
    }


def test_normalize_working_memory_drops_invalid_and_empty():
    out = SummaryCompaction._normalize_working_memory({
        "summary": "  did stuff  ",
        "actions_completed": ["a", "  ", "", "b", 3],  # blanks/non-str dropped
        "entities": [
            {"type": "workstream", "title": "Port", "request": "ship", "note": ""},
            {"type": "bogus", "title": "x"},     # invalid type → dropped
            "not a dict",                          # non-dict → dropped
            {"type": "finding", "text": "f", "source": None},  # null field stripped
        ],
    })
    assert out["summary"] == "did stuff"
    assert out["actions_completed"] == ["a", "b"]
    types = [e["type"] for e in out["entities"]]
    assert types == ["workstream", "finding"]
    # Empty-string and null fields are stripped from stored entities.
    assert "note" not in out["entities"][0]
    assert "source" not in out["entities"][1]


def test_normalize_working_memory_handles_missing_keys():
    out = SummaryCompaction._normalize_working_memory({})
    assert out == {"summary": "", "actions_completed": [], "entities": []}


# ── overview (high-level summary + actions-completed) ────────────────────────

def test_set_overview_persists_and_renders(tmp_path):
    wm = _mk(tmp_path)
    wm.set_overview(
        summary="Built the two-call compaction split.",
        actions_completed=["Added _extract_working_memory", "Wired Call B"],
    )
    out = wm.render()
    assert "## Summary" in out
    assert "Built the two-call compaction split." in out
    assert "## Actions completed" in out
    assert "- Added _extract_working_memory" in out
    # Survives a reload (new instance over the same dir).
    wm2 = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm2.ensure()
    ov = wm2.overview()
    assert ov["summary"] == "Built the two-call compaction split."
    assert ov["actionsCompleted"] == ["Added _extract_working_memory", "Wired Call B"]
    assert "## Summary" in wm2.render()


def test_overview_normalizes_blanks(tmp_path):
    wm = _mk(tmp_path)
    wm.set_overview(summary="  s  ", actions_completed=["x", "  ", "", "y"])
    ov = wm.overview()
    assert ov["summary"] == "s"
    assert ov["actionsCompleted"] == ["x", "y"]


def test_overview_combines_with_entities(tmp_path):
    wm = _mk(tmp_path)
    wm.upsert(type="workstream", fields={"title": "Port", "request": "ship"})
    wm.set_overview(summary="Working on the port.", actions_completed=["did x"])
    out = wm.render()
    # Both the overview and the entity graph are present.
    assert "## Summary" in out and "Working on the port." in out
    assert "Port" in out


def test_overview_alone_makes_render_nonempty_and_not_empty(tmp_path):
    wm = _mk(tmp_path)
    assert wm.render() is None
    assert wm.is_empty()
    wm.set_overview(summary="only a summary", actions_completed=[])
    assert wm.render() is not None
    assert not wm.is_empty()


def test_apply_upserts_creates_and_links(tmp_path):
    wm = _mk(tmp_path)
    n = apply_working_memory_upserts(wm, [
        {"type": "workstream", "title": "Port widgets", "request": "ship"},
        {"type": "decision", "title": "SSE", "rationale": "simple", "workstream": "Port widgets"},
        {"type": "finding", "text": "MCP differs", "workstream": "Port widgets"},
        # references a workstream by title that wasn't explicitly created — auto-created.
        {"type": "open_thread", "text": "verify deploy", "workstream": "Rendering"},
    ])
    assert n >= 4
    ws = wm.list(type="workstream")
    titles = {w["title"] for w in ws}
    assert "Port widgets" in titles
    assert "Rendering" in titles  # auto-created from the open_thread reference
    dec = wm.list(type="decision")[0]
    assert dec["workstreamId"] == next(w["id"] for w in ws if w["title"] == "Port widgets")


def test_apply_upserts_dedups_on_repeat(tmp_path):
    wm = _mk(tmp_path)
    upserts = [
        {"type": "workstream", "title": "W", "request": "r"},
        {"type": "decision", "title": "D", "rationale": "because", "workstream": "W"},
    ]
    apply_working_memory_upserts(wm, upserts)
    # Re-applying the same set across a later compaction round must not duplicate.
    apply_working_memory_upserts(wm, upserts)
    assert len(wm.list(type="workstream")) == 1
    assert len(wm.list(type="decision")) == 1


def test_apply_upserts_empty_is_noop(tmp_path):
    wm = _mk(tmp_path)
    assert apply_working_memory_upserts(wm, []) == 0
    assert wm.is_empty()
