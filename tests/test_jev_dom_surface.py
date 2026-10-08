"""DOM surface: snapshot mapping, stale rebinding, readback, gates, probe, and
surface selection / fallback, all against a fake `run_js`."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from bridge.tools.jev_operator import act as act_module
from bridge.tools.jev_operator import dom_surface
from bridge.tools.jev_operator.dom_surface import DOMSurface, DOMUnavailable
from bridge.tools.jev_operator.loop import is_irreversible
from bridge.tools.jev_operator.observe import meaningful_change
from tests.test_jev_operator import (
    FakeCalculator,
    ScriptedProvider,
    make_operator,
)

TARGET = SimpleNamespace(name="Arc", bundle="company.thebrowser.Browser", pid=7)


def action(i, kind="click", label="Go", context="", **kw):
    d = {"id": i, "kind": kind, "role": "button", "label": label, "value": "", "checked": None,
         "expanded": None, "offscreen": None, "context": context, "focused": False, "guard": f"g{i}"}
    d.update(kw)
    return d


def snap(actions, url="https://x.test/", text="hello", title="X"):
    return {"url": url, "title": title, "readyState": "complete", "scroll": {"x": 0, "y": 0, "maxY": 0},
            "viewport": {"w": 800, "h": 600}, "text": text, "mutations": 0, "actions": actions, "unoffered": []}


class FakePage:
    def __init__(self, actions):
        self.actions = actions
        self.calls: list[list] = []
        self.act_results: list[dict] = []
        self.fail = False

    def __call__(self, bundle, js):
        if self.fail:
            raise DOMUnavailable("boom")
        if js.endswith("snapshot();})()"):
            return json.dumps(snap(self.actions))
        m = re.search(r"act\((.*)\);\}\)\(\)$", js, re.S)
        args = json.loads("[" + m.group(1) + "]")
        self.calls.append(args)
        res = self.act_results.pop(0) if self.act_results else {"ok": True, "reason": None, "readback": None}
        return json.dumps(res)


def surface(page):
    return DOMSurface(bundle=TARGET.bundle, actuator=SimpleNamespace(), run_js=page, js_source="/*js*/")


def run(coro):
    return asyncio.run(coro)


def test_observation_mapping_and_context_folding():
    page = FakePage([
        action(1, label="Add", context="Claude · $200.00"),
        action(2, "link", "Docs"),
        action(3, "fill", "Search", value="abc", focused=True),
        action(4, "toggle", "Agree", checked=True),
        action(5, "select", "Plan", value="Pro"),
        action(6, "click", "Footer", offscreen="below"),
    ])
    s = surface(page)
    obs = run(s.observe(TARGET))
    labels = [e.label for e in obs.elements]
    assert labels[0] == "Add (Claude · $200.00)"
    assert [e.role for e in obs.elements] == ["AXButton", "AXLink", "AXTextField", "AXCheckBox", "AXComboBox", "AXButton"]
    assert obs.elements[3].toggle_state() == "on"
    # Off-screen page elements stay targetable: act() scrolls them into view.
    assert "below the visible area" in obs.elements[5].row() and obs.elements[5] in obs.click_targets
    assert obs.screen_text == "hello" and "x.test" in obs.focused_window
    assert len(obs.type_targets) == 2
    other = run(surface(FakePage([action(1)])).observe(TARGET))
    other.focused_window = other.focused_window.replace("x.test", "y.test")
    assert meaningful_change(obs, other)


def test_click_passes_guard_and_stale_rebinds_once():
    page = FakePage([action(1, label="Add", context="A"), action(2, label="Add", context="B")])
    s = surface(page)
    obs = run(s.observe(TARGET))
    page.actions = [action(9, label="Add", context="A", guard="new"), action(10, label="Add", context="B")]
    page.act_results = [{"ok": False, "reason": "stale"}, {"ok": True}]
    rec = run(s.execute("click", obs.elements[0]))
    assert rec.ok
    assert page.calls == [[1, "click", None, "g1"], [9, "click", None, "new"]]


def test_stale_ambiguous_fails_without_retry():
    page = FakePage([action(1, label="Add", context="A")])
    s = surface(page)
    obs = run(s.observe(TARGET))
    page.actions = [action(5, label="Add", context="A"), action(6, label="Add", context="A")]
    page.act_results = [{"ok": False, "reason": "stale"}]
    rec = run(s.execute("click", obs.elements[0]))
    assert not rec.ok and "stale" in rec.error and "2 elements" in rec.error
    assert len(page.calls) == 1


def test_fill_readback_mismatch_and_match():
    page = FakePage([action(1, "fill", "Name")])
    s = surface(page)
    obs = run(s.observe(TARGET))
    page.act_results = [{"ok": True, "readback": "Bo"}]
    rec = run(s.execute("type_into", obs.elements[0], "Bob", replace=True))
    assert not rec.ok and "readback mismatch" in rec.error
    assert page.calls[-1][2] == {"text": "Bob", "mode": "replace"}
    page.act_results = [{"ok": True, "readback": "Bob"}]
    assert run(s.execute("type_into", obs.elements[0], "Bob", replace=True)).ok


def test_secure_field_refused():
    page = FakePage([action(1, "fill", "Password", role="password", value="x")])
    s = surface(page)
    obs = run(s.observe(TARGET))
    assert obs.elements[0].value == "••••" and obs.elements[0] not in obs.type_targets
    rec = run(s.execute("type_into", obs.elements[0], "pw", replace=True))
    assert not rec.ok and "secure" in rec.error and page.calls == []


def test_irreversible_gate_sees_dom_targets():
    obs = run(surface(FakePage([action(1, label="Submit"), action(2, "link", "Delete", context="Row 3"),
                                action(3, label="Next")])).observe(TARGET))
    assert [is_irreversible(e) for e in obs.elements] == [True, True, False]


def dom_operator(page, goal="go", **kw):
    op, events = make_operator(FakeCalculator(), kw.pop("provider"), goal=goal, **kw)
    op.surface = surface(page)
    op._surface_injected = True
    return op


def test_loop_stops_on_dom_delete(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    page = FakePage([action(1, label="Delete", context="Row 3")])
    op = dom_operator(page, provider=ScriptedProvider([("click", "Delete (Row 3)")]), goal="delete row 3")
    res = run(op.run())
    assert res.status == "needs_confirmation" and page.calls == [] and res.surface == "dom"


def test_enter_in_form_field_gated_by_submit_button(tmp_path, monkeypatch):
    """Enter is decided per field from the snapshot's `enter` hint: a form whose
    submit button looks irreversible, or a field whose Enter handling is unknown
    (a chat box may send), needs confirmation; a search box or a form with a
    harmless submit button does not."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    def attempt(enter: str, label: str = "Amount") -> tuple[str, list]:
        page = FakePage([action(1, "fill", label, focused=True, enter=enter), action(2, label="Go")])
        op = dom_operator(page, provider=ScriptedProvider([("key", "return"), ("done", None)]), goal="go")
        return run(op.run()).status, page.calls

    assert attempt("form:Pay now") == ("needs_confirmation", [])
    assert attempt("unknown", "Message") == ("needs_confirmation", [])
    assert attempt("search", "Search")[0] == "done"
    assert attempt("form:Save")[1] == [[0, "key", "Enter", ""]]
    assert attempt("form")[0] == "done"


