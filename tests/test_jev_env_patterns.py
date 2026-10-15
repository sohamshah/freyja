"""The decision and loop changes that came out of the computer-use envs: a typing
step the planner names, a label every row repeats, and a goal that waits on the
world. (The page script's row names are tested in a real browser in
test_jev_dom_snapshot.py.)
"""

from __future__ import annotations

import asyncio
import json
import time

from bridge.decisions.types import Answers, ChoiceAnswer, NoulAnswer
from bridge.tools.jev_operator.decide import Thresholds, interpret
from bridge.tools.jev_operator.handoff import LLMHelper
from tests.test_jev_dom_surface import TARGET, FakePage, action, dom_operator, run, snap, surface
from tests.test_jev_operator import ScriptedProvider


def _choice(probs: dict[str, float]) -> ChoiceAnswer:
    pick = max(probs, key=probs.get)
    return ChoiceAnswer(choice=pick, probabilities=probs, confidence=probs[pick])


def _answers(op: dict[str, float], **picks: tuple[str, float]) -> Answers:
    out = {"operation": _choice(op), "unexpected_dialog": NoulAnswer(p=0.04)}
    for name, (choice, conf) in picks.items():
        out[name] = ChoiceAnswer(choice=choice, probabilities={choice: conf}, confidence=conf)
    return Answers(answers=out, model="fake", provider="fake", latency_ms=5)


def _obs(actions):
    return surface(FakePage([]))._build(snap(actions), TARGET, 1)


def test_a_typing_step_the_planner_names_is_taken_when_jev_spreads_the_operation():
    """A catalog run's planner said "Type 'Claude' into the 'Search models' field
    [45]". Jev put that text in that field at 0.95 and 0.99, but scored the
    operation scroll up 0.42, click 0.40 (with no click target) and type 0.15:
    both are steps typing does itself. Three replans later the run stopped."""
    obs = _obs(
        [
            action(1, "fill", "Search for resources, docs, products, and more"),
            action(2, "fill", "Search models", offscreen="above"),
            action(3, "link", "Claude Sonnet 5.5"),
        ]
    )
    wide, field, _card = obs.elements
    sub = f"Type 'Claude' into the 'Search models' field [{field.index}], then press return."
    lits = ["Claude", "Search models"]
    spread = {"scroll_up": 0.42, "click": 0.40, "type": 0.15, "scroll_down": 0.02, "done": 0.01}

    def decide(op=spread, subgoal=sub, done=(), tt=(str(field.index), 0.95), ts=("lit0", 0.99),
               ct=("none", 0.95)):
        a = _answers(
            op, type_target=tt, text_source=ts, click_target=ct, key_target=("return", 0.68)
        )
        return interpret(a, obs, literals=lits, launch_candidates=[], thresholds=Thresholds(),
                         subgoal=subgoal, subgoal_actions=list(done))

    d = decide()
    assert d.operation == "type" and d.target is field and d.text_literal == "Claude"
    assert "taken as type" in d.reasons[-1]
    # Not for another field, other text, a step already typed, a sub-goal that
    # starts with something else, or Jev unsure of the field.
    assert decide(tt=(str(wide.index), 0.95)).operation == "need_help"
    assert decide(ts=("lit1", 0.99)).operation == "need_help"
    typed = f"type 'Claude' into [{field.index}] {field.label!r}"
    assert decide(done=[typed]).operation == "need_help"
    assert decide(subgoal="Press escape, then " + sub).operation == "need_help"
    assert decide(tt=(str(field.index), 0.45)).operation == "need_help"
    # A scroll away from the field is not a step toward typing into it, and a
    # click with a target of its own is not focusing the field.
    away = {"scroll_down": 0.45, "type": 0.30, "done": 0.25}
    assert decide(op=away).operation == "need_help"
    elsewhere = {"click": 0.45, "done": 0.35, "type": 0.15, "scroll_up": 0.05}
    assert decide(op=elsewhere, ct=("3", 0.9)).operation == "need_help"


def test_the_type_option_says_typing_focuses_and_scrolls_by_itself():
    from bridge.tools.jev_operator.decide import OPERATION_OPTIONS

    assert "no click first" in OPERATION_OPTIONS["type"]
    assert "out of view" in OPERATION_OPTIONS["type"]


