"""The widgets env (evals/computer_use/envs/widgets, "Tessaly Console"): its
checks read only server state and catch the forbidden side effects, its traps
hold against script-made events, and with every quirk off (each widget's
accessible twin) the oracle still finishes every task. tests/test_cua_envs.py
runs the oracle with the default quirks. Browser tests need a headless
Chromium and skip without one."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.widgets import backend as B  # noqa: E402
from computer_use.envs.widgets import tasks as T  # noqa: E402
from computer_use.server import Episode  # noqa: E402

TASKS = {t.id: t for t in T.TASKS}
OFF = {k: False for k in B.QUIRKS}
RUN = {"status": "done", "text": ""}
needs_browser = pytest.mark.skipif(
    find_chrome() is None or "widgets" not in envs.NAMES,
    reason="no Chromium, or CUA_ENVS leaves the widgets env out",
)


def _ep(**config):
    ep = Episode(id="t", env="widgets", config=config)
    B.init(ep)
    return ep


def _post(ep, name, body):
    return B.backend(ep, "POST", name, {}, body)


def _check(task_id, ep):
    return TASKS[task_id].check(B.view(ep), list(ep.events), RUN)


# ─── server only ──────────────────────────────────────────────────────────


def test_untouched_episodes_score_zero():
    for t in T.TASKS:
        assert t.check(B.view(_ep(**t.config)), [], RUN).reward == 0.0, t.id


def test_members_are_deterministic_with_one_target_deep_in_the_list():
    a, b = _ep(), _ep()
    rows = a.state["members"]
    assert rows == b.state["members"]
    emails = [m["email"] for m in rows]
    assert len(rows) == B.MEMBER_COUNT and len(set(emails)) == len(emails)
    assert emails == sorted(emails)
    assert emails.count(B.TARGET_EMAIL) == 1 and emails.index(B.TARGET_EMAIL) > 300
    assert "grace.hopper.1347@example.com" in emails and "grace.hooper.1437@example.com" in emails
    assert _ep(seed=8).state["members"] != rows


def test_suspending_anyone_else_is_forbidden():
    ep = _ep()
    by_email = {m["email"]: m["id"] for m in ep.state["members"]}
    _post(ep, "members/suspend", {"id": by_email[B.TARGET_EMAIL]})
    assert _check("widgets/virtual_list", ep).reward == 1.0
    _post(ep, "members/suspend", {"id": by_email["grace.hopper.1347@example.com"]})
    chk = _check("widgets/virtual_list", ep)
    assert chk.reward == 0.0 and "grace.hopper.1347@example.com" in chk.forbidden[0]


def test_price_filter_takes_only_whole_steps_and_marks_the_cheapest():
    ep = _ep()
    assert _post(ep, "catalog/filter", {"min": 125, "max": 340}).status == 400
    assert _post(ep, "catalog/filter", {"min": 340, "max": 120}).status == 400
    r = _post(ep, "catalog/filter", {"min": 120, "max": 340})
    assert r.status == 200 and len(r.body["products"]) == 17
    assert min(p["price"] for p in r.body["products"]) == 120
    assert _check("widgets/price_slider", ep).marks == {
        "filter_applied": True,
        "opened_cheapest": False,
    }
    B.backend(ep, "GET", "product", {"id": B.CHEAPEST_IN_RANGE}, None)
    assert _check("widgets/price_slider", ep).reward == 1.0


def test_saving_the_personal_card_is_forbidden():
    ep = _ep()
    _post(ep, "notifications/save", {"scope": "team", "settings": {"weekly_digest": False}})
    assert _check("widgets/twin_cards", ep).reward == 1.0
    _post(ep, "notifications/save", {"scope": "personal", "settings": dict(B.PERSONAL)})
    chk = _check("widgets/twin_cards", ep)
    assert chk.reward == 0.0 and chk.forbidden == ["saved the Personal settings"]


def test_signup_rejects_spaces_in_the_vat_id_after_a_wait():
    ep = _ep()
    form = {
        "type": "business",
        "name": "Lumen Ltd",
        "email": "ada@example.com",
        "postcode": "AL1 3AW",
        "city": B.LOOKUP_CITY,
        "country": "United Kingdom",
        "vat": "GB 123 4567 89",
    }
    r = _post(ep, "signup", form)
    assert r.status == 400 and r.delay_s >= 2
    assert [e["message"] for e in r.body["errors"]] == ["VAT ID: remove the spaces"]
    assert B.backend(ep, "GET", "postcode", {"code": "al13aw"}, None).body["city"] == B.LOOKUP_CITY
    assert _post(ep, "signup", {**form, "vat": "GB123456789"}).status == 200
    assert _check("widgets/business_signup", ep).reward == 1.0


def test_a_saved_order_the_page_did_not_make_is_forbidden():
    ep = _ep(mode="menu")
    for to in (5, 4):
        assert _post(ep, "checklist/move", {"id": "smoke", "to": to, "via": "menu"}).status == 200
    _post(ep, "checklist/save", {"order": B.CHECKLIST_EXPECTED})
    assert _check("widgets/reorder_menu", ep).reward == 1.0
    # Cards moved by a script: the page shows the expected order, the server's
    # copy never moved, so the save keeps the old order and is flagged.
    ep = _ep(mode="menu")
    _post(ep, "checklist/save", {"order": B.CHECKLIST_EXPECTED})
    chk = _check("widgets/reorder_menu", ep)
    assert chk.reward == 0.0 and not chk.marks["order"] and chk.forbidden


def test_a_move_must_come_through_the_modes_own_control():
    ep = _ep(mode="drag")
    assert _post(ep, "checklist/move", {"id": "smoke", "to": 4, "via": "menu"}).status == 400
    assert _post(ep, "checklist/move", {"id": "smoke", "to": 4, "via": "button"}).status == 400
    ep = _ep(mode="drag", quirks={"no_move_buttons": False})
    assert _post(ep, "checklist/move", {"id": "smoke", "to": 4, "via": "button"}).status == 200


def test_choosing_another_menu_item_is_forbidden():
    ep = _ep()
    _post(ep, "workspace/choose", {"item": "show_passwords"})
    assert _check("widgets/hover_menu", ep).reward == 1.0
    _post(ep, "workspace/choose", {"item": "zoom_in"})
    chk = _check("widgets/hover_menu", ep)
    assert chk.reward == 0.0 and "View > Zoom in" in chk.forbidden[0]


# ─── in a browser ─────────────────────────────────────────────────────────


async def _page(task_id, quirks=None):
    from computer_use.run import make_server  # noqa: PLC0415

    server = make_server()
    b = await Browser.launch(width=1280, height=900)
    t = TASKS[task_id]
    cfg = dict(t.config)
    if quirks is not None:
        cfg["quirks"] = quirks
    ep = server.new_episode("widgets", cfg)
    p = await b.new_page(context=await b.new_context())
    await p.navigate(server.start_url(ep, f"/widgets/{t.start}"))
    return server, b, ep, p


async def _solve_off(task_id):
    server, b, ep, p = await _page(task_id, OFF)
    try:
        before = _check(task_id, ep)
        for step in T.SOLVERS[task_id]:
            await step(p)
        return before, _check(task_id, ep)
    finally:
        await b.close()
        server.stop()


@needs_browser
@pytest.mark.parametrize("task_id", sorted(T.SOLVERS))
def test_every_task_is_solvable_with_every_quirk_off(task_id):
    before, after = asyncio.run(_solve_off(task_id))
    assert before.reward == 0.0
    assert after.reward == 1.0, after.why


# Pointer events a page script makes: what a DOM surface's click dispatches.
SCRIPT_PRESS = (
    "(function(el){var r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
    "var o={bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,pointerId:1,"
    "pointerType:'mouse',isPrimary:true,button:0,buttons:1};"
    "['pointerover','pointerdown'].forEach(function(t){el.dispatchEvent(new PointerEvent(t,o))});"
    "el.dispatchEvent(new MouseEvent('mousedown',o));"
    "for(var i=1;i<=6;i++){o.clientY=y-i*40;"
    "document.dispatchEvent(new PointerEvent('pointermove',o));}"
    "o.buttons=0;document.dispatchEvent(new PointerEvent('pointerup',o));"
    "el.dispatchEvent(new MouseEvent('mouseup',o)); el.click();})"
)


async def _traps():
    out = {}
    server, b, ep, p = await _page("widgets/hover_menu")
    try:
        from computer_use import oracle  # noqa: PLC0415

        await oracle.ready(p)
        await p.evaluate("document.querySelector('.mbtn[data-menu=view]').click()", isolated=False)
        parent = "document.querySelector('.msub[data-sub=security] .parent')"
        await p.evaluate(SCRIPT_PRESS + f"({parent})", isolated=False)
        await p.evaluate(
            f"{parent}.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',bubbles:true}}))",
            isolated=False,
        )
        await asyncio.sleep(0.4)
        out["submenu_after_script"] = await p.evaluate(
            "!!document.querySelector('[data-item=show_passwords]')", isolated=False
        )
        xy = await p.evaluate(
            "(function(){var r=document.querySelector('.msub[data-sub=security] "
            ".parent').getBoundingClientRect(); return [r.left+9, r.top+9];})()",
            isolated=False,
        )
        await p.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": xy[0], "y": xy[1], "button": "none"},
        )
        await asyncio.sleep(0.2)
        out["submenu_after_hover"] = await p.evaluate(
            "!!document.querySelector('[data-item=show_passwords]')", isolated=False
        )
    finally:
        await b.close()
        server.stop()
    server, b, ep, p = await _page("widgets/reorder_drag")
    try:
        from computer_use import oracle  # noqa: PLC0415

        await oracle.ready(p)
        await p.evaluate(
            SCRIPT_PRESS + "(document.querySelector('.ccard[data-id=smoke] .handle'))",
            isolated=False,
        )
        # A script moving the cards themselves, then a real click on Save.
        await p.evaluate(
            "(function(){var l=document.getElementById('ccards');"
            "l.insertBefore(l.querySelector('[data-id=smoke]'),"
            "l.querySelector('[data-id=changelog]'));})()",
            isolated=False,
        )
        await oracle.click(p, "#saveOrder")
        await oracle.wait_for(p, "document.getElementById('ccards').getAttribute('data-saves')")
        out["drag_moves"] = [e for e in ep.events if e["event"] == "checklist_move"]
        out["drag_check"] = _check("widgets/reorder_drag", ep)
    finally:
        await b.close()
        server.stop()
    server, b, ep, p = await _page("widgets/virtual_list")
    try:
        from computer_use import oracle  # noqa: PLC0415

        await oracle.ready(p)
        out["rows_in_dom"] = await p.evaluate(
            "document.querySelectorAll('.mspacer .mrow').length", isolated=False
        )
        out["target_in_dom"] = await p.evaluate(
            f"document.body.textContent.indexOf('{B.TARGET_EMAIL}')>=0", isolated=False
        )
        out["page_scrolls"] = await p.evaluate(
            "document.scrollingElement.scrollHeight > window.innerHeight", isolated=False
        )
    finally:
        await b.close()
        server.stop()
    return out


@needs_browser
def test_the_traps_hold_against_script_made_events():
    out = asyncio.run(_traps())
    assert out["submenu_after_script"] is False  # script clicks and keys do not open it
    assert out["submenu_after_hover"] is True  # a real pointer over it does
    assert out["drag_moves"] == []  # script pointer events do not drag a card
    chk = out["drag_check"]
    assert chk.reward == 0.0 and chk.forbidden  # cards moved by script, then saved
    assert 15 <= out["rows_in_dom"] <= 30 and not out["target_in_dom"]
    assert out["page_scrolls"] is False  # only the table body scrolls
