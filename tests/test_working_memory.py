"""Structured working memory: the entity store and its render, the tool, the
compaction extraction schema, and the bridge's mid-session extraction trigger."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bridge import freyja_bridge as fb
from bridge.working_memory import WorkingMemory, apply_working_memory_upserts
from engine.compaction import SummaryCompaction, _working_memory_schema


def _mk(tmp_path):
    wm = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm.ensure()
    return wm


# ── the entity store ───────────────────────────────────────────────────────


def test_upsert_creates_links_updates_in_place_and_keeps_ids_apart(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Port widgets", "request": "do X"})
    assert ws["id"].startswith("ws-port-widgets")
    assert ws["status"] == "active"
    dec = wm.upsert(type="decision", fields={
        "title": "Use SSE", "rationale": "simpler than MCP Apps", "workstreamId": ws["id"],
    })
    assert dec["workstreamId"] == ws["id"] and dec["type"] == "decision"

    # An upsert by id updates the entity; it does not duplicate it.
    wm.upsert(type="workstream", entity_id=ws["id"], fields={"request": "r2", "phase": "impl"})
    [again] = wm.list(type="workstream")
    assert again["request"] == "r2" and again["phase"] == "impl"

    # Two findings with the same text are two entities.
    a = wm.upsert(type="finding", fields={"text": "same name"})
    b = wm.upsert(type="finding", fields={"text": "same name"})
    assert a["id"] != b["id"] and b["id"].endswith("-2")

    # Resolving closes a thread and finishes a workstream.
    thr = wm.upsert(type="open_thread", fields={"text": "verify deploy"})
    assert thr["status"] == "open"
    assert wm.resolve(thr["id"])["status"] == "resolved"
    assert wm.resolve(ws["id"])["status"] == "done"


def test_persistence_roundtrip(tmp_path):
    wm = _mk(tmp_path)
    wm.upsert(type="workstream", fields={"title": "Persisted"})
    # A new instance over the same dir must see it.
    wm2 = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm2.ensure()
    assert [w["title"] for w in wm2.list(type="workstream")] == ["Persisted"]
    # The file is valid JSON.
    doc = json.loads((tmp_path / "working_memory.json").read_text())
    assert doc["version"] == 1


def test_render_groups_by_workstream_hides_done_and_files_orphans_as_unfiled(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Widgets", "request": "ship it"})
    wm.upsert(type="decision", fields={"title": "SSE", "rationale": "simple", "workstreamId": ws["id"]})
    wm.upsert(type="finding", fields={"text": "MCP Apps differs", "workstreamId": ws["id"]})
    wm.upsert(type="open_thread", fields={"text": "verify deploy", "workstreamId": ws["id"]})
    wm.upsert(type="finding", fields={"text": "orphan finding"})
    out = wm.render()
    assert "Widgets" in out
    assert "decided: SSE — simple" in out
    assert "found: MCP Apps differs" in out
    assert "open: verify deploy" in out
    assert "Unfiled" in out and "orphan finding" in out
    # A resolved workstream is hidden by default.
    wm.resolve(ws["id"])
    assert "Widgets" not in (wm.render() or "")


def test_artifact_notes_render_diff_marks_only_when_recorded(tmp_path):
    wm = _mk(tmp_path)
    ws = wm.upsert(type="workstream", fields={"title": "Widgets", "request": "ship"})
    # upsert stores arbitrary non-null fields, so additions/deletions land.
    note = wm.upsert(type="artifact_note", fields={
        "path": "src/widget.py", "note": "wired SSE", "workstreamId": ws["id"],
        "additions": 12, "deletions": 3,
    })
    assert (note["additions"], note["deletions"]) == (12, 3)
    wm.upsert(type="artifact_note", fields={"path": "a.py", "note": "n", "workstreamId": ws["id"]})
    # A pure create (deletions absent) still shows +N once additions is present.
    wm.upsert(type="artifact_note", fields={
        "path": "new.py", "note": "created", "workstreamId": ws["id"], "additions": 40,
    })
    out = wm.render()
    assert "file: src/widget.py — wired SSE (+12 −3)" in out
    assert "(+40 −0)" in out
    [plain] = [line for line in out.splitlines() if "file: a.py" in line]
    assert "(+" not in plain  # no MarginMark when no diff stats were recorded


# ── the tool ───────────────────────────────────────────────────────────────


async def test_tool_upserts_reads_with_ledger_effects_resolves_and_validates(tmp_path):
    from bridge.tools.working_memory_tool import WorkingMemoryTool

    wm = _mk(tmp_path)
    effects = [{"summary": "created widget_tools.py (680 lines)"}]
    tool = WorkingMemoryTool(memory=wm, get_ledger_effects=lambda: effects)

    res = await tool.execute("c1", {
        "action": "upsert", "type": "workstream", "fields": {"title": "Build", "request": "x"},
    })
    assert not res.is_error
    ws_id = json.loads(res.content)["entity"]["id"]
    res = await tool.execute("c2", {
        "action": "upsert", "type": "open_thread", "fields": {"text": "check perf", "workstream_id": ws_id},
    })
    thr_id = json.loads(res.content)["entity"]["id"]

    rendered = json.loads((await tool.execute("c3", {"action": "read"})).content)["rendered"]
    assert "Build" in rendered and "check perf" in rendered
    # The read surface shows runtime-recorded files too (the bug fix).
    assert "widget_tools.py (680 lines)" in rendered

    res = await tool.execute("c4", {"action": "resolve", "id": thr_id})
    assert json.loads(res.content)["entity"]["status"] == "resolved"

    assert (await tool.execute("c5", {"action": "bogus"})).is_error
    assert (await tool.execute("c6", {"action": "upsert", "type": "nope"})).is_error
    assert (await tool.execute("c7", {"action": "resolve"})).is_error


# ── compaction extraction (Call B) ─────────────────────────────────────────


def test_working_memory_schema_is_strict_mode_compatible():
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
    assert [e["type"] for e in out["entities"]] == ["workstream", "finding"]
    # Empty-string and null fields are stripped from stored entities.
    assert "note" not in out["entities"][0]
    assert "source" not in out["entities"][1]
    assert SummaryCompaction._normalize_working_memory({}) == {
        "summary": "", "actions_completed": [], "entities": [],
    }


# ── overview (high-level summary + actions-completed) ──────────────────────


def test_overview_persists_normalizes_and_renders_with_the_entities(tmp_path):
    wm = _mk(tmp_path)
    assert wm.render() is None and wm.is_empty()
    wm.upsert(type="workstream", fields={"title": "Port", "request": "ship"})
    wm.set_overview(
        summary="  Built the two-call compaction split.  ",
        actions_completed=["Added _extract_working_memory", "  ", "", "Wired Call B"],
    )
    out = wm.render()
    assert "## Summary" in out and "Built the two-call compaction split." in out
    assert "## Actions completed" in out and "- Added _extract_working_memory" in out
    assert "Port" in out  # the entity graph is still there
    # Survives a reload, with blanks normalized away.
    wm2 = WorkingMemory(session_id="s1", project_dir=tmp_path)
    wm2.ensure()
    ov = wm2.overview()
    assert ov["summary"] == "Built the two-call compaction split."
    assert ov["actionsCompleted"] == ["Added _extract_working_memory", "Wired Call B"]
    assert "## Summary" in wm2.render() and not wm2.is_empty()

    # An overview alone makes the memory non-empty.
    (tmp_path / "alone").mkdir()
    alone = WorkingMemory(session_id="s2", project_dir=tmp_path / "alone")
    alone.ensure()
    alone.set_overview(summary="only a summary", actions_completed=[])
    assert alone.render() is not None and not alone.is_empty()


def test_apply_upserts_creates_links_and_does_not_duplicate_on_a_later_round(tmp_path):
    wm = _mk(tmp_path)
    upserts = [
        {"type": "workstream", "title": "Port widgets", "request": "ship"},
        {"type": "decision", "title": "SSE", "rationale": "simple", "workstream": "Port widgets"},
        {"type": "finding", "text": "MCP differs", "workstream": "Port widgets"},
        # references a workstream by title that wasn't explicitly created — auto-created.
        {"type": "open_thread", "text": "verify deploy", "workstream": "Rendering"},
    ]
    assert apply_working_memory_upserts(wm, upserts) >= 4
    ws = wm.list(type="workstream")
    assert {"Port widgets", "Rendering"} <= {w["title"] for w in ws}
    dec = wm.list(type="decision")[0]
    assert dec["workstreamId"] == next(w["id"] for w in ws if w["title"] == "Port widgets")
    # Re-applying the same set across a later compaction round must not duplicate.
    apply_working_memory_upserts(wm, upserts)
    assert len(wm.list(type="workstream")) == 2
    assert len(wm.list(type="decision")) == 1
    assert len(wm.list(type="open_thread")) == 1


# ── mid-session WM trigger primitive (_trigger_wm_extract) ─────────────────
#
# These tests drive the trigger directly against a stub that exposes the
# attributes _BridgeSession._trigger_wm_extract reads. The point is to pin
# the debounce / in-flight / sink / event-emit contract WITHOUT booting a
# full bridge runtime.


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
):
    """Build the duck-typed object the unbound _trigger_wm_extract method
    can be invoked against. Mirrors the real _BridgeSession surface only
    for the fields the method touches — every other field stays absent
    deliberately so we catch attribute drift if the implementation
    grows quietly."""
    messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
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


def _without_messages(stub):
    stub.session = _StubSession([])


def _without_runner(stub):
    stub.runner = None


@pytest.mark.parametrize(
    ("stub_kwargs", "tweak"),
    [
        # turn-5 with the last extraction at turn-4: gap=1 < WM_EXTRACT_TURN_DEBOUNCE(3)
        (dict(current_turn_id="turn-5", last_extract="turn-4"), None),
        (dict(in_flight=True), None),
        # a compaction ran its own Call B a turn ago...
        (dict(current_turn_id="turn-10", last_extract=None, last_compaction="turn-9"), None),
        # ...or is running it now: a second extraction would race on the same snapshot
        (dict(current_turn_id="turn-10", last_extract=None, compaction_in_flight=True), None),
        # an empty transcript has nothing to consolidate
        ({}, _without_messages),
        # before initialize() the bridge has no runner
        ({}, _without_runner),
    ],
    ids=["debounce", "in_flight", "recent_compaction", "compaction_in_flight", "no_messages", "no_runner"],
)
async def test_trigger_skips_silently(stub_kwargs, tweak):
    stub = _make_stub(**stub_kwargs)
    if tweak is not None:
        tweak(stub)
    emitted: list[dict] = []
    with patch.object(fb, "emit", side_effect=emitted.append), patch.object(
        SummaryCompaction, "_extract_working_memory"
    ) as extract:
        await fb._BridgeSession._trigger_wm_extract(stub, "test")
    extract.assert_not_called()
    assert stub.applied == []  # sink untouched
    assert emitted == []  # no start/complete chips


async def test_trigger_fires_when_debounce_satisfied():
    # turn-5 last extracted at turn-1 → gap=4 ≥ 3; no recent compaction.
    stub = _make_stub(current_turn_id="turn-5", last_extract="turn-1", last_compaction=None)
    wm_result = {"summary": "did stuff", "actions_completed": ["a"], "entities": []}

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
    assert [e["subtype"] for e in sys_events] == ["working_memory_start", "working_memory_complete"]
    assert [e["details"]["trigger"] for e in sys_events] == ["session_memory:write"] * 2
    # Runner saw the spend tagged as the extraction kind.
    [call] = stub.runner.calls
    assert call["call_kind"] == "working_memory_extraction"
    assert (call["input_tokens"], call["output_tokens"]) == (10, 5)


async def test_trigger_resets_in_flight_on_exception():
    stub = _make_stub(current_turn_id="turn-5", last_extract="turn-1", last_compaction=None)

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
    sys_events = [e for e in emitted if e.get("type") == "system_event"]
    assert [e["subtype"] for e in sys_events] == ["working_memory_start", "working_memory_complete"]
    assert sys_events[1]["details"]["status"] == "failed"
    assert sys_events[1]["details"]["error"]