def test_a_label_every_row_repeats_names_no_row():
    """The planner wrote "then click the Suspend button" for grace.hopper.1437.
    Jev scored the Grace Hooper row's Suspend 0.47, and the rescue took it because
    "Suspend" was in the text: the wrong member, three times."""
    rows = [
        "Grace Hooper · grace.hooper.1437@example.com · Member · Active",
        "Grace Hopper · grace.hopper.1347@example.com · Member · Active",
        "Grace Hopper · grace.hopper.1437@example.com · Member · Active",
    ]
    obs = _obs(
        [*(action(i + 1, "click", "Suspend", context=c) for i, c in enumerate(rows)),
         action(4, "click", "Export")]
    )
    hooper, other, hopper, export = obs.elements
    goal = (
        "Suspend the member whose email is grace.hopper.1437@example.com. Do not suspend "
        "anyone else.\nThen click the Suspend button for that member."
    )

    def decide(el):
        a = _answers({"click": 0.88, "done": 0.12}, click_target=(str(el.index), 0.47))
        return interpret(a, obs, literals=[], launch_candidates=[], thresholds=Thresholds(),
                         goal_text=goal)

    assert decide(hooper).operation == "need_help"
    assert decide(other).operation == "need_help"
    assert decide(hopper).target is hopper
    # A label no other row has still counts on its own.
    a = _answers({"click": 0.88, "done": 0.12}, click_target=(str(export.index), 0.47))
    d = interpret(a, obs, literals=[], launch_candidates=[], thresholds=Thresholds(),
                  goal_text="Export the member list.")
    assert d.target is export


def test_a_click_on_a_look_alike_row_goes_to_the_row_the_goal_names_exactly():
    """With every row's email in its name, Jev still chose grace.hooper.1437's
    Suspend at 0.65 for grace.hopper.1437, and on a real console that is the
    wrong account suspended."""
    rows = [
        "Grace Hooper · grace.hooper.1437@example.com · Viewer · Active",
        "Grace Hopper · grace.hopper.1347@example.com · Editor · Active",
        "Grace Hopper · grace.hopper.1437@example.com · Viewer · Active",
    ]
    obs = _obs([action(i + 1, "click", "Suspend", context=c) for i, c in enumerate(rows)])
    hooper, transposed, hopper = obs.elements
    goal = (
        "Suspend the member whose email is grace.hopper.1437@example.com. Do not suspend "
        "anyone else."
    )

    def decide(el, text=goal):
        a = _answers({"click": 0.98, "scroll_up": 0.02}, click_target=(str(el.index), 0.65))
        return interpret(a, obs, literals=[], launch_candidates=[], thresholds=Thresholds(),
                         goal_text=text)

    d = decide(hooper)
    assert d.target is hopper and "look-alike" in d.reasons[-1]
    assert decide(transposed).target is hopper, "digits swapped are a look-alike too"
    assert decide(hopper).target is hopper and not decide(hopper).reasons
    # Without an identifier in the goal there is nothing to match exactly.
    assert decide(hooper, text="Suspend Grace Hopper.").target is hooper
    # Two rows with the identifier: no single row to take.
    twice = _obs(
        [action(i + 1, "click", "Suspend", context=c) for i, c in enumerate([*rows, rows[2]])]
    )
    a = _answers({"click": 0.98, "scroll_up": 0.02}, click_target=("1", 0.65))
    d = interpret(
        a, twice, literals=[], launch_candidates=[], thresholds=Thresholds(), goal_text=goal
    )
    assert d.target is twice.elements[0]


class Dashboard(FakePage):
    """A page whose backlog figure moves once `switch_at` has passed."""

    def __init__(self, actions):
        super().__init__(actions)
        self.switch_at = float("inf")

    def __call__(self, bundle, js):
        if js.endswith("snapshot();})()"):
            n = 430 if time.monotonic() >= self.switch_at else 385
            return json.dumps(snap(self.actions, text=f"Support backlog\n{n}\nopen tickets"))
        return super().__call__(bundle, js)


_real_sleep = asyncio.sleep


async def _no_sleep(*a, **k):
    await _real_sleep(0)


