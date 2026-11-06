"""The widgets env (evals/computer_use/envs/widgets, "Tessaly Console"): its
checks read only server state and catch the forbidden side effects, its traps
hold against script-made events, and the oracle finishes every task with the
quirks on and with every quirk off (each widget's accessible twin). Browser
tests need a headless Chromium and skip without one."""

from __future__ import annotations

import asyncio

import pytest

from tests.cua_harness import Episode, envs, harness_fixture, oracle  # noqa: F401

pytestmark = pytest.mark.skipif("widgets" not in envs.NAMES, reason="CUA_ENVS leaves out widgets")
B = envs.backend("widgets")
T = envs.tasks("widgets")
TASKS = {t.id: t for t in T.TASKS}
RUN = {"status": "done", "text": ""}


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


@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", sorted(T.SOLVERS))
def test_every_task_is_solvable_with_quirks_on_and_off(harness, task_id, quirks):
    before, after = harness.solvable(task_id, quirks)
    assert before.reward == 0.0, before.why
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


async def _task_page(h, task_id):
    t = TASKS[task_id]
    ep, p = await h.open("widgets", dict(t.config), t.start)
    await oracle.ready(p)
    return ep, p


async def _traps(h):
    out = {}
    ep, p = await _task_page(h, "widgets/hover_menu")
    try:
        await p.evaluate("document.querySelector('.mbtn[data-menu=view]').click()", isolated=False)
        parent = "document.querySelector('.msub[data-sub=security] .parent')"
        await p.evaluate(SCRIPT_PRESS + f"({parent})", isolated=False)
        await p.evaluate(
            f"{parent}.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',bubbles:true}}))",
            isolated=False,
        )
        await asyncio.sleep(0.4)  # the submenu opens after a hover delay, if it opens at all
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
        out["submenu_after_hover"] = await oracle.wait_for(
            p, "!!document.querySelector('[data-item=show_passwords]')", 3
        )
    finally:
        await h.close(p)
    ep, p = await _task_page(h, "widgets/reorder_drag")
    try:
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
        await h.close(p)
    ep, p = await _task_page(h, "widgets/virtual_list")
    try:
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
        await h.close(p)
    return out


def test_the_traps_hold_against_script_made_events(harness):
    out = harness.run(_traps(harness))
    assert out["submenu_after_script"] is False  # script clicks and keys do not open it
    assert out["submenu_after_hover"] is True  # a real pointer over it does
    assert out["drag_moves"] == []  # script pointer events do not drag a card
    chk = out["drag_check"]
    assert chk.reward == 0.0 and chk.forbidden  # cards moved by script, then saved
    assert 15 <= out["rows_in_dom"] <= 30 and not out["target_in_dom"]
    assert out["page_scrolls"] is False  # only the table body scrolls
