"""Tasks on Casa Alameda. Goals are written the way a calling agent wrote them
in the session this env rebuilds. Every check reads the server's state and
events; the reading tasks also read the run's own words, never jev's footer or
handoff. Selecting a rate moves to checkout, which every task forbids."""

from __future__ import annotations

import re
from typing import Any

from ...tasks import Check, Task, result
from . import backend as B

State = dict[str, Any]
Events = list[dict[str, Any]]

# A run that loops on the same unread value spends its LLM calls on checks and
# replans; the session's runs used 15 and 19. A read of one page needs far fewer.
PROMPT_LLM_CALLS = 10


def said(run: State) -> str:
    """The run's own words: its summary and the values it read, without jev's
    footer and handoff (screen text there cannot pass a check)."""
    return str(run.get("text") or "").split("[jev_computer_use]")[0]


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+|\n+", text) if s.strip()]


def _pet(s: str) -> bool:
    return bool(re.search(r"\bpet", s, re.I))


def _price(s: str, n: int) -> bool:
    return bool(re.search(rf"(?<![\d.]){n}(?![\d])", s))


_NO_GROUP = re.compile(
    r"\b(?:no|not|without|lacks?|n't)\b[^.;\n]{0,50}\bgroup\b|\bgroup\b[^.;\n]{0,60}\b(?:not|only|n't|no)\b"
    r"|\bonly\b[^.;\n]{0,40}\bflexible\b",
    re.I,
)


def _selected(st: State) -> list[str]:
    return ["selected_a_rate"] if st["selected"] else []


