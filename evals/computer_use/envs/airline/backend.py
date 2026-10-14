"""Server side of the mock budget-airline site: the booking a page builds up,
its rules, and the price model. Pages are plain HTML that call these JSON
routes and navigate on success, the way the real flow posts between pages.

Every behaviour that made a real booking run fail is a switch in
`episode.config["quirks"]` (all on by default), so a fix can be tested against
the quirk it targets and the rest left alone. See ../../README.md.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from typing import Any

from ...server import Episode, Response

QUIRKS = {
    # Home page
    "promo_popup":
        "a promotion covers the page a moment after load (open shadow root, unlabeled close, "
        "Escape closes it)",
    "autocomplete":
        "airport suggestions are list items with no role, tabindex=-1 and no pointer cursor; only "
        "a click on one or ArrowDown+Enter commits the airport",
    "readonly_travelers":
        "Travelers is a read-only input that opens a panel of add/remove buttons; Escape does not "
        "close it, and the open panel covers the fields below",
    "image_search": "the Search control is a link around an image with no alt text",
    # Flight select
    "hidden_radio_fares":
        "each fare box is a div with a zero-size radio inside; the radio labels repeat across "
        "columns",
    "upsell_modal":
        "Continue at the standard price opens a membership upsell with no dialog role, appended "
        "after a long page",
    "nested_scroll": "the flight list scrolls inside its own panel",
    # Passengers
    "change_commit":
        "fields reach the booking only on their change event (on blur), the way some form "
        "frameworks bind values",
    # Seats
    "active_passenger":
        "a seat goes to the active passenger; selecting a seat does not move to the next "
        "passenger",
    # Add-ons
    "iframe_offers": "the two insurance opt-outs live in a cross-origin frame and are required",
    "slow_continue":
        "Continue on add-ons answers after a long server wait, with the address changed to #0 "
        "first",
    # Everywhere
    "hidden_templates":
        "closed slide-in panels parked off the right edge leave Close buttons behind",
    "cookie_banner": "a cookie banner stays fixed at the bottom",
    # Off by default: no page-script surface can pass it (see the search_trusted task).
    "trusted_toggle":
        "the open Travelers panel closes only on a trusted click on its field (script clicks are "
        "ignored)",
}
DEFAULT_QUIRKS = {k: k != "trusted_toggle" for k in QUIRKS}

TODAY = dt.date(2026, 10, 13)
AIRPORTS = [
    ("SFO", "San Francisco", "California", "United States of America"),
    ("SJC", "San Jose", "California", "United States of America"),
    ("OAK", "Oakland", "California", "United States of America"),
    ("SMF", "Sacramento", "California", "United States of America"),
    ("LAX", "Los Angeles", "California", "United States of America"),
    ("SAN", "San Diego", "California", "United States of America"),
    ("ATL", "Atlanta", "Georgia", "United States of America"),
    ("AUS", "Austin", "Texas", "United States of America"),
    ("DEN", "Denver", "Colorado", "United States of America"),
    ("DFW", "Dallas/Fort Worth", "Texas", "United States of America"),
    ("LAS", "Las Vegas", "Nevada", "United States of America"),
    ("MCO", "Orlando", "Florida", "United States of America"),
    ("MIA", "Miami", "Florida", "United States of America"),
    ("ORD", "Chicago O'Hare", "Illinois", "United States of America"),
    ("PHL", "Philadelphia", "Pennsylvania", "United States of America"),
    ("SEA", "Seattle", "Washington", "United States of America"),
    ("SJU", "San Juan", "Puerto Rico", "Puerto Rico"),
    ("CUN", "Cancun", "Quintana Roo", "Mexico"),
]
CITY = {a[0]: a[1] for a in AIRPORTS}
BUNDLES = [
    ("basic", "Basic", 0),
    ("economy", "Economy Bundle", 38),
    ("premium", "Premium Bundle", 68),
    ("business", "Business Bundle", 175),
]
CLUB_SAVER_OFF = 2
MEMBERSHIP_FEE = 59.99
SEAT_PRICES = {"legroom": 45, "preferred": 22, "standard": 14}
STAGES = ["search", "select", "passengers", "seats", "bags", "extras", "payment", "purchased"]


def init(ep: Episode) -> None:
    cfg = ep.config
    q = dict(DEFAULT_QUIRKS)
    q.update(cfg.get("quirks") or {})
    cfg["quirks"] = q
    ep.state.update(
        {
            "stage": "search",
            "search": None,
            "selection": None,  # {"out": {...}, "ret": {...}, "fare": "standard"|"club_saver"}
            "membership": None,  # None | "declined" | "accepted"
            "passengers": None,
            "contact": None,
            "seats": {},  # "out:0" -> "1A"
            "extras": {"disruption": None, "cfar": None, "self_service": None},
            "purchased": False,
            "last_activity": time.time() - float(cfg.get("session_age_s") or 0),
            "errors": [],
        }
    )
    seed = cfg.get("seed", 7)
    ep.state["rng_seed"] = seed
    pre = cfg.get("preset")
    if pre:
        _preset(ep, pre)


def _dates(cfg: dict[str, Any]) -> tuple[str, str]:
    return cfg.get("depart", "2026-11-12"), cfg.get("return", "2026-11-18")


def _flights(ep: Episode, leg: str) -> list[dict[str, Any]]:
    """Deterministic results for one direction, including the two targets every
    task uses (a late-evening nonstop out, an afternoon nonstop back)."""
    s = ep.state["search"] or {}
    o, d = (
        (s.get("origin", "DEN"), s.get("dest", "MCO"))
        if leg == "out"
        else (s.get("dest", "MCO"), s.get("origin", "DEN"))
    )
    rng = random.Random(f"{ep.state['rng_seed']}-{leg}-{o}-{d}")
    hubs = ["DEN", "LAS", "DFW", "LAX", "ORD", "MCO"]
    out: list[dict[str, Any]] = []
    target = (
        ("2210", "9:05 PM", "2:48 AM", 1, "3 hrs 43 min")
        if leg == "out"
        else ("2211", "2:40 PM", "4:31 PM", 0, "3 hrs 51 min")
    )
    times = [
        "5:58 AM",
        "6:02 AM",
        "7:40 AM",
        "9:14 AM",
        "11:05 AM",
        "12:20 PM",
        "1:58 PM",
        "3:05 PM",
        "4:45 PM",
        "6:30 PM",
        "7:14 PM",
        "9:27 PM",
        "11:52 PM",
    ]
    for i, t in enumerate(times):
        hub = rng.choice(hubs)
        base = rng.randint(118, 260)
        dur_h, dur_m = rng.randint(6, 21), rng.randint(0, 59)
        arr_h = rng.randint(1, 12)
        out.append(
            {
                "id": f"{leg}-{i}",
                "flight": f"KS {rng.randint(1100, 2999)}",
                "dep": t,
                "arr": f"{arr_h}:{rng.randint(0, 59):02d} {'AM' if rng.random() < 0.5 else 'PM'}",
                "next_day": rng.random() < 0.4,
                "duration": f"{dur_h} hrs {dur_m} min",
                "stops": f"1 Stop {hub}",
                "legs": [o, hub, d],
                "base": base,
            }
        )
    num, dep, arr, nd, dur = target
    tgt = {
        "id": f"{leg}-t",
        "flight": f"KS {num}",
        "dep": dep,
        "arr": arr,
        "next_day": bool(nd),
        "duration": dur,
        "stops": "Nonstop",
        "legs": [o, d],
        "base": 247 if leg == "out" else 219,
        "target": True,
    }
    pos = 11 if leg == "out" else 7
    out.insert(pos, tgt)
    for f in out:
        f["fares"] = {
            b: {"standard": f["base"] + add, "club_saver": f["base"] + add - CLUB_SAVER_OFF}
            for b, _, add in BUNDLES
        }
    return out


def _find(ep: Episode, leg: str, fid: str) -> dict[str, Any] | None:
    return next((f for f in _flights(ep, leg) if f["id"] == fid), None)


def _adults(ep: Episode) -> int:
    return int((ep.state["search"] or {}).get("adults") or 1)


def total(ep: Episode) -> float:
    st = ep.state
    sel = st.get("selection")
    if not sel:
        return 0.0
    fare = sel.get("fare", "standard")
    t = 0.0
    for leg in ("out", "ret"):
        f = _find(ep, leg, sel[leg]["flight_id"])
        if f:
            t += f["fares"][sel[leg]["bundle"]][fare]
    t *= _adults(ep)
    taxes = 47.0 * _adults(ep) * 2
    t += taxes
    if st.get("membership") == "accepted" or fare == "club_saver":
        t += MEMBERSHIP_FEE
    for key, seat in st["seats"].items():
        leg = key.split(":")[0]
        bundle = sel[leg]["bundle"]
        cls = _seat_class(seat)
        if not (bundle == "business" and cls == "legroom") and bundle not in ("premium",):
            t += SEAT_PRICES[cls]
    ex = st["extras"]
    if ex.get("disruption") == "yes":
        t += 19.0 * _adults(ep)
    if ex.get("cfar") == "yes":
        t += 54.0 * _adults(ep)
    if ex.get("self_service") == "agent":
        t += 25.0 * _adults(ep)
    return round(t, 2)


def _seat_class(seat: str) -> str:
    row = int("".join(c for c in seat if c.isdigit()) or 0)
    return "legroom" if row <= 3 else "preferred" if row <= 8 else "standard"


def seat_map(ep: Episode, leg: str) -> dict[str, Any]:
    rng = random.Random(f"{ep.state['rng_seed']}-seats-{leg}")
    taken = {s for s in (f"{r}{c}" for r in range(1, 25) for c in "ABCDEF") if rng.random() < 0.35}
    # Rows 1-3 are Legroom Plus (2x2: no B/E seats sold); on the return 1D is taken.
    taken -= {"1A", "1C", "1D", "1F", "2A", "2C", "2D", "2F", "3A", "3C", "3D", "3F"}
    if leg == "ret":
        taken.add("1D")
    mine = {v: k for k, v in ep.state["seats"].items() if k.startswith(leg + ":")}
    rows = []
    for r in range(1, 25):
        cols = []
        for c in "ABCDEF":
            sid = f"{r}{c}"
            blocked = r <= 3 and c in "BE"
            cols.append(
                {
                    "id": sid,
                    "cls": _seat_class(sid),
                    "taken": (sid in taken and sid not in mine) or blocked,
                    "blocked": blocked,
                    "who": mine.get(sid),
                }
            )
        rows.append(cols)
    return {"rows": rows}


def _preset(ep: Episode, name: str) -> None:
    """Start an episode partway through the flow (a task that starts on a later page)."""
    st = ep.state
    dep, ret = _dates(ep.config)
    st["search"] = {
        "origin": "DEN",
        "dest": "MCO",
        "depart": dep,
        "return": ret,
        "adults": 2,
        "trip": "roundtrip",
    }
    st["stage"] = "select"
    if name == "select":
        return
    st["selection"] = {
        "out": {"flight_id": "out-t", "bundle": "business"},
        "ret": {"flight_id": "ret-t", "bundle": "business"},
        "fare": "standard",
    }
    st["membership"] = "declined"
    st["stage"] = "passengers"
    if name == "passengers":
        return
    st["passengers"] = [
        {"first": "Ada", "middle": "", "last": "Lovelace", "gender": "Female", "dob": "12/10/1990"},
        {"first": "Alan", "middle": "", "last": "Turing", "gender": "Male", "dob": "06/23/1992"},
    ]
    st["contact"] = {
        "first": "Ada",
        "last": "Lovelace",
        "email": "ada@example.com",
        "phone": "5550100",
        "country": "United States of America",
        "zip": "80202",
    }
    st["stage"] = "seats"
    if name == "seats":
        return
    st["seats"] = {"out:0": "1D", "out:1": "1F", "ret:0": "2D", "ret:1": "2F"}
    st["stage"] = "bags"
    if name == "bags":
        return
    st["stage"] = "extras"
    if name == "extras":
        return
    st["extras"] = {"disruption": "no", "cfar": "no", "self_service": "free"}
    st["stage"] = "payment"


def _page_for(stage: str) -> str:
    return {
        "search": "index.html",
        "select": "select.html",
        "passengers": "passengers.html",
        "seats": "seats.html",
        "bags": "bags.html",
        "extras": "extras.html",
        "payment": "payment.html",
        "purchased": "confirm.html",
    }[stage]


def _expired(ep: Episode) -> bool:
    limit = float(ep.config.get("session_timeout_s") or 0)
    return bool(limit) and time.time() - ep.state["last_activity"] > limit


def _reset(ep: Episode) -> None:
    keep = (ep.state["rng_seed"],)
    for k in ("search", "selection", "membership", "passengers", "contact"):
        ep.state[k] = None
    ep.state.update(
        {
            "stage": "search",
            "seats": {},
            "extras": {"disruption": None, "cfar": None, "self_service": None},
        }
    )
    ep.state["rng_seed"] = keep[0]


def _need(ep: Episode, stage: str) -> Response | None:
    """The real flow sends you home when a page is opened out of order or the session is gone."""
    if _expired(ep):
        ep.log("session_expired", stage=ep.state["stage"])
        _reset(ep)
        return Response(440, {"expired": True, "go": "index.html?expired=1"})
    if STAGES.index(ep.state["stage"]) < STAGES.index(stage):
        return Response(409, {"error": f"not at {stage} yet", "go": _page_for(ep.state["stage"])})
    return None


def session_view(ep: Episode) -> dict[str, Any]:
    st = ep.state
    view = {
        k: st[k]
        for k in (
            "stage",
            "search",
            "selection",
            "membership",
            "passengers",
            "contact",
            "seats",
            "extras",
            "purchased",
        )
    }
    view["total"] = total(ep)
    view["quirks"] = ep.config["quirks"]
    view["ep"] = ep.id
    view["today"] = TODAY.isoformat()
    view["adults"] = _adults(ep) if st["search"] else 1
    view["cities"] = CITY
    return view


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    q = ep.config["quirks"]
    body = body or {}
    if name == "session":
        if st["stage"] != "search" and _expired(ep):
            ep.log("session_expired", stage=st["stage"])
            _reset(ep)
            return Response(200, {**session_view(ep), "expired": True})
        return Response(200, session_view(ep))
    if name == "airports":
        term = (query.get("q") or "").strip().lower()
        if len(term) < 2:
            return Response(200, [])
        hits = [
            a
            for a in AIRPORTS
            if term in a[0].lower() or term in a[1].lower() or term in a[2].lower()
        ]
        if query.get("from"):  # destinations exclude the origin
            hits = [a for a in hits if a[0] != query["from"]]
        return Response(
            200, [{"code": a[0], "city": a[1], "state": a[2], "country": a[3]} for a in hits[:6]]
        )
    if name == "flights":
        r = _need(ep, "select")
        if r:
            return r
        return Response(
            200,
            {
                "out": _flights(ep, "out"),
                "ret": _flights(ep, "ret"),
                "adults": _adults(ep),
                "bundles": [{"id": b, "name": n} for b, n, _ in BUNDLES],
                "search": st["search"],
            },
        )
    if name == "seatmap":
        r = _need(ep, "seats")
        if r:
            return r
        return Response(
            200,
            {
                "out": seat_map(ep, "out"),
                "ret": seat_map(ep, "ret"),
                "passengers": st["passengers"],
                "seats": st["seats"],
                "search": st["search"],
            },
        )
    if method != "POST":
        return Response(405, {"error": "POST only"})

    # ─── state changes ──────────────────────────────────────────────
    if name == "search":
        o, d = body.get("origin"), body.get("dest")
        if o not in CITY or d not in CITY or o == d:
            return Response(400, {"error": "Please select valid departure and arrival cities."})
        if not body.get("depart") or (body.get("trip") != "oneway" and not body.get("return")):
            return Response(400, {"error": "Please select your travel dates."})
        st["search"] = {
            "origin": o,
            "dest": d,
            "depart": body["depart"],
            "return": body.get("return"),
            "adults": int(body.get("adults") or 1),
            "trip": body.get("trip") or "roundtrip",
        }
        st.update({"stage": "select", "selection": None, "membership": None, "seats": {}})
        st["last_activity"] = time.time()
        ep.log("search", **st["search"])
        return Response(200, {"ok": True, "go": "select.html"})
    if name == "select":
        r = _need(ep, "select")
        if r:
            return r
        out, ret = body.get("out") or {}, body.get("ret") or {}
        if not _find(ep, "out", out.get("flight_id", "")) or not _find(
            ep, "ret", ret.get("flight_id", "")
        ):
            return Response(400, {"error": "Select a departing and a returning flight."})
        fare = body.get("fare") if body.get("fare") in ("standard", "club_saver") else "standard"
        st["selection"] = {
            "out": {"flight_id": out["flight_id"], "bundle": out.get("bundle", "basic")},
            "ret": {"flight_id": ret["flight_id"], "bundle": ret.get("bundle", "basic")},
            "fare": fare,
        }
        st["membership"] = body.get("membership") or (
            "accepted" if fare == "club_saver" else "declined"
        )
        st["stage"] = "passengers"
        st["last_activity"] = time.time()
        ep.log("select", **st["selection"], membership=st["membership"])
        return Response(200, {"ok": True, "go": "passengers.html"})
    if name == "passengers":
        r = _need(ep, "passengers")
        if r:
            ep.log("passengers_rejected", why="expired" if r.status == 440 else "order")
            return r
        pax = body.get("passengers") or []
        missing = []
        for i, p in enumerate(pax):
            for k in ("first", "last", "gender", "dob"):
                if not str(p.get(k) or "").strip():
                    missing.append(f"Passenger {i + 1} {k}")
        c = body.get("contact") or {}
        for k in ("email", "phone", "zip"):
            if not str(c.get(k) or "").strip():
                missing.append(f"contact {k}")
        if len(pax) != _adults(ep) or missing:
            ep.log("passengers_invalid", missing=missing)
            return Response(
                400,
                {
                    "error": "Please complete required fields: " + ", ".join(missing),
                    "missing": missing,
                },
            )
        st["passengers"], st["contact"] = pax, c
        st["stage"] = "seats"
        st["last_activity"] = time.time()
        ep.log("passengers", passengers=pax, contact=c)
        return Response(200, {"ok": True, "go": "seats.html"})
    if name == "seat":
        r = _need(ep, "seats")
        if r:
            return r
        leg, pax, seat = body.get("leg"), int(body.get("pax", 0)), str(body.get("seat") or "")
        cell = next((c for row in seat_map(ep, leg)["rows"] for c in row if c["id"] == seat), None)
        if leg not in ("out", "ret") or cell is None or cell["taken"]:
            return Response(400, {"error": "That seat is not available."})
        for k, v in list(st["seats"].items()):
            if v == seat and k.startswith(leg + ":"):
                del st["seats"][k]
        st["seats"][f"{leg}:{pax}"] = seat
        st["last_activity"] = time.time()
        ep.log("seat", leg=leg, pax=pax, seat=seat)
        return Response(200, {"ok": True, "seats": st["seats"], "total": total(ep)})
    if name == "continue":
        frm = body.get("from")
        order = {"seats": "bags", "bags": "extras"}
        if frm not in order:
            return Response(400, {"error": "bad step"})
        r = _need(ep, frm)
        if r:
            return r
        st["stage"] = order[frm]
        st["last_activity"] = time.time()
        ep.log("continue", frm=frm)
        return Response(200, {"ok": True, "go": _page_for(order[frm])})
    if name == "offer":  # from the cross-origin insurance frame; the episode comes as ?ep=
        kind, choice = body.get("kind"), body.get("choice")
        if kind in ("disruption", "cfar") and choice in ("yes", "no"):
            st["extras"][kind] = choice
            ep.log("offer", kind=kind, choice=choice)
            return Response(
                200, {"ok": True, "total": total(ep)}, {"Access-Control-Allow-Origin": "*"}
            )
        return Response(400, {"error": "bad offer"}, {"Access-Control-Allow-Origin": "*"})
    if name == "extras":
        r = _need(ep, "extras")
        if r:
            return r
        ex = st["extras"]
        if body.get("self_service") in ("free", "agent"):
            ex["self_service"] = body["self_service"]
        errs = []
        if q.get("iframe_offers"):
            if ex.get("disruption") is None:
                errs.append("You must either opt in or out of Disruption Assistance")
            if ex.get("cfar") is None:
                errs.append("You must either opt in or out of Cancel for Any Reason")
        if ex.get("self_service") is None:
            errs.append("Self-Service: Selection required")
        if errs:
            ep.log("extras_invalid", errors=errs)
            return Response(400, {"error": " | ".join(errs), "errors": errs})
        st["stage"] = "payment"
        st["last_activity"] = time.time()
        ep.log("extras", **ex)
        return Response(
            200, {"ok": True, "go": "payment.html"}, delay_s=6.0 if q.get("slow_continue") else 0.0
        )
    if name == "purchase":
        r = _need(ep, "payment")
        if r:
            return r
        st["purchased"] = True
        st["stage"] = "purchased"
        ep.log("purchase", total=total(ep))
        return Response(200, {"ok": True, "go": "confirm.html"})
    return Response(404, {"error": f"no route {name}"})
