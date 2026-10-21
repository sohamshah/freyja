"""The canvas env (evals/computer_use/envs/canvas, "Ledger"): the page has no
DOM controls and no DOM text, every task is solvable with its quirks on and
off through pointer and keyboard events alone, and each check catches what it
forbids. The page tests need a headless Chromium and skip without one."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from computer_use import envs, oracle  # noqa: E402
from computer_use.browser import Browser, find_chrome  # noqa: E402
from computer_use.envs.canvas import backend as B  # noqa: E402
from computer_use.envs.canvas import tasks as T  # noqa: E402
from computer_use.run import make_server  # noqa: E402
from computer_use.server import Episode  # noqa: E402

pytestmark = pytest.mark.skipif("canvas" not in envs.NAMES, reason="CUA_ENVS leaves out canvas")
chrome = pytest.mark.skipif(find_chrome() is None, reason="no Chromium for the environments")
TASK = {t.id: t for t in T.TASKS}


def _ep(task_id: str, **cfg):
    t = TASK[task_id]
    ep = Episode(id=f"test-{task_id.split('/')[1]}", env="canvas", config={**t.config, **cfg})
    B.init(ep)
    return t, ep


def _check(t, ep, text: str = "", llm_calls: int = 0):
    return t.check(B.view(ep), list(ep.events), {"status": "done", "text": text, "llm_calls": llm_calls})


def test_the_open_total_is_what_the_page_draws():
    _, ep = _ep("canvas/open_total")
    assert B.open_total_cents(ep.state) == 367525 and B.dollars(367525) == "$3,675.25"
    assert sum(o["status"] == "open" for o in ep.state["orders"]) == 5


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
    assert _check(t, ep, "5 open orders, total $3,675.25").reward == 1
    assert _check(t, ep, "5 open orders totalling 3675.25 dollars").reward == 1
    assert _check(t, ep, "4 open orders, total $3,675.25").why == "missing: count"
    B.backend(ep, "POST", "ship", {}, {"id": 1041})
    assert not _check(t, ep, "5 open orders, total $3,675.25").marks["nothing_changed"]


async def _solve(task_id: str, quirks: dict | None = None):
    server = make_server()
    browser = await Browser.launch()
    try:
        t = TASK[task_id]
        cfg = dict(t.config)
        if quirks is not None:
            cfg["quirks"] = quirks
        ep = server.new_episode("canvas", cfg)
        ctx = await browser.new_context()
        page = await browser.new_page(context=ctx)
        await page.navigate(server.start_url(ep, f"/canvas/{t.start}"), timeout=20)
        out = await oracle.solve(task_id, page)
        return t.check(B.view(ep), list(ep.events), {**out, "llm_calls": 0}), page, browser, server
    except BaseException:
        await browser.close()
        server.stop()
        raise


def _run(task_id: str, quirks: dict | None = None):
    async def go():
        chk, page, browser, server = await _solve(task_id, quirks)
        await browser.close()
        server.stop()
        return chk

    return asyncio.run(go())


@chrome
@pytest.mark.parametrize("task_id", sorted(TASK))
@pytest.mark.parametrize("plain", [False, True])
def test_every_task_is_solvable(task_id, plain):
    quirks = {k: False for k in B.QUIRKS} if plain else None
    chk = _run(task_id, quirks)
    assert chk.reward == 1, chk.why


@chrome
def test_the_embedding_page_shows_a_frame_the_surface_cannot_read():
    """The portal page has only header text in its DOM, and the app is in an
    iframe: the snapshot lists the frame, not what it draws, and offers no
    way around it."""
    async def go():
        server = make_server()
        browser = await Browser.launch()
        try:
            ep = server.new_episode("canvas", {})
            page = await browser.new_page(context=await browser.new_context())
            await page.navigate(server.start_url(ep, "/canvas/embed.html"), timeout=20)
            await oracle.wait_for(page, "document.querySelector('iframe').contentWindow.__ledger", 10)
            controls = await page.evaluate("document.querySelectorAll('a,button,input,select,textarea').length")
            frames = await page.evaluate("document.querySelectorAll('iframe').length")
            inner = await page.evaluate(
                "document.querySelector('iframe').contentDocument.querySelectorAll('a,button,input').length"
            )
            return controls, frames, inner
        finally:
            await browser.close()
            server.stop()

    controls, frames, inner = asyncio.run(go())
    assert controls == 0 and frames == 1 and inner == 0


@chrome
def test_the_page_exposes_no_controls_and_no_text():
    async def go():
        server = make_server()
        browser = await Browser.launch()
        try:
            ep = server.new_episode("canvas", {})
            page = await browser.new_page(context=await browser.new_context())
            await page.navigate(server.start_url(ep, "/canvas/index.html"), timeout=20)
            await oracle.wait_for(page, "window.__ledger && window.__ledger.ids().length > 0", 10)
            controls = await page.evaluate(
                "document.querySelectorAll('a,button,input,select,textarea,[role],[contenteditable]').length"
            )
            text = await page.evaluate("document.body.innerText.trim()")
            drawn = await page.evaluate("window.__ledger.ids().length", isolated=False)
            return controls, text, drawn
        finally:
            await browser.close()
            server.stop()

    controls, text, drawn = asyncio.run(go())
    assert controls == 0 and text == "" and drawn >= 10
