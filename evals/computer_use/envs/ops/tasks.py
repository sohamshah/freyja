"""Tasks on Quaymark Ops. Goals are written the way a calling agent writes them.
Every check reads the server's state and events; the reading and reporting
tasks also read the run's own words, never jev's footer or handoff."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ...tasks import Check, Task, result
from . import backend as B

State = dict[str, Any]
Events = list[dict[str, Any]]

# ─── checks ───────────────────────────────────────────────────────────────


def said(run: dict[str, Any]) -> str:
    """The run's own words: its summary and the values it read. jev's footer
    (step counts, timings, the log path) and handoff (screen text) are cut, so a
    number or phrase there cannot pass a check."""
    return str(run.get("text") or "").split("[jev_computer_use]")[0]


def check_monitor_backlog(st: State, ev: Events, run: State) -> Check:
    q = st["queue"]
    pages, cross = q["pages"], q["cross_t"]
    marks = {"paged_in_time": bool(pages) and cross <= pages[0] <= cross + q["window_s"]}
    return result(marks, forbidden=["paged_early"] if any(t < cross for t in pages) else [])


# Words that say the rise did not happen: "never reached 424", "has not
# happened", "is still below the line", "no 10% rise". Saying only that nobody
# was paged does not count, because the mark is about the backlog.
_NEG = (
    r"(?:did\s*not|didn['’]t|does\s*not|doesn['’]t|has\s*not|hasn['’]t|have\s*not|haven['’]t"
    r"|never|not)"
)
_VERB = (
    r"(?:happen(?:s|ed)?|occur(?:s|red)?|reach(?:es|ed)?|rise[ns]?|rose|climb(?:s|ed)?|go(?:es)?"
    r"|went|gone|gets?|got|hits?|cross(?:es|ed)?|exceed(?:s|ed)?|increase[sd]?|grow[sn]?|grew"
    r"|trigger(?:s|ed)?|meets?|met|pass(?:es|ed)?|surpass(?:es|ed)?|jump(?:s|ed)?|spike[sd]?)"
)
NO_TRIGGER = re.compile(
    rf"\b{_NEG}\b[^.;!?]{{0,60}}?\b{_VERB}\b"
    r"|\b(?:stayed|remained|stays|remains|kept|held|still)\b[^.;!?]{0,40}?"
    r"\b(?:below|under|beneath|short of)\b"
    r"|\bno\s+(?:[\w%+.-]+\s+){0,3}?(?:rise|increase|spike|jump|breach|trigger|crossing)\b",
    re.I,
)


def check_monitor_quiet(st: State, ev: Events, run: State) -> Check:
    q = st["queue"]
    late = 0.9 * float(q["report_after_s"])
    marks = {
        # It looked at the dashboard again at (about) the two-minute mark.
        "watched_window": any(e["event"] == "queue_read" and e["t"] >= late for e in ev),
        "reported_no_trigger": bool(NO_TRIGGER.search(said(run))),
    }
    return result(marks, forbidden=["paged"] if q["pages"] else [])


# (item as the goal names it, what identifies it, quantity, unit price)
PO_ITEMS = [
    ("Pallet wrap rolls", "pallet wrap", 12, 18.50),
    ("Safety glove boxes", "glove", 30, 14.25),
    ("Label printer ribbons", "ribbon", 8, 22.00),
]
CAPPED = int(B.CAP * 100) // round(PO_ITEMS[1][3] * 100)  # 28 boxes: $399.00
GRACE_S = 2.0  # a save this soon after the message reached the page was already under way


def check_rule_change(st: State, ev: Events, run: State) -> Check:
    o = st["orders"]
    pos = o["pos"]
    found = [[p for p in pos if key in p["item"].lower()] for _, key, _, _ in PO_ITEMS]
    exact = len(pos) == 3 and all(len(f) == 1 for f in found)
    mine = [f[0] for f in found] if exact else []

    def listed(i: int, p: dict[str, Any]) -> bool:
        _, _, qty, price = PO_ITEMS[i]
        return abs(p["unit_price"] - price) < 0.005 and (
            p["qty"] == qty or (i == 1 and p["qty"] == CAPPED)
        )

    seen = o["message_seen"]
    gloves = mine[1] if mine else None
    marks = {
        "three_pos": exact and all(listed(i, p) for i, p in enumerate(mine)),
        "all_cobalt": exact and all(p["vendor"] == "Cobalt Parts" for p in mine),
        "all_under_cap": exact and all(p["total"] <= B.CAP for p in mine),
        # The over-$400 PO was saved before the message: its last save came after
        # the message and follows it (Cobalt Parts, capped at 28 at the same price).
        "po2_amended": gloves is not None
        and seen is not None
        and gloves["qty"] == CAPPED
        and abs(gloves["unit_price"] - PO_ITEMS[1][3]) < 0.005
        and gloves["vendor"] == "Cobalt Parts"
        and gloves["saves"][-1] >= seen,
    }
    late = [
        e
        for e in ev
        if e["event"] == "po_saved"
        and e.get("vendor") == "Basalt Supply"
        and seen is not None
        and e["t"] > seen + GRACE_S
    ]
    return result(marks, forbidden=["basalt_after_message"] if late else [])


def _key(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _same(s: dict[str, Any], r: dict[str, str]) -> bool:
    return (
        s["date"] == r["date"]
        and _key(s["merchant"]) == _key(r["merchant"])
        and abs(float(s["amount"]) - float(r["amount"])) < 0.005
        and s["currency"] == r["currency"]
        and s["category"] == r["category"]
    )


def check_sticky_form(st: State, ev: Events, run: State) -> Check:
    x = st["expenses"]
    by: dict[str, list[dict[str, Any]]] = {}
    for s in x["saved"]:
        by.setdefault(_key(s["merchant"]), []).append(s)
    marks = {"count_8": len(x["saved"]) == len(x["receipts"])}
    for i, r in enumerate(x["receipts"], 1):
        marks[f"rec{i}_ok"] = any(_same(s, r) for s in by.get(_key(r["merchant"]), []))
    # One receipt saved twice: the double click on a slow save, or a re-entry
    # after a wrong save. Saved expenses cannot be edited or removed.
    return result(marks, forbidden=["duplicate"] if any(len(v) > 1 for v in by.values()) else [])


_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split()
)}


def numbers_in(text: str) -> set[int]:
    out = {int(n) for n in re.findall(r"\b\d+\b", text)}
    out |= {v for w, v in _WORDS.items() if re.search(rf"\b{w}\b", text, re.I)}
    return out


def check_count_reviews(st: State, ev: Events, run: State) -> Check:
    r = st["reviews"]
    marks = {
        "answer": r["answer"] in numbers_in(said(run)),
        "loaded_all": r["total"] - B.PAGE in r["served"],  # not required: the dense signal
    }
    return result(marks, required=["answer"])


# ─── tasks ────────────────────────────────────────────────────────────────

MONITOR_GOAL = (
    "Watch the support backlog on the queue dashboard. When it is at least 10% above the value "
    "it had when you started, page the on-call engineer. Do not page before that."
)
PO_GOAL = (
    "Create these three purchase orders: (1) item Pallet wrap rolls, quantity 12, unit price "
    "$18.50, vendor Basalt Supply; (2) item Safety glove boxes, quantity 30, unit price $14.25, "
    "vendor Basalt Supply; (3) item Label printer ribbons, quantity 8, unit price $22.00, vendor "
    "Basalt Supply. Follow any instructions your team lead posts in the chat while you work."
)

TASKS = [
    # The timelines live in the config, so a test can shorten them.
    Task(
        "ops/monitor_backlog",
        "ops",
        "queue.html",
        MONITOR_GOAL,
        check_monitor_backlog,
        config={"timeline": {"cross_s": [20, 40], "window_s": 60}},
        max_steps=100,
        timeout_s=240,
        tags=["slow"],  # tests/test_cua_env_ops.py solves it on a shortened timeline
    ),
    Task(
        "ops/monitor_quiet",
        "ops",
        "queue.html",
        MONITOR_GOAL + " If it has not happened within 2 minutes, stop and tell me so.",
        check_monitor_quiet,
        config={"timeline": {"quiet_s": 180, "report_after_s": 120}},
        max_steps=160,
        timeout_s=330,
        tags=["slow"],  # tests/test_cua_env_ops.py solves it on a shortened timeline
    ),
    Task(
        "ops/rule_change",
        "ops",
        "orders.html",
        PO_GOAL,
        check_rule_change,
        max_steps=80,
        timeout_s=600,
    ),
    Task(
        "ops/sticky_form",
        "ops",
        "expenses.html",
        "Enter the 8 expenses from the table into the expense form, one record each.",
        check_sticky_form,
        max_steps=100,  # 8 records x (5 fields + save), plus the dialog and checks
        timeout_s=900,
    ),
    Task(
        "ops/count_reviews",
        "ops",
        "reviews.html",
        "How many 1- or 2-star reviews of the Harbor kettle mention battery? Tell me the number.",
        check_count_reviews,
        max_steps=80,  # 6 loads and about 22 screens of scrolling, with room to spare
        timeout_s=600,
    ),
]


# ─── the privileged oracle ────────────────────────────────────────────────
# It acts with trusted pointer and keyboard input and reads the page's own
# world. It finds the episode by the page's cookie (EPISODES), for the quiet
# task's window and the quirk switches; the page never carries either.


def _o() -> Any:
    from ... import oracle  # noqa: PLC0415  (oracle imports this module for SOLVERS)

    return oracle


async def _js(page: Any, js: str) -> Any:
    return await page.evaluate(js, isolated=False)


async def _episode(page: Any) -> Any:
    cookie = await _js(page, "document.cookie")
    m = re.search(r"(?:^|;\s*)cua_ep=([^;]+)", cookie or "")
    ep = B.EPISODES.get(m.group(1)) if m else None
    if ep is None:
        raise RuntimeError("the page is not bound to an ops episode")
    return ep


async def _count(page: Any, attr: str) -> int:
    return int(await _js(page, f"+(document.body.getAttribute('{attr}')||0)"))


async def _choose(page: Any, selector: str, value: str) -> None:
    sel, val = json.dumps(selector), json.dumps(value)
    ok = await _js(
        page,
        f"(function(){{var s=document.querySelector({sel}); if(!s) return false; s.value={val};"
        "s.dispatchEvent(new Event('input',{bubbles:true}));"
        f"s.dispatchEvent(new Event('change',{{bubbles:true}})); return s.value==={val};}})()",
    )
    if not ok:
        raise RuntimeError(f"no option {value!r} in {selector}")


async def _backlog(page: Any) -> int:
    return int(
        await _js(
            page,
            "parseInt(document.querySelector('[data-tile=support] .num').textContent"
            ".replace(/[^0-9]/g,''),10)",
        )
    )


async def _refresh(page: Any) -> None:
    o = _o()
    n = await _count(page, "data-reads")
    await o.click(page, "#refresh")
    await o.wait_for(page, f"+(document.body.getAttribute('data-reads')||0) > {n}", 5)


def _line(b0: int) -> int:
    return -(-b0 * 11 // 10)


async def watch_and_page(page: Any) -> None:
    o = _o()
    await o.ready(page)
    b0 = await _backlog(page)  # the value when the watch starts
    line, deadline = _line(b0), time.time() + 300
    while await _backlog(page) < line:
        if time.time() > deadline:
            raise TimeoutError("the support backlog never reached the line")
        await asyncio.sleep(0.6)
        await _refresh(page)
    await o.click(page, "#pageOncall")
    await o.wait_for(page, "/^Paged /.test(document.getElementById('pageStatus').textContent)", 5)


async def watch_quiet(page: Any) -> str:
    o = _o()
    await o.ready(page)
    ep = await _episode(page)
    b0 = await _backlog(page)
    line = _line(b0)
    end = ep.started + float(ep.state["queue"]["report_after_s"]) + 0.5
    while (left := end - time.time()) > 0:
        await asyncio.sleep(min(2.0, left))
        await _refresh(page)  # the last one lands at the end of the window
        if await _backlog(page) >= line:
            raise AssertionError("the quiet timeline crossed inside its window")
    return (
        f"The support backlog started at {b0} and never reached {line} (10% above it) within "
        "2 minutes, so I did not page the on-call engineer."
    )


STAY_HIDDEN = "document.getElementById('stay').classList.contains('hidden')"
EDITING = "document.getElementById('formTitle').textContent.indexOf('Edit')===0"
LEAD_SHOWN = (
    "Array.from(document.querySelectorAll('#chatLog .msg')).some(function(m){"
    "return m.textContent.indexOf('Basalt Supply is suspended')>=0})"
)


async def _save_po(page: Any, item: str, qty: str, price: str, vendor: str) -> None:
    o = _o()
    n = await _count(page, "data-pos")
    await o.type_into(page, "#poItem", item)
    await o.type_into(page, "#poQty", qty)
    await o.type_into(page, "#poPrice", price)
    await _choose(page, "#poVendor", vendor)
    await o.click(page, "#poSave")
    await o.wait_for(page, f"+(document.body.getAttribute('data-pos')||0) > {n}", 5)


async def _edit_po(page: Any, key: str, vendor: str, qty: str | None = None) -> None:
    o = _o()
    k, v = json.dumps(key), json.dumps(vendor)
    row = (
        "Array.from(document.querySelectorAll('#openPos tbody tr')).filter(function(r){"
        f"return r.textContent.toLowerCase().indexOf({k})>=0}})[0]"
    )
    idx = await _js(
        page,
        "Array.from(document.querySelectorAll('#openPos tbody tr button')).indexOf("
        f"{row}.querySelector('button'))",
    )
    await o.click(page, "#openPos tbody tr button", index=idx)
    await o.wait_for(page, EDITING, 5)
    if qty is not None:
        await o.type_into(page, "#poQty", qty)
    await _choose(page, "#poVendor", vendor)
    await o.click(page, "#poSave")
    await o.wait_for(
        page,
        f"(function(){{var r={row}; return !!r && r.textContent.indexOf({v})>=0 && "
        "document.getElementById('formTitle').textContent==='New purchase order';})()",
        5,
    )


async def solve_rule_change(page: Any) -> None:
    o = _o()
    await o.ready(page)
    late = not await _js(page, LEAD_SHOWN)  # with late_message off the rules are there already
    (a, _, qa, pa), (b, _, qb, pb), (c, _, qc, pc) = PO_ITEMS
    first = "Basalt Supply" if late else "Cobalt Parts"
    await _save_po(page, a, str(qa), f"{pa:.2f}", first)
    await _save_po(page, b, str(qb if late else CAPPED), f"{pb:.2f}", first)
    if late:
        await o.wait_for(page, LEAD_SHOWN, 15)
        await _edit_po(page, "pallet wrap", "Cobalt Parts")
        await _edit_po(page, "glove", "Cobalt Parts", qty=str(CAPPED))
    await _save_po(page, c, str(qc), f"{pc:.2f}", "Cobalt Parts")


async def solve_sticky_form(page: Any) -> None:
    o = _o()
    await o.ready(page)
    ep = await _episode(page)
    rows = await _js(
        page,
        "Array.from(document.querySelectorAll('#receipts tbody tr')).map(function(r){"
        "return Array.from(r.cells).map(function(c){return c.textContent.trim()})})",
    )
    for i, (date, merchant, amount, currency, category) in enumerate(rows, 1):
        n = await _count(page, "data-saved")
        await o.type_into(page, "#expDate", date)
        await o.type_into(page, "#expMerchant", merchant)
        await o.type_into(page, "#expAmount", amount)
        # Set both every time: the form may keep the last record's values.
        await _choose(page, "#expCurrency", currency)
        await _choose(page, "#expCategory", category)
        await o.click(page, "#expSave")  # once: a second click while it saves is a duplicate
        await o.wait_for(page, f"+(document.body.getAttribute('data-saved')||0) > {n}", 10)
        if i == B.STAY_AFTER and ep.config["quirks"]["timeout_dialog"]:
            await o.wait_for(page, "!" + STAY_HIDDEN, 5)
            await o.click(page, "#stayBtn")
            await o.wait_for(page, STAY_HIDDEN, 3)


async def solve_count_reviews(page: Any) -> str:
    o = _o()
    await o.ready(page)
    while await _js(page, "!document.getElementById('loadMore').classList.contains('hidden')"):
        n = await _count(page, "data-loaded")
        await o.click(page, "#loadMore")
        await o.wait_for(page, f"+(document.body.getAttribute('data-loaded')||0) > {n}", 5)
    total = await _count(page, "data-loaded")
    seen: dict[str, tuple[int, str]] = {}
    y = 0
    while True:  # read the list the way a person scrolls it
        await _js(page, f"window.scrollTo(0, {y})")
        await asyncio.sleep(0.1)
        rows = await _js(
            page,
            "Array.from(document.querySelectorAll('#reviewList .review')).map(function(r){"
            "return [r.getAttribute('data-id'), +r.getAttribute('data-stars'), r.textContent]})",
        )
        for rid, stars, text in rows:
            seen[rid] = (stars, text)
        bottom = await _js(page, "Math.max(0, document.documentElement.scrollHeight - innerHeight)")
        if y >= bottom:
            break
        y = min(y + 600, bottom)
    if len(seen) != total:
        raise AssertionError(f"scrolling showed {len(seen)} of {total} reviews")
    n = sum(1 for stars, text in seen.values() if stars <= 2 and "battery" in text.lower())
    return f"{n} of the 1- or 2-star reviews of the Harbor kettle mention battery."


SOLVERS = {
    "ops/monitor_backlog": [watch_and_page],
    "ops/monitor_quiet": [watch_quiet],
    "ops/rule_change": [solve_rule_change],
    "ops/sticky_form": [solve_sticky_form],
    "ops/count_reviews": [solve_count_reviews],
}
