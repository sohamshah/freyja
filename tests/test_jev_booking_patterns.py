"""The loop and DOM-surface changes that came out of a failed airline booking
run: what a page row says when a control has no name, dialogs and frames in the
observation, waiting on requests an action started, Enter that picks a
suggestion, the end-state check on long forms, and toggles that undo themselves.
(The page script's side is tested in a real browser in test_jev_dom_snapshot.py.)
"""

from __future__ import annotations

import asyncio
import json

from bridge.decisions.types import Answers, ChoiceAnswer, NoulAnswer
from bridge.tools.jev_operator.decide import Thresholds, interpret
from bridge.tools.jev_operator.handoff import LLMHelper
from bridge.tools.jev_operator.loop import RunResult
from tests.test_jev_dom_surface import TARGET, FakePage, action, dom_operator, run, snap, surface
from tests.test_jev_operator import ScriptedProvider, answers


def test_a_control_with_no_name_is_not_named_by_its_surroundings():
    """An airline's Search link (an image with no alt text) read "Search By*
    Promo Code Dollars Miles ...", the text around it; no run clicked it."""
    s = surface(FakePage([]))
    obs = s._build(
        snap(
            [
                action(1, "link", "", context="Search By* Promo Code Dollars Miles"),
                action(2, "link", "search", hint="no visible name; from image search_btn.svg"),
                action(3, "click", "Denver (DEN)", role="option", inferred=True),
            ]
        ),
        TARGET,
        1,
    )
    assert obs.elements[0].label == "(no name) near: Search By* Promo Code Dollars Miles"
    assert obs.elements[1].label == "search (no visible name; from image search_btn.svg)"
    assert obs.elements[2].role == "AXMenuItem"


def test_open_suggestions_are_listed_right_after_their_field():
    """Suggestions appended at the end of the page sat 20 rows from the field
    being typed in; the run moved on to the dates with the airport uncommitted."""
    s = surface(FakePage([]))
    obs = s._build(
        snap(
            [
                action(1, "fill", "From*", value="DEN"),
                action(2, "fill", "To*", value="MCO", focused=True, enter="pick"),
                action(3, label="Depart Date*"),
                action(4, "click", "Orlando Florida (MCO)", role="option", inferred=True),
            ]
        ),
        TARGET,
        1,
    )
    assert [e.label for e in obs.elements] == [
        "From*",
        "To*",
        "Orlando Florida (MCO)",
        "Depart Date*",
    ]
    assert obs.elements[1].row().endswith("(suggestions open: pick one)")
    assert [e.index for e in obs.elements] == [1, 2, 3, 4]


def test_a_dialog_frames_and_hidden_controls_reach_the_observation():
    s = surface(FakePage([]))
    data = snap([action(1, label="SIGN UP")], text="[dialog] Join the club\nflights")
    data.update(
        {
            "dialog": {"title": "Join the club", "text": "Join the club SIGN UP"},
            "behind": 40,
            "frames": [
                {"host": "offers.example", "w": 600, "h": 300, "x": 10, "y": 400, "offscreen": None}
            ],
        }
    )
    obs = s._build(data, TARGET, 1)
    assert obs.dialog == {"title": "Join the club", "text": "Join the club SIGN UP"}
    lines = obs.screen_text.split("\n")
    assert lines[0] == "[40 controls behind the dialog are not listed until it closes]"
    assert lines[1].startswith("[embedded frame from offers.example, 600x300 at (10, 400)")
    assert "[dialog] Join the club" in obs.screen_text


class SlowPage(FakePage):
    """A page whose last action started a request that answers on the 4th read."""

    def __init__(self):
        super().__init__([action(1, label="Continue")])
        self.reads = 0

    def __call__(self, bundle, js):
        if js.endswith("quiet();})()"):
            self.reads += 1
            return json.dumps(
                {
                    "m": 3,
                    "rs": "complete",
                    "u": "https://x.test/",
                    "p": 1 if self.reads < 4 else 0,
                    "nav": False,
                }
            )
        return super().__call__(bundle, js)


