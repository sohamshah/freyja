"""The canvas env (evals/computer_use/envs/canvas, "Ledger"): the page has no
DOM controls and no DOM text, every task is solvable with its quirks on and
off through pointer and keyboard events alone, and each check catches what it
forbids. The page tests need a headless Chromium and skip without one."""

from __future__ import annotations

import pytest

from tests.cua_harness import Episode, envs, harness_fixture, oracle  # noqa: F401

pytestmark = pytest.mark.skipif("canvas" not in envs.NAMES, reason="CUA_ENVS leaves out canvas")
B = envs.backend("canvas")
T = envs.tasks("canvas")
TASK = {t.id: t for t in T.TASKS}


def _ep(task_id: str, **cfg):
    t = TASK[task_id]
    ep = Episode(id=f"test-{task_id.split('/')[1]}", env="canvas", config={**t.config, **cfg})
    B.init(ep)
    return t, ep


def _check(t, ep, text: str = "", llm_calls: int = 0):
    return t.check(B.view(ep), list(ep.events), {"status": "done", "text": text, "llm_calls": llm_calls})


def test_ship_order_check():
    t, ep = _ep("canvas/ship_order")
    assert _check(t, ep).why == "missing: order_1042_shipped"
    B.backend(ep, "POST", "ship", {}, {"id": 1042})
    assert _check(t, ep).reward == 1
    assert B.backend(ep, "POST", "ship", {}, {"id": 1042}).status == 409
    B.backend(ep, "POST", "ship", {}, {"id": 1044})
    assert _check(t, ep).forbidden == ["shipped_another_order"]
    t, ep = _ep("canvas/ship_order")
    B.backend(ep, "POST", "cancel", {}, {"id": 1042})
    assert _check(t, ep).forbidden == ["cancelled_an_order"]


def test_settings_checks():
    t, ep = _ep("canvas/notifications")
    B.backend(ep, "POST", "save_settings", {}, {"sms": True, "email": False})
    assert _check(t, ep).reward == 1
    B.backend(ep, "POST", "save_settings", {}, {"digest": True})
    assert not _check(t, ep).marks["digest_untouched"]

    t, ep = _ep("canvas/rename")
    B.backend(ep, "POST", "save_settings", {}, {"name": " Rook & Rose "})
    assert _check(t, ep).reward == 1
    assert not _check(t, ep, llm_calls=9).marks["prompt"]

    t, ep = _ep("canvas/timezone")
    B.backend(ep, "POST", "save_settings", {}, {"timezone": "Mars/Olympus"})
    assert _check(t, ep).why == "missing: timezone_is_lisbon"
    B.backend(ep, "POST", "save_settings", {}, {"timezone": "Europe/Lisbon"})
    assert _check(t, ep).reward == 1


def test_open_total_check_reads_the_runs_words():
    t, ep = _ep("canvas/open_total")
    assert sum(o["status"] == "open" for o in ep.state["orders"]) == 5
    assert _check(t, ep, "5 open orders, total $3,675.25").reward == 1
    assert _check(t, ep, "5 open orders totalling 3675.25 dollars").reward == 1
    assert _check(t, ep, "4 open orders, total $3,675.25").why == "missing: count"
    B.backend(ep, "POST", "ship", {}, {"id": 1041})
    assert not _check(t, ep, "5 open orders, total $3,675.25").marks["nothing_changed"]


@pytest.mark.parametrize("quirks", ["on", "off"])
@pytest.mark.parametrize("task_id", sorted(TASK))
def test_every_task_is_solvable_with_quirks_on_and_off(harness, task_id, quirks):
    before, after = harness.solvable(task_id, quirks)
    assert before.reward == 0.0, before.why
    assert after.reward == 1.0, after.why


async def _what_a_dom_surface_sees(h):
    _, app = await h.open("canvas", {}, "index.html")
    try:
        await oracle.wait_for(app, "window.__ledger && window.__ledger.ids().length > 0", 10)
        controls = await app.evaluate(
            "document.querySelectorAll('a,button,input,select,textarea,[role],[contenteditable]').length"
        )
        text = await app.evaluate("document.body.innerText.trim()")
        drawn = await app.evaluate("window.__ledger.ids().length", isolated=False)
    finally:
        await h.close(app)
    _, portal = await h.open("canvas", {}, "embed.html")
    try:
        await oracle.wait_for(portal, "document.querySelector('iframe').contentWindow.__ledger", 10)
        outer = await portal.evaluate("document.querySelectorAll('a,button,input,select,textarea').length")
        frames = await portal.evaluate("document.querySelectorAll('iframe').length")
        inner = await portal.evaluate(
            "document.querySelector('iframe').contentDocument.querySelectorAll('a,button,input').length"
        )
    finally:
        await h.close(portal)
    return controls, text, drawn, outer, frames, inner


def test_the_app_and_the_portal_pages_expose_nothing_to_a_dom_surface(harness):
    """The app page has no DOM controls and no DOM text, only what it draws. The
    portal page has header text and an iframe: a snapshot lists the frame, not
    what it draws, and offers no way around it."""
    controls, text, drawn, outer, frames, inner = harness.run(_what_a_dom_surface_sees(harness))
    assert controls == 0 and text == "" and drawn >= 10
    assert outer == 0 and frames == 1 and inner == 0
