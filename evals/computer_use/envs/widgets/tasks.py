"""Tasks on Tessaly Console (the widgets env). Each check reads only what the
server saved or logged; the goals are written the way a calling agent writes
them. SOLVERS is the privileged oracle: trusted input plus page JavaScript."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...tasks import Check, Task, result
from . import backend as b

ENV = "widgets"


# ─── checks ───────────────────────────────────────────────────────────────


def _events(ev: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in ev if e.get("event") == name]


def check_price_slider(st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]) -> Check:
    c = st.get("catalog") or {}
    f = c.get("filter") or {}
    marks = {
        "filter_applied": f.get("min") == b.GOAL_MIN and f.get("max") == b.GOAL_MAX,
        "opened_cheapest": any(
            e.get("id") == c.get("cheapest_in_range") for e in _events(ev, "product_view")
        ),
    }
    return result(marks)


def check_twin_cards(st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]) -> Check:
    n = st.get("notifications") or {}
    team, personal = n.get("team") or {}, n.get("personal") or {}
    saves = _events(ev, "notifications_saved")
    marks = {
        "team_digest_off": team.get("weekly_digest") is False,
        "team_saved": any(e.get("scope") == "team" for e in saves),
        # Not required: the goal names only the digest; this shows extra toggles.
        "team_rest_unchanged": all(
            team.get(k) == v for k, v in b.TEAM.items() if k != "weekly_digest"
        ),
    }
    bad = []
    if any(e.get("scope") == "personal" for e in saves):
        bad.append("saved the Personal settings")
    if personal and personal != b.PERSONAL:
        bad.append("changed the Personal settings")
    return result(marks, required=["team_digest_off", "team_saved"], forbidden=bad)


def check_reorder(st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]) -> Check:
    c = st.get("checklist") or {}
    saves = _events(ev, "checklist_saved")
    marks = {"order": c.get("saved") == b.CHECKLIST_EXPECTED, "saved": bool(saves)}
    bad = []
    if any(e.get("mismatch") for e in saves):
        bad.append("saved an order the page's own controls did not make (cards moved by script)")
    return result(marks, forbidden=bad)


def check_business_signup(
    st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]
) -> Check:
    accounts = (st.get("signup") or {}).get("accounts") or []
    a = accounts[-1] if accounts else {}
    marks = {
        "created": bool(a),
        "business": a.get("type") == "business",
        "vat": a.get("vat") == "GB123456789",
        "city": a.get("city") == b.LOOKUP_CITY,
        "email": str(a.get("email") or "").lower() == "ada@example.com",
        # Not required by the task's spec; shows whether the name came through.
        "company": a.get("name") == "Lumen Ltd",
    }
    return result(marks, required=["created", "business", "vat", "city", "email"])


def check_virtual_list(st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]) -> Check:
    m = st.get("members") or {}
    others = sorted(
        {
            e.get("email")
            for e in _events(ev, "member_suspended")
            if e.get("email") != b.TARGET_EMAIL
        }
    )
    marks = {"suspended_target": m.get("target_status") == "suspended"}
    bad = [f"suspended another member ({', '.join(others[:5])})"] if others else []
    return result(marks, forbidden=bad)


def check_hover_menu(st: dict[str, Any], ev: list[dict[str, Any]], run: dict[str, Any]) -> Check:
    w = st.get("workspace") or {}
    other = [
        e.get("label") for e in _events(ev, "menu_choice") if e.get("item") != "show_passwords"
    ]
    marks = {"show_passwords": w.get("show_passwords") is True}
    bad = [f"chose another menu item ({', '.join(dict.fromkeys(other))})"] if other else []
    return result(marks, forbidden=bad)


REORDER_GOAL = (
    "In the release checklist, put 'Smoke test' right after 'Deploy to staging', then click "
    "Save order."
)

TASKS = [
    Task(
        f"{ENV}/price_slider",
        ENV,
        "catalog.html",
        "On the products page, show only items priced from $120 to $340, apply the filter, and "
        "open the cheapest item in that range.",
        check_price_slider,
        max_steps=60,
        timeout_s=600,
    ),
    Task(
        f"{ENV}/twin_cards",
        ENV,
        "notifications.html",
        "Turn off the weekly digest for the Team workspace only, and save the team settings.",
        check_twin_cards,
        max_steps=20,
        timeout_s=300,
    ),
    Task(
        f"{ENV}/reorder_menu",
        ENV,
        "checklist.html",
        REORDER_GOAL,
        check_reorder,
        config={"mode": "menu"},
        max_steps=30,
        timeout_s=400,
    ),
    Task(
        f"{ENV}/reorder_keyboard",
        ENV,
        "checklist.html",
        REORDER_GOAL,
        check_reorder,
        config={"mode": "keyboard"},
        max_steps=30,
        timeout_s=400,
    ),
    # Known limit: only a trusted pointer drag moves a card, and no page surface
    # can drag.
    Task(
        f"{ENV}/reorder_drag",
        ENV,
        "checklist.html",
        REORDER_GOAL,
        check_reorder,
        config={"mode": "drag"},
        max_steps=25,
        timeout_s=360,
        tags=["limit"],
    ),
    Task(
        f"{ENV}/business_signup",
        ENV,
        "signup.html",
        "Create a business account for Lumen Ltd with the email ada@example.com, VAT ID GB 123 "
        "4567 89 and postcode AL1 3AW. Keep the city the postcode lookup fills in.",
        check_business_signup,
        max_steps=40,
        timeout_s=500,
    ),
    Task(
        f"{ENV}/virtual_list",
        ENV,
        "members.html",
        "Suspend the member whose email is grace.hopper.1437@example.com. Do not suspend anyone "
        "else.",
        check_virtual_list,
        max_steps=60,
        timeout_s=600,
    ),
    # Known limit: the submenu opens only on hover, and no page surface can hover.
    Task(
        f"{ENV}/hover_menu",
        ENV,
        "workspace.html",
        "Use the View menu to turn on Security > Show passwords.",
        check_hover_menu,
        max_steps=25,
        timeout_s=360,
        tags=["limit"],
    ),
]


# ─── the oracle ───────────────────────────────────────────────────────────


def _o() -> Any:
    from ... import oracle  # noqa: PLC0415  (oracle imports this module for SOLVERS)

    return oracle


async def _js(page: Any, expr: str) -> Any:
    return await page.evaluate(expr, isolated=False)


async def _center(page: Any, js: str) -> list[float]:
    """The centre of the element `js` finds, scrolled into view."""
    return await _o().wait_for(
        page,
        f"(function(){{var e=({js}); if(!e) return null; e.scrollIntoView({{block:'center'}});"
        "var r=e.getBoundingClientRect(); return [r.left+r.width/2, r.top+r.height/2];})()",
    )


async def _mouse(page: Any, kind: str, x: float, y: float, *, held: bool = False) -> None:
    """One trusted mouse event; `held` while the left button is down (a drag)."""
    p: dict[str, Any] = {"type": kind, "x": x, "y": y}
    if kind == "mouseMoved":
        p.update(button="left" if held else "none", buttons=1 if held else 0)
    else:
        p.update(button="left", buttons=1 if kind == "mousePressed" else 0, clickCount=1)
    await page.send("Input.dispatchMouseEvent", p)


async def _drag(page: Any, x0: float, y0: float, x1: float, y1: float, steps: int = 14) -> None:
    await _mouse(page, "mouseMoved", x0, y0)
    await _mouse(page, "mousePressed", x0, y0)
    for i in range(1, steps + 1):
        await _mouse(
            page, "mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, held=True
        )
        await asyncio.sleep(0.03)
    await _mouse(page, "mouseReleased", x1, y1)


async def solve_price_slider(page: Any) -> None:
    o = _o()
    await o.ready(page)
    if await _js(page, "!!document.getElementById('minPrice')"):
        await o.type_into(page, "#minPrice", str(b.GOAL_MIN))
        await o.type_into(page, "#maxPrice", str(b.GOAL_MAX))
    else:
        await o.click(page, "#thumbMin")  # a press without a move: focus only
        for _ in range((b.GOAL_MIN - b.PRICE_LO) // b.PRICE_STEP):
            await page.key("ArrowUp")
        await o.click(page, "#thumbMax")
        for _ in range((b.PRICE_HI - b.GOAL_MAX) // b.PRICE_STEP):
            await page.key("ArrowDown")
    await o.wait_for(
        page,
        f"document.getElementById('thumbMin').getAttribute('aria-valuenow')==='{b.GOAL_MIN}' && "
        f"document.getElementById('thumbMax').getAttribute('aria-valuenow')==='{b.GOAL_MAX}'",
    )
    await o.click(page, "#applyBtn")
    applied = f"{b.GOAL_MIN}-{b.GOAL_MAX}"
    await o.wait_for(page, f"document.body.getAttribute('data-applied')==='{applied}'")
    idx = await _js(
        page,
        "(function(){var a=Array.from(document.querySelectorAll('.product')), k=0;"
        "a.forEach(function(p,i){if(+p.dataset.price < +a[k].dataset.price) k=i;}); return k;})()",
    )
    await o.click(page, ".product a.pname", index=idx)
    await o.wait_for(page, "document.body.getAttribute('data-product')", 10)


async def solve_twin_cards(page: Any) -> None:
    o = _o()
    await o.ready(page)
    sw = ".ncard[data-scope=team] [role=switch][data-key=weekly_digest]"
    await o.click(page, sw)
    await o.wait_for(page, f"document.querySelector('{sw}').getAttribute('aria-checked')==='false'")
    await o.click(page, ".ncard[data-scope=team] button.save")
    await o.wait_for(
        page, "document.querySelector('.ncard[data-scope=team]').getAttribute('data-saved')==='1'"
    )


SMOKE_AT = (
    "Array.from(document.querySelectorAll('.ccard'))"
    ".map(function(c){return c.getAttribute('data-id')}).indexOf('smoke')"
)


async def solve_reorder(page: Any) -> None:
    o = _o()
    await o.ready(page)
    mode = await _js(page, "TC.session.mode")
    start, target = await _js(page, SMOKE_AT), b.CHECKLIST_EXPECTED.index("smoke")
    card = ".ccard[data-id=smoke] "
    if await _js(page, "!!document.querySelector('.ccard .moveup')"):
        for k in range(start - target):  # the accessible twin: labelled buttons
            await o.click(page, card + ".moveup")
            await o.wait_for(page, f"{SMOKE_AT}==={start - k - 1}")
    elif mode == "menu":
        for k in range(start - target):
            await o.click(page, card + ".card-act")
            await o.wait_for(page, "document.querySelector('.ccard[data-id=smoke] .popmenu')")
            await o.click(page, card + ".popmenu button")  # Move up
            await o.wait_for(page, f"{SMOKE_AT}==={start - k - 1}")
    elif mode == "keyboard":
        await o.click(page, card + ".handle")
        await page.key("Enter")
        for _ in range(start - target):
            await page.key("ArrowUp")
        await page.key("Enter")
        await o.wait_for(page, f"{SMOKE_AT}==={target}")
    else:
        x, y = await _center(page, "document.querySelector('.ccard[data-id=smoke] .handle')")
        # The slot between the cards that end up just above and below it.
        y1 = await _js(
            page,
            "(function(){var c=Array.from(document.querySelectorAll('.ccard')).filter(function(x)"
            "{return x.getAttribute('data-id')!=='smoke'});"
            f"var a=c[{target - 1}].getBoundingClientRect(), z=c[{target}].getBoundingClientRect();"
            "return (a.top+a.height/2+z.top+z.height/2)/2;})()",
        )
        await _drag(page, x, y, x, y1)
        await o.wait_for(page, f"{SMOKE_AT}==={target}")
    await o.click(page, "#saveOrder")
    await o.wait_for(page, "document.getElementById('ccards').getAttribute('data-saves')")


async def solve_business_signup(page: Any) -> None:
    o = _o()
    await o.ready(page)
    await o.click(page, "input[name=acct][value=business]")
    await o.wait_for(page, "document.getElementById('vat')")
    await o.type_into(page, "#name", "Lumen Ltd")
    await o.type_into(page, "#email", "ada@example.com")
    await o.type_into(page, "#postcode", "AL1 3AW")
    await page.key("Tab")  # leaving the field starts the lookup, ~2 s later
    city = json.dumps(b.LOOKUP_CITY)
    await o.wait_for(page, f"document.getElementById('city').value==={city}", 8)
    await o.type_into(page, "#vat", "GB123456789")
    await o.click(page, "#createBtn")
    await o.wait_for(page, "document.body.getAttribute('data-account')", 12)


async def solve_virtual_list(page: Any) -> None:
    o = _o()
    await o.ready(page)
    email = json.dumps(b.TARGET_EMAIL)
    if await _js(page, "!!document.getElementById('memberFilter')"):
        await o.type_into(page, "#memberFilter", b.TARGET_EMAIL)
    else:  # privileged: jump the table body to the row's offset
        await _js(
            page,
            "(function(){var s=document.getElementById('memberScroll');"
            f"s.scrollTop=Math.max(0, TC.memberIndex({email})*36-120);}})()",
        )
    row = f'.mrow[data-email="{b.TARGET_EMAIL}"]'
    await o.wait_for(page, f"document.querySelector({json.dumps(row + ' button.act')})")
    await o.click(page, row + " button.act")
    await o.wait_for(
        page,
        f"(function(){{var r=document.querySelector({json.dumps(row)});"
        "return !!r && r.getAttribute('data-status')==='suspended';})()",
    )


async def solve_hover_menu(page: Any) -> None:
    o = _o()
    await o.ready(page)
    await o.click(page, ".mbtn[data-menu=view]")
    await o.wait_for(page, "document.querySelector('.mdrop[data-menu=view]')")
    parent = ".msub[data-sub=security] .parent"
    if await _js(page, "!!(TC.session.quirks||{}).hover_only"):
        x, y = await _center(page, f"document.querySelector('{parent}')")
        await _mouse(page, "mouseMoved", x, y)  # a hover: the only way in
    else:
        await o.click(page, parent)
    await o.wait_for(page, "document.querySelector('[data-item=show_passwords]')")
    await o.click(page, "[data-item=show_passwords]")
    await o.wait_for(page, "document.body.getAttribute('data-show-passwords')==='1'")


SOLVERS = {
    f"{ENV}/price_slider": [solve_price_slider],
    f"{ENV}/twin_cards": [solve_twin_cards],
    f"{ENV}/reorder_menu": [solve_reorder],
    f"{ENV}/reorder_keyboard": [solve_reorder],
    f"{ENV}/reorder_drag": [solve_reorder],
    f"{ENV}/business_signup": [solve_business_signup],
    f"{ENV}/virtual_list": [solve_virtual_list],
    f"{ENV}/hover_menu": [solve_hover_menu],
}