def test_settle_waits_while_a_request_the_action_started_is_open():
    """A real airline's Continue answered after ~10 s. The page looked settled
    after 3 s, and the run judged the click a no-op."""
    page = SlowPage()
    run(surface(page).settle("click"))
    assert page.reads >= 5  # four busy reads, then two quiet ones


def test_enter_that_picks_a_suggestion_needs_no_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    page = FakePage([action(1, "fill", "From*", focused=True, enter="pick"), action(2, label="Go")])
    op = dom_operator(
        page, provider=ScriptedProvider([("key", "return"), ("done", None)]), goal="pick DEN"
    )
    res = run(op.run())
    assert res.status == "done" and page.calls == [[0, "key", "Enter", ""]]


def test_down_and_up_are_page_keys():
    page = FakePage([])
    s = surface(page)
    assert {"down", "up"} <= set(s.key_options)
    assert run(s.execute("press", "down")).ok
    assert page.calls == [[0, "key", "ArrowDown", ""]]


class SubgoalProvider(ScriptedProvider):
    """Like ScriptedProvider, but says the planner's sub-goal is complete."""

    async def decide(self, state, questions, *, model=None) -> Answers:
        a = await super().decide(state, questions, model=model)
        if "subgoal_complete" in a.answers:
            a.answers["subgoal_complete"] = NoulAnswer(p=0.95)
        return a


def test_done_on_a_finished_subgoal_goes_back_to_the_goal_without_a_check(tmp_path, monkeypatch):
    """A passenger form drew three end-state checks: Jev said done each time a
    planner sub-goal (one passenger's fields) was finished. That done means the
    sub-goal; the run goes back to the goal instead of stopping to check."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    checks: list[int] = []

    async def verifier(messages, system_prompt, max_tokens):
        checks.append(1)
        return json.dumps({"satisfied": True, "summary": "Both boxes are ticked.", "subgoal": None})

    page = FakePage(
        [action(1, "toggle", "Box 1", checked=False), action(2, "toggle", "Box 2", checked=False)]
    )
    op = dom_operator(
        page,
        provider=SubgoalProvider([("done", None), ("click", "Box 2"), ("done", None)]),
        goal="tick both boxes",
        llm=LLMHelper(verifier, max_calls=4),
    )
    op._subgoal = "tick Box 1"
    res = run(op.run())
    assert res.status == "done" and len(checks) == 1
    assert [c[1] for c in page.calls] == ["click"], "the run went on to Box 2 before checking"


def test_a_check_that_disagrees_after_real_progress_does_not_end_the_run(tmp_path, monkeypatch):
    """Two end-state disagreements used to end a run "blocked" even when it was
    filling more of a long form between them."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    verdicts = iter([False, False, True])

    async def verifier(messages, system_prompt, max_tokens):
        ok = next(verdicts)
        return json.dumps(
            {
                "satisfied": ok,
                "summary": "all ticked" if ok else "more boxes to tick",
                "subgoal": None,
            }
        )

    class Ticking(FakePage):
        def __call__(self, bundle, js):
            if js.endswith("snapshot();})()"):
                n = len(self.calls)
                return json.dumps(
                    snap([action(i + 1, "toggle", f"Box {i + 1}", checked=i < n) for i in range(3)])
                )
            return super().__call__(bundle, js)

    script = [
        ("done", None),
        ("click", "Box 1"),
        ("done", None),
        ("click", "Box 2"),
        ("done", None),
    ]
    op = dom_operator(
        Ticking([]),
        provider=ScriptedProvider(script),
        goal="tick the boxes",
        llm=LLMHelper(verifier, max_calls=6),
    )
    res = run(op.run())
    assert res.status == "done" and res.summary == "all ticked"