def test_the_planner_waits_on_the_world_and_keeps_its_notes(tmp_path, monkeypatch):
    """A watch run had only Jev's wait, under a second: it replanned every few
    seconds and ended "stuck" 35 s in. Its planner also never saw its own notes:
    "the starting backlog is 385" became "treat 422 as the baseline"."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = Dashboard([action(1, label="Refresh"), action(2, label="Page on-call")])
    payloads: list[dict] = []

    systems: list[str] = []

    async def door(messages, system_prompt, max_tokens):
        if system_prompt.startswith("You are the planner"):
            systems.append(system_prompt)
            payloads.append(json.loads(messages[0].content))
            if len(payloads) == 1:
                page.switch_at = time.monotonic() + 0.3
                return json.dumps({
                    "status": "continue", "subgoal": "Read the Support backlog.",
                    "direct_action": {"kind": "wait", "seconds": 1},
                    "note": "The starting backlog is 385, so page at 424 or more.",
                })
            return json.dumps({
                "status": "continue", "subgoal": "Click Page on-call.", "direct_action": None,
                "note": "430 is at least 424.",
            })
        return json.dumps({"satisfied": True, "summary": "Paged at 430.", "subgoal": None})

    jev = ScriptedProvider([("wait", None)] * 8 + [("click", "Page on-call"), ("done", None)])
    op = dom_operator(
        page,
        provider=jev,
        goal="Watch the support backlog and page on-call when it is 10% above its starting value.",
        llm=LLMHelper(door, max_calls=6),
        max_steps=20,
    )
    res = run(op.run())
    assert res.status == "done", res.summary
    waited = [h for h in res.history if h.get("action") == "planner wait 1 s"]
    assert waited and waited[0]["changed"] and "430" in waited[0]["diff"]
    # The second call sees the first one's note, the first screen and the run time.
    assert payloads[1]["planner_notes"] == ["The starting backlog is 385, so page at 424 or more."]
    assert "385" in payloads[1]["start_screen_text"] and "430" in payloads[1]["screen_text"]
    assert isinstance(payloads[1]["run_seconds"], int)
    assert page.calls and page.calls[-1][0] == 2, "Page on-call was clicked"
    # Jev never sees the notes: they go stale one action later ("PO-24071 ... needs
    # saving" got PO-24072 saved unchanged). The planner puts what Jev needs in
    # the sub-goal instead.
    assert all("planner_notes" not in c["state"] for c in jev.calls)
    assert "never your notes" in systems[0] and "starts with the action" in systems[0]
    log = [json.loads(x) for x in (tmp_path / f"{res.run_id}.jsonl").read_text().splitlines()]
    assert any(e.get("action") == "planner wait 1 s" and e.get("meaningful") for e in log)


def test_a_planner_wait_never_outlasts_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = FakePage([action(1, label="Refresh")])

    async def door(messages, system_prompt, max_tokens):
        if system_prompt.startswith("You are the planner"):
            return json.dumps({"status": "continue", "subgoal": "Wait.", "note": "",
                               "direct_action": {"kind": "wait", "seconds": 600}})
        return json.dumps({"satisfied": False, "summary": "Not yet.", "subgoal": None})

    op = dom_operator(
        page, provider=ScriptedProvider([("wait", None)] * 6), goal="wait for it",
        llm=LLMHelper(door, max_calls=3), max_steps=8, max_runtime_s=10,
    )
    t0 = time.monotonic()
    res = run(op.run())
    assert time.monotonic() - t0 < 5
    assert any(h.get("action") == "planner wait 600 s refused" for h in res.history), res.history


def test_a_check_that_says_not_yet_waits_instead_of_ending_the_run(tmp_path, monkeypatch):
    """Jev claims done early while it watches. Two checks in a row with nothing
    done between them end a run "blocked", and a watch run ended that way a
    minute into its two."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = FakePage([action(1, label="Refresh")])
    checks: list[str] = []

    async def door(messages, system_prompt, max_tokens):
        checks.append("check")
        if len(checks) < 3:
            return json.dumps(
                {"satisfied": False, "summary": "Not yet.", "subgoal": "Read the value.",
                 "wait_seconds": 1}
            )
        return json.dumps({"satisfied": True, "summary": "Two minutes passed; no rise.",
                           "subgoal": None})

    op = dom_operator(
        page, provider=ScriptedProvider([("done", None)] * 3),
        goal="Watch the value for 2 minutes and tell me if it rises.",
        llm=LLMHelper(door, max_calls=5), max_steps=6,
    )
    res = run(op.run())
    assert res.status == "done", res.summary
    assert [h["action"] for h in res.history].count("check wait 1 s") == 2


def test_the_target_rescue_takes_only_the_first_step_of_a_sub_goal():
    """"Click Cobalt Parts [10] in the Vendor list, then click Save changes [14]":
    Jev scored Save changes 0.43, the rescue found its label in the sub-goal, and
    the order was saved with the old vendor before the new one was chosen."""
    obs = _obs([action(1, "click", "Cobalt Parts"), action(2, "click", "Save changes")])
    cobalt, save = obs.elements

    def decide(subgoal):
        a = _answers({"click": 0.84, "done": 0.16}, click_target=(str(save.index), 0.43))
        return interpret(a, obs, literals=[], launch_candidates=[], thresholds=Thresholds(),
                         goal_text=f"Create the purchase orders.\n{subgoal}", subgoal=subgoal)

    two = f"Click Cobalt Parts [{cobalt.index}] in the Vendor list, then click Save changes [2]."
    assert decide(two).operation == "need_help"
    assert decide("Click Save changes [2] to save the order.").target is save

