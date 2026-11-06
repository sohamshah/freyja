"""The hotel env (evals/computer_use/envs/hotel, "Casa Alameda"): every task is
solvable with its quirks on and off, and each check catches what it forbids.
The page tests need a headless Chromium and skip without one."""

from __future__ import annotations

import pytest

from tests.cua_harness import Episode, envs, harness_fixture  # noqa: F401

pytestmark = pytest.mark.skipif("hotel" not in envs.NAMES, reason="CUA_ENVS leaves out hotel")
B = envs.backend("hotel")
T = envs.tasks("hotel")
TASK = {t.id: t for t in T.TASKS}


def _ep(task_id: str, **cfg):
    t = TASK[task_id]
    ep = Episode(id=f"test-{task_id.split('/')[1]}", env="hotel", config={**t.config, **cfg})
    B.init(ep)
    return t, ep


def _check(t, ep, text: str = "", llm_calls: int = 0):
    return t.check(B.view(ep), list(ep.events), {"status": "done", "text": text, "llm_calls": llm_calls})


def test_the_rates_follow_the_contract():
    """The checks expect these numbers; the rate model must keep producing them."""
    four = dict(B.DEFAULT_SEARCH)
    three = {**four, "checkin": "2027-01-21"}
    assert (B.average("group", four), B.average("flex", four)) == (316, 411)
    assert (B.average("group", three), B.average("flex", three)) == (318, 431)
    assert sum(B.plan_nights("group", three)) == 955 and B.total_with_tax("group", three) == 1241.5
    assert B.parse_date("January 21, 2027") == B.parse_date("01/21/2027") == B.parse_date("2027-01-21")


def test_read_rates_check():
    t, ep = _ep("hotel/read_rates")
    good = ("Deluxe Room: AL2027 WED JAN 23 group rate 316 per night, flexible 411.\n"
            "Pet-Friendly Room: no group rate, flexible 411 per night.\n"
            "Deposit of US$275 per room, charged 45 days before arrival.")
    assert _check(t, ep, good).reward == 1
    wrong = good.replace("Pet-Friendly Room: no group rate, flexible 411", "Pet-Friendly Room: group rate 316, flexible 411")
    assert not _check(t, ep, wrong).marks["pet_room_no_group_rate"]
    assert not _check(t, ep, good, llm_calls=19).marks["prompt"]
    B.backend(ep, "POST", "select", {}, {"room": "deluxe", "plan": "group"})
    assert _check(t, ep, good).forbidden == ["selected_a_rate"]


def test_change_dates_check():
    t, ep = _ep("hotel/change_dates")
    B.backend(ep, "POST", "search", {}, {"checkin": "01/21/2027", "checkout": "01/24/2027", "adults": "2", "code": "al2027"})
    text = "Deluxe group 318 per night; flexible 431 per night."
    assert _check(t, ep, text).reward == 1
    B.backend(ep, "POST", "book_page", {}, {})
    assert not _check(t, ep, text).marks["stayed_in_the_results_tab"]


def test_quote_hidden_check():
    t, ep = _ep("hotel/quote_hidden")
    assert _check(t, ep, "The site does not show a quoted total before a rate is selected.").reward == 1
    assert _check(t, ep, "The total is $1,241.50 including taxes.").reward == 0


@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", sorted(TASK))
def test_every_task_is_solvable_with_quirks_on_and_off(harness, task_id, quirks):
    before, after = harness.solvable(task_id, quirks)
    assert before.reward == 0.0, before.why
    assert after.reward == 1.0, after.why