def test_a_toggle_that_undoes_itself_is_not_progress(tmp_path, monkeypatch):
    """A run flipped a travelers panel open and shut eight times; each flip
    changed the screen, so the stuck rule never fired."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    class Toggle(FakePage):
        def __call__(self, bundle, js):
            if js.endswith("snapshot();})()"):
                open_ = sum(1 for c in self.calls if c[1] == "click") % 2 == 1
                rows = [action(1, label="Travelers")] + (
                    [action(i, label=f"Opt {i}") for i in range(2, 8)] if open_ else []
                )
                return json.dumps(snap(rows, text="panel open" if open_ else "panel closed"))
            return super().__call__(bundle, js)

    async def planner(messages, system_prompt, max_tokens):
        return json.dumps(
            {
                "status": "give_up",
                "subgoal": None,
                "direct_action": None,
                "note": "the panel only toggles",
            }
        )

    page = Toggle([])
    op = dom_operator(
        page,
        provider=ScriptedProvider([("click", "Travelers")] * 12),
        goal="close the panel and search",
        llm=LLMHelper(planner, max_calls=3),
    )
    res = run(op.run())
    assert res.status == "blocked"
    assert len(page.calls) <= 6, f"stopped after {len(page.calls)} flips"


def test_a_page_with_an_embedded_frame_hands_that_part_to_screenshots():
    r = RunResult(
        status="blocked",
        summary="",
        steps=1,
        jev_calls=1,
        jev_ms=[1],
        llm_calls=0,
        llm_ms=0,
        llm_tokens=(0, 0),
        elapsed_s=1.0,
        history=[],
        run_id="r",
        log_path="l",
        surface="dom",
        final_screen_text="[embedded frame from offers.example, 600x300 at (10, 400): ...]\nAdd",
    )
    assert "embedded frame from offers.example" in r.next_hint() and "computer_use" in r.next_hint()


def test_probe_waits_for_a_page_that_is_still_loading(tmp_path, monkeypatch):
    """The probe failed twice while a heavy results page loaded, and the run
    stopped before reading the results."""
    from bridge.tools.jev_operator import dom_surface
    from tests.test_jev_operator import FakeCalculator, make_operator

    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    answers = iter(
        [
            (False, "Arc did not answer within 3s"),
            (False, "Arc did not answer within 8s"),
            (True, "title='Select'"),
        ]
    )
    monkeypatch.setattr(dom_surface, "probe", lambda *a, **k: next(answers))
    monkeypatch.setattr(dom_surface, "supported", lambda b: True)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    op, _ = make_operator(FakeCalculator(), ScriptedProvider([]), goal="read the results")
    target = type("T", (), {"bundle": "company.thebrowser.Browser", "pid": 1, "name": "Arc"})()

    async def pin():
        return None

    monkeypatch.setattr(dom_surface.DOMSurface, "pin_active", lambda self: pin())
    run(op._select_surface(target))
    assert op.surface.name == "dom" and not op._page_unreadable


_real_sleep = asyncio.sleep


async def _no_sleep(*a, **k):
    await _real_sleep(0)


def test_numbers_in_the_goal_do_not_carry_the_sentence_punctuation():
    from bridge.tools.jev_operator.observe import literals_from_goal

    lits = literals_from_goal(
        "Contact: mobile number 5550100, ZIP code 80202. Total $1,280.40, version 1.2.3"
    )
    assert "5550100" in lits and "80202" in lits and "1,280.40" in lits
    assert not any(x.endswith((",", ".")) for x in lits) and "1.2.3" not in lits
    # A date or a phone number is one literal, never its parts ("1992" went into a
    # date-of-birth field once the sentence's period no longer hid it).
    lits = literals_from_goal("born 06/23/1992. Depart 2026-11-12, call 303-555-0147.")
    assert lits == ["06/23/1992", "2026-11-12", "303-555-0147"]


def test_the_last_llm_call_is_kept_for_the_end_state_check(tmp_path, monkeypatch):
    """A search run reached its results page with every LLM call spent on
    replans, so nothing checked the end state and it reported "blocked"."""
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)
    calls: list[str] = []

    async def door(messages, system_prompt, max_tokens):
        if system_prompt.startswith("You are the planner"):
            calls.append("replan")
            return json.dumps(
                {"status": "continue", "subgoal": "click Search", "direct_action": None, "note": ""}
            )
        calls.append("check")
        return json.dumps(
            {"satisfied": True, "summary": "The results are on screen.", "subgoal": None}
        )

    page = FakePage([action(1, label="Search")])
    op = dom_operator(
        page,
        provider=ScriptedProvider([("click", "Search"), ("need_help", None), ("need_help", None)]),
        goal="run the search",
        llm=LLMHelper(door, max_calls=2),
    )
    res = run(op.run())
    assert calls == ["replan", "check"] and res.status == "done"


def test_a_click_the_planner_names_is_not_vetoed_by_a_hesitant_operation():
    """A seat run gave one passenger's seat away, and the planner said "Click [14]
    'Select seat for Ada Lovelace ...'". Jev picked [14] at 0.77 but scored the
    click 0.40, three times, and the run stopped one seat short."""
    obs = surface(FakePage([]))._build(
        snap([action(1, "link", "Select seat for Ada Lovelace"), action(2, "click", "seat 1D")]),
        TARGET,
        1,
    )
    ada, seat = obs.elements
    sub = f"Click [{ada.index}] 'Select seat for Ada Lovelace', then click 'seat 1D'."
    hesitant = answers(
        operation=("click", 0.40), click_target=(str(ada.index), 0.77), unexpected_dialog=0.02
    )

    def decide(a=hesitant, subgoal=sub, done=()):
        return interpret(
            a, obs, literals=[], launch_candidates=[], thresholds=Thresholds(),
            subgoal=subgoal, subgoal_actions=list(done),
        )

    d = decide()
    assert d.operation == "click" and d.target is ada
    # The planner also names the label first.
    named_first = f"Click the 'Select seat for Ada Lovelace' link [{ada.index}]."
    assert decide(subgoal=named_first).target is ada
    # Not when the sub-goal starts with another row or another step, once the
    # run has clicked it under this sub-goal, without a sub-goal, or for a
    # hesitant "done".
    assert decide(subgoal=f"Click [{seat.index}] 'seat 1D'.").operation == "need_help"
    assert decide(subgoal="Close the banner, then " + sub).operation == "need_help"
    then = f"Click 'seat 1D', then click [{ada.index}] 'Select seat for Ada Lovelace'."
    assert decide(subgoal=then).operation == "need_help"
    assert decide(done=[f"click [{ada.index}] AXLink {ada.label!r}"]).operation == "need_help"
    assert decide(subgoal="").operation == "need_help"
    unsure = answers(
        operation=("done", 0.40), click_target=(str(ada.index), 0.77), unexpected_dialog=0.02
    )
    assert decide(a=unsure).operation == "need_help"
    # Jev must still agree on the row.
    vague = answers(
        operation=("click", 0.40), click_target=(str(ada.index), 0.45), unexpected_dialog=0.02
    )
    assert decide(a=vague).operation == "need_help"


class HesitantProvider(ScriptedProvider):
    """Like ScriptedProvider, but scores every click 0.40."""

    async def decide(self, state, questions, *, model=None) -> Answers:
        a = await super().decide(state, questions, model=model)
        op = a.answers.get("operation")
        if op is not None and op.choice == "click":
            a.answers["operation"] = ChoiceAnswer(
                choice="click", probabilities={**op.probabilities, "click": 0.40}, confidence=0.40
            )
        return a


def test_the_loop_takes_the_click_the_planner_named(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)

    async def verifier(messages, system_prompt, max_tokens):
        return json.dumps({"satisfied": True, "summary": "Ada has seat 1D.", "subgoal": None})

    page = FakePage(
        [action(1, "link", "Select seat for Ada Lovelace"), action(2, "click", "seat 1D")]
    )
    op = dom_operator(
        page,
        provider=HesitantProvider([("click", "Select seat for Ada Lovelace"), ("done", None)]),
        goal="give Ada Lovelace seat 1D",
        llm=LLMHelper(verifier, max_calls=4),
    )
    op._subgoal = "Click [1] 'Select seat for Ada Lovelace', then click 'seat 1D'."
    res = run(op.run())
    assert page.calls and page.calls[0][1] == "click", "the planner's click was not taken"
    assert page.calls[0][0] == page.actions[0]["id"] and res.status == "done"
