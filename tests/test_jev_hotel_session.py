"""Regressions from the wedding hotel session (2026-10-17): two read-only
availability searches on a booking site in Arc. The runs reached the right
pages and read most of the values, and lost minutes to how the run read the
page and decided it was finished. Each test names the finding it covers."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.tools.jev_operator.dom_surface import ABOVE_MARK, DOMSurface
from bridge.tools.jev_operator.handoff import LLMHelper
from bridge.tools.jev_operator.loop import MAX_IDLE_SCROLLS, Target, fallback_urls, same_site
from tests.test_jev_dom_surface import TARGET, action
from tests.test_jev_operator import FakeCalculator, ScriptedProvider, make_operator, read_log

@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.tools.jev_operator.loop.RUN_DIR", tmp_path)


POLICY = " ".join(f"Policy sentence {i} about deposits and cancellation." for i in range(200))


class Page:
    """A page behind the DOM surface's injected `run_js`: snapshots come from
    `state()`, every act() is recorded, and quiet() always reports a settled page."""

    def __init__(self, url="https://alameda.bookwell.test/rooms", text="Rooms and rates", above="", cut=False):
        self.url, self.text, self.above, self.cut = url, text, above, cut
        self.actions: list[dict[str, Any]] = [action(1, label="MODIFY"), action(2, label="VIEW DETAILS")]
        self.calls: list[list[Any]] = []
        self.on_act = None

    def snap(self) -> dict[str, Any]:
        return {"url": self.url, "title": "Rooms & Rates", "readyState": "complete",
                "scroll": {"x": 0, "y": 0, "maxY": 2000}, "viewport": {"w": 1200, "h": 800},
                "text": self.text, "above": self.above, "textCut": self.cut, "mutations": 0,
                "actions": self.actions, "unoffered": []}

    def __call__(self, bundle: str, js: str) -> str:
        if js.endswith("snapshot();})()"):
            return json.dumps(self.snap())
        if js.endswith("quiet();})()"):
            return json.dumps({"m": 0, "rs": "complete", "u": self.url, "p": 0, "nav": False})
        m = re.search(r"act\((.*)\);\}\)\(\)$", js, re.S)
        # The scroll call quotes its strings with single quotes.
        args = json.loads("[" + m.group(1).replace("'", '"') + "]")
        self.calls.append(args)
        if self.on_act:
            self.on_act(args)
        return json.dumps({"ok": True, "reason": None, "readback": None})


def operator(page: Page, provider, llm=None, goal="read the rooms and rates", **kw):
    op, _ = make_operator(FakeCalculator(), provider, goal=goal, llm=llm, **kw)
    op.surface = DOMSurface(bundle=TARGET.bundle, actuator=SimpleNamespace(), run_js=page, js_source="/*js*/")
    op._surface_injected = True

    async def arc(app):
        return Target(TARGET.pid, TARGET.bundle, "Arc")

    op._resolve_target = arc  # a browser, so the goal's addresses count
    return op


def door(replan=None, verify=None, seen: list | None = None):
    """A fake LLM: `replan` and `verify` give each call's answer in turn."""
    replans, checks = list(replan or []), list(verify or [])

    async def complete(messages, system_prompt, max_tokens):
        payload = json.loads(messages[0].content if isinstance(messages[0].content, str)
                             else messages[0].content[0].text)
        if seen is not None:
            seen.append(payload)
        if "why_help_was_requested" in payload:
            return json.dumps(replans.pop(0) if replans else {"status": "give_up", "note": "no plan"})
        return json.dumps(checks.pop(0) if checks else {"satisfied": False, "summary": "?", "report": ""})

    return complete


# ── J1: the page's whole text reaches the planner and the check ──────────


def test_the_check_reads_the_whole_page_and_the_decision_step_a_marked_start():
    # A room's policy below two room cards was cut at "Cancella…" and read as
    # missing from the page. The run scrolled for it for minutes.
    page = Page(text="Deluxe Room 1 King Bed\nFrom 318\n[below the visible area]\n" + POLICY + "\nPOLICY-END")
    seen: list[dict] = []
    check = {"satisfied": True, "summary": "Read the rates.", "report": "Deluxe: 318"}
    op = operator(page, ScriptedProvider([("done", None)]), llm=LLMHelper(door(verify=[check], seen=seen)))
    res = asyncio.run(op.run())
    obs = op._last_obs
    assert len(obs.screen_text) <= 3000
    assert re.search(r"\[[\d,]+ more characters of page text are not shown here\]$", obs.screen_text)
    assert "POLICY-END" in obs.page_text
    assert "POLICY-END" in seen[-1]["final_screen_text"]
    assert res.status == "done"


