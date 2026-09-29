"""Unit tests for the Jev operator: observation, decision gating, and the
loop against a fake accessibility tree and a scripted decision provider."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.decisions.types import Answers, Choice, ChoiceAnswer, NoulAnswer
from bridge.tools.computer_tools import ComputerToolSpec
from bridge.tools.jev_operator import act as act_module
from bridge.tools.jev_operator.decide import Thresholds, build_questions, interpret
from bridge.tools.jev_operator.handoff import LLMHelper, parse_json
from bridge.tools.jev_operator.loop import (
    Operator,
    OperatorConfig,
    is_irreversible,
    launch_candidates_from_goal,
)
from bridge.tools.jev_operator.observe import (
    build_observation,
    diff_observations,
    literals_from_goal,
)

# ─── fixtures ────────────────────────────────────────────────────────

ROLE_DESCRIPTIONS = {
    "AXApplication": "application",
    "AXButton": "button",
    "AXGroup": "group",
    "AXMenu": "menu",
    "AXMenuBar": "menu bar",
    "AXMenuBarItem": "menu bar item",
    "AXMenuItem": "menu item",
    "AXRow": "row",
    "AXScrollArea": "scroll area",
    "AXStaticText": "text",
    "AXTextArea": "text entry area",
    "AXTextField": "text field",
    "AXWindow": "standard window",
}


@pytest.fixture(autouse=True)
def nobody_at_the_keyboard(monkeypatch):
    monkeypatch.setattr(act_module, "seconds_since_input", lambda: 999.0)
    monkeypatch.setattr(act_module, "ACTIVATION_SETTLE_S", 0.0)


CLOSED = (0, 1117, 0, 0)  # where AppKit reports the items of a menu that is not showing


def node(role: str, title: str | None = None, *, value: Any = None, bounds=(0, 0, 40, 30), children=None, **kw):
    """Shaped like freyja_native.read_ax_tree: `label` is AXDescription, and every
    element carries its AXRoleDescription in `description`."""
    d: dict[str, Any] = {
        "role": role,
        "bounds": list(bounds),
        "children": children or [],
        "description": ROLE_DESCRIPTIONS.get(role, role.removeprefix("AX").lower()),
    }
    if title is not None:
        d["title"] = title
    if value is not None:
        d["value"] = value
    d.update(kw)
    return d


def calculator_tree(display: str = "0") -> dict[str, Any]:
    buttons = []
    labels = ["All Clear", "Negate", "Percent", "Divide", "7", "8", "9", "Multiply", "4", "5", "6", "Subtract", "1", "2", "3", "Add", "0", "Decimal Point", "Equals"]
    for i, lab in enumerate(labels):
        buttons.append(node("AXButton", None, label=lab, bounds=(10 + (i % 4) * 50, 100 + (i // 4) * 40, 40, 30)))
    menubar = node("AXMenuBar", children=[
        node("AXMenuBarItem", "Apple", bounds=(0, 0, 30, 20)),
        node("AXMenuBarItem", "File", bounds=(40, 0, 30, 20), children=[node("AXMenu", children=[node("AXMenuItem", "Close", bounds=CLOSED)])]),
        node("AXMenuBarItem", "View", bounds=(80, 0, 30, 20), children=[node("AXMenu", children=[])]),
    ])
    win = node("AXWindow", "Calculator", subrole="AXStandardWindow", focused=True, bounds=(0, 40, 220, 300), children=[
        node("AXGroup", children=[node("AXStaticText", None, value=display, bounds=(10, 50, 200, 40))]),
        node("AXGroup", children=buttons),
        node("AXButton", None, description="close button", subrole="AXCloseButton", bounds=(5, 42, 12, 12)),
    ])
    return node("AXApplication", "Calculator", bounds=(0, 0, 0, 0), children=[menubar, win])


def test_direction_marks_are_stripped_from_screen_text_and_values():
    obs = build_observation(calculator_tree("\u200e123\u200e×\u200e45"), app_name="Calculator", bundle="b", pid=1)
    assert obs.screen_text == "123×45"


def test_toggles_read_as_on_and_off_in_rows_and_diffs():
    def tree(bold):
        win = node("AXWindow", "Untitled", focused=True, bounds=(0, 40, 500, 400), children=[
            node("AXCheckBox", None, label="bold", value=bold, bounds=(10, 50, 20, 20)),
        ])
        return node("AXApplication", "TextEdit", children=[win])

    before = build_observation(tree(0), app_name="TextEdit", bundle="b", pid=1)
    after = build_observation(tree(1), app_name="TextEdit", bundle="b", pid=1)
    assert "AXCheckBox 'bold' (off)" in before.table()
    changed, summary = diff_observations(before, after)
    assert changed and "'bold' now on" in summary


def test_llm_doors_see_whole_documents_but_jev_rows_stay_short():
    doc = "\n".join(f"line {i}: " + "x" * 40 for i in range(20))
    win = node("AXWindow", "Notes", focused=True, bounds=(0, 40, 500, 400), children=[
        node("AXTextArea", None, value=doc, bounds=(10, 60, 480, 300)),
    ])
    obs = build_observation(node("AXApplication", "TextEdit", children=[win]), app_name="TextEdit", bundle="b", pid=1)
    assert "line 19" not in obs.table()
    assert "line 19" in obs.door_table()
    assert "\\nline 1:" in obs.door_table(), "line breaks survive for the doors"


def test_scrolling_targets_the_scroll_view_that_hides_the_named_row():
    names = ["General", "Appearance", "Wallpaper", "Keyboard", "Trackpad", "Printers & Scanners"]
    rows = [node("AXRow", n, bounds=(0, 60 + 60 * i, 200, 28)) for i, n in enumerate(names)]
    sidebar = node("AXScrollArea", None, bounds=(0, 50, 200, 250), children=[node("AXOutline", None, bounds=(0, 50, 200, 400), children=rows)])
    content = node("AXScrollArea", None, bounds=(210, 50, 400, 250), children=[
        node("AXButton", "Add Printer", bounds=(300, 400, 80, 20)),
    ])
    win = node("AXWindow", "Settings", focused=True, bounds=(0, 40, 620, 300), children=[sidebar, content])
    obs = build_observation(node("AXApplication", "System Settings", children=[win]), app_name="System Settings", bundle="b", pid=1)
    assert [e.label for e in obs.elements if e.offscreen] == ["Trackpad", "Printers & Scanners", "Add Printer"]
    assert obs.scroll_point("open the Printers & Scanners section") == (100, 175)
    assert obs.scroll_point("click Add Printer") == (410, 175)
    assert obs.scroll_point("scroll down") == (100, 175), "otherwise the view hiding the most rows"


def test_window_buttons_name_their_window():
    win = node("AXWindow", "notes.txt", focused=True, bounds=(0, 40, 500, 400), children=[
        node("AXButton", None, label="close button", subrole="AXCloseButton", bounds=(8, 44, 14, 14)),
    ])
    obs = build_observation(node("AXApplication", "TextEdit", children=[win]), app_name="TextEdit", bundle="b", pid=1)
    assert "AXButton 'close button' (of window 'notes.txt')" in obs.table()


def test_unnamed_state_controls_take_their_row_name_not_their_value():
    def group(app, expanded, children):
        cell = node("AXCell", None, bounds=(10, 60, 400, 30), children=[
            node("AXHeading", None, value=app, bounds=(40, 64, 100, 20)),
            node("AXDisclosureTriangle", None, value=1 if expanded else 0, bounds=(380, 66, 14, 14)),
        ])
        return [node("AXRow", None, bounds=(10, 60, 400, 30), children=[cell]), *children]

    def switch_row(app, on, y):
        return node("AXRow", None, bounds=(10, y, 400, 30), children=[node("AXCell", None, bounds=(10, y, 400, 30), children=[
            node("AXStaticText", None, value=app, bounds=(60, y + 4, 100, 20)),
            node("AXCheckBox", None, value=1 if on else 0, bounds=(360, y + 6, 30, 18)),
        ])])

    rows = group("Freyja.app", True, [switch_row("TextEdit", False, 100), switch_row("Preview", False, 140)])
    win = node("AXWindow", "Automation", focused=True, bounds=(0, 40, 500, 400), children=[
        node("AXOutline", None, bounds=(10, 60, 400, 300), children=rows),
    ])
    table = build_observation(node("AXApplication", "System Settings", children=[win]), app_name="S", bundle="b", pid=1).table()
    assert "AXDisclosureTriangle 'Freyja.app' (expanded)" in table, table
    assert "AXCheckBox 'TextEdit' (off)" in table and "AXCheckBox 'Preview' (off)" in table, table
    assert "'0'" not in table and "'1'" not in table


def form_tree() -> dict[str, Any]:
    win = node("AXWindow", "Sign in", focused=True, bounds=(0, 40, 400, 300), children=[
        node("AXTextField", "Username", value="", bounds=(20, 80, 200, 24)),
        node("AXTextField", None, subrole="AXSecureTextField", label="Password", description="secure text field", value="", bounds=(20, 120, 200, 24)),
        node("AXTextArea", "Notes", value="draft", bounds=(20, 160, 300, 80)),
        node("AXButton", "Delete account", bounds=(20, 260, 120, 30)),
        node("AXButton", "Continue", bounds=(160, 260, 120, 30)),
        node("AXButton", "Disabled thing", enabled=False, bounds=(300, 260, 60, 30)),
    ])
    return node("AXApplication", "FormApp", children=[win])


def test_observation_table_and_text():
    obs = build_observation(calculator_tree("42"), app_name="Calculator", bundle="com.apple.calculator", pid=1)
    rows = obs.table().split("\n")
    assert any("AXButton 'Equals'" in r for r in rows)
    assert any("AXMenuBarItem 'File'" in r for r in rows), "menu bar items are clickable rows"
    assert not any("'Close'" in r and "AXMenuItem" in r for r in rows), "closed menus are not expanded"
    assert obs.screen_text == "42"
    assert obs.focused_window == "Calculator"
    assert obs.windows == ["Calculator"]
    assert all(e.kind == "click" for e in obs.elements)
    assert obs.by_index(1) is not None and obs.by_index(1).index == 1
    assert not obs.menu_open and obs.dialog is None


def test_open_menu_items_become_rows():
    t = calculator_tree()
    # simulate the File menu being open: its AXMenu now has children (already does in fixture)
    # and mark it via a top-level AXMenu child as macOS does for open menus
    t["children"].append(node("AXMenu", children=[node("AXMenuItem", "Basic", bounds=(80, 20, 100, 20)), node("AXMenuItem", "Scientific", bounds=(80, 40, 100, 20))]))
    obs = build_observation(t, app_name="Calculator", bundle="b", pid=1)
    assert obs.menu_open
    assert any(e.role == "AXMenuItem" and e.label == "Scientific" for e in obs.elements)


def test_form_observation_marks_secure_and_disabled():
    obs = build_observation(form_tree(), app_name="FormApp", bundle="b", pid=2)
    labels = {e.label: e for e in obs.elements}
    assert labels["Username"].kind == "type"
    assert labels["Password"].kind == "type" and labels["Password"].subrole == "AXSecureTextField"
    assert "(secure)" in labels["Password"].row()
    assert labels["Notes"].value == "draft"
    assert not labels["Disabled thing"].enabled and "(disabled)" in labels["Disabled thing"].row()
    assert labels["Disabled thing"] not in obs.click_targets
    assert [e.label for e in obs.type_targets] == ["Username", "Notes"], "secure fields are context, never targets"
    assert obs.element_at(30, 90).label == "Username" and obs.element_at(999, 999) is None


def test_diff_reports_value_change_and_window_switch():
    a = build_observation(calculator_tree("0"), app_name="c", bundle="b", pid=1)
    b = build_observation(calculator_tree("12"), app_name="c", bundle="b", pid=1)
    changed, summary = diff_observations(a, b)
    assert changed and "new text: 12" in summary
    same, summary2 = diff_observations(a, build_observation(calculator_tree("0"), app_name="c", bundle="b", pid=1))
    assert not same and summary2 == "no visible change"
    f = build_observation(form_tree(), app_name="c", bundle="b", pid=1)
    changed, summary = diff_observations(a, f)
    assert changed and "window 'Calculator' -> 'Sign in'" in summary


def test_literals_and_launch_candidates():
    assert literals_from_goal('type "hello world" then enter 42 into the amount field') == ["hello world", "42"]
    assert literals_from_goal("open the downloads folder") == []
    apps = ["Calculator", "Finder", "System Settings", "TextEdit"]
    assert launch_candidates_from_goal("open calculator and compute 12 x 7", apps) == ["Calculator"]
    assert launch_candidates_from_goal("in System Settings, open Wallpaper", apps) == ["System Settings"]
    assert launch_candidates_from_goal("recalculators are not apps", apps) == []


def test_irreversible_detection():
    obs = build_observation(form_tree(), app_name="c", bundle="b", pid=1)
    labels = {e.label: e for e in obs.elements}
    assert is_irreversible(labels["Delete account"])
    assert not is_irreversible(labels["Continue"])


# ─── decision interpretation ─────────────────────────────────────────


def answers(**kw: Any) -> Answers:
    out: dict[str, Any] = {}
    for k, v in kw.items():
        if isinstance(v, tuple):
            choice, conf = v
            out[k] = ChoiceAnswer(choice=choice, probabilities={choice: conf}, confidence=conf)
        else:
            out[k] = NoulAnswer(p=float(v))
    return Answers(answers=out, model="fake", provider="fake", latency_ms=5)


def test_interpret_gates_low_confidence_and_none():
    obs = build_observation(calculator_tree(), app_name="c", bundle="b", pid=1)
    eq = next(e for e in obs.elements if e.label == "Equals")
    th = Thresholds()
    d = interpret(answers(operation=("click", 0.9), click_target=(str(eq.index), 0.95), key_target=("none", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "click" and d.target is eq and not d.reasons
    d = interpret(answers(operation=("click", 0.9), click_target=(str(eq.index), 0.3), key_target=("none", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "need_help" and d.target is None
    d = interpret(answers(operation=("click", 0.9), click_target=("none", 0.9), key_target=("none", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "need_help"
    d = interpret(answers(operation=("click", 0.9), click_target=("999", 0.9), key_target=("none", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "need_help", "ids outside the table are refused"
    d = interpret(answers(operation=("done", 0.4), click_target=("none", 0.9), key_target=("none", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "need_help"


def test_interpret_type_paths():
    obs = build_observation(form_tree(), app_name="c", bundle="b", pid=1)
    user = next(e for e in obs.elements if e.label == "Username")
    pw = next(e for e in obs.elements if e.label == "Password")
    th = Thresholds()
    base = dict(operation=("type", 0.9), click_target=("none", 0.9), key_target=("none", 0.9), unexpected_dialog=0.02)
    d = interpret(answers(**base, type_target=(str(user.index), 0.9), text_source=("lit0", 0.9)), obs, literals=["soham"], launch_candidates=[], thresholds=th)
    assert d.operation == "type" and d.text_literal == "soham" and not d.text_needs_llm
    d = interpret(answers(**base, type_target=(str(user.index), 0.9), text_source=("needs_llm", 0.9)), obs, literals=["soham"], launch_candidates=[], thresholds=th)
    assert d.operation == "type" and d.text_literal is None and d.text_needs_llm
    d = interpret(answers(**base, type_target=(str(pw.index), 0.95), text_source=("needs_llm", 0.9)), obs, literals=[], launch_candidates=[], thresholds=th)
    assert d.operation == "blocked" and "secure" in d.reasons[0]


def test_no_click_targets_omits_click_question_and_routes_to_need_help():
    empty = build_observation(node("AXApplication", "X", children=[node("AXWindow", "W", focused=True, bounds=(0, 0, 100, 100))]), app_name="x", bundle="b", pid=1)
    qs = build_questions(empty, goal="g", subgoal=None, literals=[], launch_candidates=[])
    assert "click_target" not in qs and "click" not in qs["operation"].options
    d = interpret(answers(operation=("click", 0.9), key_target=("none", 0.9), unexpected_dialog=0.1), empty, literals=[], launch_candidates=[], thresholds=Thresholds())
    assert d.operation == "need_help"


def test_malformed_option_ids_do_not_raise():
    obs = build_observation(form_tree(), app_name="c", bundle="b", pid=1)
    user = next(e for e in obs.elements if e.label == "Username")
    base = dict(operation=("type", 0.9), click_target=("none", 0.9), key_target=("none", 0.9), unexpected_dialog=0.02)
    d = interpret(answers(**base, type_target=(str(user.index), 0.9), text_source=("litx", 0.9)), obs, literals=["a"], launch_candidates=[], thresholds=Thresholds())
    assert d.operation == "type" and d.text_needs_llm
    d = interpret(answers(operation=("launch_app", 0.9), click_target=("none", 0.9), key_target=("none", 0.9), launch_target=("appx", 0.9), unexpected_dialog=0.02), obs, literals=[], launch_candidates=["Safari"], thresholds=Thresholds())
    assert d.operation == "need_help"


def test_build_questions_shape():
    obs = build_observation(form_tree(), app_name="c", bundle="b", pid=1)
    qs = build_questions(obs, goal="sign in as bob", subgoal="click Continue", literals=["bob"], launch_candidates=["Safari"])
    assert set(qs) == {"operation", "click_target", "type_target", "text_source", "key_target", "launch_target", "unexpected_dialog", "subgoal_complete"}
    op = qs["operation"]
    assert isinstance(op, Choice) and "launch_app" in op.options and "type" in op.options
    assert "none" in qs["click_target"].options and "needs_llm" in qs["text_source"].options
    assert "Goal: sign in as bob" in op.instructions and "Current sub-goal" in op.instructions
    # every option set has a null option and fewer than 256 entries
    for q in qs.values():
        if isinstance(q, Choice):
            assert len(q.options) <= 255


def test_parse_json_variants():
    assert parse_json('```json\n{"text": "hi"}\n```') == {"text": "hi"}
    assert parse_json('Sure. {"a": 1, "b": {"c": [1,2]}} trailing') == {"a": 1, "b": {"c": [1, 2]}}
    assert parse_json("no json here") is None


# ─── loop with fakes ─────────────────────────────────────────────────


@dataclass
class FakeBounds:
    x: float
    y: float
    w: float
    h: float


@dataclass
class FakeWindow:
    id: int
    pid: int
    bundle: str
    title: str
    bounds: FakeBounds
    is_frontmost: bool = True
    layer: int = 0


@dataclass
class FakeDisplay:
    id: int = 1
    width: int = 1000
    height: int = 800
    scale: float = 1.0
    is_primary: bool = True


class FakePermissions:
    @staticmethod
    def screen_recording() -> bool:
        return False


class FakeCalculator:
    """A calculator whose AX tree changes when its buttons are clicked."""

    def __init__(self) -> None:
        self.display = "0"
        self.pending: tuple[str, float] | None = None
        self.clicks: list[str] = []
        self.keys: list[str] = []
        self.focused: list[str] = []
        self.Permissions = FakePermissions

    def list_windows(self, *, include_helpers: bool = False):
        return [FakeWindow(1, 100, "com.apple.calculator", "Calculator", FakeBounds(0, 40, 220, 300))]

    def list_displays(self):
        return [FakeDisplay()]

    def read_ax_tree(self, pid: int, max_depth: int = 8) -> str:
        return json.dumps(calculator_tree(self.display))

    def focus_app(self, bundle: str) -> None:
        self.focused.append(bundle)

    def click(self, x, y, *, button="left", double=False, modifiers=None) -> None:
        tree = json.loads(self.read_ax_tree(0))
        label = None

        def walk(n):
            nonlocal label
            b = n.get("bounds")
            if n.get("role") == "AXButton" and b and b[0] <= x <= b[0] + b[2] and b[1] <= y <= b[1] + b[3]:
                label = n.get("label") or n.get("title")
            for c in n.get("children") or []:
                walk(c)

        walk(tree)
        self.clicks.append(label or f"({x},{y})")
        if label is None:
            return
        if label.isdigit():
            self.display = label if self.display == "0" or self.pending and self.pending[1] == float(self.display) else self.display + label
        elif label == "Multiply":
            self.pending = ("*", float(self.display))
        elif label == "Equals" and self.pending:
            self.display = str(int(self.pending[1] * float(self.display)))
            self.pending = None
        elif label == "All Clear":
            self.display = "0"
            self.pending = None

    def type_text(self, text: str) -> None:
        pass

    def press_key(self, key: str, modifiers=None) -> None:
        self.keys.append("+".join([*(modifiers or []), key]))

    def scroll(self, dx, dy, x=None, y=None) -> None:
        pass


class ScriptedProvider:
    """Answers each step from a script of (operation, target_label) pairs."""

    def __init__(self, script: list[tuple[str, str | None]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.name = "scripted"

    async def decide(self, state, questions, *, model=None) -> Answers:
        self.calls.append({"state": state, "questions": questions})
        op, label = self.script.pop(0) if self.script else ("done", None)
        out: dict[str, Any] = {}
        for name, q in questions.items():
            if isinstance(q, Choice):
                pick = "none"
                if name == "operation":
                    pick = op
                elif name == "click_target" and label and op in ("click", "double_click"):
                    pick = next((k for k, v in q.options.items() if v.endswith(" " + label)), "none")
                elif name == "key_target" and op == "key" and label:
                    pick = label
                probs = {k: (0.92 if k == pick else 0.08 / max(1, len(q.options) - 1)) for k in q.options}
                out[name] = ChoiceAnswer(choice=pick, probabilities=probs, confidence=0.92)
            else:
                out[name] = NoulAnswer(p=0.05)
        return Answers(answers=out, model="fake", provider="scripted", latency_ms=3)

    def decide_sync(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def make_operator(native, provider, goal="compute 12 × 7 and show the result", llm=None, frontmost=100, app="com.apple.calculator", **cfg):
    spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
    config = OperatorConfig(goal=goal, app=app, max_steps=cfg.pop("max_steps", 12), use_llm=llm is not None, settle_ms=0, launch_if_missing=False, **cfg)
    events: list[str] = []
    op = Operator(config, provider=provider, spec=spec, native=native, llm=llm, on_step=events.append, apps=["Calculator"])
    op.actuator.frontmost = frontmost if callable(frontmost) else (lambda: frontmost)
    op._screen_locked = lambda: False
    return op, events


def test_loop_calculator_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    provider = ScriptedProvider([("click", "1"), ("click", "2"), ("click", "Multiply"), ("click", "7"), ("click", "Equals"), ("done", None)])
    op, events = make_operator(native, provider)
    res = asyncio.run(op.run())
    assert res.status == "done", res.summary
    assert native.display == "84"
    assert native.clicks == ["1", "2", "Multiply", "7", "Equals"]
    assert res.steps == 5 and res.jev_calls == 6 and res.llm_calls == 0
    diffs = [h["diff"] for h in res.history]
    assert "new text: 1" in diffs[0] and "new text: 12" in diffs[1]
    assert res.history[2]["changed"] is False, "multiply does not change the display"
    assert "84" in res.summary
    # Jev saw the goal in instructions and the history in state
    first = provider.calls[0]
    assert "Goal: compute 12 × 7" in first["questions"]["operation"].instructions
    assert provider.calls[-1]["state"]["recent_actions"][-1]["action"].startswith("click")
    log_lines = [json.loads(l) for l in (tmp_path / f"{res.run_id}.jsonl").read_text().splitlines()]
    assert [r["event"] for r in log_lines][:2] == ["start", "decision"] and log_lines[-1]["event"] == "finish"


def test_loop_stops_for_irreversible_control(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class FormNative(FakeCalculator):
        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.calculator", "Sign in", FakeBounds(0, 40, 400, 300))]

        def read_ax_tree(self, pid, max_depth=8):
            return json.dumps(form_tree())

    native = FormNative()
    op, _ = make_operator(native, ScriptedProvider([("click", "Delete account")]), goal="delete the account")
    res = asyncio.run(op.run())
    assert res.status == "needs_confirmation" and "Delete account" in (res.pending_action or "")
    assert native.clicks == []
    op2, _ = make_operator(native, ScriptedProvider([("click", "Delete account"), ("done", None)]), goal="delete the account", allow_irreversible=True)
    res2 = asyncio.run(op2.run())
    assert res2.status == "done" and native.clicks == ["Delete account"]


def test_loop_stuck_goes_through_replan_door(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class Inert(FakeCalculator):
        def click(self, x, y, **kw):
            self.clicks.append("noop")

    seen: list[str] = []

    async def fake_complete(messages, system_prompt, max_tokens):
        seen.append(system_prompt[:40])
        return json.dumps({"status": "give_up", "subgoal": None, "direct_action": None, "note": "the buttons do nothing"})

    llm = LLMHelper(fake_complete, max_calls=3)
    op, events = make_operator(Inert(), ScriptedProvider([("click", "1")] * 8), llm=llm)
    res = asyncio.run(op.run())
    assert res.status == "blocked" and "buttons do nothing" in res.summary
    assert len(seen) == 1 and seen[0].startswith("You are the planner")
    assert sum(1 for h in res.history if h.get("changed") is False) == 3, "stuck rule fires after three unchanged actions"


def test_loop_need_help_replan_sets_subgoal_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()

    async def fake_complete(messages, system_prompt, max_tokens):
        payload = json.loads(messages[0].content if isinstance(messages[0].content, str) else messages[0].content[-1].text)
        if "why_help_was_requested" in payload:
            return json.dumps({"status": "continue", "subgoal": "click the 7 button", "direct_action": None, "note": ""})
        return json.dumps({"satisfied": True, "summary": "Typed 7; display shows 7.", "subgoal": None})

    llm = LLMHelper(fake_complete, max_calls=4)
    op, events = make_operator(native, ScriptedProvider([("need_help", None), ("click", "7"), ("done", None)]), goal="show 7", llm=llm)
    res = asyncio.run(op.run())
    assert res.status == "done" and res.summary == "Typed 7; display shows 7."
    assert native.clicks == ["7"] and res.llm_calls == 2
    assert any("sub-goal: click the 7 button" in e for e in events)
    # the sub-goal reached Jev via instructions and the subgoal_complete Noul was asked
    call = op.provider.calls[1]
    assert "Current sub-goal (from the planner): click the 7 button" in call["questions"]["operation"].instructions
    assert "subgoal_complete" in call["questions"]


def test_planner_done_is_held_to_the_end_state_check(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    verdicts = iter([
        {"satisfied": False, "summary": "the display is empty", "subgoal": "click the 7 button"},
        {"satisfied": True, "summary": "display shows 7", "subgoal": None},
    ])

    async def fake_complete(messages, system_prompt, max_tokens):
        payload = json.loads(messages[0].content if isinstance(messages[0].content, str) else messages[0].content[-1].text)
        if "why_help_was_requested" in payload:
            return json.dumps({"status": "done", "subgoal": None, "direct_action": None, "note": "looks done"})
        return json.dumps(next(verdicts))

    llm = LLMHelper(fake_complete, max_calls=6)
    op, events = make_operator(native, ScriptedProvider([("need_help", None), ("click", "7"), ("done", None)]), goal="show 7", llm=llm)
    res = asyncio.run(op.run())
    assert native.clicks == ["7"], "a planner 'done' the check rejects must not end the run"
    assert res.status == "done" and res.summary == "display shows 7"
    assert any("end-state check disagrees" in e for e in events)


def test_stalled_run_checks_the_end_state_before_reporting_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()

    async def fake_complete(messages, system_prompt, max_tokens):
        payload = json.loads(messages[0].content if isinstance(messages[0].content, str) else messages[0].content[-1].text)
        if "why_help_was_requested" in payload:
            return json.dumps({"status": "continue", "subgoal": "click the 7 button", "direct_action": None, "note": ""})
        return json.dumps({"satisfied": True, "summary": "display shows 7", "subgoal": None})

    llm = LLMHelper(fake_complete, max_calls=8)
    op, _ = make_operator(native, ScriptedProvider([("click", "7")] + [("need_help", None)] * 4), goal="show 7", llm=llm)
    res = asyncio.run(op.run())
    assert res.status == "done" and res.summary == "display shows 7", (res.status, res.summary)


def test_closing_a_window_the_goal_does_not_name_goes_through_the_end_state_check(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class TwoDocs(FakeCalculator):
        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.TextEdit", "notes.txt", FakeBounds(0, 40, 500, 400)),
                    FakeWindow(2, 100, "com.apple.TextEdit", "Untitled", FakeBounds(30, 70, 500, 400))]

        def read_ax_tree(self, pid, max_depth=8):
            wins = [node("AXWindow", title, focused=(i == 0), bounds=b, children=[
                node("AXButton", None, label=label, subrole="AXCloseButton", bounds=(b[0] + 8, b[1] + 4, 14, 14)),
            ]) for i, (title, b, label) in enumerate([("notes.txt", (0, 40, 500, 400), "close button"),
                                                       ("Untitled", (30, 70, 500, 400), "close")])]
            return json.dumps(node("AXApplication", "TextEdit", children=wins))

    async def fake_complete(messages, system_prompt, max_tokens):
        return json.dumps({"satisfied": True, "summary": "the Untitled window is closed", "subgoal": None})

    native = TwoDocs()
    op, _ = make_operator(native, ScriptedProvider([("click", "close button")]), goal="Close the Untitled window",
                          app="com.apple.TextEdit", llm=LLMHelper(fake_complete, max_calls=4))
    res = asyncio.run(op.run())
    assert native.clicks == [], "must not close notes.txt"
    assert res.status == "done" and "Untitled" in res.summary

    async def wrong_verdict(messages, system_prompt, max_tokens):
        return json.dumps({"satisfied": False, "summary": "a window is open", "subgoal": "click the close button"})

    native = TwoDocs()
    op, _ = make_operator(native, ScriptedProvider([("click", "close button")] * 5), goal="Close the Untitled window",
                          app="com.apple.TextEdit", llm=LLMHelper(wrong_verdict, max_calls=4))
    res = asyncio.run(op.run())
    assert native.clicks == [], "a wrong end-state verdict must not unlock closing notes.txt"
    assert res.status == "blocked" and "names a different window" in res.summary


def test_a_switch_whose_value_updates_late_is_not_clicked_twice(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class LateSwitch(FakeCalculator):
        def __init__(self):
            super().__init__()
            self.reads_since_click: int | None = None

        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.systempreferences", "Automation", FakeBounds(0, 40, 500, 400))]

        def read_ax_tree(self, pid, max_depth=8):
            on = self.reads_since_click is not None and self.reads_since_click >= 3
            if self.reads_since_click is not None:
                self.reads_since_click += 1
            row = node("AXRow", None, bounds=(10, 60, 400, 30), children=[node("AXCell", None, bounds=(10, 60, 400, 30), children=[
                node("AXStaticText", None, value="TextEdit.app", bounds=(60, 64, 100, 20)),
                node("AXCheckBox", None, label="TextEdit switch", value=1 if on else 0, bounds=(360, 66, 30, 18)),
            ])])
            win = node("AXWindow", "Automation", focused=True, bounds=(0, 40, 500, 400), children=[row])
            return json.dumps(node("AXApplication", "System Settings", children=[win]))

        def click(self, x, y, *, button="left", double=False, modifiers=None):
            self.clicks.append((x, y))
            self.reads_since_click = 0

    native = LateSwitch()
    provider = ScriptedProvider([("click", "TextEdit switch"), ("done", None)])
    op, _ = make_operator(native, provider, goal="turn on the TextEdit switch", app="com.apple.systempreferences")
    res = asyncio.run(op.run())
    assert len(native.clicks) == 1
    assert "'TextEdit switch' now on" in res.history[0]["diff"], "the loop waits for the late value"


def test_budget_exhaustion_observes_last_action(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7")]), goal="press 7", max_steps=1)
    res = asyncio.run(op.run())
    assert res.status == "budget_exhausted" and native.clicks == ["7"]
    assert len(res.history) == 1 and res.history[0]["changed"] is True and "new text: 7" in res.history[0]["diff"]
    assert "7" in res.final_screen_text


def test_commit_key_in_dialog_with_irreversible_button_is_gated(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class DialogNative(FakeCalculator):
        def read_ax_tree(self, pid, max_depth=8):
            t = calculator_tree()
            t["children"].append(node("AXWindow", "alert", subrole="AXDialog", bounds=(50, 100, 300, 120), children=[
                node("AXStaticText", None, value="Delete this file?", bounds=(60, 110, 200, 20)),
                node("AXButton", "Delete", bounds=(60, 180, 80, 24)), node("AXButton", "Cancel", bounds=(160, 180, 80, 24))]))
            return json.dumps(t)

        def press_key(self, key, modifiers=None):
            self.clicks.append(f"key {key}")

    native = DialogNative()
    op, _ = make_operator(native, ScriptedProvider([("key", "return")]), goal="confirm")
    res = asyncio.run(op.run())
    assert res.status == "needs_confirmation" and "Delete" in (res.pending_action or "") and native.clicks == []
    op2, _ = make_operator(native, ScriptedProvider([("key", "escape"), ("done", None)]), goal="dismiss")
    res2 = asyncio.run(op2.run())
    assert res2.status == "done" and native.clicks == ["key escape"], "non-committing keys are not gated"


class FormNative(FakeCalculator):
    def list_windows(self, *, include_helpers=False):
        return [FakeWindow(1, 100, "com.apple.calculator", "Sign in", FakeBounds(0, 40, 400, 300))]

    def read_ax_tree(self, pid, max_depth=8):
        return json.dumps(form_tree())


def message_text(m) -> str:
    if isinstance(m.content, str):
        return m.content
    return next(b.text for b in m.content if getattr(b, "text", None))


def window_frame(width: int, height: int):
    async def frame(reason):
        return SimpleNamespace(data=b"jpeg", width=width, height=height, mime_type="image/jpeg")

    return frame


def test_direct_action_requires_screenshot_and_respects_gates(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    # 'Delete account' is at native (20..140, 260..290); the window starts at y=40,
    # so in a 1:1 window capture its center is (80, 235).
    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            return json.dumps({"status": "continue", "subgoal": "x", "direct_action": {"kind": "click", "x": 80, "y": 235}, "note": ""})
        return json.dumps({"satisfied": True, "summary": "ok", "subgoal": None})

    native = FormNative()
    op, _ = make_operator(native, ScriptedProvider([("need_help", None), ("done", None)]), goal="g", llm=LLMHelper(planner, max_calls=4))
    res = asyncio.run(op.run())
    assert native.clicks == [], "direct_action without a screenshot is ignored"
    assert res.status == "done"

    native2 = FormNative()
    op2, _ = make_operator(native2, ScriptedProvider([("need_help", None), ("need_help", None), ("done", None)]), goal="g", llm=LLMHelper(planner, max_calls=6))
    op2.actuator.frame = window_frame(400, 300)
    res2 = asyncio.run(op2.run())
    assert native2.clicks == [], "hit-tested irreversible control is refused"
    assert any("refused" in h["action"] for h in res2.history)


def test_direct_action_maps_window_capture_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    sizes = []

    # A 2x (retina) capture of the 400x300 window at (0, 40): 'Continue' at native
    # (160..280, 260..290) has its center at image pixel (440, 470).
    async def planner(messages, system_prompt, max_tokens):
        if "planner" in system_prompt:
            payload = json.loads(message_text(messages[0]))
            sizes.append(payload.get("screenshot_size"))
            return json.dumps({"status": "continue", "subgoal": "x", "direct_action": {"kind": "click", "x": 440, "y": 470}, "note": ""})
        return json.dumps({"satisfied": True, "summary": "ok", "subgoal": None})

    native = FormNative()
    op, _ = make_operator(native, ScriptedProvider([("need_help", None), ("need_help", None), ("done", None)]), goal="g", llm=LLMHelper(planner, max_calls=6))
    op.actuator.frame = window_frame(800, 600)
    asyncio.run(op.run())
    assert native.clicks == ["Continue"]
    assert {"width": 800, "height": 600} in sizes, "the planner is told the capture's own size"


def test_verifier_must_return_boolean(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    calls = []

    async def verifier(messages, system_prompt, max_tokens):
        calls.append(1)
        return json.dumps({"satisfied": "false", "summary": "nope", "subgoal": None})

    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("done", None)]), goal="g", llm=LLMHelper(verifier, max_calls=2))
    res = asyncio.run(op.run())
    assert res.status == "done" and res.summary != "nope", "malformed verifier output falls back to the template summary"


def test_dry_run_acts_on_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "1")]), dry_run=True)
    res = asyncio.run(op.run())
    assert res.status == "dry_run" and native.clicks == [] and "Would click" in res.summary


# ─── regressions from live runs ──────────────────────────────────────


def test_names_come_from_label_and_values_from_static_text():
    obs = build_observation(calculator_tree("5,535"), app_name="Calculator", bundle="b", pid=1)
    labels = [e.label for e in obs.elements]
    assert "Equals" in labels and "7" in labels
    assert "button" not in labels, "the role description is never used as a name"
    assert "5,535" in obs.screen_text and "text" not in obs.screen_text.split("\n")
    assert "close button" in labels, "window buttons keep their specific role description"


def scroll_tree() -> dict[str, Any]:
    rows = [
        node("AXRow", bounds=(0, 60 + i * 40, 200, 40), children=[node("AXStaticText", value=f"Item {i}", bounds=(10, 60 + i * 40, 150, 20))])
        for i in range(8)
    ]
    area = node("AXScrollArea", bounds=(0, 60, 200, 160), children=[node("AXOutline", children=rows)])
    win = node("AXWindow", "List", focused=True, bounds=(0, 40, 400, 400), children=[area, node("AXButton", "Delete", bounds=(0, 230, 60, 30))])
    return node("AXApplication", "ListApp", children=[win])


def test_rows_scrolled_out_of_view_are_not_targets():
    obs = build_observation(scroll_tree(), app_name="ListApp", bundle="b", pid=1)
    rows = {e.label: e for e in obs.elements if e.role == "AXRow"}
    assert not rows["Item 0"].offscreen and not rows["Item 3"].offscreen
    # Item 5's center (y=280) lies below the scroll area (60..220) but inside the
    # window, on top of the 'Delete' button.
    assert rows["Item 5"].offscreen and rows["Item 5"] not in obs.click_targets
    assert "scrolled out of view" in rows["Item 5"].row()


def test_literals_keep_apostrophes():
    assert literals_from_goal('type "Don\'t panic, it\'s fine"') == ["Don't panic, it's fine"]
    assert literals_from_goal("In Soham's notes, type 'buy milk'") == ["buy milk"]
    assert literals_from_goal("type ‘Don’t stop’") == ["Don’t stop"]


def test_gate_applies_to_commit_roles_only():
    menubar_format = build_observation(
        node("AXApplication", children=[node("AXMenuBar", children=[node("AXMenuBarItem", "Format", bounds=(0, 0, 50, 20))])]),
        app_name="a", bundle="b", pid=1,
    ).elements[0]
    assert not is_irreversible(menubar_format), "a menu-bar item only opens its menu"
    row = build_observation(
        node("AXApplication", children=[node("AXWindow", "w", bounds=(0, 0, 300, 300), children=[
            node("AXRow", bounds=(0, 20, 200, 20), children=[node("AXStaticText", value="Remove me.txt", bounds=(0, 20, 100, 20))]),
            node("AXButton", "Clear History…", bounds=(0, 60, 100, 20)),
            node("AXMenuItem", "Move to Bin", bounds=(0, 90, 100, 20)),
        ])]),
        app_name="a", bundle="b", pid=1,
    )
    by = {e.label: e for e in row.elements}
    assert not is_irreversible(by["Remove me.txt"])
    assert is_irreversible(by["Clear History…"]) and is_irreversible(by["Move to Bin"])


def test_input_refused_when_target_is_not_frontmost(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7")]), frontmost=999)
    res = asyncio.run(op.run())
    assert native.clicks == []
    assert res.status == "blocked" and "not the frontmost app" in res.summary
    assert native.focused, "the operator tries to refocus the target before giving up"


def test_refusal_names_the_lock_screen(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7")]), frontmost=1085)
    op._screen_locked = lambda: True
    res = asyncio.run(op.run())
    assert native.clicks == [] and "screen locked" in res.summary


class CoveredCalculator(FakeCalculator):
    def list_windows(self, *, include_helpers=False):
        own = FakeWindow(1, 100, "com.apple.calculator", "Calculator", FakeBounds(0, 40, 220, 300))
        if not include_helpers:
            return [own]
        slack = FakeWindow(7, 555, "com.tinyspeck.slackmacgap", "Activity", FakeBounds(0, 150, 400, 400), is_frontmost=False)
        return [slack, own]


def test_click_refused_when_another_window_covers_the_point(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = CoveredCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "1")]))
    res = asyncio.run(op.run())
    assert native.clicks == []
    assert res.status == "blocked" and "com.tinyspeck.slackmacgap" in res.summary


def test_click_outside_every_window_is_refused_and_the_run_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class ShrunkCalculator(FakeCalculator):
        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.calculator", "Calculator", FakeBounds(0, 40, 220, 120))]

    native = ShrunkCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "1"), ("click", "7"), ("done", None)]))
    res = asyncio.run(op.run())
    assert native.clicks == ["7"], "'1' (y=275) is outside the window; '7' (y=195) is inside"
    assert any(str(h.get("diff", "")).startswith("refused") for h in res.history)
    assert res.status == "done"


def test_verifier_refusals_end_blocked_not_done(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    async def verifier(messages, system_prompt, max_tokens):
        return json.dumps({"satisfied": False, "summary": "display shows 0", "subgoal": "press Equals"})

    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("done", None)] * 6), goal="g", llm=LLMHelper(verifier, max_calls=8))
    res = asyncio.run(op.run())
    assert res.status == "blocked" and "display shows 0" in res.summary


def test_door_replies_are_written_to_the_run_log(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    async def verifier(messages, system_prompt, max_tokens):
        return '{"satisfied": true, "summary": "shows 84"'  # truncated JSON

    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("done", None)]), goal="g", llm=LLMHelper(verifier, max_calls=2))
    res = asyncio.run(op.run())
    with open(res.log_path) as f:
        rows = [json.loads(line) for line in f]
    llm_rows = [r for r in rows if r["event"] == "llm"]
    assert llm_rows and llm_rows[0]["raw"].startswith('{"satisfied": true') and llm_rows[0]["parsed"] is None


def test_user_activity_means_input_newer_than_the_operators_own():
    spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
    actuator = act_module.Actuator(spec, object())
    actuator.idle_seconds = lambda: 1.0
    assert actuator._user_active(), "input 1 s ago and the operator has sent none"
    actuator._last_input = time.monotonic() - 1.2
    assert not actuator._user_active(), "that input was the operator's own last action"
    actuator._last_input = time.monotonic() - 2.5
    assert actuator._user_active(), "the input came after the operator's last action"
    actuator.idle_seconds = lambda: 10.0
    assert not actuator._user_active()


def test_focus_is_not_taken_back_from_someone_using_the_computer(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7")]), frontmost=999)
    op.actuator.idle_seconds = lambda: 0.4
    res = asyncio.run(op.run())
    assert res.status == "blocked" and "someone is using the computer" in res.summary, res.summary
    assert native.focused == ["com.apple.calculator"], "only the run-start focus, no refocus by the guard"
    assert native.clicks == []


def test_no_app_and_no_frontmost_window_does_not_guess(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7")]), app=None, frontmost=4242)
    res = asyncio.run(op.run())
    assert native.clicks == [] and res.status == "blocked" and "Pass `app`" in res.summary


def test_delete_key_is_gated_outside_a_text_field(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = FakeCalculator()
    op, _ = make_operator(native, ScriptedProvider([("key", "delete")]))
    res = asyncio.run(op.run())
    assert res.status == "needs_confirmation" and native.keys == []


def edit_menu_tree(open_path: tuple[str, ...] = ()) -> dict[str, Any]:
    def at(menu: str, bounds):
        return bounds if menu in open_path else CLOSED

    transformations = node("AXMenu", children=[
        node("AXMenuItem", "Make Upper Case", bounds=at("Transformations", (260, 90, 140, 20))),
        node("AXMenuItem", "Make Lower Case", bounds=at("Transformations", (260, 110, 140, 20))),
    ])
    edit_menu = node("AXMenu", children=[
        node("AXMenuItem", "Select All", bounds=at("Edit", (40, 30, 200, 20))),
        node("AXMenuItem", "Transformations", bounds=at("Edit", (40, 90, 200, 20)), children=[transformations]),
    ])
    menubar = node("AXMenuBar", children=[node("AXMenuBarItem", "Edit", bounds=(40, 0, 40, 20), children=[edit_menu])])
    win = node("AXWindow", "Untitled", focused=True, bounds=(0, 40, 500, 400), children=[
        node("AXTextArea", None, value="hello", bounds=(10, 60, 480, 300)),
    ])
    return node("AXApplication", "TextEdit", children=[menubar, win])


def test_menu_items_are_listed_only_while_their_menu_is_showing():
    closed = build_observation(edit_menu_tree(), app_name="TextEdit", bundle="b", pid=1)
    assert not closed.menu_open
    assert [e.label for e in closed.elements if e.role == "AXMenuItem"] == []
    top = build_observation(edit_menu_tree(("Edit",)), app_name="TextEdit", bundle="b", pid=1)
    items = {e.label: e for e in top.elements if e.role == "AXMenuItem"}
    assert top.menu_open
    assert set(items) == {"Select All", "Transformations"}, "a closed submenu's items are not listed"
    assert items["Transformations"].has_submenu and "opens submenu" in items["Transformations"].row()
    sub = build_observation(edit_menu_tree(("Edit", "Transformations")), app_name="TextEdit", bundle="b", pid=1)
    assert "Make Upper Case" in [e.label for e in sub.elements]


def test_menu_state_follows_the_screen_not_the_clicks(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class TextEditNative(FakeCalculator):
        def __init__(self):
            super().__init__()
            self.open_path: tuple[str, ...] = ()

        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.TextEdit", "Untitled", FakeBounds(0, 40, 500, 400))]

        def read_ax_tree(self, pid, max_depth=8):
            return json.dumps(edit_menu_tree(self.open_path))

        def click(self, x, y, *, button="left", double=False, modifiers=None):
            self.clicks.append((x, y))
            self.open_path = {(60, 10): ("Edit",), (140, 100): ("Edit", "Transformations")}.get((x, y), ())

    native = TextEditNative()
    op, _ = make_operator(
        native,
        ScriptedProvider([("click", "Edit"), ("click", "Transformations"), ("click", "Make Upper Case"), ("done", None)]),
        goal="upper-case the text",
        app="com.apple.TextEdit",
    )
    res = asyncio.run(op.run())
    assert native.clicks == [(60, 10), (140, 100), (330, 100)], res.history
    diffs = [h.get("diff", "") for h in res.history if h.get("kind") == "click"]
    assert "menu opened" in diffs[0] and "menu closed" in diffs[-1], diffs


def test_typing_into_an_unfocused_text_area_appends():
    from bridge.tools.jev_operator.observe import Element

    class Recorder(FakeCalculator):
        def __init__(self):
            super().__init__()
            self.events: list[tuple] = []

        def click(self, x, y, *, button="left", double=False, modifiers=None):
            self.events.append(("click", x, y))

        def press_key(self, key, modifiers=None):
            self.events.append(("key", "+".join([*(modifiers or []), key])))

        def type_text(self, text):
            self.events.append(("type", text))

    async def go(native):
        spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
        actuator = act_module.Actuator(spec, native, settle_ms=0)
        area = Element(index=1, role="AXTextArea", subrole=None, label="Notes", value="draft", bounds=(20, 160, 300, 80), enabled=True, focused=False, window="w", kind="type")
        field = Element(index=2, role="AXTextField", subrole=None, label="Username", value="old", bounds=(20, 80, 200, 24), enabled=True, focused=False, window="w", kind="type")
        await actuator.type_into(area, " more", replace=False)
        await actuator.type_into(field, "soham", replace=True)

    native = Recorder()
    asyncio.run(go(native))
    assert native.events == [
        ("click", 170, 200), ("key", "cmd+down"), ("type", " more"),
        ("click", 120, 92), ("key", "cmd+a"), ("key", "cmd+a"), ("type", "soham"),
    ]


def test_select_all_key_is_sent_twice_and_other_keys_once():
    class Recorder(FakeCalculator):
        def press_key(self, key, modifiers=None):
            self.keys.append("+".join([*(modifiers or []), key]))

    async def go(native):
        spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
        actuator = act_module.Actuator(spec, native, settle_ms=0)
        await actuator.press("cmd+a")
        await actuator.press("cmd+z")

    native = Recorder()
    asyncio.run(go(native))
    assert native.keys == ["cmd+a", "cmd+a", "cmd+z"]


def test_input_is_refused_when_another_window_of_the_app_is_in_front_of_the_intended_one():
    mine, other, sheet = (0, 0, 500, 400), (30, 30, 500, 400), (40, 40, 300, 200)

    class Windows(FakeCalculator):
        def __init__(self, stack):
            super().__init__()
            self.stack = [FakeWindow(i, 100, "com.apple.TextEdit", t, FakeBounds(*f)) for i, (t, f) in enumerate(stack)]

        def list_windows(self, *, include_helpers=False):
            return self.stack

    async def guard(stack, x=50, y=50):
        spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
        actuator = act_module.Actuator(spec, Windows(stack))
        actuator.set_target(100, "com.apple.TextEdit", "TextEdit")
        actuator.frontmost = lambda: 100
        actuator.window_frames = [mine, other]
        await actuator._ensure_input_reaches_target(x, y, None if x is None else "AXButton", mine)

    # same title, different window: frames tell them apart
    with pytest.raises(act_module.Refused, match="covered by another TextEdit window") as exc:
        asyncio.run(guard([("Untitled", other), ("Untitled", mine)]))
    assert not exc.value.fatal
    with pytest.raises(act_module.Refused, match="in front of the one this input is meant for"):
        asyncio.run(guard([("notes.txt", other), ("Untitled", mine)], x=None, y=None))
    asyncio.run(guard([("Save", sheet), ("Untitled", mine)]))  # the window's own sheet is on top
    asyncio.run(guard([("Save", sheet), ("Untitled", mine)], x=None, y=None))
    asyncio.run(guard([("", mine), ("notes.txt", other)]))


def test_menu_item_that_cannot_be_pressed_gets_no_pointer_click():
    from bridge.tools.jev_operator.observe import Element

    async def go(native):
        spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
        actuator = act_module.Actuator(spec, native, settle_ms=0)
        actuator.set_target(100, "com.apple.calculator", "Calculator")
        actuator.frontmost = lambda: 100
        item = Element(index=1, role="AXMenuItem", subrole=None, label="Close", value=None,
                       bounds=(40, 20, 100, 20), enabled=True, focused=False, window="", kind="click")
        return await actuator.click(item)

    native = PressingCalculator(press_works=False)
    rec = asyncio.run(go(native))
    assert not rec.ok and "could not be pressed" in (rec.error or "")
    assert native.pointer_clicks == 0


def test_scroll_operation_scrolls_inside_the_view_hiding_the_goal_row(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class Settings(FakeCalculator):
        def __init__(self):
            super().__init__()
            self.scrolls: list[tuple] = []

        def list_windows(self, *, include_helpers=False):
            return [FakeWindow(1, 100, "com.apple.systempreferences", "Settings", FakeBounds(0, 40, 620, 300))]

        def read_ax_tree(self, pid, max_depth=8):
            rows = [node("AXRow", n, bounds=(0, 60 + 100 * i, 200, 28)) for i, n in enumerate(["General", "Printers & Scanners"])]
            sidebar = node("AXScrollArea", None, bounds=(0, 50, 200, 100), children=rows)
            content = node("AXScrollArea", None, bounds=(210, 50, 400, 250), children=[node("AXButton", "Help", bounds=(300, 60, 60, 20))])
            win = node("AXWindow", "Settings", focused=True, bounds=(0, 40, 620, 300), children=[sidebar, content])
            return json.dumps(node("AXApplication", "System Settings", children=[win]))

        def scroll(self, dx, dy, *, x=None, y=None):
            self.scrolls.append((x, y, dy))

    native = Settings()
    op, _ = make_operator(native, ScriptedProvider([("scroll_down", None)]), goal="open Printers & Scanners",
                          app="com.apple.systempreferences")
    op.cfg.max_steps = 1
    asyncio.run(op.run())
    assert native.scrolls and native.scrolls[0][:2] == (100, 100), native.scrolls


class PressingCalculator(FakeCalculator):
    def __init__(self, press_works: bool = True):
        super().__init__()
        self.press_works = press_works
        self.presses: list[str] = []
        self.pointer_clicks = 0

    def ax_press(self, pid, x, y, role, bounds):
        if not self.press_works:
            return False
        self.presses.append(role)
        super().click(x, y)
        return True

    def click(self, x, y, *, button="left", double=False, modifiers=None):
        self.pointer_clicks += 1
        super().click(x, y, button=button, double=double, modifiers=modifiers)


def test_buttons_are_pressed_through_accessibility(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = PressingCalculator()
    op, _ = make_operator(native, ScriptedProvider([("click", "7"), ("click", "Multiply"), ("click", "2"), ("click", "Equals"), ("done", None)]))
    res = asyncio.run(op.run())
    assert native.display == "14" and native.pointer_clicks == 0
    assert native.presses == ["AXButton"] * 4
    assert all(h.get("via") == "AXPress" for h in res.history if h["kind"] == "click")


def test_press_failure_falls_back_to_a_pointer_click(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    native = PressingCalculator(press_works=False)
    op, _ = make_operator(native, ScriptedProvider([("click", "7"), ("done", None)]))
    asyncio.run(op.run())
    assert native.pointer_clicks == 1 and native.display == "7"


def test_launch_by_bundle_id_uses_open_b(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(act_module.subprocess, "run", fake_run)

    async def both():
        spec = ComputerToolSpec(session_id="t", emit_event=lambda _e: None, cancel_event=asyncio.Event())
        actuator = act_module.Actuator(spec, FakeCalculator(), settle_ms=0)

        async def no_settle(ms=None):
            return None

        actuator.settle = no_settle
        await actuator.launch("com.apple.calculator")
        await actuator.launch("Calculator")

    asyncio.run(both())
    assert calls[0] == ["open", "-b", "com.apple.calculator"] and calls[1] == ["open", "-a", "Calculator"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
