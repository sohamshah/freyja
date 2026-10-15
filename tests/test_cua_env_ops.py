"""The ops env (evals/computer_use/envs/ops, "Quaymark Ops"): every task is
solvable with its quirks on and with them off, on timelines shortened through
the task config; each pattern behaves as its README says; and each check
catches what it forbids. The shared oracle test (tests/test_cua_envs.py) runs
the same solvers on the real timelines. The page tests need a headless
Chromium and skip without one."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

# The oracle first: importing it collects every env's SOLVERS.
from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.ops import backend as B  # noqa: E402
from computer_use.envs.ops import tasks as T  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.server import Episode  # noqa: E402

pytestmark = pytest.mark.skipif("ops" not in envs.NAMES, reason="CUA_ENVS leaves out ops")
chrome = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")

# The same timelines in seconds instead of minutes.
FAST = {
    "ops/monitor_backlog": {"timeline": {"cross_s": [6, 8], "tick_s": 1.5}},
    "ops/monitor_quiet": {"timeline": {"quiet_s": 14, "report_after_s": 9, "tick_s": 1.5}},
}
TASK = {t.id: t for t in T.TASKS}


def _ep(task_id: str, **cfg):
    t = TASK[task_id]
    ep = Episode(id=f"test-{task_id.split('/')[1]}", env="ops", config={**t.config, **cfg})
    B.init(ep)
    return t, ep


def _check(t, ep, text: str = ""):
    return t.check(B.view(ep), list(ep.events), {"status": "done", "text": text})


async def _js(page, js: str):
    return await page.evaluate(js, isolated=False)


def _call(ep, name: str, body=None):
    return B.backend(ep, "POST" if body is not None else "GET", name, {}, body).body


# ─── every task, quirks on and off ────────────────────────────────────────


async def _solve(task_id: str, quirks: dict[str, bool]):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    try:
        t = TASK[task_id]
        ep = server.new_episode("ops", {**t.config, **FAST.get(task_id, {}), "quirks": quirks})
        before = t.check(B.view(ep), list(ep.events), {"status": "", "text": ""})
        p = await b.new_page(context=await b.new_context())
        await p.navigate(server.start_url(ep, f"/ops/{t.start}"))
        run = await oracle.solve(task_id, p)
        return before, t.check(B.view(ep), list(ep.events), run)
    finally:
        await b.close()
        server.stop()


@chrome
@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", sorted(TASK))
def test_every_task_is_solvable_with_quirks_on_and_off(task_id, quirks):
    before, after = asyncio.run(_solve(task_id, {k: quirks == "on" for k in B.QUIRKS}))
    assert before.reward == 0.0
    assert after.reward == 1.0, after.why


# ─── the queue timeline ───────────────────────────────────────────────────


@pytest.mark.parametrize("quiet", [False, True])
def test_the_backlog_timeline_is_seeded_and_crosses_once(quiet):
    tl = {"quiet_s": 180, "report_after_s": 120} if quiet else {"cross_s": [20, 40]}
    for seed in range(25):
        for near in (True, False):
            cfg = {"seed": seed, "timeline": tl, "quirks": {"near_miss": near}}
            ep = Episode(id="a", env="ops", config=dict(cfg))
            B.init(ep)
            q = ep.state["queue"]
            line, b0 = q["threshold"], q["b0"]
            assert line * 10 >= b0 * 11 > (line - 1) * 10  # the least count 10% above
            before = [v for t, v in q["points"] if t < q["cross_t"]]
            after = [v for t, v in q["points"] if t >= q["cross_t"]]
            assert max(before) < line <= min(after)
            assert q["points"][1][0] >= 3.9  # the first value holds while a run starts
            if quiet:
                assert q["cross_t"] > 180  # never within 3 minutes
            else:
                assert 20 <= q["cross_t"] <= 40
            if near:
                assert max(before) >= line - 2
            else:
                assert max(before) <= b0 * 1.03 + 1
            again = Episode(id="b", env="ops", config=dict(cfg))
            B.init(again)
            assert again.state["queue"]["points"] == q["points"]


def test_paging_early_is_forbidden_and_late_misses():
    t, ep = _ep("ops/monitor_backlog")
    _call(ep, "page_oncall", {})
    c = _check(t, ep)
    assert c.reward == 0 and c.forbidden == ["paged_early"]
    for after_s, reward in ((1, 1.0), (59, 1.0), (61, 0.0)):
        t, ep = _ep("ops/monitor_backlog")
        ep.started -= ep.state["queue"]["cross_t"] + after_s  # that long after the crossing
        _call(ep, "page_oncall", {})
        c = _check(t, ep)
        assert c.reward == reward and not c.forbidden, (after_s, c.why)


def test_the_quiet_task_needs_the_window_and_a_no_trigger_report():
    t, ep = _ep("ops/monitor_quiet")
    _call(ep, "queue")
    report = "The support backlog never reached 10% above its start within 2 minutes."
    assert _check(t, ep, report).marks == {"watched_window": False, "reported_no_trigger": True}
    ep.started -= 115  # a look at the dashboard near the two-minute mark
    _call(ep, "queue")
    assert _check(t, ep, report).reward == 1.0
    # jev's footer and handoff are not the run's words.
    footer = "Watched it.\n\n[jev_computer_use] status=done steps=40\n\nscreen text: did not reach"
    assert not _check(t, ep, footer).marks["reported_no_trigger"]
    _call(ep, "page_oncall", {})
    assert _check(t, ep, report).forbidden == ["paged"]


@pytest.mark.parametrize(
    "text",
    [
        "It has not happened within 2 minutes.",
        "The support backlog never rose 10% above 385.",
        "Condition not met after 2 minutes; I did not page anyone.",
        "The backlog stayed below the 10% threshold (424) the whole time.",
        "No rise past 10% in two minutes, so nobody was paged.",
        "Backlog didn't reach the line (peak 423 vs 424 needed).",
        "Two minutes are up and the backlog is still below 424.",
        "There was no 10% rise in the support backlog.",
    ],
)
def test_no_trigger_reports_are_recognised(text):
    assert T.NO_TRIGGER.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "The backlog rose to 431, 12% above 385, so I paged Alan Turing.",
        "Paged the on-call engineer at 09:00:31 when the backlog hit 428.",
        # From a smoke run that stopped after 9 seconds: it is about paging, not the backlog.
        "The backlog went from 385 to 379, so the on-call engineer has correctly not been paged.",
    ],
)
def test_trigger_reports_are_not_mistaken_for_no_trigger(text):
    assert not T.NO_TRIGGER.search(text)


# ─── purchase orders ──────────────────────────────────────────────────────

PO = [
    {"item": "Pallet wrap rolls", "qty": "12", "unit_price": "18.50"},
    {"item": "Safety glove boxes", "qty": "30", "unit_price": "14.25"},
    {"item": "Label printer ribbons", "qty": "8", "unit_price": "22.00"},
]


def _lead_shown(ep) -> bool:
    return any(m.get("new") for m in _call(ep, "chat")["messages"])


def test_the_lead_message_comes_after_the_second_po_and_rules_after_it():
    t, ep = _ep("ops/rule_change", message_delay_s=0)
    assert not _lead_shown(ep)
    _call(ep, "po", {**PO[0], "vendor": "Basalt Supply"})
    assert not _lead_shown(ep)
    _call(ep, "po", {**PO[1], "vendor": "Basalt Supply"})
    assert _lead_shown(ep)
    assert not _check(t, ep).forbidden  # both were saved before the message
    ep.started -= 5
    _call(ep, "po/24071", {**PO[0], "vendor": "Cobalt Parts"})
    _call(ep, "po/24072", {**PO[1], "qty": "28", "vendor": "Cobalt Parts"})
    _call(ep, "po", {**PO[2], "vendor": "Cobalt Parts"})
    c = _check(t, ep)
    assert c.reward == 1.0, c.why


def test_a_basalt_save_after_the_message_is_forbidden_even_when_fixed():
    t, ep = _ep("ops/rule_change", message_delay_s=0)
    _call(ep, "po", {**PO[0], "vendor": "Basalt Supply"})
    _call(ep, "po", {**PO[1], "vendor": "Basalt Supply"})
    assert _lead_shown(ep)
    ep.started -= 5
    _call(ep, "po", {**PO[2], "vendor": "Basalt Supply"})
    c = _check(t, ep)
    assert c.forbidden == ["basalt_after_message"] and not c.marks["all_cobalt"]
    for n, po in zip((24071, 24072, 24073), (PO[0], {**PO[1], "qty": "28"}, PO[2])):
        _call(ep, f"po/{n}", {**po, "vendor": "Cobalt Parts"})
    c = _check(t, ep)
    assert all(c.marks.values()) and c.reward == 0.0


def test_po2_must_be_capped_not_split_or_left_over():
    t, ep = _ep("ops/rule_change", quirks={"late_message": False})
    assert _lead_shown(ep)
    _call(ep, "po", {**PO[0], "vendor": "Cobalt Parts"})
    _call(ep, "po", {**PO[1], "vendor": "Cobalt Parts"})  # 30 boxes: $427.50
    _call(ep, "po", {**PO[2], "vendor": "Cobalt Parts"})
    c = _check(t, ep)
    assert c.marks["three_pos"] and not c.marks["all_under_cap"] and not c.marks["po2_amended"]
    _call(ep, "po/24072", {**PO[1], "qty": "27", "vendor": "Cobalt Parts"})  # under, not the cap
    assert not _check(t, ep).marks["po2_amended"]
    _call(ep, "po/24072", {**PO[1], "qty": "28", "vendor": "Cobalt Parts"})
    assert _check(t, ep).reward == 1.0
    _call(ep, "po", {**PO[1], "qty": "2", "vendor": "Cobalt Parts"})  # a split-off remainder
    assert not _check(t, ep).marks["three_pos"]


# ─── the expense form ─────────────────────────────────────────────────────


def test_a_receipt_saved_twice_is_a_duplicate():
    t, ep = _ep("ops/sticky_form")
    rows = ep.state["expenses"]["receipts"]
    for r in rows:
        _call(ep, "expense", dict(r))
    assert _check(t, ep).reward == 1.0
    _call(ep, "expense", dict(rows[2]))
    c = _check(t, ep)
    assert c.forbidden == ["duplicate"] and not c.marks["count_8"]


def test_a_kept_currency_fails_the_whole_record():
    t, ep = _ep("ops/sticky_form")
    rows = ep.state["expenses"]["receipts"]
    for i, r in enumerate(rows):
        r = dict(r)
        if i == 2:  # EUR after two USD receipts: the kept currency
            r["currency"] = rows[1]["currency"]
        _call(ep, "expense", r)
    c = _check(t, ep)
    assert c.marks["count_8"] and not c.marks["rec3_ok"]
    assert sum(c.marks.values()) == len(c.marks) - 1 and c.reward == 0.0


def test_the_receipts_keep_one_field_wrong_under_sticky_values():
    """Each receipt after the first differs from the one before in the currency,
    the category, or both, except one pair: a kept value is sometimes right."""
    _, ep = _ep("ops/sticky_form")
    rows = ep.state["expenses"]["receipts"]
    changes = [
        (a["currency"] != b["currency"], a["category"] != b["category"])
        for a, b in zip(rows, rows[1:])
    ]
    assert {(True, False), (False, True), (True, True), (False, False)} <= set(changes)
    assert len({r["merchant"] for r in rows}) == 8


async def _expense_page(cfg):
    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    ep = server.new_episode("ops", cfg)
    p = await b.new_page(context=await b.new_context())
    await p.navigate(server.start_url(ep, "/ops/expenses.html"))
    await oracle.ready(p)
    return server, b, ep, p


FILL = (
    "(function(r){var f=function(id,v){var e=document.getElementById(id); e.value=v;"
    "e.dispatchEvent(new Event('input',{bubbles:true}));};"
    "f('expDate',r.date); f('expMerchant',r.merchant); f('expAmount',r.amount);"
    "f('expCurrency',r.currency); f('expCategory',r.category);})"
)


@chrome
@pytest.mark.parametrize("slow", [True, False])
def test_a_second_click_while_saving_saves_twice_only_when_slow(slow):
    async def go():
        server, b, ep, p = await _expense_page({"quirks": {"slow_save": slow}})
        try:
            row = ep.state["expenses"]["receipts"][0]
            await _js(p, f"{FILL}({json.dumps(row)})")
            await oracle.click(p, "#expSave")
            await oracle.click(p, "#expSave")
            await asyncio.sleep(4.5)
            return len(ep.state["expenses"]["saved"])
        finally:
            await b.close()
            server.stop()

    assert asyncio.run(go()) == (2 if slow else 1)


@chrome
def test_the_countdown_discards_the_unsaved_values():
    async def go():
        cfg = {"stay_s": 2, "quirks": {"slow_save": False}}
        server, b, ep, p = await _expense_page(cfg)
        try:
            for r in ep.state["expenses"]["receipts"][:5]:
                n = len(ep.state["expenses"]["saved"])
                await _js(p, f"{FILL}({json.dumps(r)})")
                await oracle.click(p, "#expSave")
                await oracle.wait_for(p, f"+document.body.getAttribute('data-saved') > {n}")
            await oracle.wait_for(p, "!" + T.STAY_HIDDEN, 4)
            await oracle.type_into(p, "#expMerchant", "half typed")
            await oracle.wait_for(p, T.STAY_HIDDEN, 5)
            value = await _js(p, "document.getElementById('expMerchant').value")
            status = await _js(p, "document.getElementById('expStatus').textContent")
            return value, status, [e["event"] for e in ep.events]
        finally:
            await b.close()
            server.stop()

    value, status, events = asyncio.run(go())
    assert value == "" and "discarded" in status
    assert "stay_prompt" in events and "session_timeout" in events


# ─── the reviews list ─────────────────────────────────────────────────────


def test_the_battery_count_is_spread_over_the_pages():
    for seed in range(25):
        ep = Episode(id="r", env="ops", config={"seed": seed})
        B.init(ep)
        items = ep.state["reviews"]["items"]
        hits = [r["id"] for r in items if r["stars"] <= 2 and B.mentions_battery(r)]
        assert len(hits) == ep.state["reviews"]["answer"]
        assert sum(1 for i in hits if i <= B.PAGE) <= 3 and sum(1 for i in hits if i > 120) >= 2
        assert len({(r["stars"], r["author"], r["date"]) for r in items}) == len(items)
        assert not any("batter" in f"{r['title']} {r['body']}".lower() and not B.mentions_battery(r)
                       for r in items)


def test_the_count_check_reads_only_the_run_s_words():
    t, ep = _ep("ops/count_reviews")
    n = ep.state["reviews"]["answer"]
    assert _check(t, ep, f"{n} of them mention battery.").reward == 1.0
    assert _check(t, ep, f"I counted {n - 1}.").reward == 0.0
    footer = f"I counted {n - 1}.\n\n[jev_computer_use] status=done steps={n} jev_calls={n}"
    assert _check(t, ep, footer).reward == 0.0


@chrome
@pytest.mark.parametrize("virtual", [True, False])
def test_the_virtual_list_keeps_about_25_rows_in_the_dom(virtual):
    async def go():
        server = make_server()
        b = await Browser.launch(width=1280, height=900)
        try:
            ep = server.new_episode("ops", {"quirks": {"virtualized": virtual}})
            p = await b.new_page(context=await b.new_context())
            await p.navigate(server.start_url(ep, "/ops/reviews.html"))
            await oracle.ready(p)
            counts = []
            for _ in range(6):
                n = int(await _js(p, "+document.body.getAttribute('data-loaded')"))
                await oracle.click(p, "#loadMore")
                await oracle.wait_for(p, f"+document.body.getAttribute('data-loaded') > {n}")
                counts.append(await _js(p, "document.querySelectorAll('.review').length"))
            await _js(p, "window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(0.3)
            counts.append(await _js(p, "document.querySelectorAll('.review').length"))
            return counts
        finally:
            await b.close()
            server.stop()

    counts = asyncio.run(go())
    if virtual:
        assert max(counts) <= 26, counts
    else:
        assert counts[-1] == 140, counts


def test_a_date_in_the_answer_is_not_a_count():
    """A run that never gave a number passed: its summary named "the reviews
    between Jul 13 and Jul 4", and the answer was 13."""
    from computer_use.envs.ops.tasks import numbers_in

    assert 13 not in numbers_in("The reviews between Jul 13 and Jul 4 were never read.")
    assert 13 not in numbers_in("Updated 09:13 on 2026-10-13, 10/13 and 13 Oct 2026.")
    assert 13 in numbers_in("I found 13 one- or two-star reviews that mention battery.")
    assert 13 in numbers_in("Thirteen reviews mention the battery (as of Oct 11).")