def test_text_above_the_view_comes_first_in_page_order():
    page = Page(text="Rates in view", above="Hotel header\nDates: January 21 2027 - January 24 2027")
    op = operator(page, ScriptedProvider([]))
    obs = asyncio.run(op.surface.observe(TARGET))
    assert obs.page_text.startswith(ABOVE_MARK) and "January 21 2027" in obs.page_text
    assert "January 21 2027" not in obs.screen_text  # the decision step reads what is in view


# ── J4: every "done" goes through one check with one count ───────────────


def test_planner_done_claims_count_with_jevs():
    # Five rejected checks in a row came from planner claims with nothing new
    # read between them; only Jev's own claims were counted.
    seen: list[dict] = []
    reject = {"satisfied": False, "summary": "The pet room's plan is not read.", "report": "Deluxe: 318",
              "subgoal": "Click VIEW DETAILS for the pet room", "unresolved": []}
    llm = LLMHelper(door(replan=[{"status": "done"}] * 6, verify=[reject] * 6, seen=seen))
    op = operator(Page(), ScriptedProvider([("need_help", None)] * 10), llm=llm, max_steps=10)
    res = asyncio.run(op.run())
    checks = [p for p in seen if "final_screen_text" in p]
    assert res.status == "blocked" and len(checks) == 2, (res.status, len(checks))
    assert "with nothing new read in between" in res.summary and res.report == "Deluxe: 318"


def test_values_the_page_does_not_show_end_the_read_as_partial():
    # The verifier asked five times for an attribution the page never gave.
    unread = {"satisfied": False, "summary": "Both rooms' rates were read.", "report": "Deluxe: group 318",
              "subgoal": None, "unresolved": ["whether the pet room has the group rate"]}
    op = operator(Page(), ScriptedProvider([("done", None)]), llm=LLMHelper(door(verify=[unread])))
    res = asyncio.run(op.run())
    assert res.status == "partial" and res.code == "unresolved"
    assert "Not shown on the pages the run could read: whether the pet room has the group rate" in res.summary
    assert res.report == "Deluxe: group 318" and "Running again will not find it" in res.next_hint()


# ── J5: a scroll at the edge, and scrolls over text already read ─────────


def test_scrolls_over_text_already_read_end_in_a_replan():
    # Scrolling up and down a results page the run had read moved the screen,
    # which reset the counts that end a stuck run.
    page = Page(text="Room A\nRoom B")
    flip = {"n": 0}

    def scrolled(args):
        flip["n"] += 1
        page.text = "Room A\nRoom B" if flip["n"] % 2 == 0 else "Room B\nRoom A"

    page.on_act = scrolled
    reasons: list[str] = []

    async def complete(messages, system_prompt, max_tokens):
        payload = json.loads(messages[0].content)
        if "why_help_was_requested" in payload:
            reasons.append(payload["why_help_was_requested"])
            return json.dumps({"status": "give_up", "note": "nothing new to read"})
        return json.dumps({"satisfied": False, "summary": "no", "report": ""})

    moves = [("scroll_down", None), ("scroll_up", None)] * 6
    op = operator(page, ScriptedProvider(moves), llm=LLMHelper(complete), max_steps=12)
    res = asyncio.run(op.run())
    assert reasons and f"{MAX_IDLE_SCROLLS} scrolls in a row brought no text" in reasons[0]
    assert len(page.calls) == MAX_IDLE_SCROLLS and res.status == "blocked"


# ── J6: an address given only as a fallback ──────────────────────────────

GOAL_R2 = (
    "Continue in the Casa Alameda booking results tab opened by the previous Jev run. Change "
    "the availability search to check-in January 21, 2027. You can use MODIFY. If the owned tab is "
    "unavailable, open https://t.bookwell.test/Xk3Yd9q and apply these dates and group code."
)


def test_a_fallback_address_is_recognised():
    assert fallback_urls(GOAL_R2) == {"https://t.bookwell.test/Xk3Yd9q"}
    assert fallback_urls("Open https://t.bookwell.test/Xk3Yd9q and search January 20") == set()
    assert fallback_urls("Use the open tab; otherwise go to https://a.test/b.") == {"https://a.test/b"}
    assert same_site("https://alameda.bookwell.test/x", "https://t.bookwell.test/y")
    assert not same_site("https://mail.example.test/", "https://t.bookwell.test/y")


def test_a_run_continues_in_its_tab_instead_of_opening_the_fallback():
    # The second search found the first run's results tab, then opened the
    # entry page in a new tab and started over.
    page = Page(url="https://alameda.bookwell.test/rooms")
    op = operator(page, ScriptedProvider([("done", None)]), goal=GOAL_R2)
    op.actuator.target_bundle = TARGET.bundle
    opened: list[str] = []
    real_open = op.surface.open_url

    async def spy(url):
        opened.append(url)
        return await real_open(url)

    op.surface.open_url = spy
    asyncio.run(op.run())
    assert opened == []
    rows = read_log(op.log_path.parent, op.run_id)
    assert any(r.get("event") == "open_url" and "fallback" in str(r.get("skipped")) for r in rows)


