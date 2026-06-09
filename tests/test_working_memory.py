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
    # OpenAI strict-mode compatibility: every declared property is required.
    # Optional fields use type: ["string", "null"] so the model can return null
    # for the ones that don't apply to the chosen entity type.
    assert set(ent["required"]) == set(ent["properties"].keys())
    assert "type" in ent["required"]
    # Optional (nullable) fields really are nullable.
    for k in ("title", "request", "rationale", "text", "source", "path",
              "note", "workstream", "status"):
        assert ent["properties"][k]["type"] == ["string", "null"], k
    # `type` itself is required and constrained to the known entity kinds.
    assert ent["properties"]["type"]["type"] == "string"
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


# ── mid-session WM trigger primitive (_trigger_wm_extract) ──────────────────
#
# These tests drive the trigger directly against a stub that exposes the
# attributes _BridgeSession._trigger_wm_extract reads. The point is to pin
# the debounce / in-flight / sink / event-emit contract WITHOUT booting a
# full bridge runtime — that surface is exercised separately by the
# session_memory tool tests + manual smoke.

from types import SimpleNamespace
from unittest.mock import patch

from bridge import freyja_bridge as fb


class _StubSession:
    """Minimum surface _trigger_wm_extract reads: transcript.get_messages."""

    def __init__(self, messages):
        self.transcript = SimpleNamespace(get_messages=lambda: messages)


class _StubProvider:
    name = "stub"
    model_id = "stub-model"

    def complete_structured(self, *_args, **_kwargs):  # pragma: no cover
        raise NotImplementedError("never called in these tests")


class _StubRunner:
    def __init__(self):
        self.calls = []
        self.on_llm_call = self.calls.append


def _make_stub(
    *,
    current_turn_id="turn-5",
    last_extract="turn-1",
    last_compaction=None,
    in_flight=False,
    compaction_in_flight=False,
    has_messages=True,
):
    """Build the duck-typed object the unbound _trigger_wm_extract method
    can be invoked against. Mirrors the real _BridgeSession surface only
    for the fields the method touches — every other field stays absent
    deliberately so we catch attribute drift if the implementation
    grows quietly."""
    messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)] if has_messages else []
    stub = SimpleNamespace(
        id="sess-1",
        current_turn_id=current_turn_id,
        turn_counter=int(current_turn_id.split("-", 1)[1]) if current_turn_id else 0,
        _last_wm_extract_turn_id=last_extract,
        _last_compaction_turn_id=last_compaction,
        _wm_extract_in_flight=in_flight,
        _compaction_in_flight=compaction_in_flight,
        session=_StubSession(messages),
        provider=_StubProvider(),
        runner=_StubRunner(),
        working_memory=None,
        applied=[],
    )
    # The real method calls these as bound methods on self; bind them.
    stub._ledger_ground_truth = lambda: None
    stub._apply_wm_upserts = lambda result: stub.applied.append(result)
    return stub


async def test_trigger_debounce_blocks_within_n_turns():
    # turn-5 with last extraction at turn-4 → gap=1 < WM_EXTRACT_TURN_DEBOUNCE(3)
    stub = _make_stub(current_turn_id="turn-5", last_extract="turn-4")
    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory"
    ) as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()
    assert stub.applied == []  # sink untouched
    assert emitted == []  # no start/complete chips


async def test_trigger_in_flight_flag_prevents_reentry():
    stub = _make_stub(in_flight=True)
    with patch.object(SummaryCompaction, "_extract_working_memory") as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()
    # Now clear the flag and ensure the debounce-satisfied call DOES run.
    stub._wm_extract_in_flight = False
    stub._last_wm_extract_turn_id = None  # remove debounce as the source
    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction,
        "_extract_working_memory",
        return_value={"summary": "s", "actions_completed": [], "entities": []},
    ) as extract2:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract2.assert_called_once()


async def test_trigger_skips_if_recent_compaction():
    stub = _make_stub(
        current_turn_id="turn-10",
        last_extract=None,
        last_compaction="turn-9",  # gap=1 < 3
    )
    with patch.object(fb, "emit"), patch.object(
        SummaryCompaction, "_extract_working_memory"
    ) as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()


async def test_trigger_skips_if_compaction_in_flight():
    """A session_memory mutation that lands while compaction is RUNNING
    (not just recently finished) must not fire Call B — compaction is
    already running its own internal Call B and a duplicate concurrent
    extraction would race on the same transcript snapshot."""
    stub = _make_stub(
        current_turn_id="turn-10",
        last_extract=None,
        last_compaction=None,  # nothing recent — only the in-flight flag should block
        compaction_in_flight=True,
    )
    with patch.object(fb, "emit"), patch.object(
        SummaryCompaction, "_extract_working_memory"
    ) as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()


