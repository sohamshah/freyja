"""The hotel env (evals/computer_use/envs/hotel, "Casa Alameda"): every task is
solvable with its quirks on and off, each check catches what it forbids, and
the page shows each pattern its README names. The page tests need a headless
Chromium and skip without one."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.hotel import backend as B  # noqa: E402
from computer_use.envs.hotel import tasks as T  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.server import Episode  # noqa: E402

pytestmark = pytest.mark.skipif("hotel" not in envs.NAMES, reason="CUA_ENVS leaves out hotel")
chrome = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")
TASK = {t.id: t for t in T.TASKS}


def _ep(task_id: str, **cfg):
    t = TASK[task_id]
    ep = Episode(id=f"test-{task_id.split('/')[1]}", env="hotel", config={**t.config, **cfg})
    B.init(ep)
    return t, ep


def _check(t, ep, text: str = "", llm_calls: int = 0):
    return t.check(B.view(ep), list(ep.events), {"status": "done", "text": text, "llm_calls": llm_calls})


def test_the_rates_follow_the_contract():
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


async def _solve(task_id: str, quirks: dict | None = None):
    server = make_server()
    browser = await Browser.launch()
    try:
        t = TASK[task_id]
        cfg = dict(t.config)
        if quirks is not None:
            cfg["quirks"] = quirks
        ep = server.new_episode("hotel", cfg)
        ctx = await browser.new_context()
        page = await browser.new_page(context=ctx)
        await page.navigate(server.start_url(ep, f"/hotel/{t.start}"), timeout=20)
        out = await oracle.solve(task_id, page)
        return t.check(B.view(ep), list(ep.events), {**out, "llm_calls": 0})
    finally:
        await browser.close()
        server.stop()


@chrome
@pytest.mark.parametrize("task_id", sorted(TASK))
@pytest.mark.parametrize("plain", [False, True])
def test_every_task_is_solvable(task_id, plain):
    quirks = {k: False for k in B.QUIRKS} if plain else None
    chk = asyncio.run(_solve(task_id, quirks))
    assert chk.reward == 1, chk.why