def test_a_fallback_on_another_site_is_still_opened():
    page = Page(url="https://mail.example.test/inbox")
    op = operator(page, ScriptedProvider([("done", None)]), goal=GOAL_R2)
    opened: list[str] = []

    async def spy(url):
        opened.append(url)
        page.url = url
        return SimpleNamespace(ok=True, error=None, description="", confirm_declined=None)

    op.surface.open_url = spy
    asyncio.run(op.run())
    assert opened == ["https://t.bookwell.test/Xk3Yd9q"]


# ── J11: a sub-goal's indices follow their controls ──────────────────────


def test_subgoal_indices_follow_their_controls():
    # Picking the arrival day inserted RESET CALENDAR, the departure day moved
    # from [30] to [31], and Jev, told [30], doubted the right day.
    page = Page()
    page.actions = [action(i, label=f"{d} (January)") for i, d in enumerate((21, 22, 24), start=1)]
    op = operator(page, ScriptedProvider([]))
    before = asyncio.run(op.surface.observe(TARGET))
    op._set_subgoal("Click 24 (January) [3] to set departure", before)
    page.actions = [action(9, label="RESET CALENDAR"), *page.actions]
    after = asyncio.run(op.surface.observe(TARGET))
    assert op._bound_subgoal(after) == "Click 24 (January) [4] to set departure"
    page.actions = [action(1, label="21 (January)")]
    gone = asyncio.run(op.surface.observe(TARGET))
    assert op._bound_subgoal(gone) == "Click 24 (January) to set departure"


# ── J3: text read on earlier pages keeps its page ────────────────────────


def test_earlier_screens_keep_each_record_whole():
    # The pet room's "Room Only Flexible" plan read like the Deluxe room's, and a
    # memory of each line once made the check say the pet plans were never shown.
    op = operator(Page(), ScriptedProvider([]))
    page = "Rooms (https://h.test/r)"
    deluxe = "Deluxe Room\nAL2027 WED JAN 23\n316\nRoom Only Flexible\n411"
    pet = "Pet-Friendly Room\nRoom Only Flexible\n411"
    assert op._remember_text(deluxe, page) == 5
    assert op._remember_text("411\nRoom Only Flexible\nAL2027 WED JAN 23\n316\nDeluxe Room", page) == 0
    assert op._remember_text(pet, page) == 1  # only the heading is new text
    final = SimpleNamespace(full_text=deluxe, focused_window=page)
    text = op._earlier_text(final)
    assert text.split("\n") == [
        f"[earlier screen 1, read on {page} (the final page)]",
        "Pet-Friendly Room",
        "Room Only Flexible",
        "411",
    ]


# ── J8, J9, J12: the log and the counters ────────────────────────────────


def test_log_rows_keep_their_time_and_the_finish_row_carries_the_report():
    # An outcome row's `t` was the action's run seconds (15) between rows
    # stamped in epoch seconds, and the finish row had no report.
    page = Page()
    check = {"satisfied": True, "summary": "Opened the room.", "report": "Deluxe: 318"}
    op = operator(page, ScriptedProvider([("click", "VIEW DETAILS"), ("done", None)]),
                  llm=LLMHelper(door(verify=[check])))
    res = asyncio.run(op.run())
    rows = read_log(op.log_path.parent, op.run_id)
    assert all(r["t"] > 1e9 for r in rows)
    outcome = next(r for r in rows if r.get("event") == "outcome")
    assert isinstance(outcome["at_s"], int) and "elapsed_s" in outcome
    finish = rows[-1]
    assert finish["event"] == "finish" and finish["version"] == 2
    assert finish["report"] == "Deluxe: 318" and finish["actions"] == 1 and finish["decisions"] == 2
    assert "actions=1 decisions=2" in res.footer()


def test_the_memo_counts_actions_and_decisions():
    from bridge.tools.sub_agent_registry import SubAgentRecord, SubAgentState
    from bridge.tools.sub_agent_tool import build_subagent_memo

    rec = SubAgentRecord(id="jev_1", label="jev", task="read rates", mode="background", state=SubAgentState.DONE)
    rec.tools_called, rec.iterations = 25, 36
    rec.work_note = "25 actions, 36 Jev decisions, 15 LLM calls"
    memo = build_subagent_memo(rec)
    text = memo.content if hasattr(memo, "content") else str(memo)
    assert "Work: 25 actions, 36 Jev decisions, 15 LLM calls" in text and "tool calls" not in text