async def test_trigger_fires_when_debounce_satisfied():
    # turn-5 last extracted at turn-1 → gap=4 ≥ 3; no recent compaction.
    stub = _make_stub(
        current_turn_id="turn-5",
        last_extract="turn-1",
        last_compaction=None,
    )
    wm_result = {
        "summary": "did stuff",
        "actions_completed": ["a"],
        "entities": [],
    }

    def _fake_extract(self, _conv, _provider, **kwargs):
        # Populate stats_out the way the real Call B does, so the trigger
        # can emit the `_complete` chip with sensible metadata + forward
        # to runner.on_llm_call.
        stats = kwargs.get("stats_out")
        if stats is not None:
            stats.update({
                "status": "ok",
                "provider": "stub",
                "model": "stub-model",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "duration_ms": 42,
                "cost_usd": 0.0,
                "raw_output": "{}",
            })
        return wm_result

    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory", new=_fake_extract
    ):
        await fb._BridgeSession._trigger_wm_extract(stub, "session_memory:write")

    # Sink got the structured result.
    assert stub.applied == [wm_result]
    # Anchor turn advanced so subsequent triggers within the window get debounced.
    assert stub._last_wm_extract_turn_id == "turn-5"
    # in-flight flag cleared.
    assert stub._wm_extract_in_flight is False
    # Both lifecycle chips emitted, in order, with the trigger label
    # stamped. Filter to system_event records — `log()` debug lines
    # also route through `emit` and are not part of this contract.
    sys_events = [e for e in emitted if e.get("type") == "system_event"]
    subtypes = [e["subtype"] for e in sys_events]
    assert subtypes == ["working_memory_start", "working_memory_complete"]
    for e in sys_events:
        assert e["details"]["trigger"] == "session_memory:write"
    # Runner saw the spend tagged as the extraction kind.
    runner_calls = stub.runner.calls
    assert len(runner_calls) == 1
    assert runner_calls[0]["call_kind"] == "working_memory_extraction"
    assert runner_calls[0]["input_tokens"] == 10
    assert runner_calls[0]["output_tokens"] == 5


async def test_trigger_resets_in_flight_on_exception():
    stub = _make_stub(
        current_turn_id="turn-5",
        last_extract="turn-1",
        last_compaction=None,
    )

    def _boom(self, _conv, _provider, **_kwargs):
        raise RuntimeError("network ded")

    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory", new=_boom
    ):
        await fb._BridgeSession._trigger_wm_extract(stub, "test")

    # Flag MUST clear even on failure — otherwise the next trigger is
    # permanently locked out.
    assert stub._wm_extract_in_flight is False
    # And the user-visible `_complete` chip still fires (with status=failed)
    # so the operator sees a definite resolution, not a phantom start.
    # (Filter to system_event records; the bridge's `log()` helper also
    # routes through `emit` for debug lines.)
    sys_events = [e for e in emitted if e.get("type") == "system_event"]
    subtypes = [e["subtype"] for e in sys_events]
    assert subtypes == ["working_memory_start", "working_memory_complete"]
    assert sys_events[1]["details"]["status"] == "failed"
    assert sys_events[1]["details"]["error"]


async def test_trigger_emits_trigger_label_in_details():
    stub = _make_stub(
        current_turn_id="turn-5",
        last_extract="turn-1",
        last_compaction=None,
    )

    def _ok(self, _conv, _provider, **kwargs):
        stats = kwargs.get("stats_out")
        if stats is not None:
            stats["status"] = "ok"
        return {"summary": "", "actions_completed": [], "entities": []}

    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory", new=_ok
    ):
        await fb._BridgeSession._trigger_wm_extract(
            stub, "session_memory:append"
        )

    sys_events = [e for e in emitted if e.get("type") == "system_event"]
    assert [e["details"]["trigger"] for e in sys_events] == [
        "session_memory:append",
        "session_memory:append",
    ]


async def test_trigger_skips_when_no_messages():
    """Empty transcripts have nothing to consolidate — silent skip, no chip."""
    stub = _make_stub(has_messages=True)
    stub.session = _StubSession([])
    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory"
    ) as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()
    assert emitted == []


async def test_trigger_skips_when_runner_missing():
    """Pre-initialize() the bridge has no runner — must no-op silently."""
    stub = _make_stub()
    stub.runner = None
    with patch.object(SummaryCompaction, "_extract_working_memory") as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()
