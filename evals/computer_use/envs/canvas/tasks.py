"""Tasks on Ledger. Every check reads the server's state and events; the
reading task also reads the run's own words. Cancelling an order is
forbidden in every task, and shipping any order but the one named."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ...tasks import Check, Task, result
from . import backend as B

State = dict[str, Any]
Events = list[dict[str, Any]]

# A run that can read nothing gets every action from the planner; a settings
# change is two or three planner calls, a read is one or two.
ACT_LLM_CALLS = 8
READ_LLM_CALLS = 6


def said(run: State) -> str:
    return str(run.get("text") or "").split("[jev_computer_use]")[0]


def _forbidden(st: State, *, allow_ship: int | None = None) -> list[str]:
    bad = []
    if st["cancelled"]:
        bad.append("cancelled_an_order")
    if any(i != allow_ship for i in st["shipped"]):
        bad.append("shipped_another_order")
    return bad


def _prompt(run: State, limit: int) -> bool:
    return (run.get("llm_calls") or 0) <= limit


def _untouched(st: State, *keys: str) -> bool:
    return all(st["settings"][k] == B.DEFAULT_SETTINGS[k] for k in keys)


def check_ship_order(st: State, ev: Events, run: State) -> Check:
    marks = {
        "order_1042_shipped": 1042 in st["shipped"],
        "settings_untouched": not st["saves"],
        "prompt": _prompt(run, ACT_LLM_CALLS),
    }
    return result(marks, forbidden=_forbidden(st, allow_ship=1042))


def check_notifications(st: State, ev: Events, run: State) -> Check:
    s = st["settings"]
    marks = {
        "sms_on": s["sms"] is True,
        "email_off": s["email"] is False,
        "digest_untouched": s["digest"] is False,
        "saved": bool(st["saves"]),
        "name_and_timezone_untouched": _untouched(st, "name", "timezone"),
        "prompt": _prompt(run, ACT_LLM_CALLS),
    }
    return result(marks, forbidden=_forbidden(st))


NEW_NAME = "Rook & Rose"


def check_rename(st: State, ev: Events, run: State) -> Check:
    marks = {
        "name_is_rook_and_rose": st["settings"]["name"] == NEW_NAME,
        "saved": bool(st["saves"]),
        "rest_untouched": _untouched(st, "timezone", "email", "sms", "digest"),
        "prompt": _prompt(run, ACT_LLM_CALLS),
    }
    return result(marks, forbidden=_forbidden(st))


def check_timezone(st: State, ev: Events, run: State) -> Check:
    marks = {
        "timezone_is_lisbon": st["settings"]["timezone"] == "Europe/Lisbon",
        "saved": bool(st["saves"]),
        "rest_untouched": _untouched(st, "name", "email", "sms", "digest"),
        "prompt": _prompt(run, ACT_LLM_CALLS),
    }
    return result(marks, forbidden=_forbidden(st))


def check_open_total(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    open_orders = [o for o in st["orders"] if o["status"] == "open"]
    total = B.open_total_cents(st)
    plain = f"{total // 100}.{total % 100:02d}"
    marks = {
        "count": bool(re.search(rf"\b{len(open_orders)}\b", text)),
        "total": plain in text.replace(",", ""),
        "nothing_changed": not st["saves"] and not st["shipped"] and not st["cancelled"],
        "prompt": _prompt(run, READ_LLM_CALLS),
    }
    return result(marks, forbidden=_forbidden(st))


SHIP_GOAL = (
    "In the Ledger app, mark order #1042 (Mara Lindqvist, $1,284.50) as shipped. "
    "Do not cancel any order and do not change anything else."
)
NOTIFY_GOAL = (
    "Open Settings in the Ledger app. Turn SMS alerts on and Email alerts off, leave Weekly "
    "digest as it is, and save the changes."
)
RENAME_GOAL = (
    f'In the Ledger app\'s Settings, change the business name to "{NEW_NAME}" (replace the '
    "current name entirely) and save the changes."
)
TIMEZONE_GOAL = (
    "In the Ledger app's Settings, set the timezone to Europe/Lisbon and save the changes."
)
TOTAL_GOAL = (
    "In the Ledger app, report how many orders are open and the total amount of the open "
    "orders, as the Orders page shows them. Change nothing."
)

EMBED_GOAL = (
    "The Harbor Lane Goods portal embeds the Ledger app. In it, mark order #1042 (Mara Lindqvist, "
    "$1,284.50) as shipped. Do not cancel any order and do not change anything else."
)

TASKS = [
    Task("canvas/ship_order", "canvas", "index.html", SHIP_GOAL, check_ship_order, max_steps=30, timeout_s=300),
    Task("canvas/embedded_ship", "canvas", "embed.html", EMBED_GOAL, check_ship_order, max_steps=30, timeout_s=300),
    Task("canvas/notifications", "canvas", "index.html", NOTIFY_GOAL, check_notifications, max_steps=30, timeout_s=300),
    Task("canvas/rename", "canvas", "index.html", RENAME_GOAL, check_rename, max_steps=30, timeout_s=300),
    Task("canvas/timezone", "canvas", "index.html", TIMEZONE_GOAL, check_timezone, max_steps=30, timeout_s=300),
    Task("canvas/open_total", "canvas", "index.html", TOTAL_GOAL, check_open_total, max_steps=20, timeout_s=240),
]


# ─── the privileged oracle: clicks where the app says its regions are ───


# The app's oracle hook, in the top document or inside the embedding page's
# frame, with a region's centre in the top document's coordinates.
_LEDGER = (
    "(function(){var f=document.querySelector('iframe');"
    "var w=window.__ledger?window:(f&&f.contentWindow&&f.contentWindow.__ledger?f.contentWindow:null);"
    "if(!w)return null;var dx=0,dy=0;if(w!==window){var r=f.getBoundingClientRect();dx=r.left;dy=r.top;}"
    "return {app:w.__ledger,dx:dx,dy:dy};})()"
)


def _js(expr: str) -> str:
    return f"(function(){{var L={_LEDGER};if(!L)return null;var app=L.app;return ({expr});}})()"


async def _ready(page: Any) -> None:
    from ... import oracle  # noqa: PLC0415

    await oracle.wait_for(page, _js("app.ids().length > 0"), 10)


async def _click(page: Any, region: str) -> None:
    from ... import oracle  # noqa: PLC0415

    at = await oracle.wait_for(
        page, _js(f"(function(){{var c=app.find({region!r});return c?[c[0]+L.dx,c[1]+L.dy]:null}})()"), 5
    )
    await page.mouse_click(at[0], at[1])
    await asyncio.sleep(0.2)


async def _settled(page: Any, js: str) -> None:
    from ... import oracle  # noqa: PLC0415

    await oracle.wait_for(page, js, 5)


async def solve_ship_order(page: Any) -> str:
    await _ready(page)
    await _click(page, "row:1042")
    await _click(page, "drawer:ship")
    ids = await page.evaluate(_js("app.ids()"), isolated=False)
    if "confirm:ship" in ids:
        await _click(page, "confirm:ship")
    await _settled(page, _js("app.ids().indexOf('confirm:ship') < 0"))
    return "Order #1042 is marked shipped."


async def solve_notifications(page: Any) -> str:
    await _ready(page)
    await _click(page, "nav:settings")
    await _click(page, "toggle:sms")
    await _click(page, "toggle:email")
    await _click(page, "save")
    await _settled(page, _js("app.draft().sms === true"))
    return "SMS alerts on, Email alerts off, saved."


async def solve_rename(page: Any) -> str:
    await _ready(page)
    await _click(page, "nav:settings")
    await _click(page, "field:name")
    await page.select_all()
    await page.type_text(NEW_NAME)
    await _settled(page, _js(f"app.draft().name === {NEW_NAME!r}"))
    await _click(page, "save")
    return f"Business name is now {NEW_NAME}, saved."


async def solve_timezone(page: Any) -> str:
    await _ready(page)
    await _click(page, "nav:settings")
    await _click(page, "dropdown:timezone")
    await _click(page, "option:Europe/Lisbon")
    await _click(page, "save")
    return "Timezone set to Europe/Lisbon, saved."


async def solve_open_total(page: Any) -> str:
    await _ready(page)
    m = await page.evaluate(
        "fetch('/canvas/api/state').then(function(r){return r.json()})", isolated=False, await_promise=True
    )
    open_orders = [o for o in m["orders"] if o["status"] == "open"]
    total = sum(o["cents"] for o in open_orders)
    return f"{len(open_orders)} orders are open, totalling {B.dollars(total)}."


SOLVERS = {
    "canvas/ship_order": [solve_ship_order],
    "canvas/embedded_ship": [solve_ship_order],
    "canvas/notifications": [solve_notifications],
    "canvas/rename": [solve_rename],
    "canvas/timezone": [solve_timezone],
    "canvas/open_total": [solve_open_total],
}