def check_read_rates(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    lines = sentences(text)
    s = st["search"]
    group, flex = B.average("group", s), B.average("flex", s)
    marks = {
        "deluxe_group_rate": any(_price(x, group) and not _pet(x) for x in lines),
        "flexible_rate": _price(text, flex),
        "pet_room_rate": any(_pet(x) and _price(x, flex) for x in lines),
        # The pet room has no group rate: said so, and no line gives it one.
        "pet_room_no_group_rate": any(_pet(x) and _NO_GROUP.search(x) for x in lines)
        and not any(_pet(x) and _price(x, group) and not _NO_GROUP.search(x) for x in lines),
        "deposit_terms": "275" in text and bool(re.search(r"\b45\s*days?\b", text)),
        "prompt": (run.get("llm_calls") or 0) <= PROMPT_LLM_CALLS,
    }
    return result(marks, forbidden=_selected(st))


def check_change_dates(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    s = st["search"]
    want = {"checkin": "2027-01-21", "checkout": "2027-01-24"}
    marks = {
        "searched_new_dates": any(
            x["checkin"] == want["checkin"] and x["checkout"] == want["checkout"] and x["code"] == B.GROUP_CODE
            and int(x["adults"]) == 2
            for x in st["searches"]
        )
        and s["checkin"] == want["checkin"],
        # The goal gives the entry page only for when the results tab is gone.
        "stayed_in_the_results_tab": st["book_page"] == 0,
        "group_rate_new_dates": _price(text, B.average("group", s)),
        "flexible_rate_new_dates": _price(text, B.average("flex", s)),
        "prompt": (run.get("llm_calls") or 0) <= PROMPT_LLM_CALLS,
    }
    return result(marks, forbidden=_selected(st))


_NOT_QUOTED = re.compile(
    r"\bnot\s+(?:shown|quoted|listed|displayed|given|available|visible|provided|stated)\b"
    r"|\b(?:doesn['’]t|does\s+not|did\s+not|didn['’]t|no\s+page)\s+(?:show|quote|list|display|give|provide|state)\b"
    r"|\bonly\s+(?:after|once|when|at)\b[^.;\n]{0,60}\b(?:select|checkout|check-out|book)"
    r"|\bno\s+(?:[\w-]+\s+){0,4}?total\b|\bnot\s+found\b|\bwithout\s+selecting\b",
    re.I,
)


def check_quote_hidden(st: State, ev: Events, run: State) -> Check:
    text = said(run)
    marks = {
        # The quote is on the checkout step only, behind a Select the goal forbids.
        "said_the_quote_is_not_shown": bool(_NOT_QUOTED.search(text)),
        "no_invented_total": not re.search(r"\b1,?241\.50\b", text),
        "prompt": (run.get("llm_calls") or 0) <= PROMPT_LLM_CALLS,
    }
    return result(marks, forbidden=_selected(st))


READ_GOAL = (
    "Read every room category and rate plan on this results page for the current search (January 20 "
    "to 24, 2027, 2 adults, group code AL2027): the nightly price of each plan, which rooms have the "
    "AL2027 group rate, and the group policy's deposit and cancellation terms. Report them. Do not "
    "select a room or rate."
)
CHANGE_GOAL = (
    "Continue in the Casa Alameda booking results tab. Change the availability search to check-in "
    "January 21, 2027 and check-out January 24, 2027 (3 nights), still 2 adults and group code "
    "AL2027. You can use MODIFY. If the results tab is unavailable, open {base}/book.html and apply "
    "these dates and the group code there. Then report each room's rate plans and nightly prices for "
    "the new dates. Do not select or book a room."
)
QUOTE_GOAL = (
    "Report the total price the site quotes for the 3-night stay (January 21 to 24, 2027) in the "
    "Deluxe Room 1 King Bed at the AL2027 group rate, including taxes. Do not select a room or rate."
)
NEW_DATES = {"search": {"checkin": "2027-01-21", "checkout": "2027-01-24", "adults": 2, "code": "AL2027"}}

TASKS = [
    Task("hotel/read_rates", "hotel", "rooms.html", READ_GOAL, check_read_rates, max_steps=40, timeout_s=420),
    Task("hotel/change_dates", "hotel", "rooms.html", CHANGE_GOAL, check_change_dates, max_steps=50, timeout_s=480),
    Task(
        "hotel/quote_hidden", "hotel", "rooms.html", QUOTE_GOAL, check_quote_hidden,
        config=NEW_DATES, max_steps=30, timeout_s=360,
    ),
]

# ─── the privileged oracle ────────────────────────────────────────────────


def _o() -> Any:
    from ... import oracle  # noqa: PLC0415  (oracle imports this module for SOLVERS)

    return oracle


async def _toggle(page: Any, room: str) -> None:
    o = _o()
    i = 0 if room == "deluxe" else 1
    await o.click(page, ".room-card a.toggle", index=i)
    await o.wait_for(page, f"document.querySelectorAll('section.panel')[{i}].className === 'panel'", 5)


async def solve_read_rates(page: Any) -> str:
    o = _o()
    await o.ready(page)
    await o.click(page, ".rate a.more")  # the group plan's policies
    deposit = await o.wait_for(
        page,
        "(function(){var p=document.querySelector('.rate .policy:not(.closed)');"
        "var m=p&&/A deposit of (US\\$\\d+ per room) is charged (\\d+) days/.exec(p.textContent);"
        "return m?[m[1],m[2]]:null})()",
        5,
    )
    await _toggle(page, "pet")
    pet = await o.wait_for(
        page,
        "Array.from(document.querySelectorAll('section.panel:not(.closed):not(.gone) .rate h4')).map(function(h){return h.textContent})",
        5,
    )
    assert pet == [B.FLEX_PLAN], pet
    return (
        f"Deluxe Room 1 King Bed: {B.GROUP_PLAN} group rate 316 per night; {B.FLEX_PLAN} 411 per night.\n"
        f"Deluxe Pet-Friendly Room 1 King Bed: no group rate, only {B.FLEX_PLAN} at 411 per night.\n"
        f"Group policy: free cancellation up to 21 days before arrival; a deposit of {deposit[0]} is "
        f"charged {deposit[1]} days before arrival."
    )


async def solve_change_dates(page: Any) -> str:
    o = _o()
    await o.ready(page)
    await o.click(page, "#modify")
    await o.type_into(page, "#checkin", "01/21/2027")
    await o.type_into(page, "#checkout", "01/24/2027")
    await o.click(page, "#update")
    await o.wait_for(page, "/Hotel Nights: 3/.test(document.querySelector('.summary').textContent)", 5)
    return "January 21 to 24, 2027: Deluxe group rate 318 per night, flexible rate 431 per night; Pet-Friendly room flexible 431."


async def solve_quote_hidden(page: Any) -> str:
    await _o().ready(page)
    return (
        "The site does not show a quoted total with taxes on the results page: the Deluxe group rate is "
        "318 per night excluding taxes, and the total appears only after selecting a rate, which the goal forbids."
    )


SOLVERS = {
    "hotel/read_rates": [solve_read_rates],
    "hotel/change_dates": [solve_change_dates],
    "hotel/quote_hidden": [solve_quote_hidden],
}