def test_probe():
    assert dom_surface.probe("company.thebrowser.Browser", lambda b, js: "Title") == (True, "title='Title'")
    def bad(b, js):
        raise DOMUnavailable("Allow JavaScript from Apple Events")
    ok, detail = dom_surface.probe("com.google.Chrome", bad)
    assert not ok and "Allow JavaScript" in detail
    assert dom_surface.probe("com.apple.Safari", bad) == (False, "unsupported browser")


def browser_operator(tmp_path, monkeypatch, probe_result, surface_arg="auto", bundle="company.thebrowser.Browser"):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    monkeypatch.setattr(dom_surface, "probe", lambda b, run_js=None, timeout_s=None: probe_result)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("done", None)]), surface=surface_arg)
    return op, native, SimpleNamespace(name="Arc", bundle=bundle, pid=100)


def rows(op):
    return [json.loads(l) for l in op.log_path.read_text().splitlines()]


@pytest.mark.parametrize("probe_result,bundle,surf,expect", [
    ((True, "title='x'"), "company.thebrowser.Browser", "auto", "dom"),
    ((False, "no js"), "company.thebrowser.Browser", "auto", "ax"),
    ((True, "x"), "com.apple.Notes", "auto", "ax"),
    ((True, "x"), "company.thebrowser.Browser", "ax", "ax"),
])
def test_auto_selection(tmp_path, monkeypatch, probe_result, bundle, surf, expect):
    op, _, target = browser_operator(tmp_path, monkeypatch, probe_result, surf, bundle)
    run(op._select_surface(target))
    assert op.surface.name == expect
    if surf == "auto" and bundle != "com.apple.Notes":
        probes = [r for r in rows(op) if r.get("event") == "surface_probe"]
        assert probes and probes[0]["ok"] is probe_result[0]


def test_double_failure_falls_back_to_ax_once(tmp_path, monkeypatch):
    op, native, target = browser_operator(tmp_path, monkeypatch, (True, "ok"))
    run(op._select_surface(target))
    assert op.surface.name == "dom"
    op.surface._run_js = lambda b, js: (_ for _ in ()).throw(RuntimeError("osascript died"))
    obs = run(op._observe(SimpleNamespace(name="Calculator", bundle="com.apple.calculator", pid=100)))
    assert op.surface.name == "ax" and obs.elements
    evs = [r for r in rows(op) if r.get("event") in ("dom_error", "surface_fallback")]
    assert [e["event"] for e in evs] == ["dom_error", "dom_error", "surface_fallback"]
    assert evs[-1]["surface"] == "dom" and op._surface_label() == "dom→ax"
    op._swap_if_dom_failing()
    assert op.surface.name == "ax"


