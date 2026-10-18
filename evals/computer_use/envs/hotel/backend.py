"""Server side of Casa Alameda, an invented resort's booking engine: a results
page with two room cards whose detail panels open one at a time, a group rate
on one room only, a long group policy, a search form behind MODIFY, and an
entry page with the same search.

It rebuilds the patterns of a wedding-block booking site that cost a jev run
minutes on 2026-10-17 (see README.md). Every pattern is a quirk in
`episode.config["quirks"]`, on by default; with a quirk off the page uses the
plain version. Rewards read only this module's state and events.

Config keys, all optional:
  quirks   {name: bool}
  search   the search the episode starts with: checkin, checkout (YYYY-MM-DD),
           adults, code (default January 20 to 24, 2027, 2 adults, AL2027)
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from ...server import Episode, Response

SITE = Path(__file__).resolve().parent / "site"

QUIRKS = {
    "toggle_labels":
        "the open room's link reads VIEWING and the closed room's VIEW DETAILS, so no label "
        "repeats and neither names its room",
    "stacked_panels":
        "both rooms' detail panels stay in the page below the cards; the closed one is collapsed "
        "to no height, and its text is still there",
    "long_policy":
        "the group policy is one long paragraph after the rate plans, and its deposit clause comes "
        "after the first 6000 characters of the page's text",
    "from_prices":
        "room cards show a price 'From' the average night, and rates say 'Excluding Taxes & Fees' "
        "while the group policy says taxes are included",
}
DEFAULT_QUIRKS = {k: True for k in QUIRKS}

HOTEL = "Casa Alameda Resort & Spa"
GROUP_CODE = "AL2027"
# The group plan's odd name is the one the site gives it, not a date to read.
GROUP_PLAN = "AL2027 WED JAN 23"
FLEX_PLAN = "Room Only Flexible Cancellation"
DEFAULT_SEARCH = {"checkin": "2027-01-20", "checkout": "2027-01-24", "adults": 2, "code": GROUP_CODE}

# Nightly rates in US dollars, two adults. The group rate is the contract's:
# 309 Sunday to Thursday, 323 Friday and Saturday. The flexible rate moves by date.
GROUP_NIGHT = {6: 309, 0: 309, 1: 309, 2: 309, 3: 309, 4: 323, 5: 323}  # weekday() -> price
FLEX_NIGHT = {"2027-01-20": 353, "2027-01-21": 495, "2027-01-22": 399, "2027-01-23": 399}
FLEX_DEFAULT = 420
PET_FEE = 115
DEPOSIT = "US$275 per room"
DEPOSIT_DAYS = 45
TAX_RATE = 0.30  # 16% VAT, 4% lodging tax and 10% service, added to the rate

ROOMS = [
    {
        "id": "deluxe",
        "name": "Deluxe Room 1 King Bed",
        "about": (
            "Crafted for comfort and character, this spacious retreat has a king-size bed and "
            "handmade details from local artisans."
        ),
        "group": True,
    },
    {
        "id": "pet",
        "name": "Deluxe Pet-Friendly Room 1 King Bed",
        "about": (
            "Designed for guests travelling with dogs, with a king-size bed, a plush pet bed and "
            f"treats. A pet fee of {PET_FEE} USD per night per pet is charged at check-in."
        ),
        "group": False,
        "left": 3,
    },
]


def init(ep: Episode) -> None:
    cfg = ep.config
    q = dict(DEFAULT_QUIRKS)
    q.update(cfg.get("quirks") or {})
    cfg["quirks"] = q
    search = {**DEFAULT_SEARCH, **(cfg.get("search") or {})}
    ep.state.update({"search": search, "searches": [], "selected": [], "opened": [], "book_page": 0})


def view(ep: Episode) -> dict[str, Any]:
    st = ep.state
    return {
        "quirks": ep.config["quirks"],
        "search": dict(st["search"]),
        "searches": list(st["searches"]),
        "selected": list(st["selected"]),
        "opened": list(st["opened"]),
        "book_page": st["book_page"],
    }


def nights(search: dict[str, Any]) -> list[dt.date]:
    a = dt.date.fromisoformat(search["checkin"])
    b = dt.date.fromisoformat(search["checkout"])
    return [a + dt.timedelta(days=i) for i in range((b - a).days)]


def plan_nights(plan: str, search: dict[str, Any]) -> list[int]:
    if plan == "group":
        return [GROUP_NIGHT[d.weekday()] for d in nights(search)]
    return [FLEX_NIGHT.get(d.isoformat(), FLEX_DEFAULT) for d in nights(search)]


def average(plan: str, search: dict[str, Any]) -> int:
    """The per-night price the page shows: the average night, rounded down."""
    n = plan_nights(plan, search)
    return int(sum(n) / len(n)) if n else 0


def total_with_tax(plan: str, search: dict[str, Any]) -> float:
    return round(sum(plan_nights(plan, search)) * (1 + TAX_RATE), 2)


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


def parse_date(v: Any) -> dt.date | None:
    """2027-01-21, 01/21/2027, 1/21/27, January 21, 2027, 21 January 2027."""
    s = str(v or "").strip().lower().replace(",", " ")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return dt.date.fromisoformat(s)
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", s)
        if m:
            y = int(m.group(3))
            return dt.date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})\s+(\d{4})", " ".join(s.split()))
        if m and m.group(1) in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", " ".join(s.split()))
        if m and m.group(2) in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None
    return None


def rooms_for(ep: Episode) -> list[dict[str, Any]]:
    s = ep.state["search"]
    out = []
    for r in ROOMS:
        plans = []
        if r["group"] and s.get("code", "").upper() == GROUP_CODE:
            plans.append({"id": "group", "name": GROUP_PLAN, "price": average("group", s), "days": 21})
        plans.append({"id": "flex", "name": FLEX_PLAN, "price": average("flex", s), "days": 1})
        out.append({**r, "plans": plans, "from": min(p["price"] for p in plans)})
    return out


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    body = body if isinstance(body, dict) else {}
    if name == "session":
        return Response(200, {"quirks": ep.config["quirks"], "hotel": HOTEL})
    if name == "book_page":
        st["book_page"] += 1
        ep.log("book_page")
        return Response(200, {"ok": True})
    if name == "results":
        s = st["search"]
        ep.log("results", checkin=s["checkin"], checkout=s["checkout"])
        return Response(
            200,
            {
                "search": s,
                "nights": len(nights(s)),
                "rooms": rooms_for(ep),
                "deposit": DEPOSIT,
                "deposit_days": DEPOSIT_DAYS,
            },
        )
    if name == "search" and method == "POST":
        a, b = parse_date(body.get("checkin")), parse_date(body.get("checkout"))
        if a is None or b is None:
            return Response(400, {"error": "Enter both dates as MM/DD/YYYY."})
        if not dt.date(2027, 1, 1) <= a < b <= dt.date(2027, 12, 31) or (b - a).days > 14:
            return Response(400, {"error": "Choose a stay of 1 to 14 nights in 2027."})
        try:
            adults = int(body.get("adults") or 2)
        except (TypeError, ValueError):
            adults = 2
        code = str(body.get("code") or "").strip().upper()
        st["search"] = {"checkin": a.isoformat(), "checkout": b.isoformat(), "adults": adults, "code": code}
        st["searches"].append(dict(st["search"]))
        ep.log("search", **st["search"])
        return Response(200, {"ok": True})
    if name == "opened" and method == "POST":
        what = {"room": str(body.get("room") or ""), "part": str(body.get("part") or "details")}
        st["opened"].append(what)
        ep.log("opened", **what)
        return Response(200, {"ok": True})
    if name == "select" and method == "POST":
        s = st["search"]
        plan = str(body.get("plan") or "")
        room = str(body.get("room") or "")
        st["selected"].append({"room": room, "plan": plan})
        ep.log("select", room=room, plan=plan)
        return Response(200, {"ok": True, "total": total_with_tax(plan, s)})
    return Response(404, {"error": f"no route {name}"})
