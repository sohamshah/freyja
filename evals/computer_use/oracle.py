"""A privileged scripted solver for the airline tasks. It knows the page
structure and acts with trusted pointer and keyboard events, so a task it
cannot finish is a broken env or a broken check, not a weak agent. Tests use
it to prove every task is solvable and every check can pass.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .browser import Page

PAX = [("Ada", "Lovelace", "Female", "12/10/1990"), ("Alan", "Turing", "Male", "06/23/1992")]


async def wait_for(page: Page, js: str, timeout: float = 10.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    probe = f"(function(){{var v=({js}); return (v && typeof v === 'object' && v.nodeType) ? true : v;}})()"  # noqa: E501
    while True:
        try:
            v = await page.evaluate(probe, isolated=False)
        except Exception:  # noqa: BLE001  (navigating)
            v = None
        if v:
            return v
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out waiting for {js[:80]}")
        await asyncio.sleep(0.15)


async def click(page: Page, selector: str, *, index: int = 0, frame_js: str | None = None) -> None:
    """Trusted click at the centre of the element (scrolled into view)."""
    js = (
        f"(function(){{var e=document.querySelectorAll({json.dumps(selector)})[{index}]; if(!e) return null;"  # noqa: E501
        "e.scrollIntoView({block:'center'}); var r=e.getClientRects()[0]||e.getBoundingClientRect(); return [r.left+r.width/2, r.top+r.height/2];})()"  # noqa: E501
    )
    xy = await wait_for(page, js)
    await page.mouse_click(*xy)
    await asyncio.sleep(0.25)


async def type_into(page: Page, selector: str, text: str) -> None:
    await click(page, selector)
    await page.evaluate(
        f"(function(){{var e=document.querySelector({json.dumps(selector)}); e.select && e.select();}})()",  # noqa: E501
        isolated=False,
    )
    await page.insert_text(text)
    await asyncio.sleep(0.2)


async def ready(page: Page) -> None:
    await wait_for(page, "document.body && document.body.getAttribute('data-ready')==='1'", 15)


async def dismiss_promo(page: Page) -> None:
    await asyncio.sleep(1.5)
    await page.key("Escape")


async def search(page: Page) -> None:
    await ready(page)
    await dismiss_promo(page)
    await type_into(page, "#origin", "Denv")
    await wait_for(page, "document.querySelector('ul.ui-autocomplete li.ui-menu-item')")
    await page.key("ArrowDown")
    await page.key("Enter")
    await wait_for(page, "!document.getElementById('destination').disabled")
    await type_into(page, "#destination", "Orlando")
    await wait_for(
        page,
        "Array.from(document.querySelectorAll('ul.ui-autocomplete')).some(function(u){return u.style.display!=='none' && u.querySelector('li.ui-menu-item')})",  # noqa: E501
    )
    await click(page, "ul.ui-autocomplete:not([style*='none']) li.ui-menu-item")
    await click(page, "#departureDate")
    await _pick_day(page, "November 2026", 12)
    await wait_for(page, "document.querySelector('.ui-datepicker')")
    await _pick_day(page, "November 2026", 18)
    if await page.evaluate("!!document.getElementById('passengersSelect')", isolated=False):
        await page.evaluate(
            "(function(){var s=document.getElementById('passengersSelect'); s.value='2'; s.dispatchEvent(new Event('change',{bubbles:true}));})()",  # noqa: E501
            isolated=False,
        )
    else:
        await click(page, "#passengersInput")
        await click(page, "button[aria-label='add adult']")
        await click(page, "#passengersInput")  # its field again: what closes the panel
    await click(page, "#btnSearch, #btnSearch2")
    await wait_for(page, "location.pathname.endsWith('select.html')", 10)


async def _pick_day(page: Page, month: str, day: int) -> None:
    js = (
        f"(function(){{var ms=Array.from(document.querySelectorAll('.ui-datepicker .month'));"
        f"var m=ms.filter(function(x){{return x.querySelector('.ui-datepicker-title').textContent==={json.dumps(month)}}})[0];"  # noqa: E501
        f"if(!m) return null; var a=Array.from(m.querySelectorAll('td[data-handler=selectDay] a')).filter(function(a){{return a.textContent==='{day}'}})[0];"  # noqa: E501
        "if(!a) return null; var r=a.getBoundingClientRect(); return [r.left+r.width/2, r.top+r.height/2];})()"  # noqa: E501
    )
    for _ in range(3):
        xy = await wait_for(page, f"document.querySelector('.ui-datepicker') && ({js})", 5)
        if xy:
            await page.mouse_click(*xy)
            await asyncio.sleep(0.3)
            return
        await click(page, ".ui-datepicker-next")


async def select_fares(page: Page) -> None:
    await ready(page)
    await wait_for(page, "document.querySelectorAll('.frow').length > 10")
    # The target rows: the 9:05 PM nonstop out, the 2:40 PM nonstop back; Business is the 4th box.
    for times in ("9:05 PM", "2:40 PM"):
        idx = await page.evaluate(
            f"(function(){{var rows=Array.from(document.querySelectorAll('.frow')); var all=Array.from(document.querySelectorAll('.farebox'));"  # noqa: E501
            f"var r=rows.filter(function(x){{return x.querySelector('.times').textContent.indexOf({json.dumps(times)})===0 && x.textContent.indexOf('Nonstop')>=0}})[0];"  # noqa: E501
            "var b=r.querySelectorAll('.farebox')[3]; return all.indexOf(b);})()",
            isolated=False,
        )
        await click(page, ".farebox", index=idx)
    await wait_for(page, "!document.getElementById('fareBar').classList.contains('hidden')")
    await click(page, "#fareBar .opt:nth-child(2) button")
    await asyncio.sleep(0.4)
    has_modal = await page.evaluate("!!document.querySelector('.overlay')", isolated=False)
    if has_modal:
        await click(page, ".overlay a")
    await wait_for(page, "location.pathname.endsWith('passengers.html')", 10)


async def passengers(page: Page) -> None:
    await ready(page)
    for i, (first, last, gender, dob) in enumerate(PAX):
        base = f"#paxForms .card:nth-child({i + 1}) "
        await type_into(page, base + "input", first)
        await type_into(page, base + "label:nth-child(3) input", last)
        await page.evaluate(
            f"(function(){{var s=document.querySelectorAll({json.dumps(base + 'select')})[1]; s.value={json.dumps(gender)}; s.dispatchEvent(new Event('change',{{bubbles:true}}));}})()",  # noqa: E501
            isolated=False,
        )
        await type_into(page, base + "input[placeholder]", dob)
    c = "#paxForms .card:nth-child(3) "
    await type_into(page, c + "input[type=email]", "ada@example.com")
    await type_into(page, c + "input[type=tel]", "5550100")
    await type_into(page, c + "label:nth-child(6) input", "80202")
    await click(page, "#btnContinue")
    await wait_for(page, "location.pathname.endsWith('seats.html')", 10)


async def seats(page: Page, *, cont: bool = False) -> None:
    await ready(page)
    await wait_for(page, "document.querySelectorAll('#seatWho a').length >= 4")
    plan = [("out", 0, "1D"), ("out", 1, "1F"), ("ret", 0, "2D"), ("ret", 1, "2F")]
    for leg, pax, seat in plan:
        flight = "Denver to Orlando" if leg == "out" else "Orlando to Denver"
        name = " ".join(PAX[pax][:2])
        link = await page.evaluate(
            f"(function(){{var a=Array.from(document.querySelectorAll('#seatWho a')); return a.findIndex(function(x){{return x.textContent.indexOf({json.dumps(name + ' for flight ' + flight)})>=0}});}})()",  # noqa: E501
            isolated=False,
        )
        await click(page, "#seatWho a", index=link)
        await wait_for(
            page,
            f"document.getElementById('seatHead').textContent.indexOf({json.dumps(name)})>=0",
            5,
        )
        await click(page, f"#seatMap button[aria-label^='seat: {seat} ']")
        await wait_for(
            page,
            f"(function(){{var b=document.querySelector(\"#seatMap button[aria-label^='seat: {seat} ']\"); return b && b.classList.contains('mine');}})()",  # noqa: E501
            5,
        )
    if cont:
        await click(page, "#btnSeatsContinue")
        await wait_for(page, "location.pathname.endsWith('bags.html')", 10)


async def bags(page: Page) -> None:
    await ready(page)
    await click(page, "#btnBagsContinue")
    await wait_for(page, "location.pathname.endsWith('extras.html')", 10)


async def extras(page: Page) -> None:
    await ready(page)
    frame = await page.evaluate(
        "(function(){var f=document.querySelector('iframe.offer-frame'); if(!f) return null; f.scrollIntoView({block:'start'}); var r=f.getBoundingClientRect(); return [r.left, r.top, r.width];})()",  # noqa: E501
        isolated=False,
    )
    if frame:
        await asyncio.sleep(1.0)
        # The frame is cross-origin (no DOM access): click the two "No thanks" cards
        # by their place in its fixed layout (offer.html).
        for y in (75, 201):
            await page.mouse_click(frame[0] + frame[2] * 0.75, frame[1] + y)
            await asyncio.sleep(0.4)
    else:
        for name in ("disruption", "cfar"):
            await click(page, f"input[name={name}]", index=1)
    await click(page, "input[name=selfservice][value=free]")
    await click(page, "#btnExtrasContinue")
    await wait_for(page, "location.pathname.endsWith('payment.html')", 15)


SOLVERS = {
    "airline/search": [search],
    "airline/search_trusted": [search],
    "airline/select": [select_fares],
    "airline/passengers": [passengers],
    "airline/seats": [seats],
    "airline/extras": [extras],
    "airline/e2e": [search, select_fares, passengers, lambda p: seats(p, cont=True), bags, extras],
}


def _env_solvers() -> None:
    """The other environments keep their solvers next to their tasks."""
    from . import envs  # noqa: PLC0415

    for name in envs.NAMES:
        SOLVERS.update(getattr(envs.tasks(name), "SOLVERS", {}))


_env_solvers()


async def solve(task_id: str, page: Page) -> dict[str, Any]:
    """Runs the task's steps; a step that returns text gives the run's report."""
    text = "Business Bundle standard $422 on the 9:05 PM nonstop."
    for step in SOLVERS[task_id]:
        out = await step(page)
        if isinstance(out, str):
            text = out
    return {"status": "done", "text": text}