def test_arc_double_encoded_results_decode():
    """Arc JSON-encodes a script's return value, so the snapshot/act JSON
    strings arrive as quoted strings (found on a live Arc tab 2026-10-07)."""
    page = FakePage([action(1, label="Go"), action(2, "fill", "Search")])
    inner = page.__call__

    def arc_like(bundle, js):
        return json.dumps(inner(bundle, js))

    s = DOMSurface(bundle=TARGET.bundle, actuator=SimpleNamespace(), run_js=arc_like, js_source="/*js*/")
    obs = run(s.observe(TARGET))
    assert [e.label for e in obs.elements] == ["Go", "Search"]
    res = run(s._js('act(1, "click", null, "g1")'))
    assert res["ok"] is True


def test_select_options_are_click_targets():
    """A <select> lists its options as rows; clicking one selects it by value.
    (Live run 2026-10-08: option rows were built without required fields and
    every snapshot of a page with a dropdown failed.)"""
    sel = action(2, "select", "Team", value="", options=[
        {"v": "", "t": "Choose a team", "sel": True}, {"v": "r", "t": "Research", "sel": False}])
    page = FakePage([action(1, "fill", "Full name"), sel])
    s = surface(page)
    obs = run(s.observe(TARGET))
    rows_ = [e.label for e in obs.elements]
    assert "Research (option of Team)" in rows_
    opt = next(e for e in obs.elements if e.label == "Research (option of Team)")
    assert opt in obs.click_targets and opt.role == "AXMenuItem"
    rec = run(s.execute("click", opt))
    assert rec.ok and page.calls == [[2, "select", "r", "g2"]]


def test_unparseable_snapshot_falls_back_to_ax(tmp_path, monkeypatch):
    """The transport answers but the snapshot cannot be parsed: that is a surface
    failure too, so two in a row move the run to AX instead of crashing it."""
    op, native, target = browser_operator(tmp_path, monkeypatch, (True, "ok"))
    run(op._select_surface(target))
    op.surface._run_js = lambda b, js: json.dumps({"url": "x", "actions": [{"kind": "click", "id": "not-an-int"}]})
    obs = run(op._observe(SimpleNamespace(name="Calculator", bundle="com.apple.calculator", pid=100)))
    assert op.surface.name == "ax" and obs.elements and op._surface_label() == "dom→ax"


def test_probe_retries_once_when_the_browser_is_slow(tmp_path, monkeypatch):
    """One 3 s timeout sent a whole Arc run to the accessibility tree (8 s reads);
    a slow first answer now gets one longer retry."""
    op, native, target = browser_operator(tmp_path, monkeypatch, (True, "x"))
    answers = iter([(False, "Arc did not answer within 3s"), (True, "title='x'")])
    seen = []

    def probe(b, run_js=None, timeout_s=None):
        seen.append(timeout_s)
        return next(answers)

    monkeypatch.setattr(dom_surface, "probe", probe)
    monkeypatch.setattr(DOMSurface, "pin_active", lambda self: asyncio.sleep(0))
    run(op._select_surface(target))
    assert op.surface.name == "dom" and seen == [None, dom_surface.PROBE_RETRY_TIMEOUT_S]
    probe_rows = [r for r in rows(op) if r.get("event") == "surface_probe"]
    assert probe_rows[-1]["ok"] is True and "retry" in probe_rows[-1]["detail"]


def test_filling_a_field_is_progress():
    """Typing into a form changes nothing else on screen; the field taking the
    text is the progress. Three verified fields were counted as "stuck"."""
    from bridge.tools.jev_operator.loop import own_effect

    s = surface(FakePage([]))
    before = s._build(snap([action(1, "fill", "Full name"), action(2, "toggle", "Remote", checked=False)]), TARGET, 1)
    after = s._build(snap([action(1, "fill", "Full name", value="Ada"), action(2, "toggle", "Remote", checked=True)]), TARGET, 1)
    name_b, remote_b = before.elements
    key = lambda e: (e.window, e.role, e.label)  # noqa: E731
    assert own_effect({"kind": "type", "ok": True}, key(name_b), before, after)
    assert own_effect({"kind": "click", "ok": True}, key(remote_b), before, after)
    assert not own_effect({"kind": "type", "ok": True}, key(name_b), before, before)
    assert not own_effect({"kind": "key", "ok": True}, key(name_b), before, after)


def test_planner_open_url_stays_on_known_sites(tmp_path, monkeypatch):
    """The planner may build an address (a site's own search URL) but only on a
    site the goal names or the run has been on."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    page = FakePage([action(1, label="Docs")])
    op = dom_operator(page, provider=ScriptedProvider([("done", None)]),
                      goal="Open https://developer.mozilla.org/en-US/ and find Array.flat")
    op.actuator.target_bundle = "company.thebrowser.Browser"
    obs = run(op.surface.observe(TARGET))
    run(op._direct({"kind": "open_url", "url": "https://developer.mozilla.org/en-US/search?q=flat"}, obs))
    run(op._direct({"kind": "open_url", "url": "https://evil.example/steal"}, obs))
    acts = [h["action"] for h in op.history]
    assert acts[0].startswith("planner open_url https://developer.mozilla.org") and op.history[0]["ok"]
    assert acts[1].endswith("refused") and op.history[1]["ok"] is False
